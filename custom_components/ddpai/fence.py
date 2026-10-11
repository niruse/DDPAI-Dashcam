"""Geofence ("electronic fence") helpers.

The cloud stores each fence as ``gpsData`` JSON —
``{"radius": <m>, "data": ["<lat>,<lon>,<gpsType>"], "location": ""}`` — plus a
``triggerMode``: 3 alerts in the app when the car enters, 2 when it leaves, and
the app disables a fence by storing the negative of its mode.
"""
from __future__ import annotations

import math
from typing import Any

from .const import FENCE_ENTER, FENCE_LEAVE

NOTIFY_ON = {FENCE_ENTER: "enter", FENCE_LEAVE: "leave"}
NOTIFY_MODE = {v: k for k, v in NOTIFY_ON.items()}


def parse_fence(fence: dict[str, Any]) -> dict[str, Any]:
    """Flatten a fence record into the fields people care about."""
    gps = fence.get("gpsData") or {}
    lat = lon = None
    gps_type = 0
    points = gps.get("data") or []
    if points:
        parts = str(points[0]).split(",")
        try:
            lat, lon = float(parts[0]), float(parts[1])
            gps_type = int(parts[2]) if len(parts) > 2 else 0
        except (ValueError, IndexError):
            lat = lon = None
    try:
        mode = int(fence.get("triggerMode") or 0)
    except (TypeError, ValueError):
        mode = 0
    try:
        radius = float(gps.get("radius") or 0)
    except (TypeError, ValueError):
        radius = 0.0
    return {
        "id": fence.get("id"),
        "name": fence.get("name") or "",
        "latitude": lat,
        "longitude": lon,
        "gps_type": gps_type,
        "radius": radius,
        "notify_on": NOTIFY_ON.get(abs(mode), str(abs(mode))),
        "enabled": mode > 0,
        "location": gps.get("location") or "",
    }


def build_gps(
    latitude: float, longitude: float, radius: float, gps_type: int = 0, location: str = ""
) -> dict[str, Any]:
    """The ``gpsData`` object for a circular fence."""
    return {
        "radius": int(round(radius)),
        "data": [f"{latitude},{longitude},{gps_type}"],
        "location": location,
    }


def trigger_mode(notify_on: str, enabled: bool) -> int:
    """``triggerMode`` for a notify direction and on/off state."""
    mode = NOTIFY_MODE[notify_on]
    return mode if enabled else -mode


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
