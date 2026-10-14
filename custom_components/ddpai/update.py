"""Firmware of each camera ("About device" / "New version available" in the app).

The cloud says which firmware is newest. Installing it is not offered: the app
itself can only upload firmware to the camera over the camera's own Wi-Fi.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DdpaiConfigEntry
from .coordinator import DdpaiCoordinator
from .entity import DdpaiEntity

# checkVersion "suggest".
URGENCY = {0: "optional", 5: "recommended", 10: "required"}


async def async_setup_entry(
    hass: HomeAssistant, entry: DdpaiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up a firmware entity per 4G camera."""
    coordinator = entry.runtime_data
    async_add_entities(DdpaiFirmware(coordinator, uuid) for uuid in coordinator.data)


class DdpaiFirmware(DdpaiEntity, UpdateEntity):
    """Installed and newest firmware of one camera."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_translation_key = "firmware"

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str) -> None:
        super().__init__(coordinator, uuid)
        self._attr_unique_id = f"{uuid}_firmware"

    @property
    def _offer(self) -> dict[str, Any]:
        return (self.camera or {}).get("firmware") or {}

    @property
    def installed_version(self) -> str | None:
        return self.info.get("version")

    @property
    def latest_version(self) -> str | None:
        return self._offer.get("version") or self.installed_version

    @property
    def release_summary(self) -> str | None:
        offer = self._offer
        text = offer.get("descEn") or offer.get("desc")
        if not text:
            return None
        return text[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        offer = self._offer
        if not offer:
            return {}
        size = offer.get("size")
        return {
            "urgency": URGENCY.get(offer.get("suggest"), offer.get("suggest")),
            "size_mb": round(int(size) / 1048576, 1) if str(size or "").isdigit() else None,
            "how_to_install": "Connect the phone to the camera's Wi-Fi in the DDPAI app",
        }
