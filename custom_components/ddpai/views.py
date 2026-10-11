"""Serve cloud album files through Home Assistant.

The DDPAI media CDN is plain HTTP, which a browser refuses to load inside an
HTTPS Home Assistant page, so album photos and clips are proxied through this
authenticated view. Only files of album items the integration has listed can
be fetched — the view never takes a URL from the request.
"""
from __future__ import annotations

import logging

import aiohttp
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .coordinator import media_kind

_LOGGER = logging.getLogger(__name__)

URL_BASE = "/api/ddpai/media"
_PASS_HEADERS = ("Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag")
IMAGE_THUMB = "?imageView2/1/w/320/h/180"
VIDEO_THUMB = "?vframe/jpg/offset/1/w/320/h/180"


def media_path(entry_id: str, item_id: str, thumb: bool = False) -> str:
    """Path of one album file (sign it before handing it to a browser)."""
    path = f"{URL_BASE}/{entry_id}/{item_id}"
    return f"{path}?thumb=1" if thumb else path


class DdpaiMediaView(HomeAssistantView):
    """Proxy an album file from the DDPAI CDN."""

    url = URL_BASE + "/{entry_id}/{item_id}"
    name = "api:ddpai:media"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, entry_id: str, item_id: str) -> web.StreamResponse:
        coordinator = self.hass.data.get(DOMAIN, {}).get(entry_id)
        item = coordinator.media.get(item_id) if coordinator else None
        url = ((item or {}).get("content") or {}).get("url")
        if not url:
            return web.Response(status=404)
        _camera, kind = media_kind(item)
        thumb = request.query.get("thumb") == "1"
        if thumb:
            url += VIDEO_THUMB if kind == "video" else IMAGE_THUMB
            content_type = "image/jpeg"
        else:
            content_type = "video/mp4" if kind == "video" else "image/jpeg"

        headers = {}
        if not thumb and "Range" in request.headers:
            headers["Range"] = request.headers["Range"]
        session = async_get_clientsession(self.hass)
        try:
            upstream = await session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=None, sock_read=60))
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Album file %s unreachable: %s", item_id, err)
            return web.Response(status=502)
        try:
            if upstream.status not in (200, 206):
                return web.Response(status=upstream.status if upstream.status < 600 else 502)
            response = web.StreamResponse(status=upstream.status)
            response.content_type = content_type
            for name in _PASS_HEADERS:
                if name in upstream.headers:
                    response.headers[name] = upstream.headers[name]
            response.headers["Cache-Control"] = "private, max-age=86400"
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            upstream.release()
