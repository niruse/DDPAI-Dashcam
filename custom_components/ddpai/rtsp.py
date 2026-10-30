"""RTSP output, so an NVR can record the dashcam (same design as Proof Plus).

    dashcam --TUTK P2P--> live.LiveStream (one session, shared)
      --H.264 access units--> DdpaiRtspFeed
      --MPEG-TS over http://127.0.0.1:<feed>/feed/<uuid>/<lens>--> go2rtc
      --rtsp://<home-assistant>:<port>/ddpai_<id>[_rear]--> NVR

go2rtc is the copy Home Assistant ships (/bin/go2rtc), run as a second instance
with its own configuration: Home Assistant's own instance only listens on
localhost, where an NVR cannot reach it. go2rtc pulls a feed only while an RTSP
client is connected, so with no NVR connected nothing here touches the dashcam.

A feed carries either the dashcam's video, passed through exactly as the
dashcam encoded it, or the standby picture (see standby.py). Which one depends
on the mode:

* always — live video whenever the feed is pulled;
* trigger — live video while a trigger is active: a device event of a chosen
  type, driving (plus a grace period), the "RTSP live" switch, or the
  ddpai.rtsp_trigger action. The standby picture the rest of the time.

The dashcam has one live encoder: switchCam changes the picture of every
session. So with both lenses on the stream each gets its own RTSP path and
they take turns while live, each showing its last picture between turns.
Somebody watching the live camera in Home Assistant keeps the lens they chose.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import tempfile
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api import DdpaiError
from .const import (
    ALBUM_PARKED,
    ALBUM_URGENT,
    CONF_RTSP_ALERT_MINUTES,
    CONF_RTSP_ALERTS,
    CONF_RTSP_ALTERNATE,
    CONF_RTSP_CAMERA,
    CONF_RTSP_DRIVING,
    CONF_RTSP_DRIVING_GRACE,
    CONF_RTSP_MODE,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_PORT,
    CONF_RTSP_USERNAME,
    DEFAULT_RTSP_ALERT_MINUTES,
    DEFAULT_RTSP_ALERTS,
    DEFAULT_RTSP_ALTERNATE,
    DEFAULT_RTSP_CAMERA,
    DEFAULT_RTSP_DRIVING,
    DEFAULT_RTSP_DRIVING_GRACE,
    DEFAULT_RTSP_MODE,
    DEFAULT_RTSP_PORT,
    DOMAIN,
    EVENT_DEVICE,
    EVENT_NEW_MEDIA,
    RTSP_MODE_ALWAYS,
    RTSP_MODE_OFF,
    RTSP_MODE_TRIGGER,
)
from .coordinator import media_kind
from .live import LENSES, LiveViewError, get_stream, keyframe_to_jpeg
from .live.mpegts import TSMuxer
from .standby import DEFAULT_SIZE, STANDBY_FPS, encode_standby, is_keyframe, render_standby, sps_profile

if TYPE_CHECKING:
    from .coordinator import DdpaiCoordinator

_LOGGER = logging.getLogger(__name__)

# No live frame for this long while live means the session has stalled.
STALL_SECONDS = 8
# A session on our lens that has not produced a keyframe by then is restarted.
FIRST_KEYFRAME_SECONDS = 45
# Live timestamps follow the producer's frame-to-frame steps but never stray
# from the wall clock by more than this (90 kHz ticks).
MAX_FRAME_STEP = 90000
MAX_LEAD = 90000
MAX_LAG = 3 * 90000
# Waits between attempts to reach a dashcam that would not answer.
RETRY_BACKOFF = (15, 30, 60, 120, 300)
# How often to re-ask the dashcam for our lens while it shows the other one.
RECLAIM_SECONDS = 3
# Frames queued for the feed before newer ones are dropped.
LIVE_QUEUE = 300
# Chunks queued for one reader (go2rtc) before it is cut off.
READER_QUEUE = 600

LIVE_SIZES = {"front": (1280, 720), "rear": (848, 480)}

GO2RTC_BINARY = "/bin/go2rtc"
GO2RTC_RESTART_SECONDS = 10
GO2RTC_API_OFFSET = 10000


class RtspError(Exception):
    """The RTSP server could not be started."""


def _port_free(port: int) -> bool:
    """Whether a TCP port can be listened on, on all interfaces."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _build_standby(
    still: bytes | None, size: tuple[int, int] | None, caption: str, profile: str, level: str
) -> list[bytes]:
    """Render and encode a standby picture (runs in an executor).

    Always at the live stream's size (from its last keyframe, or the DDPAI
    live size): the parked photos are 4K, and an NVR must not see the
    resolution change when live video takes over.
    """
    width, height = size or DEFAULT_SIZE
    size = (width - width % 2, height - height % 2)
    picture = render_standby(still, size, caption)
    return encode_standby(picture, size, profile, level)


class DdpaiRtspFeed:
    """The stream go2rtc pulls for one lens of one dashcam."""

    def __init__(self, rtsp: DdpaiRtsp, uuid: str, lens: str) -> None:
        self._rtsp = rtsp
        self._hass = rtsp.hass
        self._uuid = uuid
        self.lens = lens
        self._key = f"rtsp-{lens}"
        self._readers: set[asyncio.Queue[bytes | None]] = set()
        self._task: asyncio.Task | None = None
        # True while live video (not the standby picture) is going out.
        self.live = False
        self.last_error: str | None = None
        # What the standby picture is built from, and the build.
        self._picture: bytes | None = None
        # The live size of this lens (measured on a Z60 Pro): the standby
        # picture must match it, or the NVR sees the resolution change.
        self._size: tuple[int, int] | None = LIVE_SIZES.get(lens)
        self._profile: tuple[str, str] = ("high", "3.1")
        self._standby: tuple[tuple, list[bytes]] | None = None
        self._building: asyncio.Task | None = None

    @property
    def readers(self) -> int:
        """How many readers (go2rtc connections) are pulling the feed."""
        return len(self._readers)

    async def async_serve(self, writer: asyncio.StreamWriter) -> None:
        """Stream to one reader until it goes away."""
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=READER_QUEUE)
        self._readers.add(queue)
        if self._task is None or self._task.done():
            self._task = self._hass.async_create_background_task(
                self._produce(), f"{DOMAIN} rtsp feed {self._uuid} {self.lens}"
            )
        self._rtsp.async_notify()
        try:
            while (chunk := await queue.get()) is not None:
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self._readers.discard(queue)
            self._rtsp.async_notify()

    def _send(self, data: bytes) -> None:
        for queue in list(self._readers):
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                # A reader this far behind is stuck; cut it off and let go2rtc
                # reconnect rather than hold everything back.
                self._readers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)

    async def async_stop(self) -> None:
        """End the feed for every reader."""
        for queue in list(self._readers):
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)
        self._readers.clear()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._building is not None:
            self._building.cancel()

    # --- standby picture ------------------------------------------------------

    def _still(self) -> bytes | None:
        return self._picture or self._rtsp.cached_still(self._uuid, self.lens)

    def _standby_signature(self) -> tuple:
        still = self._still()
        return (
            id(still) if still else None,
            self._size,
            self._profile,
            self._rtsp.standby_caption(self._uuid, self.lens),
        )

    def _start_standby_build(self) -> asyncio.Task:
        signature = self._standby_signature()
        still = self._still()
        profile, level = self._profile

        async def _build() -> list[bytes]:
            units = await self._hass.async_add_executor_job(
                _build_standby, still, self._size, signature[3], profile, level
            )
            self._standby = (signature, units)
            return units

        self._building = self._hass.async_create_background_task(
            _build(), f"{DOMAIN} rtsp standby {self._uuid} {self.lens}"
        )
        return self._building

    async def _standby_units(self, wait: bool) -> list[bytes] | None:
        """The standby picture's access units, rebuilding when it has changed.

        With ``wait`` False a rebuild runs in the background and the previous
        picture is returned meanwhile, so the stream never pauses for it.
        """
        current = self._standby
        if current is not None and current[0] == self._standby_signature():
            return current[1]
        task = self._building
        if task is None or task.done():
            task = self._start_standby_build()
        if wait or current is None:
            try:
                return await task
            except Exception as err:  # noqa: BLE001
                self.last_error = f"Could not build the standby picture: {err}"
                _LOGGER.error(self.last_error)
                raise
        return current[1]

    async def _remember_view(self, keyframe: bytes | None) -> None:
        """Make the last live picture (our lens' newest keyframe) the next standby picture."""
        if keyframe is None:
            return
        try:
            result = await self._hass.async_add_executor_job(keyframe_to_jpeg, keyframe)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not keep the last live picture", exc_info=True)
            return
        if result is not None:
            self._picture, self._size = result

    # --- the stream -----------------------------------------------------------

    async def _produce(self) -> None:
        """Send live video or the standby picture for as long as anyone reads."""
        hass = self._hass
        loop = hass.loop
        uuid = self._uuid
        lens = self.lens
        stream = get_stream(hass, self._rtsp.coordinator, uuid)
        muxer = TSMuxer(codec="h264")
        start = loop.time()
        frames: asyncio.Queue[tuple[bytes, bool, int]] = asyncio.Queue(maxsize=LIVE_QUEUE)
        last_keyframe: bytes | None = None

        def sink(data: bytes, keyframe: bool, pts: int, frame_lens: str) -> None:
            # Called on the event loop for every frame; the other lens' stay out.
            if frame_lens != lens:
                return
            try:
                frames.put_nowait((data, keyframe, pts))
            except asyncio.QueueFull:
                pass

        last_pts = -1

        def emit(unit: bytes, keyframe: bool, pts: int) -> None:
            nonlocal last_pts
            pts = max(pts, last_pts + 1)
            last_pts = pts
            self._send(muxer.mux_au(unit, pts, keyframe=keyframe, now_ms=pts // 90))

        def clock() -> int:
            return int((loop.time() - start) * 90000)

        # The producer's timestamps are only trusted frame to frame: they
        # restart with every session, and a stalled link bunches frames up.
        prev_ts: int | None = None
        prev_pts = 0
        prev_arrival = 0

        def live_pts(timestamp: int) -> int:
            nonlocal prev_ts, prev_pts, prev_arrival
            arrival = clock()
            if prev_ts is None:
                pts = arrival
            else:
                step = timestamp - prev_ts
                if not 0 < step <= MAX_FRAME_STEP:
                    step = min(max(arrival - prev_arrival, 1), MAX_FRAME_STEP)
                pts = prev_pts + step
                if not arrival - MAX_LAG <= pts <= arrival + MAX_LEAD:
                    pts = arrival
            prev_ts, prev_pts, prev_arrival = timestamp, pts, arrival
            return pts

        units: list[bytes] = []
        holding = False
        attached = False
        acquiring: asyncio.Future | None = None
        attached_at = 0.0
        retry_at = 0.0
        failures = 0
        waiting_key = True
        last_frame_at = 0.0
        last_reclaim = 0.0
        had_turn = False
        tick = 0
        next_tick = loop.time()

        def back_off() -> None:
            nonlocal retry_at, failures
            retry_at = loop.time() + RETRY_BACKOFF[min(failures, len(RETRY_BACKOFF) - 1)]
            failures += 1

        async def to_standby() -> None:
            """Back to the standby picture, starting on its keyframe."""
            nonlocal waiting_key, tick, next_tick, units
            _LOGGER.debug("RTSP %s %s: standby picture", uuid, lens)
            self.live = False
            self._rtsp.async_notify()
            waiting_key = True
            await self._remember_view(last_keyframe)
            # Wait for the new picture: on a turn change it is shown for only
            # a few seconds, so the previous one must not stand in for it.
            units = await self._standby_units(wait=True) or units
            tick = 0
            next_tick = loop.time()

        async def restart(reason: str) -> None:
            """Drop a session that delivers nothing; try again after a pause."""
            _LOGGER.debug("RTSP %s %s: %s", uuid, lens, reason)
            self.last_error = reason
            await stream.async_restart()
            back_off()

        async def hold() -> bool:
            try:
                await stream.async_hold(self._key)
            except (LiveViewError, DdpaiError) as err:
                self.last_error = str(err)
                return False
            return True

        last_kick = self._rtsp.kicked_at(uuid)
        try:
            units = await self._standby_units(wait=True) or []
            while self._readers:
                now = loop.time()
                if (kick := self._rtsp.kicked_at(uuid)) != last_kick:
                    # The dashcam is back after a drop: reconnect now.
                    last_kick = kick
                    retry_at, failures = 0.0, 0
                want = self._rtsp.want_live(uuid)
                # With both lenses on the stream they take turns; the lens
                # somebody is watching in Home Assistant keeps the turn.
                my_turn = want and self._rtsp.active_lens(uuid) == lens
                if my_turn and not had_turn:
                    attached_at = now
                    last_reclaim = 0.0
                had_turn = my_turn

                # Hold the dashcam session while live video is wanted, and
                # (re)open it when there is none — at first, after a failure,
                # or after a restart.
                if want and not attached:
                    stream.add_sink(sink)
                    attached = True
                if not want and attached and acquiring is None:
                    if self.live:
                        await to_standby()
                    stream.remove_sink(sink)
                    attached = False
                    if holding:
                        stream.release(self._key)
                        holding = False
                # Hold the session even when someone else (a viewer) already
                # opened it: otherwise it stops when they leave.
                if (
                    want
                    and my_turn
                    and acquiring is None
                    and (not holding or not stream.running)
                    and now >= retry_at
                ):
                    acquiring = asyncio.ensure_future(hold())
                    attached_at = now
                # The session we held ended without delivering (camera offline,
                # asleep, unreachable): wait before the next attempt.
                if holding and acquiring is None and not stream.running and stream.last_error:
                    holding = False
                    self.last_error = stream.last_error.replace("ERROR ", "")
                    back_off()
                if acquiring is not None and acquiring.done():
                    ok = (
                        not acquiring.cancelled()
                        and acquiring.exception() is None
                        and acquiring.result()
                    )
                    acquiring = None
                    holding = holding or bool(ok)
                    _LOGGER.debug("RTSP %s %s: session %s", uuid, lens, "up" if ok else "failed")
                    if not ok:
                        self.last_error = self.last_error or "Could not reach the dashcam"
                        back_off()
                    waiting_key = True
                    while not frames.empty():
                        frames.get_nowait()

                # On our turn, put the dashcam on our lens — unless somebody is
                # watching the other one in Home Assistant.
                if (
                    my_turn
                    and stream.running
                    and stream.current_lens != lens
                    and stream.viewers == 0
                    and now - last_reclaim > RECLAIM_SECONDS
                ):
                    last_reclaim = now
                    attached_at = now
                    await stream.async_set_lens(lens)

                on_our_lens = stream.running and stream.current_lens == lens

                # Next live frame, or time for the next standby frame.
                wait = 0.25 if self.live else max(0.0, min(0.25, next_tick - now))
                try:
                    data, keyframe, timestamp = await asyncio.wait_for(frames.get(), wait)
                except TimeoutError:
                    data = None
                now = loop.time()

                if data is not None and my_turn and stream.running:
                    if keyframe:
                        last_keyframe = data
                    if waiting_key:
                        if not keyframe:
                            continue
                        waiting_key = False
                        prev_ts = None
                        if (profile := sps_profile(data)) is not None:
                            self._profile = profile
                        if not self.live:
                            _LOGGER.debug("RTSP %s %s: live video", uuid, lens)
                            self.live = True
                            self.last_error = None
                            failures = 0
                            self._rtsp.async_notify()
                    emit(data, keyframe, live_pts(timestamp))
                    last_frame_at = now
                    continue

                if self.live and not my_turn:
                    # The other lens' turn (or no longer wanted).
                    await to_standby()
                elif self.live and now - last_frame_at > STALL_SECONDS:
                    await to_standby()
                    if on_our_lens:
                        # Our lens, yet nothing for a while: the session is dead.
                        await restart("The live video stalled; reconnecting")
                elif my_turn and not self.live and on_our_lens and now - attached_at > FIRST_KEYFRAME_SECONDS:
                    attached_at = now
                    await restart("The dashcam sent no picture; reconnecting")

                if not self.live and now >= next_tick and units:
                    index = tick % len(units)
                    if index == 0:
                        # Only swap pictures at a keyframe.
                        units = await self._standby_units(wait=False) or units
                    emit(units[index], index == 0, clock())
                    tick += 1
                    next_tick += 1 / STANDBY_FPS
                    if next_tick < now:
                        next_tick = now + 1 / STANDBY_FPS
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self.last_error = str(err)
            _LOGGER.exception("RTSP feed for %s %s failed", uuid, lens)
        finally:
            if acquiring is not None:
                acquiring.cancel()
            stream.remove_sink(sink)
            stream.release(self._key)
            self.live = False
            for queue in list(self._readers):
                queue.put_nowait(None)
            self._readers.clear()
            self._rtsp.async_notify()


class DdpaiGo2rtc:
    """A go2rtc process of our own, restarted if it dies."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._config = Path(tempfile.gettempdir()) / "ddpai_go2rtc" / f"{entry_id}.yaml"
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.output: deque[str] = deque(maxlen=40)
        self.error: str | None = None

    @property
    def running(self) -> bool:
        """Whether the process is up."""
        return self._process is not None and self._process.returncode is None

    def _kill_strays(self) -> int:
        """End a go2rtc of ours left behind by an earlier run."""
        marker = str(self._config).encode()
        proc = Path("/proc")
        killed = 0
        if not proc.is_dir():
            return killed
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if marker in cmdline and b"go2rtc" in cmdline:
                try:
                    os.kill(int(entry.name), signal.SIGTERM)
                    killed += 1
                except OSError:
                    pass
        return killed

    async def async_kill_strays(self) -> None:
        """End a go2rtc of ours left behind, so its port is free again."""
        if await self._hass.async_add_executor_job(self._kill_strays):
            await asyncio.sleep(1)

    async def async_start(self, config: dict[str, Any]) -> None:
        """Write the configuration and start the process."""
        binary = shutil.which("go2rtc") or (GO2RTC_BINARY if os.path.exists(GO2RTC_BINARY) else None)
        if binary is None:
            raise RtspError("go2rtc was not found (Home Assistant ships it from 2024.11)")

        def _prepare() -> None:
            self._config.parent.mkdir(parents=True, exist_ok=True)
            # YAML is a superset of JSON, so go2rtc reads this as it is.
            self._config.write_text(json.dumps(config, indent=1), encoding="utf-8")
            os.chmod(self._config, 0o600)
            self._kill_strays()

        await self._hass.async_add_executor_job(_prepare)
        self._stopping = False
        self.error = None
        self._task = self._hass.async_create_background_task(self._run(binary), f"{DOMAIN} go2rtc")

    async def _run(self, binary: str) -> None:
        delay = GO2RTC_RESTART_SECONDS
        while not self._stopping:
            started = time.monotonic()
            try:
                self._process = await asyncio.create_subprocess_exec(
                    binary, "-c", str(self._config),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except OSError as err:
                self.error = f"Could not start go2rtc: {err}"
                _LOGGER.error(self.error)
                return
            assert self._process.stdout is not None
            async for raw in self._process.stdout:
                line = raw.decode(errors="replace").rstrip()
                self.output.append(line)
                if " ERR " in line or " FTL " in line:
                    self.error = line
                    _LOGGER.warning("go2rtc: %s", line)
            code = await self._process.wait()
            if self._stopping:
                return
            self.error = f"go2rtc exited with code {code}"
            _LOGGER.warning("%s; restarting in %s s", self.error, delay)
            if time.monotonic() - started > 60:
                delay = GO2RTC_RESTART_SECONDS
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    async def async_stop(self) -> None:
        """Stop the process."""
        self._stopping = True
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._process = None


class DdpaiRtsp:
    """The RTSP output for one account: mode, triggers, feeds and go2rtc."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: DdpaiCoordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        options = entry.options
        self.mode: str = options.get(CONF_RTSP_MODE, DEFAULT_RTSP_MODE)
        self.port: int = int(options.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT))
        self._camera_mode: str = options.get(CONF_RTSP_CAMERA, DEFAULT_RTSP_CAMERA)
        self._alternate = int(options.get(CONF_RTSP_ALTERNATE, DEFAULT_RTSP_ALTERNATE))
        # Per dashcam with both lenses on: whose turn it is, and since when.
        self._turns: dict[str, tuple[str, float]] = {}
        self._username: str = options.get(CONF_RTSP_USERNAME) or ""
        self._password: str = options.get(CONF_RTSP_PASSWORD) or ""
        self._alerts: set[str] = set(options.get(CONF_RTSP_ALERTS, DEFAULT_RTSP_ALERTS))
        self._alert_seconds = 60 * int(options.get(CONF_RTSP_ALERT_MINUTES, DEFAULT_RTSP_ALERT_MINUTES))
        self._driving: bool = options.get(CONF_RTSP_DRIVING, DEFAULT_RTSP_DRIVING)
        self._grace_seconds = 60 * int(options.get(CONF_RTSP_DRIVING_GRACE, DEFAULT_RTSP_DRIVING_GRACE))
        # Per dashcam: why live video is wanted, and until when (None = until
        # that reason ends by itself, e.g. driving stopping).
        self._windows: dict[str, dict[str, float | None]] = {}
        self._driving_state: dict[str, bool] = {}
        # When each dashcam came back after a drop: the feeds retry at once then
        # instead of sitting out their back-off (up to 5 min).
        self._kicks: dict[str, float] = {}
        self._feeds: dict[tuple[str, str], DdpaiRtspFeed] = {}
        self._stills: dict[tuple[str, str], tuple[str, bytes]] = {}
        # Newest parked photo time, and the last live view (JPEG, epoch) per lens.
        self._parked_at: dict[tuple[str, str], float] = {}
        self._live_stills: dict[tuple[str, str], tuple[bytes, float]] = {}
        self._server: asyncio.Server | None = None
        self._go2rtc = DdpaiGo2rtc(hass, entry.entry_id)
        # go2rtc proves itself to the feed with this; the feed only listens on
        # localhost, but other add-ons share that.
        self._key = secrets.token_urlsafe(18)
        self._unsubs: list[Any] = []
        self._expiry: Any = None
        self.host: str | None = None
        self.error: str | None = None
        self.running = False

    # --- lifecycle --------------------------------------------------------------

    async def async_setup(self) -> None:
        """Start listening for triggers, and the server unless RTSP is off."""
        self._unsubs.append(self.hass.bus.async_listen(EVENT_DEVICE, self._on_device_event))
        self._unsubs.append(self.hass.bus.async_listen(EVENT_NEW_MEDIA, self._on_new_media))
        self._unsubs.append(self.coordinator.async_add_listener(self._on_update))
        self._on_update()
        try:
            from homeassistant.components import network

            self.host = await network.async_get_source_ip(self.hass)
        except Exception:  # noqa: BLE001
            self.host = None
        if self.mode != RTSP_MODE_OFF:
            await self._async_start_server()

    async def async_shutdown(self) -> None:
        """Stop everything."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._expiry is not None:
            self._expiry()
            self._expiry = None
        await self._async_stop_server()

    async def async_set_mode(self, mode: str) -> None:
        """Switch mode on the fly, and remember it in the entry's options."""
        if mode == self.mode:
            return
        self.mode = mode
        if self.entry.options.get(CONF_RTSP_MODE) != mode:
            # The update listener sees an option that is applied on the fly
            # and does not reload the entry for it.
            self.hass.config_entries.async_update_entry(self.entry, options={**self.entry.options, CONF_RTSP_MODE: mode})
        if mode == RTSP_MODE_OFF:
            await self._async_stop_server()
        else:
            await self._async_start_server()
        self.async_notify()

    async def _async_start_server(self) -> None:
        if self.running:
            return
        try:
            await self._go2rtc.async_kill_strays()
            if not await self.hass.async_add_executor_job(_port_free, self.port):
                raise RtspError(f"Port {self.port} is already in use")
            api_port = self.port + GO2RTC_API_OFFSET
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            feed_port = self._server.sockets[0].getsockname()[1]
            rtsp: dict[str, Any] = {"listen": f":{self.port}", "default_query": "video"}
            if self._username:
                rtsp.update(username=self._username, password=self._password)
            config = {
                "api": {"listen": f"127.0.0.1:{api_port}"},
                "rtsp": rtsp,
                "webrtc": {"listen": ""},
                "srtp": {"listen": ""},
                "rtmp": {"listen": ""},
                "log": {"format": "text", "level": "info"},
                "streams": {
                    self.stream_name(uuid, lens): (
                        f"http://127.0.0.1:{feed_port}/feed/{uuid}/{lens}?key={self._key}"
                    )
                    for uuid in self.coordinator.data
                    for lens in self.lenses(uuid)
                },
            }
            await self._go2rtc.async_start(config)
        except (RtspError, OSError) as err:
            self.error = str(err)
            _LOGGER.error("RTSP output not started: %s", err)
            if self._server is not None:
                self._server.close()
                self._server = None
            self.async_notify()
            return
        self.error = None
        self.running = True
        self.async_notify()

    async def _async_stop_server(self) -> None:
        for feed in self._feeds.values():
            await feed.async_stop()
        self._feeds.clear()
        await self._go2rtc.async_stop()
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5)
            except TimeoutError:
                pass
            self._server = None
        self.running = False

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one feed request from go2rtc."""
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            parts = request.split(b"\r\n", 1)[0].decode(errors="replace").split(" ")
            target = urlsplit(parts[1] if len(parts) > 1 else "")
            segments = target.path.strip("/").split("/")
            key = (parse_qs(target.query).get("key") or [""])[0]
            uuid = lens = None
            if len(segments) == 3 and segments[0] == "feed":
                uuid, lens = segments[1], segments[2]
            if (
                uuid not in (self.coordinator.data or {})
                or lens not in self.lenses(uuid)
                or not secrets.compare_digest(key, self._key)
            ):
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            await self.feed(uuid, lens).async_serve(writer)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    # --- what the stream shows --------------------------------------------------

    def feed(self, uuid: str, lens: str) -> DdpaiRtspFeed:
        """The feed for one lens of a dashcam, created on first use."""
        if (uuid, lens) not in self._feeds:
            self._feeds[(uuid, lens)] = DdpaiRtspFeed(self, uuid, lens)
        return self._feeds[(uuid, lens)]

    @property
    def go2rtc_log(self) -> list[str]:
        """The last lines go2rtc printed (for diagnostics)."""
        return list(self._go2rtc.output)

    def _feeds_of(self, uuid: str) -> list[DdpaiRtspFeed]:
        return [feed for lens in self.lenses(uuid) if (feed := self._feeds.get((uuid, lens))) is not None]

    def is_live(self, uuid: str) -> bool:
        """Whether live video (not the standby picture) is going out."""
        return any(feed.live for feed in self._feeds_of(uuid))

    def lenses(self, uuid: str) -> list[str]:
        """The lenses that get a stream."""
        return {"front": ["front"], "rear": ["rear"]}.get(self._camera_mode, list(LENSES))

    def stream_name(self, uuid: str, lens: str) -> str:
        """The RTSP path an NVR asks for.

        The first stream keeps the plain name, so switching between one lens
        and both does not move the NVR channel already set up.
        """
        base = f"ddpai_{uuid.replace('-', '')[-12:]}"
        return base if lens == self.lenses(uuid)[0] else f"{base}_rear"

    def active_lens(self, uuid: str) -> str | None:
        """Which lens's stream gets the live video right now.

        The lens somebody is watching in Home Assistant keeps it. Otherwise,
        with both streams being pulled, the lenses take turns.
        """
        pulled = [feed.lens for feed in self._feeds_of(uuid) if feed.readers]
        if len(pulled) <= 1:
            return pulled[0] if pulled else None
        stream = get_stream(self.hass, self.coordinator, uuid)
        if stream.viewers and stream.lens in pulled:
            return stream.lens
        now = time.monotonic()
        lens, since = self._turns.get(uuid, (pulled[0], now))
        if now - since >= self._alternate:
            lens, since = pulled[(pulled.index(lens) + 1) % len(pulled)], now
        self._turns[uuid] = (lens, since)
        return lens

    def cached_still(self, uuid: str, lens: str) -> bytes | None:
        """The newest picture of that lens: the last live view (kept on disk by
        the live stream) or the newest parked photo, whichever is newer."""
        key = (uuid, lens)
        parked = self._stills.get(key)
        live = self._live_stills.get(key)
        if live and (not parked or not parked[1] or live[1] >= self._parked_at.get(key, 0)):
            return live[0]
        return parked[1] if parked and parked[1] else None

    def standby_caption(self, uuid: str, lens: str) -> str:
        """The line written on a lens's standby picture."""
        if self.want_live(uuid) and len(self.lenses(uuid)) > 1:
            if self.active_lens(uuid) not in (lens, None):
                other = "rear" if lens == "front" else "front"
                return f"Paused - the dashcam is showing the {other} camera"
        if self.mode == RTSP_MODE_ALWAYS:
            return "Waiting for the dashcam - reconnecting"
        return "Standby - live video starts when triggered"

    def want_live(self, uuid: str) -> bool:
        """Whether the stream should be carrying live video right now."""
        if self.mode == RTSP_MODE_ALWAYS:
            return True
        if self.mode != RTSP_MODE_TRIGGER:
            return False
        return bool(self.reasons(uuid))

    def reasons(self, uuid: str) -> dict[str, float | None]:
        """The triggers currently active for a dashcam, pruning expired ones."""
        windows = self._windows.get(uuid) or {}
        now = time.time()
        for reason in [r for r, until in windows.items() if until is not None and until <= now]:
            del windows[reason]
        return dict(windows)

    def live_until(self, uuid: str) -> datetime | None:
        """When the triggers run out, if they all have an end."""
        reasons = self.reasons(uuid)
        if not reasons or any(until is None for until in reasons.values()):
            return None
        return dt_util.utc_from_timestamp(max(reasons.values()))  # type: ignore[type-var]

    def stream_url(self, uuid: str, lens: str | None = None) -> str:
        """The URL to give the NVR for a lens (the first stream by default)."""
        if lens is None:
            lens = self.lenses(uuid)[0]
        host = self.host or "<home-assistant-ip>"
        return f"rtsp://{host}:{self.port}/{self.stream_name(uuid, lens)}"

    # --- triggers ---------------------------------------------------------------

    def manual(self, uuid: str) -> bool:
        """Whether the RTSP live switch holds live video on."""
        return "manual" in (self._windows.get(uuid) or {})

    @callback
    def set_manual(self, uuid: str, on: bool) -> None:
        """The RTSP live switch."""
        windows = self._windows.setdefault(uuid, {})
        if on:
            windows["manual"] = None
        else:
            windows.pop("manual", None)
        self._changed()

    @callback
    def trigger(self, uuid: str, minutes: float | None = None) -> None:
        """Live video for a while (the ddpai.rtsp_trigger action)."""
        seconds = 60 * minutes if minutes else self._alert_seconds
        self._extend(uuid, "action", seconds)

    @callback
    def stop(self, uuid: str) -> None:
        """End every trigger now (the next driving start can start one)."""
        self._windows.pop(uuid, None)
        self._changed()

    @callback
    def _extend(self, uuid: str, reason: str, seconds: float) -> None:
        windows = self._windows.setdefault(uuid, {})
        until = time.time() + seconds
        current = windows.get(reason, 0.0)
        if reason in windows and current is None:
            return
        windows[reason] = max(until, current or 0.0)
        self._changed()

    def _recent(self, millis: Any) -> bool:
        """An event time (ms) young enough to still be worth streaming for."""
        try:
            age = time.time() - int(millis) / 1000
        except (TypeError, ValueError):
            return True
        return age <= self._alert_seconds

    @callback
    def _on_device_event(self, event: Event) -> None:
        """A device event arrived from the cloud (power, collision, ...)."""
        uuid = event.data.get("device_uuid")
        if uuid not in (self.coordinator.data or {}):
            return
        if event.data.get("event") in self._alerts and self._recent(event.data.get("time")):
            self._extend(uuid, "alert", self._alert_seconds)

    @callback
    def _on_new_media(self, event: Event) -> None:
        """A new urgent clip reached the cloud album."""
        uuid = event.data.get("device_uuid")
        if uuid not in (self.coordinator.data or {}) or "urgent_file" not in self._alerts:
            return
        if event.data.get("album") == ALBUM_URGENT:
            self._extend(uuid, "alert", self._alert_seconds)

    def kicked_at(self, uuid: str) -> float:
        return self._kicks.get(uuid, 0.0)

    @callback
    def _on_update(self) -> None:
        """Follow driving as polled, and keep the standby stills current.

        Auto-renew through drops: the dashcam's 4G link drops for a minute or two
        mid-drive and the cloud then says "offline" while the car keeps moving
        (seen 2026-10-21). While its position still changes the drive goes on,
        so live video stays wanted; and the moment it is back online the feeds
        reconnect instead of waiting out their back-off.
        """
        for uuid, cam in (self.coordinator.data or {}).items():
            status = (cam.get("status") or {}).get("status")
            if status is not None:
                was = self._driving_state.get(uuid)
                self._set_driving(uuid, status == 1)
                if status == 1 and was is False:
                    self._kicks[uuid] = time.monotonic()
                if status != 1:
                    self._keep_while_moving(uuid)
            if self.mode != RTSP_MODE_OFF:
                self._refresh_stills(uuid, cam)

    @callback
    def _refresh_stills(self, uuid: str, cam: dict[str, Any]) -> None:
        items = (cam.get("album") or {}).get(ALBUM_PARKED) or []
        stream = get_stream(self.hass, self.coordinator, uuid)
        for lens in LENSES:
            key = (uuid, lens)
            live = self._live_stills.get(key)
            if live is None or stream.last_keyframe_time.get(lens, 0) > live[1]:

                async def _load(key=key, lens=lens) -> None:
                    snap = await stream.async_snapshot(lens)
                    if snap is not None:
                        self._live_stills[key] = snap

                self.hass.async_create_task(_load())
            mine = [i for i in items if media_kind(i) == (lens, "image")]
            item = max(mine, key=lambda i: i.get("commitDate") or 0, default=None)
            url = ((item or {}).get("content") or {}).get("url")
            if not url:
                continue
            item_id = str(item.get("id"))
            self._parked_at[(uuid, lens)] = (item.get("commitDate") or 0) / 1000
            cached = self._stills.get((uuid, lens))
            if cached and cached[0] == item_id:
                continue
            self._stills[(uuid, lens)] = (item_id, cached[1] if cached else b"")

            async def _fetch(key=(uuid, lens), item_id=item_id, url=url) -> None:
                try:
                    data = await self.coordinator.client.async_fetch_media(url)
                except DdpaiError as err:
                    _LOGGER.debug("Standby still not fetched: %s", err)
                    return
                self._stills[key] = (item_id, data)

            self.hass.async_create_task(_fetch())

    @callback
    def _keep_while_moving(self, uuid: str) -> None:
        """Hold the driving window open while the car is still moving."""
        if not self._driving:
            return
        moved = self.coordinator.position_changed_at(uuid)
        if not moved or time.time() - moved > self._grace_seconds:
            return
        windows = self._windows.setdefault(uuid, {})
        until = moved + self._grace_seconds
        current = windows.get("driving", 0.0)
        if current is not None and until > (current or 0.0):
            windows["driving"] = until
            self._changed()

    @callback
    def _set_driving(self, uuid: str, on: bool) -> None:
        before = self._driving_state.get(uuid)
        self._driving_state[uuid] = on
        if not self._driving or before == on:
            return
        windows = self._windows.setdefault(uuid, {})
        if on:
            windows["driving"] = None
        elif before:
            windows["driving"] = time.time() + self._grace_seconds
        else:
            return
        self._changed()

    @callback
    def _changed(self) -> None:
        """Re-arm the expiry timer and refresh the entities."""
        if self._expiry is not None:
            self._expiry()
            self._expiry = None
        ends = [until for windows in self._windows.values() for until in windows.values() if until is not None]
        if ends:
            delay = max(0.0, min(ends) - time.time()) + 0.5

            @callback
            def _expired(_now: Any) -> None:
                self._expiry = None
                self._changed()

            self._expiry = async_call_later(self.hass, delay, _expired)
        self.async_notify()

    @callback
    def async_notify(self) -> None:
        """Let the entities show the new state."""
        self.coordinator.async_update_listeners()

    def state(self, uuid: str) -> dict[str, Any]:
        """Everything an entity shows about the stream."""
        feeds = self._feeds_of(uuid)
        lenses = self.lenses(uuid)
        until = self.live_until(uuid)
        enabled = self.mode != RTSP_MODE_OFF
        live = next((feed for feed in feeds if feed.live), None)
        return {
            "mode": self.mode,
            "url": self.stream_url(uuid, lenses[0]) if enabled else None,
            "rear_url": self.stream_url(uuid, "rear") if enabled and len(lenses) > 1 else None,
            "cameras": lenses,
            "live_camera": live.lens if live else None,
            "live_wanted": self.want_live(uuid),
            "triggers": sorted(self.reasons(uuid)) if self.mode == RTSP_MODE_TRIGGER else [],
            "live_until": until.isoformat() if until else None,
            "nvr_connected": any(feed.readers for feed in feeds),
            "error": self.error
            or self._go2rtc.error
            or next((feed.last_error for feed in feeds if feed.last_error), None),
        }
