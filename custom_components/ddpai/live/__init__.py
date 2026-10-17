"""4G live view: run the TUTK producer and serve its MPEG-TS to Home Assistant.

One producer process per camera lens, started on the first viewer and stopped
``IDLE_STOP_SECONDS`` after the last one leaves, so the camera's 4G data is
only used while somebody is watching. The stream is served from an
authenticated view; the camera entity hands its signed URL to Home
Assistant's go2rtc (WebRTC) or stream (HLS) integration.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import secrets
import struct
import time
import platform
import sys
import tarfile
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.const import KEY_AUTHENTICATED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..api import DdpaiError
from ..const import CONF_TUTK_LICENSE_KEY, DOMAIN, TUTK_AUTH_KEY
from .mpegts import TSMuxer

if TYPE_CHECKING:
    from ..coordinator import DdpaiCoordinator

_LOGGER = logging.getLogger(__name__)

URL_BASE = "/api/ddpai/live"
LENSES = ("front", "rear")
LIB_NAME = "libIOTCAPIs_ALL.so"
IDLE_STOP_SECONDS = 15
# Producer stdout records (see producer.write_record).
RECORD = struct.Struct(">IBQ")
FLAG_KEYFRAME = 1
FLAG_LENS = 2
FRAME_LIMIT = 8 * 1024 * 1024
TS_PACKET = 188
CHUNK = TS_PACKET * 64
QUEUE_CHUNKS = 256

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRODUCER = os.path.join(_HERE, "producer.py")

# The glibc TUTK build is taken from the public wyze-bridge image, the same
# source the CuboAI integration uses; only these architectures exist there.
_DOCKER_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_DOCKER_AUTH = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:mrlt8/wyze-bridge:pull"
_DOCKER_REGISTRY = "https://registry-1.docker.io/v2/mrlt8/wyze-bridge"


class LiveViewError(Exception):
    """Live view cannot start."""


def keyframe_to_jpeg(access_unit: bytes) -> tuple[bytes, tuple[int, int]] | None:
    """Decode one H.264 keyframe to JPEG (runs in an executor)."""
    import av

    codec = av.CodecContext.create("h264", "r")
    # One whole access unit: decode it as a packet (the parser would hold it
    # back waiting for the next one).
    frames = list(codec.decode(av.Packet(access_unit)))
    try:
        frames.extend(codec.decode(None))
    except av.error.EOFError:
        pass
    if not frames:
        return None
    frame = frames[-1]
    buffer = io.BytesIO()
    frame.to_image().save(buffer, format="JPEG", quality=85)
    return buffer.getvalue(), (frame.width, frame.height)


TOKEN_PARAM = "token"


def live_token(hass: HomeAssistant) -> str:
    """Secret for the stream URL handed to Home Assistant's own stream reader.

    A signed path (authSig) expires, but whatever opens the stream source -- the stream
    worker, go2rtc -- keeps the first URL and retries it forever. Past the expiry every
    retry was an invalid-auth request from 127.0.0.1, and five of them got Home Assistant
    to ban localhost (2026-10-17). This one lives as long as the process: new at every start.
    """
    data = hass.data.setdefault(DOMAIN + "_live", {})
    if "token" not in data:
        data["token"] = secrets.token_urlsafe(32)
    return data["token"]


def live_path(entry_id: str, uuid: str, lens: str | None = None) -> str:
    """Path of a camera's live stream (append ?token=live_token() before handing it out).

    With a lens, opening it switches the dashcam to that lens and serves only
    its frames; without, it follows the "Live camera" select.
    """
    path = f"{URL_BASE}/{entry_id}/{uuid}"
    return f"{path}/{lens}" if lens else path


def _library_candidates() -> list[str]:
    arch = platform.machine().lower()
    components = os.path.dirname(os.path.dirname(_HERE))
    return [
        os.path.join(os.path.dirname(_HERE), "libs", arch, LIB_NAME),
        # Already downloaded by the CuboAI integration on the same box.
        os.path.join(components, "cuboai", "libs", arch, LIB_NAME),
    ]


async def async_get_library(hass: HomeAssistant) -> str:
    """Path of the native TUTK library, downloading it on first use.

    On musl the producer adapts a copy itself (see ``elf.py``).
    """
    lock = hass.data.setdefault(f"{DOMAIN}_lib_lock", asyncio.Lock())
    async with lock:
        return await _async_library(hass)


async def _async_library(hass: HomeAssistant) -> str:
    candidates = _library_candidates()
    for path in candidates:
        if await hass.async_add_executor_job(os.path.exists, path):
            return path
    arch = platform.machine().lower()
    docker_arch = _DOCKER_ARCH.get(arch)
    if not docker_arch:
        raise LiveViewError(f"No TUTK library exists for this CPU ({arch})")
    dest = candidates[0]
    _LOGGER.info("Downloading the TUTK library (%s) for live view", docker_arch)
    try:
        blob = await _download_library(hass, docker_arch)
    except (aiohttp.ClientError, TimeoutError, KeyError, ValueError) as err:
        raise LiveViewError(f"Could not download the TUTK library: {err}") from err

    def _write() -> None:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest + ".tmp", "wb") as fh:
            fh.write(blob)
        os.replace(dest + ".tmp", dest)

    await hass.async_add_executor_job(_write)
    return dest


async def _download_library(hass: HomeAssistant, docker_arch: str) -> bytes:
    """Pull libIOTCAPIs_ALL.so out of the wyze-bridge image layers."""
    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=600)
    async with session.get(_DOCKER_AUTH, timeout=timeout) as resp:
        resp.raise_for_status()
        token = (await resp.json())["token"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": ", ".join((
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.oci.image.index.v1+json",
        )),
    }
    async with session.get(f"{_DOCKER_REGISTRY}/manifests/latest", headers=headers, timeout=timeout) as resp:
        resp.raise_for_status()
        manifest = await resp.json(content_type=None)
    if "manifests" in manifest:
        digest = next(
            (m["digest"] for m in manifest["manifests"] if m.get("platform", {}).get("architecture") == docker_arch),
            None,
        )
        if not digest:
            raise ValueError(f"no image for {docker_arch}")
        async with session.get(f"{_DOCKER_REGISTRY}/manifests/{digest}", headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            manifest = await resp.json(content_type=None)

    def _find(layer: bytes) -> bytes | None:
        with tarfile.open(fileobj=io.BytesIO(layer), mode="r:gz") as tar:
            for member in tar.getmembers():
                if os.path.basename(member.name) == LIB_NAME and member.isfile():
                    return tar.extractfile(member).read()
        return None

    for layer in reversed(manifest.get("layers", [])):
        async with session.get(f"{_DOCKER_REGISTRY}/blobs/{layer['digest']}", headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            blob = await resp.read()
        found = await hass.async_add_executor_job(_find, blob)
        if found:
            return found
    raise ValueError(f"{LIB_NAME} not found in the image")


class _Viewer:
    """One Home Assistant viewer: the lens it shows and its own TS muxer."""

    def __init__(self, lens: str | None) -> None:
        self.lens = lens
        self.muxer = TSMuxer(codec="h264")
        self.started = False


class LiveStream:
    """One producer process per camera, shared by every consumer.

    The camera has a single live encoder: ``switchCam`` changes the picture
    for every session (verified on a Z60 Pro: a second session asking for the
    rear lens turned the first one to rear too). So there is one stream per
    camera, the lens is switched on it in place, and its consumers share it:

    * viewers — Home Assistant's live camera (go2rtc / HLS), served MPEG-TS
      from the view below;
    * holders + sinks — the RTSP feeds for an NVR (``rtsp.py``), which keep the
      session open while they want live video and receive the raw frames.
    """

    def __init__(self, hass: HomeAssistant, coordinator: DdpaiCoordinator, uuid: str) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.uuid = uuid
        self._proc: asyncio.subprocess.Process | None = None
        self._viewers: dict[asyncio.Queue[bytes | None], _Viewer] = {}
        self._holders: set[str] = set()
        self._sinks: list[Callable[[bytes, bool, int, str], None]] = []
        self._lock = asyncio.Lock()
        self._idle_handle: asyncio.TimerHandle | None = None
        self._tasks: list[asyncio.Task] = []
        self.last_error: str | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._listeners: list[Callable[[], None]] = []
        # The lens the frames now arriving come from ("pending" until the
        # producer has named it), and the newest keyframe seen per lens.
        self.current_lens = "pending"
        self.last_keyframe: dict[str, bytes] = {}
        self.last_keyframe_time: dict[str, float] = {}
        self.last_frame_at = 0.0
        # Decoded newest picture per lens: (keyframe it came from, jpeg, epoch).
        self._snapshots: dict[str, tuple[bytes | None, bytes, float]] = {}

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener`` when the stream starts, ends or changes lens."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    @property
    def lens(self) -> str:
        """The lens asked for (the "Live camera" select)."""
        return self.coordinator.live_lens.get(self.uuid, LENSES[0])

    @property
    def viewers(self) -> int:
        """Home Assistant viewers (not the RTSP feeds)."""
        return len(self._viewers)

    async def async_set_lens(self, lens: str) -> None:
        """Show another lens; switches the running stream without reconnecting."""
        if lens not in LENSES:
            raise ValueError(lens)
        if lens == self.lens and self.current_lens == lens:
            return
        self.coordinator.live_lens[self.uuid] = lens
        proc = self._proc
        if proc and proc.returncode is None and proc.stdin:
            try:
                proc.stdin.write(f"{lens}\n".encode())
                await proc.stdin.drain()
            except (ConnectionError, OSError):
                pass
        self._notify()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # --- consumers ---------------------------------------------------------

    async def subscribe(self, lens: str | None = None) -> asyncio.Queue[bytes | None]:
        """A new viewer. With ``lens``, the dashcam is switched to it and the
        viewer only ever gets that lens's frames (it holds its picture while
        another viewer has the dashcam on the other lens)."""
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=QUEUE_CHUNKS)
        async with self._lock:
            self._cancel_idle()
            if lens is not None:
                await self.async_set_lens(lens)
            if not self.running:
                await self._start()
            self._viewers[queue] = _Viewer(lens)
        self._notify()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        self._viewers.pop(queue, None)
        self._maybe_idle()
        self._notify()

    async def async_hold(self, key: str) -> None:
        """Keep the session open for ``key`` (starting it if needed)."""
        async with self._lock:
            self._holders.add(key)
            self._cancel_idle()
            if not self.running:
                await self._start()

    def release(self, key: str) -> None:
        """``key`` no longer needs the session."""
        self._holders.discard(key)
        self._maybe_idle()

    def add_sink(self, sink: Callable[[bytes, bool, int, str], None]) -> None:
        """``sink(access_unit, keyframe, pts_90k, lens)`` for every frame."""
        self._sinks.append(sink)

    def remove_sink(self, sink: Callable[[bytes, bool, int, str], None]) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    async def async_restart(self) -> None:
        """Drop a session that delivers nothing; holders re-open it."""
        await self.async_stop()

    def _cancel_idle(self) -> None:
        if self._idle_handle:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _maybe_idle(self) -> None:
        if not self._viewers and not self._holders and self.running and not self._idle_handle:
            self._idle_handle = self.hass.loop.call_later(
                IDLE_STOP_SECONDS, lambda: self.hass.async_create_task(self.async_stop())
            )

    # --- the producer ------------------------------------------------------

    async def _start(self) -> None:
        options = self.coordinator.config_entry.options
        lib = await async_get_library(self.hass)
        try:
            creds = await self.coordinator.client.async_get_live_credentials(self.uuid)
        except DdpaiError as err:
            raise LiveViewError(f"Could not read the camera's live credentials: {err}") from err
        if not creds.get("uid") or not creds.get("password"):
            raise LiveViewError("The cloud returned no live credentials for this camera")
        broker: dict[str, object] = {}
        try:
            server = await self.coordinator.client.async_get_mqtt_server()
            broker = {
                "ip": server.get("ip"),
                "tcpPort": server.get("tcpPort") or 1883,
                "username": server.get("username"),
                "password": server.get("password"),
                # Stable per entry (the app uses the phone's IMEI here).
                "client_id": str(int(hashlib.sha256(
                    f"ddpai-ha-{self.coordinator.config_entry.entry_id}".encode()
                ).hexdigest(), 16))[:15],
            }
        except DdpaiError as err:
            _LOGGER.debug("No MQTT broker for the live wake-up: %s", err)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DDPAI_LIB": lib,
            "DDPAI_P2P_UID": creds["uid"],
            "DDPAI_P2P_PASSWORD": creds["password"],
            "DDPAI_AUTH_KEY": TUTK_AUTH_KEY,
            "DDPAI_LICENSE_KEY": options.get(CONF_TUTK_LICENSE_KEY, ""),
            "DDPAI_UUID": self.uuid,
            "DDPAI_LENS": self.lens,
            "DDPAI_MQTT": json.dumps(broker),
        }
        _LOGGER.debug("Starting live view of %s (%s)", self.uuid, self.lens)
        self._stderr.clear()
        self.last_error = None
        self.current_lens = "pending"
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-E", "-s", _PRODUCER,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=_HERE,
            limit=FRAME_LIMIT,
        )
        self._tasks = [
            self.hass.async_create_background_task(self._pump(self._proc), f"{DOMAIN} live {self.uuid}"),
            self.hass.async_create_background_task(self._log(self._proc), f"{DOMAIN} live log {self.uuid}"),
        ]
        self._notify()

    async def _pump(self, proc: asyncio.subprocess.Process) -> None:
        try:
            while True:
                header = await proc.stdout.readexactly(RECORD.size)
                length, flags, micros = RECORD.unpack(header)
                payload = await proc.stdout.readexactly(length) if length else b""
                if flags & FLAG_LENS:
                    self.current_lens = payload.decode(errors="replace")
                    self._notify()
                    continue
                keyframe = bool(flags & FLAG_KEYFRAME)
                pts = micros * 9 // 100
                lens = self.current_lens
                self.last_frame_at = time.monotonic()
                if keyframe and lens in LENSES:
                    self.last_keyframe[lens] = payload
                    self.last_keyframe_time[lens] = time.time()
                for sink in list(self._sinks):
                    try:
                        sink(payload, keyframe, pts, lens)
                    except Exception:  # noqa: BLE001 - one consumer never stops the rest
                        _LOGGER.exception("Live frame consumer failed")
                for queue, viewer in list(self._viewers.items()):
                    if viewer.lens is not None and lens != viewer.lens:
                        continue  # the other lens: this viewer keeps its picture
                    if not viewer.started:
                        if not keyframe:
                            continue  # a decoder can only start on a keyframe
                        viewer.started = True
                    data = viewer.muxer.mux_au(payload, pts, keyframe=keyframe, now_ms=micros // 1000)
                    try:
                        queue.put_nowait(data)
                    except asyncio.QueueFull:
                        # A viewer this far behind would only see a broken
                        # stream; drop it, it reconnects from a keyframe.
                        self._viewers.pop(queue, None)
                        _close(queue)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            code = await proc.wait()
            if code:
                self.last_error = self._stderr[-1] if self._stderr else f"exit code {code}"
                _LOGGER.warning("Live view of %s ended: %s", self.uuid, " | ".join(self._stderr) or code)
            for queue in list(self._viewers):
                _close(queue)
            self._viewers.clear()
            self.current_lens = "pending"
            self._notify()
            # Keep the last view of each lens for the "last view" pictures.
            for lens in list(self.last_keyframe):
                self.hass.async_create_task(self.async_snapshot(lens))

    def _snapshot_path(self, lens: str) -> str:
        return self.hass.config.path(".storage", DOMAIN, f"{self.uuid}_{lens}.jpg")

    async def async_snapshot(self, lens: str) -> tuple[bytes, float] | None:
        """The newest live picture of a lens as (JPEG, epoch), kept on disk."""
        keyframe = self.last_keyframe.get(lens)
        cached = self._snapshots.get(lens)
        if keyframe is not None and (cached is None or cached[0] is not keyframe):
            result = await self.hass.async_add_executor_job(keyframe_to_jpeg, keyframe)
            if result is not None:
                taken = self.last_keyframe_time.get(lens, time.time())
                self._snapshots[lens] = (keyframe, result[0], taken)
                path = self._snapshot_path(lens)

                def _save() -> None:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path + ".tmp", "wb") as fh:
                        fh.write(result[0])
                    os.replace(path + ".tmp", path)
                    os.utime(path, (taken, taken))

                await self.hass.async_add_executor_job(_save)
                cached = self._snapshots[lens]
        if cached is None:
            path = self._snapshot_path(lens)

            def _load() -> tuple[bytes, float] | None:
                try:
                    with open(path, "rb") as fh:
                        return fh.read(), os.path.getmtime(path)
                except OSError:
                    return None

            loaded = await self.hass.async_add_executor_job(_load)
            if loaded is None:
                return None
            self._snapshots[lens] = (None, loaded[0], loaded[1])
            cached = self._snapshots[lens]
        return cached[1], cached[2]

    async def _log(self, proc: asyncio.subprocess.Process) -> None:
        while line := await proc.stderr.readline():
            text = line.decode(errors="replace").rstrip()
            self._stderr.append(text)
            _LOGGER.debug("live %s: %s", self.uuid[-4:], text)

    async def async_stop(self) -> None:
        self._idle_handle = None
        proc, self._proc = self._proc, None
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        self._tasks = []
        self.current_lens = "pending"
        self._notify()


def _close(queue: asyncio.Queue[bytes | None]) -> None:
    """Tell a viewer the stream ended, discarding what it has not read."""
    while not queue.empty():
        queue.get_nowait()
    queue.put_nowait(None)


def get_stream(hass: HomeAssistant, coordinator: DdpaiCoordinator, uuid: str) -> LiveStream:
    """The shared live stream of one camera."""
    streams: dict[str, LiveStream] = coordinator.live_streams
    if uuid not in streams:
        streams[uuid] = LiveStream(hass, coordinator, uuid)
    return streams[uuid]


async def async_stop_all(coordinator: DdpaiCoordinator) -> None:
    """Stop every producer of one config entry."""
    for stream in list(coordinator.live_streams.values()):
        await stream.async_stop()
    coordinator.live_streams.clear()


class DdpaiLiveView(HomeAssistantView):
    """MPEG-TS live stream of one camera (the lens chosen by its select)."""

    url = URL_BASE + "/{entry_id}/{uuid}"
    extra_urls = [URL_BASE + "/{entry_id}/{uuid}/{lens}"]
    name = "api:ddpai:live"
    # Checked in get(): a logged-in user OR the process token. Not requires_auth, because
    # Home Assistant counts every refused request there toward an IP ban -- and the caller
    # holding a stale URL is Home Assistant itself, on 127.0.0.1.
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(
        self, request: web.Request, entry_id: str, uuid: str, lens: str | None = None
    ) -> web.StreamResponse:
        token = request.query.get(TOKEN_PARAM, "")
        if not request.get(KEY_AUTHENTICATED) and not secrets.compare_digest(token, live_token(self.hass)):
            return web.Response(status=401)
        coordinator = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is None or uuid not in (coordinator.data or {}):
            return web.Response(status=404)
        if lens is not None and lens not in LENSES:
            return web.Response(status=404)
        stream = get_stream(self.hass, coordinator, uuid)
        try:
            queue = await stream.subscribe(lens)
        except LiveViewError as err:
            _LOGGER.warning("Live view of %s not started: %s", uuid, err)
            return web.Response(status=503, text=str(err))

        response = web.StreamResponse()
        response.content_type = "video/mp2t"
        response.headers["Cache-Control"] = "no-store"
        try:
            await response.prepare(request)
            while (data := await queue.get()) is not None:
                await response.write(data)
        except ConnectionResetError:
            pass
        finally:
            stream.unsubscribe(queue)
        return response
