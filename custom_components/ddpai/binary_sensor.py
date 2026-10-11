"""Binary sensors for DDPAI 4G cameras."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DdpaiConfigEntry
from .const import DOMAIN, NO_POSITION
from .coordinator import DdpaiCoordinator
from .entity import DdpaiEntity
from .fence import distance_m, parse_fence


async def async_setup_entry(
    hass: HomeAssistant, entry: DdpaiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up connectivity and one "car inside" sensor per geofence."""
    coordinator = entry.runtime_data
    async_add_entities(DdpaiOnline(coordinator, uuid) for uuid in coordinator.data)

    known: set[tuple[str, str]] = set()
    registry = er.async_get(hass)

    @callback
    def _add_new_fences() -> None:
        new = []
        current: set[tuple[str, str]] = set()
        for uuid, cam in (coordinator.data or {}).items():
            for fence in cam.get("fences") or []:
                if fence.get("id") is None:
                    continue
                key = (uuid, str(fence["id"]))
                current.add(key)
                if key not in known:
                    known.add(key)
                    new.append(DdpaiInsideFence(coordinator, uuid, key[1]))
        if new:
            async_add_entities(new)
        # A fence deleted in the app or by an action takes its sensor with it.
        for uuid, fence_id in known - current:
            if uuid not in (coordinator.data or {}):
                continue  # camera gone from the list this poll; keep it
            known.discard((uuid, fence_id))
            entity_id = registry.async_get_entity_id(
                "binary_sensor", DOMAIN, f"{uuid}_fence_{fence_id}"
            )
            if entity_id:
                registry.async_remove(entity_id)

    _add_new_fences()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_fences))


class DdpaiOnline(DdpaiEntity, BinarySensorEntity):
    """Whether the camera currently holds its 4G link to the cloud."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "online"

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str) -> None:
        super().__init__(coordinator, uuid)
        self._attr_unique_id = f"{uuid}_online"

    @property
    def is_on(self) -> bool | None:
        status = self.status
        if not status:
            return None
        return status.get("mqtt") == 1 or status.get("status") in (1, 2, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "control_link": self.status.get("mqtt") == 1,
            "public_ip": self.info.get("ipAddr"),
        }


class DdpaiInsideFence(DdpaiEntity, BinarySensorEntity):
    """On while the car's last reported position is inside a geofence."""

    _attr_device_class = BinarySensorDeviceClass.PRESENCE
    _attr_translation_key = "inside_fence"

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str, fence_id: str) -> None:
        super().__init__(coordinator, uuid)
        self._fence_id = fence_id
        self._attr_unique_id = f"{uuid}_fence_{fence_id}"

    @property
    def translation_placeholders(self) -> dict[str, str]:
        """Follow renames of the fence."""
        return {"name": self._fence.get("name") or self._fence_id}

    @property
    def _fence(self) -> dict[str, Any]:
        for fence in (self.camera or {}).get("fences") or []:
            if str(fence.get("id")) == self._fence_id:
                return parse_fence(fence)
        return {}

    @property
    def available(self) -> bool:
        """Unavailable once the fence is deleted."""
        return super().available and bool(self._fence)

    def _distance(self) -> float | None:
        fence = self._fence
        lat, lon = self.status.get("currLatitude"), self.status.get("currLongitude")
        if (
            fence.get("latitude") is None
            or not isinstance(lat, (int, float))
            or not isinstance(lon, (int, float))
            or abs(lat) >= NO_POSITION
            or (lat, lon) == (0, 0)
        ):
            return None
        return distance_m(lat, lon, fence["latitude"], fence["longitude"])

    @property
    def is_on(self) -> bool | None:
        distance = self._distance()
        if distance is None:
            return None
        return distance <= self._fence["radius"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        fence = self._fence
        distance = self._distance()
        return {
            "geofence_id": fence.get("id"),
            "latitude": fence.get("latitude"),
            "longitude": fence.get("longitude"),
            "radius": fence.get("radius"),
            "notify_on": fence.get("notify_on"),
            "enabled": fence.get("enabled"),
            "distance": round(distance) if distance is not None else None,
        }
