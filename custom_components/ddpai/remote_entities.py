"""The app's "Remote settings" as entities, and the Capture buttons.

Settings are read from the camera over P2P whenever it comes online (and with
the "Read remote settings" button) and kept, so they show their last known
value while the car is off. Changing one sends ``saveSettings`` to the camera,
which has to be online for that.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .coordinator import DdpaiCoordinator
from .entity import DdpaiEntity
from .live import LiveViewError, get_stream

_LOGGER = logging.getLogger(__name__)

CAPTURE_TIMEOUT = 60


@dataclass(frozen=True)
class RemoteSwitch:
    key: str  # camera setting
    translation_key: str
    on: Any = "on"
    off: Any = "off"


@dataclass(frozen=True)
class RemoteSelect:
    key: str
    translation_key: str
    options: dict[str, Any]  # option -> camera value


# The three settings of the app's Remote settings page (Z60 Pro, 4G).
REMOTE_SWITCHES = (
    RemoteSwitch("emervideo_upload", "upload_captured_pictures"),
    RemoteSwitch("autoStaticShutdown", "auto_shutdown_after_parking"),
)
REMOTE_SELECTS = (
    # gsensor_mode: 0 high, 1 medium, 2 low, 3 off (GsensorHelper in the app).
    RemoteSelect("gsensor_mode", "capture_sensitivity", {"high": 0, "medium": 1, "low": 2, "off": 3}),
)


def same(a: Any, b: Any) -> bool:
    """Compare a stored setting with a value, tolerating "1" vs 1."""
    return str(a).lower() == str(b).lower()


class RemoteSettingEntity(DdpaiEntity):
    """Base: the camera's stored value of one remote setting."""

    _key: str

    @property
    def _value(self) -> Any:
        return (self.coordinator.remote_settings.get(self._uuid) or {}).get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self._value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        read = self.coordinator.remote_settings_read_at.get(self._uuid)
        return {"read_at": dt_util.utc_from_timestamp(read).isoformat() if read else None}

    async def _save(self, value: Any) -> None:
        from .remote import RemoteError

        try:
            await self.coordinator.async_save_remote_setting(self._uuid, self._key, value)
        except RemoteError as err:
            raise HomeAssistantError(f"The dashcam did not take the change: {err}") from err


async def async_capture(hass, coordinator: DdpaiCoordinator, uuid: str, lens: str) -> str:
    """Open the live stream on ``lens``, save its first full picture to /media."""
    stream = get_stream(hass, coordinator, uuid)
    key = f"capture-{lens}"
    before = stream.last_keyframe_time.get(lens, 0)
    started = time.time()
    try:
        await stream.async_set_lens(lens)
        await stream.async_hold(key)
    except LiveViewError as err:
        raise HomeAssistantError(f"Could not reach the dashcam: {err}") from err
    try:
        deadline = time.monotonic() + CAPTURE_TIMEOUT
        while time.monotonic() < deadline:
            if stream.last_keyframe_time.get(lens, 0) > max(before, started):
                break
            if not stream.running and stream.last_error:
                raise HomeAssistantError(stream.last_error.replace("ERROR ", ""))
            await asyncio.sleep(0.5)
        else:
            raise HomeAssistantError("The dashcam sent no picture in time")
        snap = await stream.async_snapshot(lens)
    finally:
        stream.release(key)
    if snap is None:
        raise HomeAssistantError("Could not decode the picture")
    folder = os.path.join(hass.config.media_dirs.get("local", hass.config.path("media")), "ddpai")
    name = f"{dt_util.now().strftime('%Y%m%d_%H%M%S')}_{lens}.jpg"

    def _write() -> str:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as fh:
            fh.write(snap[0])
        return path

    return await hass.async_add_executor_job(_write)
