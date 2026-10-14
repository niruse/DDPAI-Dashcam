#!/usr/bin/env python3
"""DDPAI 4G live view producer: one camera lens -> MPEG-TS on stdout.

Home Assistant runs this as a separate process (see ``live/__init__.py``) so a
crash inside the native ThroughTek (TUTK) Kalay library can never take Home
Assistant down with it. It:

1. wakes the camera over the DDPAI MQTT broker (``startLive``) and keeps the
   live session alive (``keepLive`` every 8 s), like the app does;
2. opens a TUTK P2P session to the camera's UID (relayed through ThroughTek
   when the 4G carrier NAT blocks a direct path) and starts AV channel 0;
3. sends the stream-start IOCTLs (and ``switchCam`` for the rear lens);
4. wraps every H.264 access unit in MPEG-TS with a wall-clock PTS and writes
   it to stdout.

Everything secret comes from the environment, never argv (argv is visible to
every process on the host):

    DDPAI_LIB          path of libIOTCAPIs_ALL.so (glibc build, TUTK SDK 4.x)
    DDPAI_P2P_UID      camera TUTK UID           (checkDevnetStatus p2pUid)
    DDPAI_P2P_PASSWORD camera AV password        (checkDevnetStatus p2pSk, decrypted)
    DDPAI_AUTH_KEY     TUTK connect auth key
    DDPAI_LICENSE_KEY  TUTK SDK license key (optional for old SDKs)
    DDPAI_UUID         camera cloud uuid, for the MQTT wake-up (optional)
    DDPAI_LENS         front | rear
    DDPAI_MQTT         broker {"ip","tcpPort","username","password","client_id"} (JSON)
    DDPAI_MODE         live (default) | p2p
    DDPAI_REQUEST      p2p mode: {"action": ..., "data": {...}} (JSON)

Logs go to stderr. In live mode stdout carries frame records (see
``write_record``); in p2p mode it
carries the camera's JSON reply to one request (``querySettings``,
``saveSettings``, ...), sent on IOCtrl 0x30000006 and answered on 0x30000007.
"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, byref, c_char, c_char_p, c_int, c_int32, c_uint, c_uint8, c_uint32, c_void_p, sizeof
import json
import logging
import os
import random
import signal
import struct
import string
import sys
import threading
import time

import elf

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
log = logging.getLogger("ddpai_live")

HERE = os.path.dirname(os.path.abspath(__file__))
FRAME_BUF_SIZE = 4 * 1024 * 1024
KEEPALIVE_SECONDS = 8
KEEPLIVE_P2P_SECONDS = 3
# No frame for this long after the stream started = the camera went away.
NO_DATA_SECONDS = 30
FIRST_FRAME_SECONDS = 60

# avRecvFrameData2 results that mean "try again" vs "the session is gone".
AV_ER_DATA_NOREADY = -20012
AV_ER_INCOMPLETE_FRAME = -20013
AV_ER_LOSED_THIS_FRAME = -20014

# App JSON requests go out on SEND and are answered on RECEIVE.
IOCTL_SEND = 0x30000006
IOCTL_RECEIVE = 0x30000007
IOTYPE_USER_IPCAM_START = 511
IOTYPE_USER_IPCAM_STOP = 767
IOTYPE_GETAUDIOOUTFORMAT_REQ = 810
LENS_CODES = {"front": 0, "rear": 1, "interior": 2}
AV_ER_TIMEOUT = -20011

IOTC_ERRORS = {
    -90: "The camera is offline (not connected to 4G)",
    -19: "The camera could not be found on the P2P network",
    -13: "The camera did not answer in time",
    -42: "The camera refused the connection (session limit reached)",
}


class AVClientStartInConfig(Structure):
    """TUTK AVClientStartInConfig, 64-bit layout (48 bytes)."""

    _fields_ = [
        ("cb", c_uint32),
        ("iotc_session_id", c_int32),
        ("iotc_channel_id", c_uint8),
        ("_pad", c_uint8 * 3),
        ("timeout_sec", c_uint32),
        ("account_or_identity", c_char_p),
        ("password_or_token", c_char_p),
        ("resend", c_int32),
        ("security_mode", c_uint32),
        ("auth_type", c_uint32),
        ("sync_recv_data", c_int32),
    ]


class AVClientStartOutConfig(Structure):
    """TUTK AVClientStartOutConfig (24 bytes)."""

    _fields_ = [
        ("cb", c_uint32),
        ("server_type", c_uint32),
        ("resend", c_uint32),
        ("two_way_streaming", c_uint32),
        ("sync_recv_data", c_uint32),
        ("security_mode", c_uint32),
    ]


class IOTCConnectInput(Structure):
    """Input of IOTC_Connect_ByUIDEx."""

    _fields_ = [
        ("cb", c_int32),
        ("authentication_type", c_int32),
        ("auth_key", c_char * 8),
        ("timeout", c_int32),
    ]


class MqttWake:
    """startLive / keepLive / stopLive over the DDPAI broker (best effort).

    Same wire format as the app (MqttRequest/MqttData): topic
    ``0/<camera uuid>/<client id>/<msgid>``, payload
    ``{"action", "msgid", "cid": <client id>}``.
    """

    def __init__(self, device_uuid: str, broker: dict) -> None:
        self.device_uuid = device_uuid
        self.broker = broker
        self.client_id = broker.get("client_id") or "".join(random.choices(string.digits, k=15))
        self.client = None
        self._msgid = random.randint(12345, 50000)
        self._stop = threading.Event()

    def _publish(self, action: str, qos: int) -> None:
        self._msgid += 1
        msg_id = str(self._msgid)
        payload = {"action": action, "msgid": msg_id, "cid": self.client_id}
        self.client.publish(f"0/{self.device_uuid}/{self.client_id}/{msg_id}", json.dumps(payload), qos=qos)

    def start(self) -> None:
        if not self.broker.get("ip"):
            log.warning("no MQTT broker address; trying P2P without the wake-up")
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            log.warning("paho-mqtt is not installed; skipping the MQTT wake-up")
            return
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        except AttributeError:  # paho-mqtt 1.x
            client = mqtt.Client(client_id=self.client_id)
        if self.broker.get("username"):
            client.username_pw_set(self.broker["username"], self.broker.get("password"))
        try:
            client.connect(self.broker["ip"], int(self.broker.get("tcpPort") or 1883), keepalive=60)
        except OSError as err:
            log.warning("MQTT broker unreachable (%s); trying P2P without the wake-up", err)
            return
        client.loop_start()
        self.client = client
        self._publish("startWaken", 1)
        log.info("startWaken sent")

    def stop(self) -> None:
        self._stop.set()
        if not self.client:
            return
        try:
            time.sleep(0.3)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.client = None


class TutkStream:
    """One TUTK P2P session + AV channel to the camera."""

    def __init__(self, lib_path: str, uid: str, password: str, auth_key: str, license_key: str, lens: str) -> None:
        if elf.is_musl():
            lib_path = elf.musl_copy(lib_path, os.path.join(os.path.dirname(HERE), "libs", os.uname().machine))
        try:
            self.lib = ctypes.CDLL(lib_path)
        except OSError as err:
            missing = missing_symbols(lib_path)
            raise RuntimeError(f"{err}; symbols this C library lacks: {' '.join(missing) or 'none found'}") from err
        if elf.is_musl():
            log.info("musl: %d stat slot(s) redirected", elf.fix_stat_slots(self.lib, lib_path))
        self.uid = uid.encode()
        self.password = password.encode()
        self.auth_key = auth_key.encode()
        self.license_key = license_key.encode()
        self.lens = lens
        self.sid = -1
        self.av = -1
        self.running = False
        self._initialized = False
        self._msg_id = 10000
        self.lens_marker: str | None = None
        self.switching_to: str | None = None
        self.switch_at = 0.0
        self._heartbeat_started = False
        self._send_lock = threading.Lock()

    def connect(self, start_video: bool = True) -> None:
        lib = self.lib
        if self.license_key and hasattr(lib, "TUTK_SDK_Set_License_Key"):
            lib.TUTK_SDK_Set_License_Key.argtypes = [c_char_p]
            log.info("license key set (%s)", lib.TUTK_SDK_Set_License_Key(self.license_key))
        ver = c_uint32()
        lib.IOTC_Get_Version(byref(ver))
        v = ver.value
        log.info("TUTK %d.%d.%d.%d", v >> 24 & 255, v >> 16 & 255, v >> 8 & 255, v & 255)

        if hasattr(lib, "IOTC_Setup_Session_Alive_Timeout"):
            lib.IOTC_Setup_Session_Alive_Timeout(20)
        lib.IOTC_Initialize2.restype = c_int
        ret = lib.IOTC_Initialize2(int(time.time() * 1000) % 10000 + 10000)
        if ret < 0:
            raise RuntimeError(f"IOTC_Initialize2 failed: {ret}")
        self._initialized = True

        lib.IOTC_Get_SessionID.restype = c_int
        lib.IOTC_Connect_ByUIDEx.restype = c_int
        lib.IOTC_Connect_ByUIDEx.argtypes = [c_char_p, c_int, c_void_p]
        ret = -1
        for attempt in range(1, 4):
            self.sid = lib.IOTC_Get_SessionID()
            if self.sid < 0:
                raise RuntimeError(f"IOTC_Get_SessionID failed: {self.sid}")
            conn = IOTCConnectInput()
            conn.cb = sizeof(conn)
            conn.authentication_type = 0
            conn.auth_key = self.auth_key[:8]
            conn.timeout = 10
            ret = lib.IOTC_Connect_ByUIDEx(self.uid, self.sid, byref(conn))
            log.info("IOTC_Connect_ByUIDEx attempt %d -> %d", attempt, ret)
            if ret >= 0:
                break
            time.sleep(1)
        if ret < 0:
            reason = IOTC_ERRORS.get(ret, "P2P connection to the camera failed")
            raise RuntimeError(f"{reason} (IOTC {ret})")

        lib.avInitialize.restype = c_int
        lib.avInitialize(4096)
        lib.avClientStartEx.restype = c_int
        lib.avClientStartEx.argtypes = [c_void_p, c_void_p]
        cfg_in = AVClientStartInConfig()
        cfg_in.cb = sizeof(cfg_in)
        cfg_in.iotc_session_id = self.sid
        cfg_in.iotc_channel_id = 0
        cfg_in.timeout_sec = 30
        cfg_in.account_or_identity = b"admin"
        cfg_in.password_or_token = self.password
        cfg_in.resend = 1
        cfg_in.security_mode = 2  # AUTO
        cfg_in.auth_type = 0
        cfg_out = AVClientStartOutConfig()
        cfg_out.cb = sizeof(cfg_out)
        self.av = lib.avClientStartEx(byref(cfg_in), byref(cfg_out))
        if self.av < 0:
            raise RuntimeError(f"avClientStartEx failed: {self.av}")
        log.info("AV channel %d up (server type %d)", self.av, cfg_out.server_type)
        if hasattr(lib, "avClientSetRecvBufMaxSize"):
            lib.avClientSetRecvBufMaxSize(self.av, 10240)  # KB, as the app
        self.running = True
        if start_video:
            self._start_video()

    def _start_video(self) -> None:
        lib = self.lib
        lib.avSendIOCtrl.restype = c_int
        lib.avSendIOCtrl.argtypes = [c_int, c_uint, c_char_p, c_int]
        channel0 = bytes(8)  # SMsgAVIoctrlAVStream {channel 0, reserved}
        # The app's order: audio-out format request, clean the local video
        # buffer, wait 50 ms, IOTYPE_USER_IPCAM_START.
        log.info("IOCtrl 810 -> %d", lib.avSendIOCtrl(self.av, IOTYPE_GETAUDIOOUTFORMAT_REQ, channel0, 8))
        if hasattr(lib, "avClientCleanLocalVideoBuf"):
            lib.avClientCleanLocalVideoBuf(self.av)
        time.sleep(0.05)
        log.info("IOCtrl 511 (start) -> %d", lib.avSendIOCtrl(self.av, IOTYPE_USER_IPCAM_START, channel0, 8))

    def after_first_frame(self) -> None:
        """What the app starts once the first frame is shown."""
        if self._heartbeat_started:
            return
        self._heartbeat_started = True
        # Always name the lens: the camera may still be on the one the last
        # session (or the app) left it on.
        self.switch_lens(self.lens)
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def switch_lens(self, lens: str) -> None:
        """switchCam on the running stream: 0 front, 1 rear, 2 interior."""
        cam = LENS_CODES.get(lens)
        if cam is None:
            log.warning("unknown lens %s", lens)
            return
        self.lens = lens
        log.info("switchCam %s -> %d", lens, self._send("switchCam", {"remoteOptCam": cam}, need_ack=True))
        # The camera keeps sending the old lens for a moment: frames are
        # "pending" until the new lens's first keyframe (see main).
        self.lens_marker = "pending"
        self.switching_to = lens
        self.switch_at = time.monotonic()

    def _send(self, action: str, data: dict | None, need_ack: bool = False) -> int:
        """Send one app JSON message (P2PRequest) on the AV channel."""
        self._msg_id += 1
        body = {"action": action, "isAck": False, "msgId": str(self._msg_id), "needAck": need_ack}
        if data is not None:
            body["data"] = data
        payload = json.dumps(body, separators=(",", ":")).encode()
        with self._send_lock:
            return self.lib.avSendIOCtrl(self.av, IOCTL_SEND, payload, len(payload))

    def _heartbeat(self) -> None:
        """keepLive + queryStatus every 3 s, like the app once video shows.

        The loop also drains the IOCtrl replies/notifications the camera
        sends back (0x30000007 / 0x30000008), so its queue never fills.
        """
        lib = self.lib
        lib.avRecvIOCtrl.restype = c_int
        lib.avRecvIOCtrl.argtypes = [c_int, POINTER(c_uint), POINTER(c_char), c_int, c_uint]
        buf = (c_char * 8192)()
        kind = c_uint()
        last = 0.0
        while self.running and self.av >= 0:
            now = time.monotonic()
            if now - last >= KEEPLIVE_P2P_SECONDS:
                last = now
                for action in ("keepLive", "queryStatus"):
                    ret = self._send(action, None)
                    if ret < 0:
                        log.warning("%s -> %d", action, ret)
            ret = lib.avRecvIOCtrl(self.av, byref(kind), buf, 8192, 500)
            if ret > 0:
                text = bytes(buf[:ret]).replace(bytes(1), b"").decode("utf-8", errors="replace")
                log.info("camera says (0x%08x): %s", kind.value, text[:200])

    def request(self, action: str, data: dict, timeout: float = 15.0) -> dict:
        """One app JSON request over the AV channel; merged reply (hasMore parts)."""
        lib = self.lib
        lib.avSendIOCtrl.restype = c_int
        lib.avSendIOCtrl.argtypes = [c_int, c_uint, c_char_p, c_int]
        lib.avRecvIOCtrl.restype = c_int
        lib.avRecvIOCtrl.argtypes = [c_int, POINTER(c_uint), POINTER(c_char), c_int, c_uint]
        msg_id = str(random.randint(10001, 60000))
        payload = json.dumps(
            {"action": action, "msgId": msg_id, "isAck": False, "needAck": True, "data": data},
            separators=(",", ":"),
        ).encode()
        if len(payload) > 1000:
            raise RuntimeError("request larger than the 1000-byte IOCtrl limit")
        ret = lib.avSendIOCtrl(self.av, IOCTL_SEND, payload, len(payload))
        log.info("%s sent (%d bytes) -> %d", action, len(payload), ret)
        if ret < 0:
            raise RuntimeError(f"avSendIOCtrl failed: {ret}")
        buf = (c_char * 65536)()
        kind = c_uint()
        merged: dict | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ret = lib.avRecvIOCtrl(self.av, byref(kind), buf, 65536, 1000)
            if ret == AV_ER_TIMEOUT or ret == 0:
                continue
            if ret < 0:
                raise RuntimeError(f"avRecvIOCtrl ended the session: {ret}")
            if kind.value != IOCTL_RECEIVE:
                log.info("ignored IOCtrl 0x%08x (%d bytes)", kind.value, ret)
                continue
            text = bytes(buf[:ret]).replace(bytes(1), b"").decode("utf-8", errors="replace")
            try:
                reply = json.loads(text)
            except ValueError:
                log.warning("unparsable reply: %s", text[:200])
                continue
            # The camera answers with "msgid" (lower case); the app sends "msgId".
            reply_id = reply.get("msgId", reply.get("msgid"))
            if reply.get("action") != action or (reply_id is not None and str(reply_id) != msg_id):
                log.info("reply for another request: %s %s", reply.get("action"), reply_id)
                continue
            if merged is None:
                merged = reply
            elif isinstance(reply.get("data"), dict):
                merged.setdefault("data", {}).update(reply["data"])
            if not reply.get("hasMore"):
                return merged
        if merged is not None:
            return merged
        raise RuntimeError(f"no reply to {action} within {timeout:.0f} s")

    def frames(self):
        """Yield raw frames (H.264 Annex-B access units)."""
        lib = self.lib
        data = (c_char * FRAME_BUF_SIZE)()
        info = (c_char * 4096)()
        actual, expected, info_len = c_int32(), c_int32(), c_int32()
        index = c_uint32()
        lib.avRecvFrameData2.restype = c_int
        lib.avRecvFrameData2.argtypes = [
            c_int, POINTER(c_char), c_int, POINTER(c_int32), POINTER(c_int32),
            POINTER(c_char), c_int, POINTER(c_int32), POINTER(c_uint32),
        ]
        last = time.monotonic()
        report = last
        count = size = 0
        codes: dict[int, int] = {}
        while self.running:
            ret = lib.avRecvFrameData2(
                self.av, data, FRAME_BUF_SIZE, byref(actual), byref(expected),
                info, 4096, byref(info_len), byref(index),
            )
            now = time.monotonic()
            if now - report >= 5:
                log.info("last 5 s: %d frames, %d bytes, other results %s", count, size, codes or "-")
                report, count, size, codes = now, 0, 0, {}
            if ret > 0:
                if count == 0 and size == 0:
                    log.info("frame #%d: %d bytes, info %s", index.value, ret, bytes(info[:min(info_len.value, 16)]).hex())
                last = now
                count += 1
                size += ret
                yield bytes(data[:ret])
                continue
            codes[ret] = codes.get(ret, 0) + 1
            if now - last > NO_DATA_SECONDS:
                raise RuntimeError(f"no video for {NO_DATA_SECONDS} s (last result {ret})")
            time.sleep(0.004 if ret == AV_ER_DATA_NOREADY else 0.033)

    def close(self) -> None:
        self.running = False
        lib = self.lib
        if self.av >= 0:
            try:
                lib.avClientStop(self.av)
            except Exception:  # noqa: BLE001
                pass
            self.av = -1
        if self.sid >= 0:
            try:
                lib.IOTC_Session_Close(self.sid)
            except Exception:  # noqa: BLE001
                pass
            self.sid = -1
        if self._initialized:
            try:
                lib.avDeInitialize()
                lib.IOTC_DeInitialize()
            except Exception:  # noqa: BLE001
                pass
            self._initialized = False


def missing_symbols(lib_path: str) -> list[str]:
    """Imports of the library that this process' C library does not export."""
    try:
        wanted = elf.undefined_symbols(lib_path)
    except (OSError, ValueError, IndexError) as err:
        return [f"(could not read the library: {err})"]
    libc = elf.musl_libc() if elf.is_musl() else None
    handles = [libc] if libc else [ctypes.CDLL(None)]
    optional = {"_ITM_deregisterTMCloneTable", "_ITM_registerTMCloneTable", "__gmon_start__"}
    out = []
    for name in wanted:
        if name in optional:
            continue
        if not any(hasattr(handle, name) for handle in handles):
            out.append(name)
    return out


SWITCH_FALLBACK_SECONDS = 3


def sps_of(au: bytes) -> bytes | None:
    """The SPS NAL unit of an Annex-B access unit (it carries the resolution)."""
    i = au.find(b"\x00\x00\x01")
    while i != -1 and i + 3 < len(au):
        start = i + 3
        nxt = au.find(b"\x00\x00\x01", start)
        if au[start] & 0x1F == 7:
            return au[start:nxt if nxt != -1 else len(au)].rstrip(b"\x00")
        i = nxt
    return None


def nal_types(au: bytes) -> set[int]:
    """H.264 NAL unit types present in an Annex-B access unit."""
    types = set()
    i = au.find(b"\x00\x00\x01")
    while i != -1 and i + 3 < len(au):
        types.add(au[i + 3] & 0x1F)
        i = au.find(b"\x00\x00\x01", i + 3)
    return types


def _read_commands(stream: "TutkStream") -> None:
    """stdin lines "front" / "rear" / "interior" switch the running stream."""
    for line in sys.stdin:
        lens = line.strip()
        if lens:
            stream.switch_lens(lens)


def run_request(stream: TutkStream, request: str) -> int:
    """p2p mode: one JSON request, the reply as JSON on stdout."""
    try:
        req = json.loads(request)
        stream.connect(start_video=False)
        reply = stream.request(req["action"], req.get("data") or {}, float(req.get("timeout") or 15))
    except Exception as err:  # noqa: BLE001 - report and exit non-zero
        log.error("%s", err)
        return 1
    finally:
        stream.close()
    sys.stdout.write(json.dumps(reply))
    sys.stdout.flush()
    return 0


# Frame record on stdout: >I length, B flags, Q microseconds since the first
# frame, then the payload. flags bit 0 = keyframe; bit 1 = lens marker (the
# payload is the lens name the following frames come from).
RECORD = struct.Struct(">IBQ")
FLAG_KEYFRAME = 1
FLAG_LENS = 2


def write_record(out, flags: int, micros: int, payload: bytes) -> None:
    out.write(RECORD.pack(len(payload), flags, micros) + payload)


def main() -> int:
    env = os.environ
    lens = env.get("DDPAI_LENS", "front")
    missing = [k for k in ("DDPAI_LIB", "DDPAI_P2P_UID", "DDPAI_P2P_PASSWORD", "DDPAI_AUTH_KEY") if not env.get(k)]
    if missing:
        log.error("missing %s", ", ".join(missing))
        return 2

    try:
        broker = json.loads(env.get("DDPAI_MQTT") or "{}")
    except ValueError:
        broker = {}
    try:
        stream = TutkStream(
            env["DDPAI_LIB"], env["DDPAI_P2P_UID"], env["DDPAI_P2P_PASSWORD"],
            env["DDPAI_AUTH_KEY"], env.get("DDPAI_LICENSE_KEY", ""), lens,
        )
    except RuntimeError as err:
        log.error("%s", err)
        return 1
    if env.get("DDPAI_MODE") == "p2p":
        return run_request(stream, env.get("DDPAI_REQUEST") or "{}")
    wake = MqttWake(env["DDPAI_UUID"], broker) if env.get("DDPAI_UUID") else None

    def _stop(*_args) -> None:
        stream.running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    out = sys.stdout.buffer
    started = None

    # The native connect can hang far past its own timeout; a session that
    # has shown no picture after this long is given up (exit, not a hang).
    first_frame = threading.Event()

    def _watchdog() -> None:
        if not first_frame.wait(FIRST_FRAME_SECONDS):
            log.error("no picture within %d s; giving up", FIRST_FRAME_SECONDS)
            sys.stderr.flush()
            os._exit(3)

    threading.Thread(target=_watchdog, daemon=True).start()
    last_sps: bytes | None = None
    frames = 0
    try:
        if wake:
            wake.start()
        stream.connect()
        write_record(out, FLAG_LENS, 0, b"pending")  # until the first switchCam
        threading.Thread(target=_read_commands, args=(stream,), daemon=True).start()
        for au in stream.frames():
            types = nal_types(au)
            keyframe = bool(types & {5, 7})
            stream.after_first_frame()
            if started is None:
                if not keyframe:
                    continue  # a decoder can only start on SPS/IDR
                first_frame.set()
                started = time.monotonic()
                log.info("first keyframe, streaming")
            micros = int((time.monotonic() - started) * 1_000_000)
            sps = sps_of(au) if keyframe else None
            if stream.switching_to and keyframe and (
                (last_sps is not None and sps != last_sps)
                or time.monotonic() - stream.switch_at > SWITCH_FALLBACK_SECONDS
            ):
                # A keyframe with another SPS (front 1280x720, rear 848x480 on
                # a Z60 Pro) — or, for same-size lenses, the first after a
                # short wait: the new lens has arrived.
                write_record(out, FLAG_LENS, micros, stream.switching_to.encode())
                stream.switching_to = None
                stream.lens_marker = None
            if sps is not None:
                last_sps = sps
            if stream.lens_marker:
                write_record(out, FLAG_LENS, micros, stream.lens_marker.encode())
                stream.lens_marker = None
            write_record(out, FLAG_KEYFRAME if keyframe else 0, micros, au)
            out.flush()
            frames += 1
            if frames % 300 == 0:
                log.info("%d frames sent", frames)
    except BrokenPipeError:
        log.info("viewer gone")
    except Exception as err:  # noqa: BLE001 - report and exit non-zero
        log.error("%s", err)
        return 1
    finally:
        stream.close()
        if wake:
            wake.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
