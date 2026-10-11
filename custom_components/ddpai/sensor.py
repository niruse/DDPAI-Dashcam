"""Sensors for DDPAI 4G cameras."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import DdpaiConfigEntry
from .const import ALBUM_PARKED, ALBUM_URGENT, DEVICE_STATES, EVENT_TYPES, STATE_UNKNOWN
from .coordinator import DdpaiCoordinator
from .entity import DdpaiEntity
from .fence import parse_fence


def _ms(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return dt_util.utc_from_timestamp(value / 1000)


def _newest(cam: dict[str, Any], album: int) -> dict[str, Any] | None:
    items = (cam.get("album") or {}).get(album) or []
    return max(items, key=lambda i: i.get("commitDate") or 0, default=None)


def _event_name(event: dict[str, Any] | None) -> str | None:
    if not event:
        return None
    return EVENT_TYPES.get(event.get("eventType"), f"type_{event.get('eventType')}")


def _recent_events(cam: dict[str, Any]) -> dict[str, Any]:
    events = cam.get("events") or []
    return {
        "recent": [
            {
                "event": _event_name(e),
                "time": t.isoformat() if (t := _ms(e.get("time"))) else None,
            }
            for e in events[:20]
        ]
    }


def _sim_attrs(cam: dict[str, Any]) -> dict[str, Any]:
    sim = cam.get("sim") or {}
    activated = _ms(sim.get("activated_date"))
    return {
        "activated": activated.isoformat() if activated else None,
        "used_flow": sim.get("usedFlow"),
        "remaining_flow": sim.get("remainFlow"),
        "total_flow": sim.get("totalFlow"),
    }


def _flow(cam: dict[str, Any], key: str) -> float | None:
    """A SIM data amount in MB, or None when the plan reports nothing."""
    sim = cam.get("sim") or {}
    value = sim.get(key)
    # A SIM without a DDPAI-managed plan reports every amount as 0.
    if not isinstance(value, (int, float)) or not (sim.get("totalFlow") or 0) > 0:
        return None
    return round(float(value), 2)


def _flow_attrs(cam: dict[str, Any]) -> dict[str, Any]:
    sim = cam.get("sim") or {}
    return {
        "plan": sim.get("packageName"),
        "used_total": sim.get("usedFlow"),
        "months": [
            {
                "month": rec.get("month") or rec.get("dateRange"),
                "used": rec.get("usageFlow"),
                "total": rec.get("totalFlow"),
            }
            for rec in (cam.get("flow") or [])[:12]
        ],
    }


def _fences(cam: dict[str, Any]) -> dict[str, Any]:
    return {"geofences": [parse_fence(f) for f in cam.get("fences") or []]}


@dataclass(frozen=True, kw_only=True)
class DdpaiSensorDescription(SensorEntityDescription):
    """Describes a DDPAI camera sensor."""

    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


SENSORS: tuple[DdpaiSensorDescription, ...] = (
    DdpaiSensorDescription(
        key="state",
        translation_key="state",
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(DEVICE_STATES.values())),
        value_fn=lambda cam: DEVICE_STATES.get(
            (cam.get("status") or {}).get("status"), STATE_UNKNOWN
        )
        if cam.get("status")
        else None,
    ),
    DdpaiSensorDescription(
        key="last_event",
        translation_key="last_event",
        value_fn=lambda cam: _event_name((cam.get("events") or [None])[0]),
        attrs_fn=_recent_events,
    ),
    DdpaiSensorDescription(
        key="last_event_time",
        translation_key="last_event_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda cam: _ms(((cam.get("events") or [{}])[0]).get("time")),
    ),
    DdpaiSensorDescription(
        key="last_parked_photo",
        translation_key="last_parked_photo",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda cam: _ms((_newest(cam, ALBUM_PARKED) or {}).get("commitDate")),
    ),
    DdpaiSensorDescription(
        key="last_urgent_event",
        translation_key="last_urgent_event",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda cam: _ms((_newest(cam, ALBUM_URGENT) or {}).get("commitDate")),
    ),
    DdpaiSensorDescription(
        key="data_used_month",
        translation_key="data_used_month",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        suggested_display_precision=1,
        value_fn=lambda cam: _flow(cam, "usedMonth"),
        attrs_fn=_flow_attrs,
    ),
    DdpaiSensorDescription(
        key="data_remaining",
        translation_key="data_remaining",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        suggested_display_precision=1,
        value_fn=lambda cam: _flow(cam, "remainFlow"),
    ),
    DdpaiSensorDescription(
        key="data_total",
        translation_key="data_total",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda cam: _flow(cam, "totalFlow"),
    ),
    DdpaiSensorDescription(
        key="data_plan_expiry",
        translation_key="data_plan_expiry",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda cam: _ms((cam.get("sim") or {}).get("expiryDate")),
    ),
    DdpaiSensorDescription(
        key="geofences",
        translation_key="geofences",
        value_fn=lambda cam: len(cam.get("fences") or []),
        attrs_fn=_fences,
    ),
    DdpaiSensorDescription(
        key="sim",
        translation_key="sim",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda cam: (cam.get("sim") or {}).get("status")
        or cam["info"].get("simState"),
        attrs_fn=_sim_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: DdpaiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up sensors for every 4G camera."""
    coordinator = entry.runtime_data
    async_add_entities(
        DdpaiSensor(coordinator, uuid, description)
        for uuid in coordinator.data
        for description in SENSORS
    )


class DdpaiSensor(DdpaiEntity, SensorEntity):
    """A value read from the DDPAI cloud."""

    entity_description: DdpaiSensorDescription

    def __init__(
        self, coordinator: DdpaiCoordinator, uuid: str, description: DdpaiSensorDescription
    ) -> None:
        super().__init__(coordinator, uuid)
        self.entity_description = description
        self._attr_unique_id = f"{uuid}_{description.key}"

    @property
    def native_value(self) -> Any:
        cam = self.camera
        return self.entity_description.value_fn(cam) if cam else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        cam = self.camera
        if not cam or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(cam)
