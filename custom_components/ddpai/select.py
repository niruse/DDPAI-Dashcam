"""Which lens the live view shows, and the RTSP output's mode."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DdpaiConfigEntry
from .const import CONF_ENABLE_LIVE_VIEW, RTSP_MODES
from .coordinator import DdpaiCoordinator
from .entity import DdpaiEntity
from .live import LENSES, get_stream
from .remote_entities import REMOTE_SELECTS, RemoteSelect, RemoteSettingEntity, same


async def async_setup_entry(
    hass: HomeAssistant, entry: DdpaiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the live lens select of each 4G camera and the RTSP mode."""
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = []
    if entry.options.get(CONF_ENABLE_LIVE_VIEW, True):
        entities.extend(DdpaiLiveLens(coordinator, uuid) for uuid in coordinator.data)
    entities.extend(
        DdpaiRemoteSelect(coordinator, uuid, spec) for uuid in coordinator.data for spec in REMOTE_SELECTS
    )
    # The RTSP mode belongs to the account's RTSP server: one, on the first camera.
    if coordinator.data:
        entities.append(DdpaiRtspMode(coordinator, next(iter(coordinator.data))))
    async_add_entities(entities)


class DdpaiLiveLens(DdpaiEntity, SelectEntity):
    """Front or rear on the live view; switches a running stream in place."""

    _attr_translation_key = "live_lens"
    _attr_options = list(LENSES)

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str) -> None:
        super().__init__(coordinator, uuid)
        self._attr_unique_id = f"{uuid}_live_lens"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._stream.async_add_listener(self.async_write_ha_state))

    @property
    def _stream(self):
        return get_stream(self.hass, self.coordinator, self._uuid)

    @property
    def current_option(self) -> str:
        return self._stream.lens

    async def async_select_option(self, option: str) -> None:
        await self._stream.async_set_lens(option)


class DdpaiRtspMode(DdpaiEntity, SelectEntity):
    """RTSP for NVRs: off, only when triggered, or always."""

    _attr_translation_key = "rtsp_mode"
    _attr_options = RTSP_MODES

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str) -> None:
        super().__init__(coordinator, uuid)
        self._attr_unique_id = f"{uuid}_rtsp_mode"

    @property
    def current_option(self) -> str | None:
        return self.coordinator.rtsp.mode if self.coordinator.rtsp else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.rtsp.async_set_mode(option)


class DdpaiRemoteSelect(RemoteSettingEntity, SelectEntity):
    """A multiple-choice remote setting of the dashcam."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str, spec: RemoteSelect) -> None:
        super().__init__(coordinator, uuid)
        self._spec = spec
        self._key = spec.key
        self._attr_translation_key = spec.translation_key
        self._attr_unique_id = f"{uuid}_remote_{spec.key}"
        self._attr_options = list(spec.options)

    @property
    def current_option(self) -> str | None:
        value = self._value
        return next((o for o, v in self._spec.options.items() if same(v, value)), None)

    async def async_select_option(self, option: str) -> None:
        await self._save(self._spec.options[option])
