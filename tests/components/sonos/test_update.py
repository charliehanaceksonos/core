"""Tests for the Sonos update platform."""

from collections.abc import Callable, Coroutine, Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant.common import MockConfigEntry
from soco.update import SoftwareUpdateError, UpdateProgress, UpdateStatus

from homeassistant.components.sonos.const import SONOS_REBOOTED
from homeassistant.components.update import (
    ATTR_IN_PROGRESS,
    ATTR_INSTALLED_VERSION,
    ATTR_LATEST_VERSION,
    ATTR_TITLE,
    ATTR_UPDATE_PERCENTAGE,
    DOMAIN as UPDATE_DOMAIN,
    SERVICE_INSTALL,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event

UPDATE_ENTITY = "update.zone_a"
UPDATE_URL = "http://update.example.com/^97.2-81220"
UPDATE_AVAILABLE = UpdateStatus("97.1-80312", "97.2-81220", UPDATE_URL)


@pytest.mark.usefixtures("async_autosetup_sonos")
async def test_up_to_date(hass: HomeAssistant) -> None:
    """Test a speaker that is up to date."""
    state = hass.states.get(UPDATE_ENTITY)
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_INSTALLED_VERSION] == "49.2-64250"
    assert state.attributes[ATTR_LATEST_VERSION] == "49.2-64250"
    assert state.attributes[ATTR_TITLE] == "Model Name"
    assert state.name == "Zone A"


@pytest.mark.parametrize(
    ("status", "latest_version"),
    [
        pytest.param(UPDATE_AVAILABLE, "97.2-81220", id="update_available"),
        pytest.param(None, "49.2-64250", id="no_manifest"),
    ],
)
async def test_check_for_update(
    hass: HomeAssistant,
    async_setup_sonos: Callable[[], Coroutine[Any, Any, None]],
    soco: MagicMock,
    status: UpdateStatus | None,
    latest_version: str,
) -> None:
    """Test the latest version comes from the speaker's update check."""
    soco.check_for_update.return_value = status
    await async_setup_sonos()

    state = hass.states.get(UPDATE_ENTITY)
    assert state.attributes[ATTR_LATEST_VERSION] == latest_version


async def test_install(
    hass: HomeAssistant,
    async_setup_sonos: Callable[[], Coroutine[Any, Any, None]],
    soco: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """Test the entity stays available with progress while the speaker updates."""
    soco.check_for_update.return_value = UPDATE_AVAILABLE
    await async_setup_sonos()
    assert hass.states.get(UPDATE_ENTITY).state == STATE_ON
    speaker = config_entry.runtime_data.discovered[soco.uid]
    states: list[tuple[str, int | None]] = []

    @callback
    def _record(event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        states.append((state.state, state.attributes[ATTR_UPDATE_PERCENTAGE]))

    async_track_state_change_event(hass, UPDATE_ENTITY, _record)
    rebooted = MagicMock()
    async_dispatcher_connect(hass, f"{SONOS_REBOOTED}-{soco.uid}", rebooted)

    def _progress() -> Iterator[UpdateProgress]:
        # The speaker drops off the network while its upgrade app runs.
        speaker.available = False
        yield UpdateProgress("DOWNLOAD", 17)
        yield UpdateProgress("FLASHWRITE", 90)
        soco.get_speaker_info.return_value = {
            **soco.get_speaker_info.return_value,
            "software_version": "97.2-81220",
        }

    with (
        patch(
            "homeassistant.components.sonos.update.monitor_update",
            return_value=_progress(),
        ) as monitor_update,
        patch("homeassistant.components.sonos.update.REBOOT_POLL_INTERVAL", 0),
        patch.object(speaker, "async_subscribe"),
        patch("homeassistant.components.sonos.speaker.RESUB_COOLDOWN_SECONDS", 0),
    ):
        await hass.services.async_call(
            UPDATE_DOMAIN,
            SERVICE_INSTALL,
            {ATTR_ENTITY_ID: UPDATE_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

    soco.begin_software_update.assert_called_once_with(UPDATE_URL)
    monitor_update.assert_called_once_with(soco.ip_address)
    assert (STATE_ON, 17) in states
    assert (STATE_ON, 90) in states
    assert all(state != STATE_UNAVAILABLE for state, _ in states)
    rebooted.assert_called_once()
    state = hass.states.get(UPDATE_ENTITY)
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_IN_PROGRESS] is False
    assert state.attributes[ATTR_INSTALLED_VERSION] == "97.2-81220"


async def test_install_without_progress(
    hass: HomeAssistant,
    async_setup_sonos: Callable[[], Coroutine[Any, Any, None]],
    soco: MagicMock,
) -> None:
    """Test an update still counts as started when progress can't be read."""
    soco.check_for_update.return_value = UPDATE_AVAILABLE
    await async_setup_sonos()

    with patch(
        "homeassistant.components.sonos.update.monitor_update",
        side_effect=ConnectionRefusedError,
    ):
        await hass.services.async_call(
            UPDATE_DOMAIN,
            SERVICE_INSTALL,
            {ATTR_ENTITY_ID: UPDATE_ENTITY},
            blocking=True,
        )

    soco.begin_software_update.assert_called_once_with(UPDATE_URL)
    state = hass.states.get(UPDATE_ENTITY)
    assert state.attributes[ATTR_IN_PROGRESS] is False
    assert state.attributes[ATTR_INSTALLED_VERSION] == "97.1-80312"


async def test_install_failed(
    hass: HomeAssistant,
    async_setup_sonos: Callable[[], Coroutine[Any, Any, None]],
    soco: MagicMock,
) -> None:
    """Test an update the speaker reports as failed."""
    soco.check_for_update.return_value = UPDATE_AVAILABLE
    await async_setup_sonos()

    with (
        patch(
            "homeassistant.components.sonos.update.monitor_update",
            side_effect=SoftwareUpdateError("DOWNLOAD", "9", "downgrade blocked"),
        ),
        pytest.raises(HomeAssistantError, match="downgrade blocked"),
    ):
        await hass.services.async_call(
            UPDATE_DOMAIN,
            SERVICE_INSTALL,
            {ATTR_ENTITY_ID: UPDATE_ENTITY},
            blocking=True,
        )

    state = hass.states.get(UPDATE_ENTITY)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_IN_PROGRESS] is False
