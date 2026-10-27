"""Support for Sonos software updates."""

import asyncio
import datetime as dt
import logging
from typing import TYPE_CHECKING, Any, override

from requests.exceptions import Timeout
from soco.exceptions import SoCoException
from soco.update import SoftwareUpdateError, monitor_update

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    SONOS_CREATE_UPDATE,
    SONOS_REBOOTED,
    SONOS_SPEAKER_ACTIVITY,
    SONOS_STATE_UPDATED,
)
from .entity import SonosPollingEntity
from .exception import SonosUpdateError
from .helpers import SonosConfigEntry, soco_error
from .speaker import SonosSpeaker

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = dt.timedelta(hours=1)
REBOOT_POLL_INTERVAL = 5
REBOOT_TIMEOUT = 300

# Speakers update independently, so "Update all" can run them at the same time.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SonosConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sonos update entities from a config entry."""

    @callback
    def async_create_entities(speaker: SonosSpeaker) -> None:
        """Handle device discovery and create update entities."""
        async_add_entities(
            [SonosUpdateEntity(speaker, config_entry)], update_before_add=True
        )

    config_entry.async_on_unload(
        async_dispatcher_connect(hass, SONOS_CREATE_UPDATE, async_create_entities)
    )


class SonosUpdateEntity(SonosPollingEntity, UpdateEntity):
    """Software update for a Sonos speaker."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_name = None
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )

    def __init__(self, speaker: SonosSpeaker, config_entry: SonosConfigEntry) -> None:
        """Initialize the update entity."""
        super().__init__(speaker, config_entry)
        self._attr_unique_id = f"{self.soco.uid}-firmware"
        self._attr_title = speaker.model_name
        self._attr_installed_version = speaker.software_version
        self._update_url: str | None = None
        self._speaker_offline = False

    @override
    async def async_added_to_hass(self) -> None:
        """Check for an update again whenever the speaker comes back online."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SONOS_STATE_UPDATED}-{self.soco.uid}",
                self._async_speaker_updated,
            )
        )

    @callback
    def _async_speaker_updated(self) -> None:
        """Refresh after the speaker reconnects, since a reboot can change its version."""
        was_offline = self._speaker_offline
        self._speaker_offline = not self.speaker.available
        if was_offline and self.speaker.available and not self.in_progress:
            self.async_schedule_update_ha_state(force_refresh=True)

    @property
    @override
    def release_summary(self) -> str | None:
        """Show which version the speaker updates from, not just to."""
        if self.installed_version == self.latest_version:
            return None
        return f"{self.installed_version} → {self.latest_version}"

    @property
    @override
    def available(self) -> bool:
        """Stay available while the speaker is offline installing the update."""
        return self.in_progress is True or super().available

    @override
    def poll_state(self) -> None:
        """Poll the speaker for an available update."""
        if self.in_progress:
            return
        try:
            status = self.soco.check_for_update()
        except (OSError, SoCoException, Timeout) as err:
            raise SonosUpdateError(f"Update check failed: {err}") from err
        if status is None:
            self._attr_latest_version = self.installed_version
            return
        self._attr_installed_version = status.installed_version
        self._attr_latest_version = status.latest_version
        self._update_url = status.update_url

    @override
    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Update the speaker and follow its progress until it reboots."""
        # The speaker goes offline as soon as the update starts.
        self._attr_in_progress = True
        self._attr_update_percentage = None
        self.async_write_ha_state()
        try:
            await self.hass.async_add_executor_job(self._begin_update)
            if await self.hass.async_add_executor_job(self._monitor_update):
                await self._async_wait_for_reboot()
        finally:
            self._attr_in_progress = False
            self._attr_update_percentage = None
            self.async_write_ha_state()

    @soco_error()
    def _begin_update(self) -> None:
        """Start the update on the speaker."""
        if TYPE_CHECKING:
            assert self._update_url
        self.soco.begin_software_update(self._update_url)

    def _monitor_update(self) -> bool:
        """Report update progress until the speaker reboots.

        Returns whether the speaker is rebooting into the new version.
        """
        try:
            for progress in monitor_update(self.soco.ip_address):
                # The reboot has no progress of its own, so don't sit at 100%.
                percent = None if progress.phase == "REBOOT" else progress.percent
                self.hass.add_job(self._async_set_progress, percent)
        except SoftwareUpdateError as err:
            raise SonosUpdateError(f"Update failed: {err}") from err
        except (ConnectionError, TimeoutError) as err:
            # The update carries on without a monitoring connection, and the
            # next poll picks up the new version.
            _LOGGER.debug(
                "Lost update progress for %s: %s", self.speaker.zone_name, err
            )
            return False
        # The speaker closes the connection when it reboots into the new version.
        return True

    @callback
    def _async_set_progress(self, percent: int | None) -> None:
        """Write one progress report."""
        self._attr_update_percentage = percent
        self.async_write_ha_state()

    async def _async_wait_for_reboot(self) -> None:
        """Wait for the speaker to come back on the new version and reconnect."""
        previous_version = self.installed_version
        rebooted = False
        try:
            async with asyncio.timeout(REBOOT_TIMEOUT):
                while True:
                    await asyncio.sleep(REBOOT_POLL_INTERVAL)
                    if not rebooted:
                        version = await self.hass.async_add_executor_job(
                            self._fetch_software_version
                        )
                        if not version or version == previous_version:
                            continue
                        rebooted = True
                        self._attr_installed_version = version
                        if self.speaker.available:
                            # Subscriptions from before the reboot are stale.
                            async_dispatcher_send(
                                self.hass, f"{SONOS_REBOOTED}-{self.soco.uid}"
                            )
                            continue
                    if self.speaker.available:
                        return
                    # Retried each poll, as the speaker can ignore activity in
                    # the cooldown after a failed resubscribe.
                    async_dispatcher_send(
                        self.hass,
                        f"{SONOS_SPEAKER_ACTIVITY}-{self.soco.uid}",
                        "software update",
                    )
        except TimeoutError:
            _LOGGER.debug("%s did not come back after updating", self.speaker.zone_name)

    def _fetch_software_version(self) -> str | None:
        """Return the speaker's software version, or None while it is offline."""
        try:
            info = self.soco.get_speaker_info(
                refresh=True, timeout=REBOOT_POLL_INTERVAL
            )
        except OSError, SoCoException, Timeout:
            return None
        return info["software_version"]
