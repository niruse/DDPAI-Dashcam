"""The newest picture of each lens: the last live view or the newest parked photo.

The cloud album only gets a parked photo now and then, so after a drive the
newest picture Home Assistant has is usually the last live frame (kept on disk
by ``live.LiveStream.async_snapshot``).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .api import DdpaiError
from .const import ALBUM_PARKED
from .coordinator import media_kind
from .live import get_stream

if TYPE_CHECKING:
    from .coordinator import DdpaiCoordinator

_LOGGER = logging.getLogger(__name__)


def newest_parked(coordinator: DdpaiCoordinator, uuid: str, lens: str) -> dict[str, Any] | None:
    """The newest parked photo of a lens, from the last album listing."""
    items = (((coordinator.data or {}).get(uuid) or {}).get("album") or {}).get(ALBUM_PARKED) or []
    mine = [i for i in items if media_kind(i) == (lens, "image")]
    return max(mine, key=lambda i: i.get("commitDate") or 0, default=None)


class LastView:
    """Fetches and caches the newest picture of one lens."""

    def __init__(self, hass: HomeAssistant, coordinator: DdpaiCoordinator, uuid: str, lens: str) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.uuid = uuid
        self.lens = lens
        self._parked_id: str | None = None
        self._parked: bytes | None = None
        self.source: str | None = None
        self.taken: float | None = None

    async def async_picture(self) -> bytes | None:
        """JPEG of the newer of the last live view and the newest parked photo."""
        live = await get_stream(self.hass, self.coordinator, self.uuid).async_snapshot(self.lens)
        item = newest_parked(self.coordinator, self.uuid, self.lens)
        parked_at = (item or {}).get("commitDate", 0) / 1000 if item else 0
        if live is not None and live[1] >= parked_at:
            self.source, self.taken = "live", live[1]
            return live[0]
        url = ((item or {}).get("content") or {}).get("url")
        if url:
            item_id = str(item.get("id"))
            if item_id != self._parked_id or not self._parked:
                try:
                    self._parked = await self.coordinator.client.async_fetch_media(url)
                    self._parked_id = item_id
                except DdpaiError as err:
                    _LOGGER.debug("Could not fetch parked photo %s: %s", item_id, err)
            if self._parked:
                self.source, self.taken = "parked_photo", parked_at
                return self._parked
        if live is not None:
            self.source, self.taken = "live", live[1]
            return live[0]
        return None
