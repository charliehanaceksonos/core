"""Support for Sonos software updates."""

import asyncio
import datetime as dt
import logging
from typing import TYPE_CHECKING, Any, override

from requests.exceptions import Timeout
from soco.exceptions import SoCoException
from soco.update import monitor_update

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

from .const import SONOS_CREATE_UPDATE, SONOS_REBOOTED
from .entity import SonosPollingEntity
from .exception import SonosUpdateError
from .helpers import SonosConfigEntry, soco_error
from .speaker import SonosSpeaker

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = dt.timedelta(hours=1)
REBOOT_POLL_INTERVAL = 5
REBOOT_TIMEOUT = 300

PARALLEL_UPDATES = 1


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

    @soco_error()
    def _monitor_update(self) -> bool:
        """Report update progress until the speaker reboots.

        Returns whether the speaker is rebooting into the new version.
        """
        try:
            for progress in monitor_update(self.soco.ip_address):
                self.hass.add_job(self._async_set_progress, progress.percent)
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
    def _async_set_progress(self, percent: int) -> None:
        """Write one progress report."""
        self._attr_update_percentage = percent
        self.async_write_ha_state()

    async def _async_wait_for_reboot(self) -> None:
        """Wait for the speaker to come back on the new version and reconnect."""
        previous_version = self.installed_version
        try:
            async with asyncio.timeout(REBOOT_TIMEOUT):
                while True:
                    await asyncio.sleep(REBOOT_POLL_INTERVAL)
                    version = await self.hass.async_add_executor_job(
                        self._fetch_software_version
                    )
                    if version and version != previous_version:
                        break
        except TimeoutError:
            _LOGGER.debug("%s did not come back after updating", self.speaker.zone_name)
            return
        self._attr_installed_version = version
        # Reconnect now rather than waiting for the speaker to be rediscovered.
        async_dispatcher_send(self.hass, f"{SONOS_REBOOTED}-{self.soco.uid}")

    def _fetch_software_version(self) -> str | None:
        """Return the speaker's software version, or None while it is offline."""
        try:
            info = self.soco.get_speaker_info(
                refresh=True, timeout=REBOOT_POLL_INTERVAL
            )
        except OSError, SoCoException, Timeout:
            return None
        return info["software_version"]
