"""Device tracker for DDPAI 4G cameras."""
from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import DdpaiConfigEntry
from .coordinator import DdpaiCoordinator, dev_config
from .entity import DdpaiEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: DdpaiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up a tracker for every 4G camera that has GPS."""
    coordinator = entry.runtime_data
    async_add_entities(
        DdpaiTracker(coordinator, uuid)
        for uuid, cam in coordinator.data.items()
        if dev_config(cam.get("info") or {}).get("cameraHasGPS", 1)
    )


class DdpaiTracker(DdpaiEntity, TrackerEntity):
    """Last position the camera reported to the cloud."""

    _attr_name = None  # takes the device name

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str) -> None:
        super().__init__(coordinator, uuid)
        self._attr_unique_id = f"{uuid}_tracker"

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        pos = self.coordinator.position(self._uuid)
        return pos[0] if pos else None

    @property
    def longitude(self) -> float | None:
        pos = self.coordinator.position(self._uuid)
        return pos[1] if pos else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"gps_type": self.status.get("currGpsType")}
        moved = self.coordinator.position_changed_at(self._uuid)
        if moved:
            attrs["position_changed"] = dt_util.utc_from_timestamp(moved).isoformat()
        return attrs
