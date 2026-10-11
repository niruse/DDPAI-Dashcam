"""Browse the DDPAI cloud album in Home Assistant's media browser.

Identifiers are ``|``-joined and told apart by segment count:

* ``""``                          → root, lists cameras
* ``entry_id|uuid``               → the album tabs
* ``entry_id|uuid|album``         → that tab's photos and clips
* ``entry_id|uuid|item|item_id``  → one file (resolve only)
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import DdpaiError
from .const import ALBUMS, CONF_ENABLE_MEDIA_BROWSER, DOMAIN
from .coordinator import DdpaiCoordinator, media_kind
from .views import media_path

_SEP = "|"
_MAX_PAGES = 4
_SIGN_FOR = timedelta(hours=6)


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Register the DDPAI media source."""
    return DdpaiMediaSource(hass)


class DdpaiMediaSource(MediaSource):
    """Provide the cloud album of each 4G camera."""

    name = "DDPAI Dashcam"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    def _coordinators(self) -> dict[str, DdpaiCoordinator]:
        result: dict[str, DdpaiCoordinator] = {}
        for entry_id, coordinator in self.hass.data.get(DOMAIN, {}).items():
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry and entry.options.get(CONF_ENABLE_MEDIA_BROWSER, True):
                result[entry_id] = coordinator
        return result

    def _sign(self, path: str) -> str:
        return async_sign_path(self.hass, path, _SIGN_FOR)

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a file identifier to a signed URL of the media view."""
        parts = (item.identifier or "").split(_SEP)
        if len(parts) != 4 or parts[2] != "item":
            raise Unresolvable(f"Unknown DDPAI media identifier: {item.identifier}")
        entry_id, _uuid, _marker, item_id = parts
        coordinator = self._coordinators().get(entry_id)
        if coordinator is None or item_id not in coordinator.media:
            raise Unresolvable("That album file is no longer listed")
        _camera, kind = media_kind(coordinator.media[item_id])
        mime = "video/mp4" if kind == "video" else "image/jpeg"
        return PlayMedia(self._sign(media_path(entry_id, item_id)), mime)

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse cameras → album tabs → files."""
        coordinators = self._coordinators()
        parts = item.identifier.split(_SEP) if item.identifier else []
        if not parts:
            return self._browse_root(coordinators)
        if len(parts) == 2:
            return self._browse_camera(coordinators, *parts)
        if len(parts) == 3:
            return await self._browse_album(coordinators, *parts)
        raise Unresolvable(f"Cannot browse {item.identifier}")

    def _folder(self, identifier: str | None, title: str, children: list[BrowseMediaSource]) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.IMAGE,
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )

    def _browse_root(self, coordinators: dict[str, DdpaiCoordinator]) -> BrowseMediaSource:
        children = []
        for entry_id, coordinator in coordinators.items():
            for uuid, cam in (coordinator.data or {}).items():
                info = cam.get("info") or {}
                children.append(
                    self._folder(
                        f"{entry_id}{_SEP}{uuid}",
                        info.get("deviceName") or info.get("name") or uuid,
                        [],
                    )
                )
        return self._folder(None, self.name, children)

    def _browse_camera(
        self, coordinators: dict[str, DdpaiCoordinator], entry_id: str, uuid: str
    ) -> BrowseMediaSource:
        coordinator = coordinators.get(entry_id)
        if coordinator is None or uuid not in (coordinator.data or {}):
            raise Unresolvable("Unknown DDPAI camera")
        children = [
            self._folder(f"{entry_id}{_SEP}{uuid}{_SEP}{tab}", title, [])
            for tab, title in ALBUMS.items()
        ]
        name = (coordinator.data[uuid].get("info") or {}).get("deviceName") or uuid
        return self._folder(f"{entry_id}{_SEP}{uuid}", name, children)

    async def _browse_album(
        self, coordinators: dict[str, DdpaiCoordinator], entry_id: str, uuid: str, tab: str
    ) -> BrowseMediaSource:
        coordinator = coordinators.get(entry_id)
        if coordinator is None or uuid not in (coordinator.data or {}):
            raise Unresolvable("Unknown DDPAI camera")
        try:
            album = int(tab)
        except ValueError as err:
            raise Unresolvable(f"Unknown album {tab}") from err
        items = []
        page = 1
        try:
            while page <= _MAX_PAGES:
                batch, pages = await coordinator.client.async_get_album(uuid, album, page)
                coordinator.remember_media(uuid, batch)
                items.extend(batch)
                if page >= pages:
                    break
                page += 1
        except DdpaiError as err:
            raise Unresolvable(f"Could not list the album: {err}") from err

        children = []
        for item in items:
            item_id = str(item.get("id"))
            camera, kind = media_kind(item)
            when = item.get("commitDate") or item.get("reportTime")
            stamp = (
                dt_util.as_local(dt_util.utc_from_timestamp(when / 1000)).strftime("%Y-%m-%d %H:%M:%S")
                if when
                else (item.get("content") or {}).get("fileName", item_id)
            )
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{entry_id}{_SEP}{uuid}{_SEP}item{_SEP}{item_id}",
                    media_class=MediaClass.VIDEO if kind == "video" else MediaClass.IMAGE,
                    media_content_type=MediaType.VIDEO if kind == "video" else MediaType.IMAGE,
                    title=f"{stamp} · {camera}",
                    can_play=True,
                    can_expand=False,
                    thumbnail=self._sign(media_path(entry_id, item_id, thumb=True)),
                )
            )
        return self._folder(
            f"{entry_id}{_SEP}{uuid}{_SEP}{tab}", ALBUMS.get(album, tab), children
        )
