"""4G live view of each camera, as a camera entity.

The stream itself comes from ``live``: a TUTK P2P producer started when the
first viewer arrives and stopped shortly after the last one leaves. Opening the
stream wakes the camera and uses its 4G data. The camera streams one lens at a
time (like the app); the lens is chosen with the "Live camera" select. The
still picture is the newest parked photo of that lens, so a dashboard
thumbnail never starts a session.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DdpaiConfigEntry
from .const import CONF_ENABLE_LIVE_VIEW
from .coordinator import DdpaiCoordinator
from .entity import DdpaiEntity
from .lastview import LastView
from .live import LENSES, get_stream, live_path

_LOGGER = logging.getLogger(__name__)

# go2rtc / ffmpeg re-request the source on every new session, so a signature
# only needs to outlive one viewing.
SIGNATURE_LIFETIME = timedelta(hours=12)


async def async_setup_entry(
    hass: HomeAssistant, entry: DdpaiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the live camera of each 4G camera."""
    coordinator = entry.runtime_data
    # 0.3.0 had one camera following a select; now one per lens again.
    registry = er.async_get(hass)
    for uuid in coordinator.data:
        if old := registry.async_get_entity_id("camera", "ddpai", f"{uuid}_live"):
            registry.async_remove(old)
    if not entry.options.get(CONF_ENABLE_LIVE_VIEW, True):
        return
    async_add_entities(DdpaiLiveCamera(coordinator, uuid, lens) for uuid in coordinator.data for lens in LENSES)


class DdpaiLiveCamera(DdpaiEntity, Camera):
    """Live view of one lens over the camera's 4G link.

    Opening it switches the dashcam to this lens. The dashcam has one live
    encoder, so with both cameras open the one opened last has the dashcam and
    the other holds its last picture (it never shows the other lens).
    """

    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_is_streaming = False

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str, lens: str) -> None:
        DdpaiEntity.__init__(self, coordinator, uuid)
        Camera.__init__(self)
        self._lens = lens
        self._attr_translation_key = f"live_{lens}"
        self._attr_unique_id = f"{uuid}_live_{lens}"
        self._views: dict[str, LastView] = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._stream.async_add_listener(self.async_write_ha_state))

    @property
    def _stream(self):
        return get_stream(self.hass, self.coordinator, self._uuid)

    @property
    def is_streaming(self) -> bool:
        return self._stream.running

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stream = self._stream
        return {
            "lens": self._lens,
            "dashcam_showing": stream.current_lens if stream.running else None,
            "last_error": stream.last_error,
        }

    async def stream_source(self) -> str | None:
        """Signed local URL of the MPEG-TS stream."""
        api = self.hass.config.api
        if api is None:
            return None
        path = async_sign_path(
            self.hass,
            live_path(self.coordinator.config_entry.entry_id, self._uuid, self._lens),
            SIGNATURE_LIFETIME,
        )
        if api.use_ssl:
            # The certificate is for the public name, not 127.0.0.1: go2rtc's
            # httpx scheme skips verification; plain ffmpeg gets https.
            scheme = "httpx" if "go2rtc" in self.hass.config.components else "https"
        else:
            scheme = "http"
        return f"{scheme}://127.0.0.1:{api.port}{path}"

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Newest picture of the chosen lens: last live view or parked photo (no 4G needed)."""
        lens = self._lens
        if self._views.get(lens) is None:
            self._views[lens] = LastView(self.hass, self.coordinator, self._uuid, lens)
        return await self._views[lens].async_picture()
