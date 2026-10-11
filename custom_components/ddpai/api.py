"""Async client for the DDPAI cloud (overseas service).

The app signs in with the account login and password (both AES encrypted with
an app-wide key), gets a ``JSESSIONID`` cookie that expires after 30 minutes
without use, and sends that cookie on every call. There is no per-request
signature. When the server says the session timed out, the client signs in
again and repeats the call once.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import (
    APP_KEY,
    ERR_BAD_PASSWORD,
    ERR_EMAIL_NOT_ACTIVATED,
    ERR_SESSION_FAILED,
    ERR_SESSION_TIMEOUT,
    ERR_USER_LOCKED,
    ERR_USER_NOT_FOUND,
    FENCE_COMPANY,
    MAIN_URL,
    MICRO_URL,
    PCODE,
)

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=30)
MEDIA_TIMEOUT = aiohttp.ClientTimeout(total=120)
_SESSION_ERRORS = {ERR_SESSION_TIMEOUT, ERR_SESSION_FAILED}


class DdpaiError(Exception):
    """Base error for the DDPAI client."""


class DdpaiConnectionError(DdpaiError):
    """The DDPAI cloud could not be reached."""


class DdpaiAuthError(DdpaiError):
    """The account login was refused."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(message)
        self.code = code


class _SessionExpired(DdpaiError):
    """Internal: the cookie is no longer valid."""


def app_encrypt(text: str) -> str:
    """Encrypt a login field the way the app does (AES-128-ECB, hex)."""
    padder = padding.PKCS7(128).padder()
    data = padder.update(text.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(APP_KEY), modes.ECB()).encryptor()
    return (enc.update(data) + enc.finalize()).hex()


def _info(body: Any) -> Any:
    """Return ``error_info`` from a wrapped reply, decoding it when it is a string."""
    if isinstance(body, dict) and "error_info" in body:
        info = body["error_info"]
        if isinstance(info, str):
            try:
                return json.loads(info)
            except ValueError:
                return info
        return info
    return body


class DdpaiApiClient:
    """Talk to the DDPAI cloud for one account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        country: str,
    ) -> None:
        self._http = session
        self._username = username
        self._password = password
        self._country = country.upper()
        self._cookie: str | None = None
        self._login_lock = asyncio.Lock()
        self.user_id: int | None = None
        self.session_timeout: int | None = None

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"doi-lel": "1", "countryCode": self._country}
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    async def async_login(self) -> int:
        """Sign in and keep the session cookie. Returns the account's user id."""
        form = {
            "from": "2",
            "username": app_encrypt(self._username),
            "password": app_encrypt(self._password),
            "pcode": PCODE,
            "client": "app",
        }
        try:
            async with self._http.post(
                f"{MAIN_URL}/login",
                data=form,
                headers={"doi-lel": "1", "countryCode": self._country},
                timeout=TIMEOUT,
            ) as resp:
                body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise DdpaiConnectionError(f"Login request failed: {err}") from err

        if not isinstance(body, dict) or "user_id" not in body:
            code = body.get("error_code") if isinstance(body, dict) else None
            message = body.get("error_info") if isinstance(body, dict) else str(body)
            raise DdpaiAuthError(code, str(message))
        info = _info(body)
        session = info.get("session") if isinstance(info, dict) else None
        if not session:
            raise DdpaiAuthError(None, "Login reply carried no session")
        self._cookie = f"JSESSIONID={session}"
        self.user_id = int(body["user_id"])
        try:
            self.session_timeout = int(body.get("session_timeout") or 0) or None
        except (TypeError, ValueError):
            self.session_timeout = None
        return self.user_id

    async def _ensure_login(self, stale_cookie: str | None) -> None:
        async with self._login_lock:
            # Another call may have signed in again while this one waited.
            if self._cookie is None or self._cookie == stale_cookie:
                await self.async_login()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
    ) -> Any:
        for attempt in (1, 2):
            if self._cookie is None:
                await self._ensure_login(None)
            cookie = self._cookie
            try:
                return await self._request_once(method, url, json_body)
            except _SessionExpired:
                if attempt == 2:
                    raise DdpaiAuthError(ERR_SESSION_TIMEOUT, "Session refused twice")
                _LOGGER.debug("DDPAI session expired, signing in again")
                await self._ensure_login(cookie)
        raise DdpaiError("unreachable")

    async def _request_once(self, method: str, url: str, json_body: Any) -> Any:
        try:
            async with self._http.request(
                method,
                url,
                json=json_body,
                headers=self._headers,
                timeout=TIMEOUT,
            ) as resp:
                text = await resp.text()
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DdpaiConnectionError(f"{url}: {err}") from err
        try:
            body = json.loads(text) if text else None
        except ValueError as err:
            raise DdpaiError(f"{url}: HTTP {status}, not JSON: {text[:200]}") from err
        if isinstance(body, dict) and body.get("error_code") in _SESSION_ERRORS:
            raise _SessionExpired()
        if isinstance(body, dict) and body.get("error_code") not in (None, 0):
            raise DdpaiError(
                f"{url}: error {body.get('error_code')} {body.get('error_info')}"
            )
        if status >= 400:
            raise DdpaiError(f"{url}: HTTP {status} {text[:200]}")
        return body

    # --- account ---------------------------------------------------------

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Every camera bound to the account."""
        body = await self._request(
            "GET", f"{MAIN_URL}/ddpai/api/v1/termdev/authorityMgr/queryUserDevs"
        )
        if not isinstance(body, list):
            raise DdpaiError(f"Unexpected device list: {str(body)[:200]}")
        return body

    async def async_get_status(self, uuid: str) -> dict[str, Any]:
        """4G state and last known position of one camera."""
        body = await self._request(
            "POST",
            f"{MICRO_URL}/device/api/v2/device2c/checkDevnetStatus",
            json_body={"uuid": uuid, "pcode": PCODE},
        )
        info = _info(body)
        if not isinstance(info, dict):
            return {}
        # The reply also carries remote-live connection fields; drop them.
        return {k: v for k, v in info.items() if not k.startswith("p2p")}

    async def async_get_events(self, uuid: str) -> list[dict[str, Any]]:
        """Recent power, collision and remote-mode events of one camera."""
        body = await self._request(
            "POST",
            f"{MICRO_URL}/device/api/v2/device2c/queryDeviceEventByUuid",
            json_body={"uuid": uuid},
        )
        info = _info(body)
        events = info.get("eventAndStatusList") if isinstance(info, dict) else None
        return events if isinstance(events, list) else []

    async def async_album_unread(self, uuid: str) -> int:
        """How many cloud album files the app has not shown yet."""
        body = await self._request(
            "POST",
            f"{MICRO_URL}/device/api/v2/device2c/countCloudSpaceList",
            json_body={"uuid": uuid},
        )
        info = _info(body)
        try:
            return int(info.get("unReadCount") or 0) if isinstance(info, dict) else 0
        except (TypeError, ValueError):
            return 0

    async def async_get_album(
        self, uuid: str, album: int, page: int = 1, page_size: int = 30
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of a cloud album tab. Marks it read in the app."""
        body = await self._request(
            "POST",
            f"{MICRO_URL}/device/api/v2/device2c/queryCloudSpaceList",
            json_body={
                "uuid": uuid,
                "alarmType": album,
                "pageNum": page,
                "pageSize": page_size,
            },
        )
        info = _info(body)
        if not isinstance(info, dict):
            return [], 0
        items: list[dict[str, Any]] = []
        for raw in info.get("data") or []:
            item = dict(raw)
            content = item.get("content")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except ValueError:
                    content = {}
            item["content"] = content if isinstance(content, dict) else {}
            items.append(item)
        return items, int(info.get("totalPage") or 0)

    async def async_get_sim(self, uuid: str, iccid: str, model: str) -> dict[str, Any]:
        """State of the camera's SIM card."""
        body = await self._request(
            "POST",
            f"{MICRO_URL}/sim/api/v1/sim/checkStatus",
            json_body={"iccid": iccid, "uuid": uuid, "model": model},
        )
        return body if isinstance(body, dict) else {}

    async def async_get_flow_records(self, iccid: str, page: int = 1) -> list[dict[str, Any]]:
        """Monthly data usage / top-up records of the camera's SIM."""
        body = await self._request(
            "POST",
            f"{MICRO_URL}/sim/api/v1/sim/perMonthFlow",
            json_body={"iccid": iccid, "page": page},
        )
        info = _info(body)
        if isinstance(info, dict):
            info = info.get("data")
        return info if isinstance(info, list) else []

    # --- geofences ("electronic fences") ----------------------------------

    async def async_get_fences(self, uuid: str) -> list[dict[str, Any]]:
        """The camera's geofences, ``gpsData`` decoded."""
        body = await self._request(
            "POST",
            f"{MICRO_URL}/device/api/v2/device2c/getPoint",
            json_body={"scope": 3, "code": uuid, "company": FENCE_COMPANY, "pcode": PCODE},
        )
        info = _info(body)
        fences = []
        for raw in info if isinstance(info, list) else []:
            fence = dict(raw)
            gps = fence.get("gpsData")
            if isinstance(gps, str):
                try:
                    gps = json.loads(gps)
                except ValueError:
                    gps = {}
            fence["gpsData"] = gps if isinstance(gps, dict) else {}
            fences.append(fence)
        return fences

    async def async_add_fence(
        self, uuid: str, name: str, gps_data: dict[str, Any], trigger_mode: int
    ) -> Any:
        """Create a geofence on the camera."""
        return _info(
            await self._request(
                "POST",
                f"{MICRO_URL}/device/api/v2/device2c/setPoint",
                json_body={
                    "scope": 3,
                    "code": uuid,
                    "company": FENCE_COMPANY,
                    "pcode": PCODE,
                    "type": 0,
                    "startTime": 0,
                    "endTime": 0,
                    "gpsData": json.dumps(gps_data, separators=(",", ":")),
                    "name": name,
                    "triggerMode": trigger_mode,
                },
            )
        )

    async def async_update_fence(
        self,
        fence_id: int,
        name: str,
        gps_data: dict[str, Any],
        trigger_mode: int,
        moved: bool,
    ) -> Any:
        """Change a geofence (name, area, trigger, on/off)."""
        return _info(
            await self._request(
                "POST",
                f"{MICRO_URL}/device/api/v2/device2c/updatePoint",
                json_body={
                    "id": fence_id,
                    "gpsData": json.dumps(gps_data, separators=(",", ":")),
                    "name": name,
                    "triggerMode": trigger_mode,
                    "isEditGps": moved,
                },
            )
        )

    async def async_delete_fence(self, fence_id: int) -> Any:
        """Delete a geofence."""
        return _info(
            await self._request(
                "GET", f"{MICRO_URL}/device/api/v2/device2c/deletePoint/{int(fence_id)}"
            )
        )

    async def async_fetch_media(self, url: str) -> bytes:
        """Download an album file from the DDPAI media CDN (no auth needed)."""
        try:
            async with self._http.get(url, timeout=MEDIA_TIMEOUT) as resp:
                if resp.status != 200:
                    raise DdpaiError(f"Media HTTP {resp.status}")
                return await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DdpaiConnectionError(f"Media download failed: {err}") from err


def auth_error_key(err: DdpaiAuthError) -> str:
    """Map a refused login to a config-flow error key."""
    return {
        ERR_USER_NOT_FOUND: "unknown_user",
        ERR_BAD_PASSWORD: "invalid_auth",
        ERR_EMAIL_NOT_ACTIVATED: "not_activated",
        ERR_USER_LOCKED: "locked",
    }.get(err.code, "invalid_auth")
