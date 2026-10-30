"""DataUpdateCoordinator for the DDPAI Dashcam integration."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DdpaiApiClient, DdpaiAuthError, DdpaiConnectionError, DdpaiError
from .const import (
    ALBUM_PAGE_SIZE,
    ALBUM_REFRESH_SECONDS,
    ALBUMS,
    CONF_PARKED_CAPTURE,
    CONF_SCAN_INTERVAL,
    DEFAULT_PARKED_CAPTURE,
    DEFAULT_SCAN_INTERVAL,
    FENCE_REFRESH_SECONDS,
    DOMAIN,
    ERR_BAD_PASSWORD,
    ERR_USER_LOCKED,
    ERR_USER_NOT_FOUND,
    EVENT_DEVICE,
    EVENT_NEW_MEDIA,
    NO_POSITION,
    EVENT_TYPES,
    MAX_STATE_EVENTS,
    RES_TYPES,
    STATE_EVENTS,
)

_LOGGER = logging.getLogger(__name__)

# How often the SIM state is read; it changes on the scale of months.
SIM_REFRESH_SECONDS = 6 * 3600
FIRMWARE_REFRESH_SECONDS = 12 * 3600
PUSH_REFRESH_SECONDS = 1800
# Logins refused for these reasons need the user, not a retry.
_FATAL_LOGIN = {ERR_BAD_PASSWORD, ERR_USER_NOT_FOUND, ERR_USER_LOCKED}


def dev_config(info: dict[str, Any]) -> dict[str, Any]:
    """The device's ``devConfig`` capability blob, decoded."""
    raw = info.get("devConfig")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def is_cloud_camera(info: dict[str, Any]) -> bool:
    """True for cameras that report to the cloud over 4G (have a SIM / 4G test)."""
    config = dev_config(info)
    return bool(
        info.get("iccid")
        or info.get("simIccid")
        or info.get("p2p")
        or config.get("support4GNetTest")
    )


def media_kind(item: dict[str, Any]) -> tuple[str, str]:
    """(camera, "image"|"video") of an album item."""
    res = (item.get("content") or {}).get("resType")
    try:
        return RES_TYPES.get(int(res), ("unknown", "image"))
    except (TypeError, ValueError):
        return ("unknown", "image")


# A point reported while offline this far from the last real one is the cloud's
# stand-in, not a move.
PLACEHOLDER_MIN_M = 2000
# A new point further than this from the last one needs the next point to agree.
JUMP_CONFIRM_M = 20000
# A refused device list is retried after this, and the last good one used for up to
# DEVICES_STALE_SECONDS (see DdpaiCoordinator._async_devices).
DEVICES_RETRY_SECONDS = 3
DEVICES_STALE_SECONDS = 600


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((rlat2 - rlat1) / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371000 * math.asin(math.sqrt(a))


class DdpaiCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Poll the account's 4G cameras.

    ``data`` maps camera uuid to ``{"info", "status", "events", "album", "sim"}``.
    ``album`` maps album tab (74 parked photos, 54 urgent events) to the newest
    page of items, newest first.
    """

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: DdpaiApiClient
    ) -> None:
        interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        # Album items seen so far, by item id, so the media view can find the
        # file of any item the browser listed (including older pages).
        self.media: dict[str, dict[str, Any]] = {}
        # uuid -> live.LiveStream, created on the first viewer.
        self.live_streams: dict[str, Any] = {}
        # uuid -> lens the live view shows ("front" / "rear").
        self.live_lens: dict[str, str] = {}
        # Remote settings read over P2P (uuid -> {key: value}), and one P2P
        # request at a time per camera.
        self.remote_settings: dict[str, dict[str, Any]] = {}
        self.remote_settings_read_at: dict[str, float] = {}
        # Cameras whose settings were read during the current online period.
        self._settings_read_online: set[str] = set()
        # Parked photos taken by the integration: uuid -> lens -> {"path", "time"}.
        self.parked_captures: dict[str, dict[str, dict[str, Any]]] = {}
        self.p2p_locks: dict[str, Any] = {}
        # The RTSP output for NVRs (rtsp.DdpaiRtsp), and the options it runs with.
        self.rtsp: Any = None
        self.applied_options: dict[str, Any] = {}
        self._album_listed_at: dict[str, float] = {}
        self._sim_read_at: dict[str, float] = {}
        self._firmware_read_at: dict[str, float] = {}
        self._push_read_at: float | None = None
        # Account-wide "push notifications for device status changing".
        self.push_switch: bool | None = None
        self._fences_read_at: dict[str, float] = {}
        self._fences_dirty: set[str] = set()
        self._seen_events: dict[str, set[tuple[int, int]]] = {}
        # State changes seen by polling, newest first, per camera — kept in
        # storage with the last state seen, so a restart neither forgets them
        # nor misses a change made while Home Assistant was down.
        self._state_events: dict[str, list[dict[str, Any]]] = {}
        self._last_state: dict[str, int] = {}
        self._store: Store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.events")
        self._store_loaded = False
        self._seen_media: dict[str, set[str]] = {}
        # Each camera's last real position and when it last moved (the server
        # sends no fix time of its own). Not simply the reported one: a while
        # after the camera goes offline, and in the minute after it wakes before
        # it has a fix, the cloud reports one fixed stand-in point instead (the
        # same coordinates to the last digit, far from where the car is). Such a
        # point is learned when it shows up while offline, far from the last
        # real position, and ignored from then on. Both are kept in storage.
        self._position: dict[str, tuple[float, float, float]] = {}
        self._placeholders: set[tuple[float, float]] = set()
        # uuid -> a far point waiting for the next one to confirm it.
        self._jumps: dict[str, tuple[float, float]] = {}
        # Cameras on the account without their own 4G link (e.g. the MINI2X
        # cabin camera of a 3-channel set, whose files arrive via the Z60).
        self.other_devices: list[dict[str, Any]] = []
        # The last device list that arrived, and when (see _async_devices).
        self._devices: list[dict[str, Any]] = []
        self._devices_at = 0.0
        # uuid -> when its status was last read in full (see _async_update_data).
        self._camera_ok_at: dict[str, float] = {}

    @property
    def has_cabin_camera(self) -> bool:
        """True when a cabin camera is bound alongside the 4G camera."""
        return any(
            "DR1003" in (d.get("model") or "") or "MINI" in (d.get("deviceName") or "").upper()
            for d in self.other_devices
        )

    async def async_refresh_fences(self, uuid: str) -> None:
        """Re-read a camera's geofences now (after one was changed).

        Read directly rather than through ``async_request_refresh``: that call
        is debounced, so back-to-back actions would answer with the list from
        before the change.
        """
        if not self.data or uuid not in self.data:
            return
        try:
            fences = await self.client.async_get_fences(uuid)
        except DdpaiError:
            self._fences_dirty.add(uuid)  # next poll retries
            raise
        self._fences_read_at[uuid] = time.monotonic()
        self._fences_dirty.discard(uuid)
        data = dict(self.data)
        data[uuid] = {**data[uuid], "fences": fences}
        self.async_set_updated_data(data)

    def position(self, uuid: str) -> tuple[float, float] | None:
        """The camera's last real position."""
        pos = self._position.get(uuid)
        return (pos[0], pos[1]) if pos else None

    def position_changed_at(self, uuid: str) -> float | None:
        """Epoch seconds when the reported position last moved."""
        pos = self._position.get(uuid)
        return pos[2] if pos else None

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        if not self._store_loaded:
            self._store_loaded = True
            stored = await self._store.async_load() or {}
            self._state_events = stored.get("events") or {}
            self._last_state = {k: int(v) for k, v in (stored.get("state") or {}).items()}
            self.remote_settings = stored.get("settings") or {}
            self.remote_settings_read_at = stored.get("settings_read_at") or {}
            self.parked_captures = stored.get("parked_captures") or {}
            self._position = {
                k: (float(v[0]), float(v[1]), float(v[2]))
                for k, v in (stored.get("position") or {}).items()
                if isinstance(v, list) and len(v) == 3
            }
            self._placeholders = {
                (round(float(p[0]), 6), round(float(p[1]), 6))
                for p in stored.get("placeholders") or []
            }
        try:
            devices = await self._async_devices()
        except DdpaiAuthError as err:
            if err.code in _FATAL_LOGIN:
                raise ConfigEntryAuthFailed(str(err)) from err
            raise UpdateFailed(f"DDPAI login failed: {err}") from err
        except DdpaiError as err:
            raise UpdateFailed(f"DDPAI device list failed: {err}") from err

        self.other_devices = [d for d in devices if not is_cloud_camera(d)]
        previous = self.data or {}
        result: dict[str, dict[str, Any]] = {}
        for info in devices:
            uuid = info.get("uuid")
            if not uuid or not is_cloud_camera(info):
                continue
            old = previous.get(uuid, {})
            cam: dict[str, Any] = {
                "info": info,
                "status": old.get("status", {}),
                "events": old.get("events", []),
                "album": old.get("album", {}),
                "sim": old.get("sim", {}),
                "flow": old.get("flow", []),
                "fences": old.get("fences", []),
                "album_unread": old.get("album_unread", 0),
                "firmware": old.get("firmware"),
                "parked_captures": (
                    self.parked_captures.get(uuid, {})
                    if self.config_entry.options.get(CONF_PARKED_CAPTURE, DEFAULT_PARKED_CAPTURE)
                    else {}
                ),
                "errors": {},
            }
            try:
                await self._update_camera(uuid, cam)
            except DdpaiAuthError as err:
                if err.code in _FATAL_LOGIN:
                    raise ConfigEntryAuthFailed(str(err)) from err
                raise UpdateFailed(f"DDPAI login failed: {err}") from err
            except DdpaiConnectionError as err:
                # The same passing refusals as the device list (see
                # _async_devices): keep what this camera showed for a while.
                if old and time.monotonic() - self._camera_ok_at.get(uuid, 0.0) < DEVICES_STALE_SECONDS:
                    _LOGGER.info("DDPAI camera %s not read, keeping the last state: %s", uuid, err)
                    result[uuid] = {**old, "info": info}
                    continue
                raise UpdateFailed(f"DDPAI cloud unreachable: {err}") from err
            self._camera_ok_at[uuid] = time.monotonic()
            result[uuid] = cam
        await self._update_push_switch()
        return result

    async def _async_devices(self) -> list[dict[str, Any]]:
        """The account's cameras, riding out the cloud's passing refusals.

        A few times an hour the device list answers "error -2
        CIRCUIT_BREAKERS" (the cloud shedding load) and works again seconds
        later. Failing the whole poll for it turned every entity unavailable
        and logged an error each time, so it is retried once, and if it still
        fails the last list is used for up to DEVICES_STALE_SECONDS.
        """
        try:
            devices = await self.client.async_get_devices()
        except DdpaiAuthError:
            raise
        except DdpaiError as err:
            _LOGGER.debug("DDPAI device list failed, retrying: %s", err)
            await asyncio.sleep(DEVICES_RETRY_SECONDS)
            try:
                devices = await self.client.async_get_devices()
            except DdpaiAuthError:
                raise
            except DdpaiError as again:
                if self._devices and time.monotonic() - self._devices_at < DEVICES_STALE_SECONDS:
                    _LOGGER.info("DDPAI device list failed twice, using the last one: %s", again)
                    return self._devices
                raise
        self._devices, self._devices_at = devices, time.monotonic()
        return devices

    async def _update_push_switch(self) -> None:
        now = time.monotonic()
        if self._push_read_at is not None and now - self._push_read_at < PUSH_REFRESH_SECONDS:
            return
        try:
            self.push_switch = await self.client.async_get_push_switch()
            self._push_read_at = now
        except DdpaiError as err:
            _LOGGER.debug("Push switch not read: %s", err)

    async def async_set_push_switch(self, on: bool) -> None:
        """Change the account's status-change push notifications."""
        await self.client.async_set_push_switch(on)
        self.push_switch = on
        self._push_read_at = time.monotonic()
        self.async_update_listeners()

    async def _update_camera(self, uuid: str, cam: dict[str, Any]) -> None:
        info = cam["info"]
        before = (cam.get("status") or {}).get("status", self._last_state.get(uuid))
        try:
            cam["status"] = await self.client.async_get_status(uuid)
            self._note_position(uuid, cam["status"])
            self._note_state(uuid, before, cam["status"])
            self._maybe_read_settings(uuid, cam["status"])
        except (DdpaiConnectionError, DdpaiAuthError):
            raise
        except DdpaiError as err:
            cam["errors"]["status"] = str(err)

        try:
            events = await self.client.async_get_events(uuid) + self._state_events.get(uuid, [])
            cam["events"] = sorted(events, key=lambda e: e.get("time") or 0, reverse=True)
            self._fire_new_events(uuid, info, cam["events"])
        except DdpaiError as err:
            cam["errors"]["events"] = str(err)

        await self._update_album(uuid, info, cam)

        iccid = info.get("iccid") or info.get("simIccid")
        now = time.monotonic()
        if iccid and now - self._sim_read_at.get(uuid, -SIM_REFRESH_SECONDS) >= SIM_REFRESH_SECONDS:
            try:
                cam["sim"] = await self.client.async_get_sim(uuid, iccid, info.get("model") or "")
                self._sim_read_at[uuid] = now
            except DdpaiError as err:
                cam["errors"]["sim"] = str(err)
            try:
                cam["flow"] = await self.client.async_get_flow_records(iccid)
            except DdpaiError as err:
                cam["errors"]["flow"] = str(err)

        if now - self._firmware_read_at.get(uuid, -FIRMWARE_REFRESH_SECONDS) >= FIRMWARE_REFRESH_SECONDS:
            try:
                cam["firmware"] = await self.client.async_check_firmware(info) or {}
                self._firmware_read_at[uuid] = now
            except DdpaiError as err:
                cam["errors"]["firmware"] = str(err)

        if uuid in self._fences_dirty or now - self._fences_read_at.get(uuid, -FENCE_REFRESH_SECONDS) >= FENCE_REFRESH_SECONDS:
            try:
                cam["fences"] = await self.client.async_get_fences(uuid)
                self._fences_read_at[uuid] = now
                self._fences_dirty.discard(uuid)
            except DdpaiError as err:
                cam["errors"]["fences"] = str(err)

    async def _update_album(self, uuid: str, info: dict[str, Any], cam: dict[str, Any]) -> None:
        try:
            unread = await self.client.async_album_unread(uuid)
        except DdpaiError as err:
            cam["errors"]["album"] = str(err)
            return
        cam["album_unread"] = unread
        now = time.monotonic()
        stale = now - self._album_listed_at.get(uuid, -ALBUM_REFRESH_SECONDS) >= ALBUM_REFRESH_SECONDS
        if not (unread or stale or not cam["album"]):
            return
        album: dict[int, list[dict[str, Any]]] = {}
        try:
            for tab in ALBUMS:
                items, _pages = await self.client.async_get_album(uuid, tab, 1, ALBUM_PAGE_SIZE)
                album[tab] = items
                self.remember_media(uuid, items)
        except DdpaiError as err:
            cam["errors"]["album"] = str(err)
            return
        cam["album"] = album
        self._album_listed_at[uuid] = now
        self._fire_new_media(uuid, info, album)

    def remember_media(self, uuid: str, items: list[dict[str, Any]]) -> None:
        """Index album items so the media view can serve them."""
        for item in items:
            if item.get("id") is not None:
                self.media[str(item["id"])] = {**item, "uuid": uuid}

    def _note_state(self, uuid: str, before: Any, status: dict[str, Any]) -> None:
        """Record a change of the polled state (driving, parking, ...) as an event."""
        now = status.get("status")
        if now not in STATE_EVENTS:
            return
        events = self._state_events.setdefault(uuid, [])
        # The very first time, record the current state so there is a value.
        if now == before and events:
            return
        when = status.get("currTime")
        if not isinstance(when, (int, float)) or when <= 0:
            when = int(time.time() * 1000)
        if events and events[0].get("eventType") == STATE_EVENTS[now]:
            self._last_state[uuid] = now
            return
        events.insert(0, {"eventType": STATE_EVENTS[now], "time": int(when), "recordType": "state"})
        del events[MAX_STATE_EVENTS:]
        if before == 1 and now != 1 and self.config_entry.options.get(CONF_PARKED_CAPTURE, DEFAULT_PARKED_CAPTURE):
            self.hass.async_create_background_task(self._async_parked_capture(uuid), f"{DOMAIN} parked capture {uuid}")
        self._last_state[uuid] = now
        self._save_store()

    def _save_store(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "events": self._state_events,
                "state": self._last_state,
                "settings": self.remote_settings,
                "settings_read_at": self.remote_settings_read_at,
                "parked_captures": self.parked_captures,
                "position": {k: list(v) for k, v in self._position.items()},
                "placeholders": [list(p) for p in sorted(self._placeholders)],
            },
            5,
        )

    async def async_read_remote_settings(self, uuid: str) -> dict[str, Any]:
        """Read every remote setting from the camera (it must be online)."""
        from .remote import async_p2p_request, setting_values

        reply = await async_p2p_request(self.hass, self, uuid, "querySettings", {"all": {}})
        values = setting_values(reply)
        if values:
            self.remote_settings[uuid] = values
            self.remote_settings_read_at[uuid] = time.time()
            self._save_store()
            self.async_update_listeners()
        return values

    async def async_save_remote_setting(self, uuid: str, key: str, value: Any) -> None:
        """Change one remote setting on the camera (it must be online)."""
        from .remote import RemoteError, async_p2p_request

        reply = await async_p2p_request(self.hass, self, uuid, "saveSettings", {key: value})
        if reply.get("resultCode") not in (0, "0", None):
            raise RemoteError(f"result {reply.get('resultCode')} {reply.get('resultDesc') or ''}".strip())
        self.remote_settings.setdefault(uuid, {})[key] = value
        self._save_store()
        self.async_update_listeners()

    async def _async_parked_capture(self, uuid: str) -> None:
        """The car stopped: take the parked photo of each lens ourselves."""
        from homeassistant.exceptions import HomeAssistantError

        from .remote_entities import async_capture

        for lens in ("front", "rear"):
            try:
                path = await async_capture(self.hass, self, uuid, lens)
            except HomeAssistantError as err:
                _LOGGER.info("Parked photo (%s) not taken: %s", lens, err)
                continue
            self.parked_captures.setdefault(uuid, {})[lens] = {"path": path, "time": time.time()}
            self._save_store()
            self.async_update_listeners()

    def _maybe_read_settings(self, uuid: str, status: dict[str, Any]) -> None:
        """Read the remote settings once each time the camera comes online."""
        online = status.get("mqtt") == 1 and status.get("status") in (1, 2, 3)
        if not online:
            self._settings_read_online.discard(uuid)
            return
        if uuid in self._settings_read_online:
            return
        self._settings_read_online.add(uuid)

        async def _read() -> None:
            from .remote import RemoteError

            try:
                await self.async_read_remote_settings(uuid)
            except RemoteError as err:
                _LOGGER.debug("Remote settings not read: %s", err)
                self._settings_read_online.discard(uuid)  # next poll retries

        self.hass.async_create_background_task(_read(), f"{DOMAIN} read settings {uuid}")

    def _note_position(self, uuid: str, status: dict[str, Any]) -> None:
        lat, lon = status.get("currLatitude"), status.get("currLongitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return
        if abs(lat) >= NO_POSITION or abs(lon) >= NO_POSITION or (lat, lon) == (0, 0):
            return
        key = (round(lat, 6), round(lon, 6))
        if key in self._placeholders:
            return
        old = self._position.get(uuid)
        if old is not None and key == (round(old[0], 6), round(old[1], 6)):
            return
        online = status.get("mqtt") == 1 and status.get("status") in (1, 2, 3)
        if not online and old is not None:
            # An offline camera cannot move; a new point now is the stand-in —
            # unless it is close (the last fix may have landed between polls).
            if _distance_m(old[0], old[1], lat, lon) > PLACEHOLDER_MIN_M:
                _LOGGER.debug("Ignoring stand-in position %s for %s", key, uuid)
                self._placeholders.add(key)
                self._save_store()
                return
        if old is not None and _distance_m(old[0], old[1], lat, lon) > JUMP_CONFIRM_M:
            # A point this far from the last one is held until the next one
            # agrees: waking up, the camera can send one wild fix (seen: 83 km
            # out to sea, back home a minute later), while a real trip made
            # offline arrives as a run of points near each other.
            held = self._jumps.get(uuid)
            if held is None or _distance_m(held[0], held[1], lat, lon) > JUMP_CONFIRM_M:
                _LOGGER.debug("Holding far position %s for %s until confirmed", key, uuid)
                self._jumps[uuid] = (lat, lon)
                return
        self._jumps.pop(uuid, None)
        self._position[uuid] = (lat, lon, time.time())
        self._save_store()

    def _fire_new_events(
        self, uuid: str, info: dict[str, Any], events: list[dict[str, Any]]
    ) -> None:
        keys = {(e.get("eventType"), e.get("time")) for e in events}
        seen = self._seen_events.get(uuid)
        self._seen_events[uuid] = keys
        if seen is None:  # first poll: remember, don't replay history
            return
        for event in events:
            if (event.get("eventType"), event.get("time")) in seen:
                continue
            self.hass.bus.async_fire(
                EVENT_DEVICE,
                {
                    "device_uuid": uuid,
                    "device_name": info.get("name"),
                    "event": EVENT_TYPES.get(event.get("eventType"), str(event.get("eventType"))),
                    "event_type": event.get("eventType"),
                    "time": event.get("time"),
                },
            )

    def _fire_new_media(
        self, uuid: str, info: dict[str, Any], album: dict[int, list[dict[str, Any]]]
    ) -> None:
        ids = {str(i.get("id")) for items in album.values() for i in items}
        seen = self._seen_media.get(uuid)
        self._seen_media[uuid] = ids | (seen or set())
        if seen is None:
            return
        for tab, items in album.items():
            for item in items:
                if str(item.get("id")) in seen:
                    continue
                camera, kind = media_kind(item)
                self.hass.bus.async_fire(
                    EVENT_NEW_MEDIA,
                    {
                        "device_uuid": uuid,
                        "device_name": info.get("name"),
                        "album": ALBUMS.get(tab, str(tab)),
                        "camera": camera,
                        "kind": kind,
                        "item_id": item.get("id"),
                        "file_name": (item.get("content") or {}).get("fileName"),
                        "time": item.get("commitDate"),
                    },
                )
