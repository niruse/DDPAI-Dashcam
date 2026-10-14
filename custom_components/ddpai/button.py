"""Buttons: Capture a picture from each lens, and re-read the remote settings."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DdpaiConfigEntry
from .coordinator import DdpaiCoordinator
from .entity import DdpaiEntity
from .live import LENSES
from .remote_entities import async_capture


async def async_setup_entry(
    hass: HomeAssistant, entry: DdpaiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the buttons of each 4G camera."""
    coordinator = entry.runtime_data
    entities: list[ButtonEntity] = []
    for uuid in coordinator.data:
        entities.extend(DdpaiCapture(coordinator, uuid, lens) for lens in LENSES)
        entities.append(DdpaiReadSettings(coordinator, uuid))
    async_add_entities(entities)


class DdpaiCapture(DdpaiEntity, ButtonEntity):
    """Take a picture with one lens now (like the app's Capture).

    Opens the live stream on that lens, saves its first full picture to
    /media/ddpai and updates the "Last view" picture. The dashcam must be online.
    """

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str, lens: str) -> None:
        super().__init__(coordinator, uuid)
        self._lens = lens
        self._attr_translation_key = f"capture_{lens}"
        self._attr_unique_id = f"{uuid}_capture_{lens}"

    async def async_press(self) -> None:
        path = await async_capture(self.hass, self.coordinator, self._uuid, self._lens)
        self.hass.bus.async_fire(
            "ddpai_capture", {"device_uuid": self._uuid, "lens": self._lens, "path": path}
        )


class DdpaiReadSettings(DdpaiEntity, ButtonEntity):
    """Read the remote settings from the dashcam now."""

    _attr_translation_key = "read_settings"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str) -> None:
        super().__init__(coordinator, uuid)
        self._attr_unique_id = f"{uuid}_read_settings"

    async def async_press(self) -> None:
        from .remote import RemoteError

        try:
            await self.coordinator.async_read_remote_settings(self._uuid)
        except RemoteError as err:
            raise HomeAssistantError(f"Could not read the settings: {err}") from err
