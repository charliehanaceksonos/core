"""Tests for the Sonos update platform."""

from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from soco.update import SoftwareUpdateError, UpdateProgress, UpdateStatus

from homeassistant.components.update import (
    ATTR_IN_PROGRESS,
    ATTR_INSTALLED_VERSION,
    ATTR_LATEST_VERSION,
    ATTR_TITLE,
    DOMAIN as UPDATE_DOMAIN,
    SERVICE_INSTALL,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

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
) -> None:
    """Test installing an update and following its progress."""
    soco.check_for_update.return_value = UPDATE_AVAILABLE
    await async_setup_sonos()
    assert hass.states.get(UPDATE_ENTITY).state == STATE_ON

    with patch(
        "homeassistant.components.sonos.update.monitor_update",
        return_value=iter(
            [UpdateProgress("DOWNLOAD", 17), UpdateProgress("FLASHWRITE", 90)]
        ),
    ) as monitor_update:
        await hass.services.async_call(
            UPDATE_DOMAIN,
            SERVICE_INSTALL,
            {ATTR_ENTITY_ID: UPDATE_ENTITY},
            blocking=True,
        )

    soco.begin_software_update.assert_called_once_with(UPDATE_URL)
    monitor_update.assert_called_once_with(soco.ip_address)
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
