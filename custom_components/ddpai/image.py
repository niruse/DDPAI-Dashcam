"""Newest cloud album photo of each camera lens, as image entities."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import DdpaiConfigEntry
from .api import DdpaiError
from .const import ALBUM_PARKED, ALBUM_URGENT
from .coordinator import DdpaiCoordinator, media_kind
from .entity import DdpaiEntity

_LOGGER = logging.getLogger(__name__)

# Frame of an urgent clip used as its picture.
URGENT_FRAME = "?vframe/jpg/offset/1"


async def async_setup_entry(
    hass: HomeAssistant, entry: DdpaiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the album pictures for each 4G camera."""
    coordinator = entry.runtime_data
    lenses = ["front", "rear"]
    if coordinator.has_cabin_camera:
        lenses.append("cabin")
    async_add_entities(
        DdpaiAlbumImage(hass, coordinator, uuid, album, lens)
        for uuid in coordinator.data
        for album in (ALBUM_PARKED, ALBUM_URGENT)
        for lens in lenses
    )


class DdpaiAlbumImage(DdpaiEntity, ImageEntity):
    """The newest file of one lens in one cloud album tab."""

    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DdpaiCoordinator,
        uuid: str,
        album: int,
        lens: str,
    ) -> None:
        DdpaiEntity.__init__(self, coordinator, uuid)
        ImageEntity.__init__(self, hass)
        self._album = album
        self._lens = lens
        prefix = "parked" if album == ALBUM_PARKED else "urgent"
        self._attr_translation_key = f"{prefix}_{lens}"
        self._attr_unique_id = f"{uuid}_{prefix}_{lens}"
        self._item: dict[str, Any] | None = None
        self._cached_id: str | None = None
        self._cached: bytes | None = None
        self._sync()

    def _newest(self) -> dict[str, Any] | None:
        items = ((self.camera or {}).get("album") or {}).get(self._album) or []
        mine = [i for i in items if media_kind(i)[0] == self._lens]
        return max(mine, key=lambda i: i.get("commitDate") or 0, default=None)

    def _sync(self) -> None:
        item = self._newest()
        if (item or {}).get("id") != (self._item or {}).get("id"):
            self._item = item
            when = (item or {}).get("commitDate")
            self._attr_image_last_updated = (
                dt_util.utc_from_timestamp(when / 1000) if when else None
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        return super().available and self._item is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self._item or {}
        content = item.get("content") or {}
        taken: datetime | None = self._attr_image_last_updated
        return {
            "file_name": content.get("fileName"),
            "kind": media_kind(item)[1] if item else None,
            "taken": taken.isoformat() if taken else None,
            "locked": item.get("subStatus") == 1,
        }

    async def async_image(self) -> bytes | None:
        item = self._item
        url = ((item or {}).get("content") or {}).get("url")
        if not url:
            return None
        item_id = str(item.get("id"))
        if self._cached_id == item_id and self._cached:
            return self._cached
        if media_kind(item)[1] == "video":
            url += URGENT_FRAME
        try:
            data = await self.coordinator.client.async_fetch_media(url)
        except DdpaiError as err:
            _LOGGER.debug("Could not fetch album picture %s: %s", item_id, err)
            return self._cached
        self._cached_id, self._cached = item_id, data
        return data

