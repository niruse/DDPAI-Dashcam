"""Diagnostics for the DDPAI Dashcam integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import DdpaiConfigEntry

TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    "username",
    "bindUsername",
    "bindNickname",
    "wifiPwd",
    "ssid",
    "bssid",
    "macAddr",
    "bleMac",
    "bluuid",
    "iccid",
    "simIccid",
    "ipAddr",
    "realNameLink",
    "currLatitude",
    "currLongitude",
    "imageLatitude",
    "imageLongitude",
    "latitude",
    "longitude",
    "url",
    "thumbUrl",
    "imagePath",
    "posteriorImagePath",
    # remote-live fields the status reply carries; never used, never shown
    "p2pUid",
    "p2pSk",
    "p2pBrand",
    "p2pRegion",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DdpaiConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "session_timeout": coordinator.client.session_timeout,
        "cameras": async_redact_data(coordinator.data or {}, TO_REDACT),
        "other_devices": async_redact_data(coordinator.other_devices, TO_REDACT),
    }
