"""Actions for the DDPAI Dashcam integration: geofences and the RTSP triggers."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .api import DdpaiError
from .const import DOMAIN, NO_POSITION, RTSP_MODE_TRIGGER
from .coordinator import DdpaiCoordinator
from .fence import NOTIFY_MODE, build_gps, parse_fence, trigger_mode

SERVICE_ADD = "add_geofence"
SERVICE_UPDATE = "update_geofence"
SERVICE_DELETE = "delete_geofence"
SERVICE_RTSP_TRIGGER = "rtsp_trigger"
SERVICE_RTSP_STOP = "rtsp_stop"
SERVICE_READ_SETTINGS = "read_settings"
ATTR_MINUTES = "minutes"

ATTR_DEVICE = "device_id"
ATTR_FENCE = "geofence_id"
ATTR_NAME = "name"
ATTR_LAT = "latitude"
ATTR_LON = "longitude"
ATTR_RADIUS = "radius"
ATTR_NOTIFY = "notify_on"
ATTR_ENABLED = "enabled"

_RADIUS = vol.All(vol.Coerce(float), vol.Range(min=100, max=100000))

ADD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE): cv.string,
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_LAT): cv.latitude,
        vol.Optional(ATTR_LON): cv.longitude,
        vol.Optional(ATTR_RADIUS, default=2000): _RADIUS,
        vol.Optional(ATTR_NOTIFY, default="leave"): vol.In(list(NOTIFY_MODE)),
        vol.Optional(ATTR_ENABLED, default=True): cv.boolean,
    }
)
UPDATE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE): cv.string,
        vol.Required(ATTR_FENCE): vol.Coerce(int),
        vol.Optional(ATTR_NAME): cv.string,
        vol.Optional(ATTR_LAT): cv.latitude,
        vol.Optional(ATTR_LON): cv.longitude,
        vol.Optional(ATTR_RADIUS): _RADIUS,
        vol.Optional(ATTR_NOTIFY): vol.In(list(NOTIFY_MODE)),
        vol.Optional(ATTR_ENABLED): cv.boolean,
    }
)
RTSP_TRIGGER_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DEVICE): vol.Any(cv.string, [cv.string]),
        vol.Optional(ATTR_MINUTES): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1440)),
    }
)
RTSP_STOP_SCHEMA = vol.Schema({vol.Optional(ATTR_DEVICE): vol.Any(cv.string, [cv.string])})
DELETE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE): cv.string,
        vol.Required(ATTR_FENCE): vol.Coerce(int),
    }
)


def _camera(hass: HomeAssistant, device_id: str) -> tuple[DdpaiCoordinator, str]:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"Unknown device {device_id}")
    uuid = next((i[1] for i in device.identifiers if i[0] == DOMAIN), None)
    for entry_id in device.config_entries:
        coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is not None and uuid in (coordinator.data or {}):
            return coordinator, uuid
    raise ServiceValidationError("That device is not a DDPAI 4G camera")


def _fence(coordinator: DdpaiCoordinator, uuid: str, fence_id: int) -> dict[str, Any]:
    for fence in coordinator.data[uuid].get("fences") or []:
        if str(fence.get("id")) == str(fence_id):
            return parse_fence(fence)
    raise ServiceValidationError(f"Geofence {fence_id} not found on this camera")


async def _result(coordinator: DdpaiCoordinator, uuid: str) -> ServiceResponse:
    try:
        await coordinator.async_refresh_fences(uuid)
    except DdpaiError as err:
        # The change itself went through; only reading the list back failed.
        raise HomeAssistantError(
            f"Done, but the geofence list could not be re-read: {err}"
        ) from err
    return {"geofences": [parse_fence(f) for f in coordinator.data[uuid].get("fences") or []]}


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the geofence actions."""

    async def add_fence(call: ServiceCall) -> ServiceResponse:
        coordinator, uuid = _camera(hass, call.data[ATTR_DEVICE])
        lat, lon = call.data.get(ATTR_LAT), call.data.get(ATTR_LON)
        if lat is None or lon is None:
            status = coordinator.data[uuid].get("status") or {}
            lat, lon = status.get("currLatitude"), status.get("currLongitude")
            if not isinstance(lat, (int, float)) or abs(lat) >= NO_POSITION:
                raise ServiceValidationError("No position known; give latitude and longitude")
        gps = build_gps(lat, lon, call.data[ATTR_RADIUS])
        mode = trigger_mode(call.data[ATTR_NOTIFY], call.data[ATTR_ENABLED])
        try:
            await coordinator.client.async_add_fence(uuid, call.data[ATTR_NAME], gps, mode)
        except DdpaiError as err:
            raise HomeAssistantError(f"DDPAI refused the geofence: {err}") from err
        return await _result(coordinator, uuid)

    async def update_fence(call: ServiceCall) -> ServiceResponse:
        coordinator, uuid = _camera(hass, call.data[ATTR_DEVICE])
        current = _fence(coordinator, uuid, call.data[ATTR_FENCE])
        lat = call.data.get(ATTR_LAT, current["latitude"])
        lon = call.data.get(ATTR_LON, current["longitude"])
        radius = call.data.get(ATTR_RADIUS, current["radius"])
        moved = (lat, lon, radius) != (current["latitude"], current["longitude"], current["radius"])
        gps = build_gps(lat, lon, radius, current["gps_type"], current["location"])
        notify = call.data.get(ATTR_NOTIFY, current["notify_on"])
        if notify not in NOTIFY_MODE:
            raise ServiceValidationError(f"Unknown notify_on {notify}")
        mode = trigger_mode(notify, call.data.get(ATTR_ENABLED, current["enabled"]))
        try:
            await coordinator.client.async_update_fence(
                call.data[ATTR_FENCE], call.data.get(ATTR_NAME, current["name"]), gps, mode, moved
            )
        except DdpaiError as err:
            raise HomeAssistantError(f"DDPAI refused the change: {err}") from err
        return await _result(coordinator, uuid)

    async def delete_fence(call: ServiceCall) -> ServiceResponse:
        coordinator, uuid = _camera(hass, call.data[ATTR_DEVICE])
        _fence(coordinator, uuid, call.data[ATTR_FENCE])
        try:
            await coordinator.client.async_delete_fence(call.data[ATTR_FENCE])
        except DdpaiError as err:
            raise HomeAssistantError(f"DDPAI refused the delete: {err}") from err
        return await _result(coordinator, uuid)

    def _rtsp_targets(call: ServiceCall) -> list[tuple[Any, str]]:
        """(RTSP output, camera uuid) pairs an RTSP action is aimed at."""
        wanted = call.data.get(ATTR_DEVICE)
        if wanted:
            ids = [wanted] if isinstance(wanted, str) else wanted
            pairs = [_camera(hass, device_id) for device_id in ids]
        else:
            pairs = [
                (coordinator, uuid)
                for coordinator in hass.data.get(DOMAIN, {}).values()
                if isinstance(coordinator, DdpaiCoordinator)
                for uuid in coordinator.data or {}
            ]
        return [(coordinator.rtsp, uuid) for coordinator, uuid in pairs if coordinator.rtsp is not None]

    async def rtsp_trigger(call: ServiceCall) -> None:
        targets = [(rtsp, uuid) for rtsp, uuid in _rtsp_targets(call) if rtsp.mode == RTSP_MODE_TRIGGER]
        if not targets:
            raise ServiceValidationError("No DDPAI dashcam has RTSP set to 'Only when triggered'")
        for rtsp, uuid in targets:
            rtsp.trigger(uuid, call.data.get(ATTR_MINUTES))

    async def rtsp_stop(call: ServiceCall) -> None:
        for rtsp, uuid in _rtsp_targets(call):
            rtsp.stop(uuid)

    async def read_settings(call: ServiceCall) -> ServiceResponse:
        """Read every remote setting from the camera (it must be online)."""
        from .remote import RemoteError

        coordinator, uuid = _camera(hass, call.data[ATTR_DEVICE])
        try:
            values = await coordinator.async_read_remote_settings(uuid)
        except RemoteError as err:
            raise HomeAssistantError(f"Could not read the settings: {err}") from err
        return {"settings": values}

    hass.services.async_register(
        DOMAIN, SERVICE_READ_SETTINGS, read_settings,
        schema=vol.Schema({vol.Required(ATTR_DEVICE): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, SERVICE_RTSP_TRIGGER, rtsp_trigger, schema=RTSP_TRIGGER_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RTSP_STOP, rtsp_stop, schema=RTSP_STOP_SCHEMA)

    for name, handler, schema in (
        (SERVICE_ADD, add_fence, ADD_SCHEMA),
        (SERVICE_UPDATE, update_fence, UPDATE_SCHEMA),
        (SERVICE_DELETE, delete_fence, DELETE_SCHEMA),
    ):
        hass.services.async_register(
            DOMAIN, name, handler, schema=schema, supports_response=SupportsResponse.OPTIONAL
        )
