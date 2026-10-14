"""Newest cloud album photo of each camera lens, as image entities."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import DdpaiConfigEntry
from .api import DdpaiError
from .const import ALBUM_PARKED, ALBUM_URGENT, DOMAIN
from .coordinator import DdpaiCoordinator, media_kind
from .entity import DdpaiEntity
from .lastview import LastView, newest_parked
from .live import get_stream

_LOGGER = logging.getLogger(__name__)

# Frame of an urgent clip used as its picture.
URGENT_FRAME = "?vframe/jpg/offset/1"


async def async_setup_entry(
    hass: HomeAssistant, entry: DdpaiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the album pictures for each 4G camera.

    A picture entity appears once the cloud album holds a file of that kind
    (e.g. the first urgent clip, or a cabin photo) — not before, so there are
    no pictures stuck on "unavailable" for files that never come.
    """
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    known: set[tuple[str, int, str]] = set()

    def _present(cam: dict[str, Any], album: int, lens: str) -> bool:
        return any(media_kind(i)[0] == lens for i in (cam.get("album") or {}).get(album) or [])

    @callback
    def _add_new() -> None:
        new = []
        for uuid, cam in (coordinator.data or {}).items():
            for album in (ALBUM_PARKED, ALBUM_URGENT):
                for lens in ("front", "rear", "cabin"):
                    key = (uuid, album, lens)
                    if key not in known and _present(cam, album, lens):
                        known.add(key)
                        new.append(DdpaiAlbumImage(hass, coordinator, uuid, album, lens))
        if new:
            async_add_entities(new)

    # Pictures created by earlier versions for files that never arrived.
    for uuid, cam in (coordinator.data or {}).items():
        for album, prefix in ((ALBUM_PARKED, "parked"), (ALBUM_URGENT, "urgent")):
            for lens in ("front", "rear", "cabin"):
                if not _present(cam, album, lens) and (
                    old := registry.async_get_entity_id("image", DOMAIN, f"{uuid}_{prefix}_{lens}")
                ):
                    registry.async_remove(old)
    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))
    async_add_entities(
        DdpaiLastView(hass, coordinator, uuid, lens)
        for uuid in coordinator.data
        for lens in ("front", "rear")
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



class DdpaiLastView(DdpaiEntity, ImageEntity):
    """The newest picture of a lens: last live view or newest parked photo."""

    _attr_content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant, coordinator: DdpaiCoordinator, uuid: str, lens: str) -> None:
        DdpaiEntity.__init__(self, coordinator, uuid)
        ImageEntity.__init__(self, hass)
        self._lens = lens
        self._view = LastView(hass, coordinator, uuid, lens)
        self._attr_translation_key = f"last_view_{lens}"
        self._attr_unique_id = f"{uuid}_last_view_{lens}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        stream = get_stream(self.hass, self.coordinator, self._uuid)
        self.async_on_remove(stream.async_add_listener(self._schedule_refresh))
        await self._async_refresh()

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule_refresh()
        super()._handle_coordinator_update()

    async def _async_refresh(self) -> None:
        """Pick up a newer live view or parked photo."""
        stream = get_stream(self.hass, self.coordinator, self._uuid)
        live = stream.last_keyframe_time.get(self._lens) or 0
        item = newest_parked(self.coordinator, self._uuid, self._lens)
        parked = ((item or {}).get("commitDate") or 0) / 1000
        if not live:
            snap = await stream.async_snapshot(self._lens)
            live = snap[1] if snap else 0
        newest = max(live, parked)
        if newest and (self._attr_image_last_updated is None or newest > self._attr_image_last_updated.timestamp() + 0.5):
            self._attr_image_last_updated = dt_util.utc_from_timestamp(newest)
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return super().available and self._attr_image_last_updated is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"lens": self._lens, "source": self._view.source}

    async def async_image(self) -> bytes | None:
        return await self._view.async_picture()
