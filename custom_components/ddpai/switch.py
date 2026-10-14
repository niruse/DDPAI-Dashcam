"""Switches of the DDPAI account, as in the app's device settings."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DdpaiConfigEntry
from .api import DdpaiError
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, RTSP_MODE_TRIGGER
from .coordinator import DdpaiCoordinator
from .entity import DdpaiEntity
from .remote_entities import REMOTE_SWITCHES, RemoteSettingEntity, RemoteSwitch, same


async def async_setup_entry(
    hass: HomeAssistant, entry: DdpaiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the status-change push switch (one per account, on the first camera)."""
    coordinator = entry.runtime_data
    if coordinator.data:
        async_add_entities([DdpaiPushSwitch(coordinator, next(iter(coordinator.data)))])
    # Live RTSP video on demand, for the "only when triggered" mode.
    async_add_entities(DdpaiRtspLiveSwitch(coordinator, uuid) for uuid in coordinator.data)
    # The app's Remote settings (0.3.0 ids used guessed keys: drop them).
    registry = er.async_get(hass)
    for uuid in coordinator.data:
        for old_id in (f"{uuid}_remote_{k}" for k in ("emervideo_upload", "autoStaticShutdown")):
            if old := registry.async_get_entity_id("switch", DOMAIN, old_id):
                registry.async_remove(old)
    async_add_entities(
        DdpaiRemoteSwitch(coordinator, uuid, spec) for uuid in coordinator.data for spec in REMOTE_SWITCHES
    )


class DdpaiPushSwitch(DdpaiEntity, SwitchEntity):
    """Push notifications for device status changes (power, parking, standby).

    The setting belongs to the account, not the camera: it applies to every
    camera on it, exactly like the switch in the app.
    """

    _attr_translation_key = "status_push"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str) -> None:
        super().__init__(coordinator, uuid)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id or coordinator.config_entry.entry_id}_status_push"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.push_switch is not None

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.push_switch

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        try:
            await self.coordinator.async_set_push_switch(on)
        except DdpaiError as err:
            raise HomeAssistantError(f"Could not change the push setting: {err}") from err


class DdpaiRtspLiveSwitch(DdpaiEntity, SwitchEntity):
    """Hold the RTSP stream on live video until switched off.

    Only meaningful when RTSP is set to "only when triggered"; the stream then
    carries live video for as long as this is on.
    """

    _attr_translation_key = "rtsp_live"

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str) -> None:
        super().__init__(coordinator, uuid)
        self._attr_unique_id = f"{uuid}_rtsp_live"

    @property
    def available(self) -> bool:
        """Available while RTSP is in the "only when triggered" mode."""
        rtsp = self.coordinator.rtsp
        return super().available and rtsp is not None and rtsp.mode == RTSP_MODE_TRIGGER

    @property
    def is_on(self) -> bool:
        return self.coordinator.rtsp.manual(self._uuid)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.rtsp.set_manual(self._uuid, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.rtsp.set_manual(self._uuid, False)


class DdpaiRemoteSwitch(RemoteSettingEntity, SwitchEntity):
    """An on/off remote setting of the dashcam."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str, spec: RemoteSwitch) -> None:
        super().__init__(coordinator, uuid)
        self._spec = spec
        self._key = spec.key
        self._attr_translation_key = spec.translation_key
        self._attr_unique_id = f"{uuid}_remote_{spec.translation_key}"

    @property
    def is_on(self) -> bool | None:
        value = self._value
        return None if value is None else same(value, self._spec.on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._save(self._spec.on)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._save(self._spec.off)
