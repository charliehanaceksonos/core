"""Support for Sonos software updates."""

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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SONOS_CREATE_UPDATE
from .entity import SonosPollingEntity
from .exception import SonosUpdateError
from .helpers import SonosConfigEntry, soco_error
from .speaker import SonosSpeaker

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = dt.timedelta(hours=1)

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
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )

    def __init__(self, speaker: SonosSpeaker, config_entry: SonosConfigEntry) -> None:
        """Initialize the update entity."""
        super().__init__(speaker, config_entry)
        self._attr_unique_id = f"{self.soco.uid}-firmware"
        self._attr_installed_version = speaker.software_version
        self._update_url: str | None = None

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
        await self.hass.async_add_executor_job(self._begin_update)
        self._attr_in_progress = True
        self._attr_update_percentage = None
        self.async_write_ha_state()
        try:
            await self.hass.async_add_executor_job(self._monitor_update)
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
    def _monitor_update(self) -> None:
        """Report update progress until the speaker reboots."""
        try:
            for progress in monitor_update(self.soco.ip_address):
                self._attr_update_percentage = progress.percent
                self.schedule_update_ha_state()
        except (ConnectionError, TimeoutError) as err:
            # The update carries on without a monitoring connection, and the
            # next poll picks up the new version.
            _LOGGER.debug(
                "Lost update progress for %s: %s", self.speaker.zone_name, err
            )
            return
        # The speaker closes the connection when it reboots into the new version.
        self._attr_installed_version = self.latest_version
