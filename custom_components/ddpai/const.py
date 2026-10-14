"""Constants for the DDPAI Dashcam integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "ddpai"
ATTRIBUTION: Final = "Data provided by the DDPAI cloud"

# Overseas service (the build of the DDPAI app sold outside China).
MAIN_URL: Final = "https://apphw.ddpai.com"
MICRO_URL: Final = "https://apphw.ddpai.com:18081"
PCODE: Final = "0x1002"
# App-wide constant from the DDPAI app: login name and password are sent
# AES-128-ECB encrypted with it, hex encoded. Not a per-user secret.
APP_KEY: Final = b"vYouTech AES_128"

CONF_COUNTRY: Final = "country"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_ENABLE_MEDIA_BROWSER: Final = "enable_media_browser"
CONF_ENABLE_LIVE_VIEW: Final = "enable_live_view"
CONF_TUTK_LICENSE_KEY: Final = "tutk_license_key"

# Live view: TUTK connect auth key the DDPAI app uses for every camera.
TUTK_AUTH_KEY: Final = "JSzk8b3P"

# Used only when Home Assistant has no country configured.
DEFAULT_COUNTRY: Final = "US"
DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 30
# Listing the cloud album marks it read in the app, so it is only listed when
# the unread counter says something new arrived, or on this slow refresh.
ALBUM_REFRESH_SECONDS: Final = 1800
ALBUM_PAGE_SIZE: Final = 30

# Server error codes (SErrCode in the app).
ERR_USER_NOT_FOUND: Final = 65540
ERR_BAD_PASSWORD: Final = 65541
ERR_EMAIL_NOT_ACTIVATED: Final = 65543
ERR_USER_LOCKED: Final = 65545
ERR_SESSION_TIMEOUT: Final = 629144
ERR_SESSION_FAILED: Final = 7340033

# checkDevnetStatus "status".
STATE_UNKNOWN: Final = "unknown"
DEVICE_STATES: Final = {
    0: STATE_UNKNOWN,
    1: "driving",
    2: "standby",
    3: "parking",
    4: "offline",
}

# queryDeviceEventByUuid "eventType" (DevnetRecord in the app).
EVENT_TYPES: Final = {
    1: "power_on",
    2: "power_off",
    7: "collision",
    9: "parking_collision",
    10: "remote_link",
    11: "remote_parking",
    12: "remote_standby",
    13: "high_temperature",
    16: "low_battery",
    # Recorded by the integration itself from the polled state (the cloud's
    # event list stays empty for many cameras; the app's own status-change
    # notifications are phone pushes, not stored there).
    "driving": "driving",
    "standby": "standby",
    "parking": "parking",
    "offline": "offline",
}
STATE_EVENTS: Final = {1: "driving", 2: "standby", 3: "parking", 4: "offline"}
MAX_STATE_EVENTS: Final = 20

# Cloud album tabs ("alarmType") and file kinds ("resType").
ALBUM_PARKED: Final = 74
ALBUM_URGENT: Final = 54
ALBUMS: Final = {ALBUM_PARKED: "Parked photos", ALBUM_URGENT: "Urgent events"}
RES_TYPES: Final = {
    1: ("front", "image"),
    2: ("front", "video"),
    3: ("rear", "image"),
    4: ("rear", "video"),
    5: ("cabin", "image"),
    6: ("cabin", "video"),
}
# Coordinates the server uses for "no position".
NO_POSITION: Final = 99999

# Geofences ("electronic fences"). triggerMode: 3 alert on entering, 2 on
# leaving; the app stores a disabled fence as the negative of its mode.
FENCE_COMPANY: Final = "0x10001"
FENCE_ENTER: Final = 3
FENCE_LEAVE: Final = 2
FENCE_REFRESH_SECONDS: Final = 300

EVENT_NEW_MEDIA: Final = f"{DOMAIN}_new_media"

# --- RTSP output for NVRs (same design as the Proof Plus integration) ------
CONF_RTSP_MODE: Final = "rtsp_mode"
CONF_RTSP_PORT: Final = "rtsp_port"
CONF_RTSP_CAMERA: Final = "rtsp_camera"
CONF_RTSP_ALTERNATE: Final = "rtsp_alternate_seconds"
CONF_RTSP_USERNAME: Final = "rtsp_username"
CONF_RTSP_PASSWORD: Final = "rtsp_password"
CONF_RTSP_ALERTS: Final = "rtsp_alerts"
CONF_RTSP_ALERT_MINUTES: Final = "rtsp_alert_minutes"
CONF_RTSP_DRIVING: Final = "rtsp_driving"
CONF_RTSP_DRIVING_GRACE: Final = "rtsp_driving_grace"

# off: no RTSP server. trigger: live video only while something triggered it,
# the standby picture otherwise. always: live video whenever an NVR pulls it.
RTSP_MODE_OFF: Final = "off"
RTSP_MODE_TRIGGER: Final = "trigger"
RTSP_MODE_ALWAYS: Final = "always"
RTSP_MODES: Final = [RTSP_MODE_OFF, RTSP_MODE_TRIGGER, RTSP_MODE_ALWAYS]
DEFAULT_RTSP_MODE: Final = RTSP_MODE_OFF
# Not Proof's 8564, so both integrations can serve an NVR from one box.
DEFAULT_RTSP_PORT: Final = 8565
RTSP_CAMERAS: Final = ["front", "rear", "both"]
DEFAULT_RTSP_CAMERA: Final = "both"
DEFAULT_RTSP_ALTERNATE: Final = 15
# Device events (EVENT_TYPES names) and "urgent_file" (a new urgent clip in
# the cloud album) that start live video.
RTSP_ALERT_TYPES: Final = [
    "collision", "parking_collision", "urgent_file", "power_on",
    "remote_link", "high_temperature", "low_battery",
]
DEFAULT_RTSP_ALERTS: Final = ["collision", "parking_collision", "urgent_file"]
DEFAULT_RTSP_ALERT_MINUTES: Final = 5
DEFAULT_RTSP_DRIVING: Final = True
DEFAULT_RTSP_DRIVING_GRACE: Final = 2
# Options applied on the fly (no reload).
RUNTIME_OPTIONS: Final = {CONF_RTSP_MODE}
EVENT_DEVICE: Final = f"{DOMAIN}_device_event"
