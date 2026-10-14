"""The app's "Remote settings" and "Capture", over the camera's P2P channel.

Each request runs in the producer process (``live/producer.py``, p2p mode) on
its own short P2P session, the way the app sends them: a JSON message on
IOCtrl 0x30000006 (``querySettings`` / ``saveSettings`` / ``remoteCapture``),
answered on 0x30000007. The camera must be online (4G connected).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .api import DdpaiError
from .const import CONF_TUTK_LICENSE_KEY, TUTK_AUTH_KEY
from .live import LiveViewError, async_get_library

if TYPE_CHECKING:
    from .coordinator import DdpaiCoordinator

_LOGGER = logging.getLogger(__name__)

_PRODUCER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live", "producer.py")
P2P_TIMEOUT = 75


class RemoteError(Exception):
    """The camera could not be reached or refused the request."""


async def async_p2p_request(
    hass: HomeAssistant, coordinator: DdpaiCoordinator, uuid: str, action: str, data: dict[str, Any]
) -> dict[str, Any]:
    """One app JSON request to the camera; returns its reply."""
    lock = coordinator.p2p_locks.setdefault(uuid, asyncio.Lock())
    async with lock:
        try:
            lib = await async_get_library(hass)
        except LiveViewError as err:
            raise RemoteError(str(err)) from err
        try:
            creds = await coordinator.client.async_get_live_credentials(uuid)
        except DdpaiError as err:
            raise RemoteError(f"Could not read the camera's P2P credentials: {err}") from err
        if not creds.get("uid") or not creds.get("password"):
            raise RemoteError("The cloud returned no P2P credentials for this camera")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DDPAI_MODE": "p2p",
            "DDPAI_REQUEST": json.dumps({"action": action, "data": data}),
            "DDPAI_LIB": lib,
            "DDPAI_P2P_UID": creds["uid"],
            "DDPAI_P2P_PASSWORD": creds["password"],
            "DDPAI_AUTH_KEY": TUTK_AUTH_KEY,
            "DDPAI_LICENSE_KEY": coordinator.config_entry.options.get(CONF_TUTK_LICENSE_KEY, ""),
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-E", "-s", _PRODUCER,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=os.path.dirname(_PRODUCER),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), P2P_TIMEOUT)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RemoteError(f"{action}: no answer within {P2P_TIMEOUT} s") from exc
        log = err.decode(errors="replace").strip().splitlines()
        _LOGGER.debug("p2p %s: %s", action, " | ".join(log))
        if proc.returncode:
            reason = next((line[6:] for line in reversed(log) if line.startswith("ERROR ")), None)
            raise RemoteError(reason or (log[-1] if log else f"exit code {proc.returncode}"))
        try:
            return json.loads(out.decode())
        except ValueError as exc:
            raise RemoteError(f"{action}: unreadable reply") from exc


def setting_values(reply: dict[str, Any]) -> dict[str, Any]:
    """``{"key": {"Value": v}}`` (``"na"`` = unsupported) -> ``{"key": v}``."""
    out: dict[str, Any] = {}
    for key, raw in (reply.get("data") or {}).items():
        value = raw.get("Value", raw.get("value")) if isinstance(raw, dict) else raw
        if value is None or value == "na":
            continue
        out[key] = value
    return out
