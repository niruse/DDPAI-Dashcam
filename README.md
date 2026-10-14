# DDPAI Dashcam for Home Assistant

Unofficial Home Assistant integration for **DDPAI 4G dashcams** (for example the Z60 / Z60 Pro
with a 4G cloud box, Z50 Pro, X5 Pro 4G), using the same cloud account as the DDPAI app.

Not affiliated with or endorsed by DDPAI.

## What you get

For every camera on the account that has a 4G link:

| Entity | What it shows |
|---|---|
| `device_tracker.<camera>` | Last position the camera reported |
| `sensor.<camera>_state` | `driving`, `standby`, `parking`, `offline` |
| `binary_sensor.<camera>_4g_link` | Whether the camera is online over 4G |
| `sensor.<camera>_last_event` / `_last_event_time` | Power, collision, parking-collision, temperature and battery events (recent list in attributes) |
| `sensor.<camera>_last_parked_photo` | When the newest parked photo reached the cloud |
| `sensor.<camera>_last_urgent_event` | When the newest urgent (collision) file reached the cloud |
| `image.<camera>_parked_photo_front` / `_rear` / `_cabin` | Newest parked photo of each lens |
| `image.<camera>_urgent_event_front` / `_rear` / `_cabin` | Frame of the newest urgent clip of each lens |
| `sensor.<camera>_sim_card` | SIM state (diagnostic) |
| `sensor.<camera>_data_used_this_month` / `_data_remaining` / `_data_plan_size` / `_data_plan_expires` | Data plan of the camera's SIM ("Manage data" in the app); `unknown` when the SIM has no DDPAI-managed plan |
| `sensor.<camera>_geofences` | Number of geofences; the list (center, radius, notify on enter/leave, enabled) in attributes |
| `binary_sensor.<camera>_geofence_<name>` | On while the car's last position is inside that geofence |
| `camera.<camera>_live` + `select.<camera>_live_camera` | 4G live view, front or rear (see below); the still picture is the newest parked photo |
| `select.<camera>_rtsp_stream`, `switch.<camera>_rtsp_live`, `binary_sensor.<camera>_rtsp_live_video` | RTSP output for an NVR (see below) |
| `switch.<camera>_status_change_notifications` | The app's push notifications for power / parking / standby changes |
| `update.<camera>_firmware` | Installed and newest firmware (install from the app over the camera's Wi-Fi) |

The cabin entities appear only when a cabin camera (MINI2X) is bound to the account.

The **media browser** gets a *DDPAI Dashcam* folder with the cloud album — *Parked photos* and
*Urgent events*, per camera, with thumbnails. Files are streamed through Home Assistant, so they
play on an HTTPS dashboard even though the DDPAI media server is plain HTTP.

Two events fire on the bus for automations:

- `ddpai_device_event` — `device_uuid`, `device_name`, `event` (e.g. `collision`), `time`
- `ddpai_new_media` — `device_uuid`, `device_name`, `album`, `camera`, `kind`, `item_id`, `time`

## Geofences

The geofences are the same ones the DDPAI app shows under *Geofence*, so the app keeps sending its
enter/leave notifications. Manage them from Home Assistant with three actions:

- `ddpai.add_geofence` — `device_id`, `name`, optional `latitude`/`longitude` (default: the car's
  last position), `radius` in metres (default 2000), `notify_on` (`enter` or `leave`), `enabled`
- `ddpai.update_geofence` — `device_id`, `geofence_id`, and any field to change (e.g.
  `enabled: false` to switch a fence off)
- `ddpai.delete_geofence` — `device_id`, `geofence_id`

Each action returns the camera's updated geofence list. The `geofence_id` is in the attributes of
the geofence sensors.

## Live view

The `camera` entities stream the camera's live picture over its 4G link, the way the app's
*Live view* does: the camera is woken over the DDPAI MQTT broker and the video comes over a
ThroughTek (TUTK) Kalay P2P session. Home Assistant's go2rtc plays it as WebRTC, so open the
camera from a dashboard like any other.

- A session starts when somebody opens the stream and stops 15 seconds after the last viewer
  leaves — watching uses the camera's 4G data and keeps it awake.
- The camera has to be online (`binary_sensor.<camera>_4g_link` on). A switched-off or deeply
  parked camera answers "offline"; the reason is in the `last_error` attribute and the log.
- The P2P part needs ThroughTek's native library (`libIOTCAPIs_ALL.so`, x86-64 or ARM64). It is
  downloaded on first use from the public wyze-bridge image (or reused from the CuboAI
  integration). Home Assistant OS runs on musl, so the integration loads an adapted copy of this
  glibc build; nothing has to be installed.
- The library also needs the TUTK SDK **license key** of the DDPAI app, which is not shipped here.
  Enter it in the integration's options.
- **One camera at a time.** The dashcam has a single live encoder: switching to the rear lens
  switches every viewer (tested — two sessions both showed the rear picture). So there is one
  `Live` camera and a `Live camera` select (Front / Rear) that switches the running stream in
  place, like the app's switch-camera button.
- The P2P session runs in a separate process, so a crash in the native library cannot take Home
  Assistant down.

## RTSP for NVRs

The dashcam can be recorded by an NVR (HiLook/Hikvision, Frigate, Blue Iris, Synology Surveillance
Station…) as an ordinary RTSP camera — the same design as the Proof Plus integration. Choose the mode
under **Configure → RTSP / NVR** or with the **RTSP stream** select:

| Mode | What the NVR gets |
|---|---|
| **Off** (default) | No RTSP server. |
| **Only when triggered** | Live video while a trigger is active, and a still **standby picture** the rest of the time. |
| **Always** | Live video whenever the NVR is connected. |

Give the NVR `rtsp://<home-assistant-ip>:8565/ddpai_<id>` for the front camera and `…_rear` for the
rear one — the exact addresses are on the **RTSP / NVR** options page and in the `url` / `rear_url`
attributes of `binary_sensor.<camera>_rtsp_live_video`. Port 8565 (not Proof's 8564), so both
integrations can feed one NVR from the same box. Video only, H.264 848×480 at 30 fps, passed through
exactly as the dashcam encodes it.

**Triggers** (only in *only when triggered*):

- **Events** — the device events you pick (collision, parking collision and a new urgent clip by
  default) start live video for 5 minutes; another one extends it. Events arrive with the poll, so
  live video starts up to one poll interval after the event.
- **While driving** — from the camera reporting *driving* until it stops, plus 2 minutes.
- **The `RTSP live` switch** — holds live video on until you switch it off.
- **The `ddpai.rtsp_trigger` action** — live video for a number of minutes, from any automation;
  `ddpai.rtsp_stop` ends every trigger at once.

**The standby picture** is the last live view from that camera (or its newest parked photo), dimmed
and labelled, at the live stream's size. It keeps the NVR connected between triggers and never
changes while shown, so the jump to live video is what the NVR's motion detection records.

**Both cameras.** With *Cameras on the stream* set to **both**, each camera has its own address and
NVR channel, and while live the dashcam switches between them every 15 seconds (configurable): each
channel shows live video on its turn and its last picture, marked *Paused*, in between. Somebody
watching the `Live` camera in Home Assistant keeps the lens they chose.

**Mobile data.** Live video runs over the dashcam's 4G at about 4 Mbit/s — roughly 1.8 GB an hour.
*Always* means that for as long as the NVR is connected. go2rtc only pulls the stream while an RTSP
client is connected, so with no NVR connected nothing reaches the dashcam.

## Setup

1. Copy `custom_components/ddpai` into your Home Assistant `config/custom_components/`
   (or add this repository to HACS as a custom repository) and restart Home Assistant.
2. **Settings → Devices & services → Add integration → DDPAI Dashcam.**
3. Sign in with the email (or phone) and password you use in the DDPAI app.

The password is stored in the config entry: the DDPAI cloud ends a session after 30 minutes
without use, and the integration signs in again when that happens. If you change the password,
Home Assistant asks for the new one.

## Good to know

- The cloud sends the last known position but no time of fix; the tracker's
  `position_changed` attribute records when Home Assistant saw the position move.
- Reading the cloud album marks it as read in the DDPAI app. The integration only lists the
  album when the unread counter says something new arrived (and every 30 minutes), but new
  items may not show the app's "new" badge.
- Remote settings over 4G are not part of this integration.

## Options

- **Poll interval** — how often state and position are read (default 60 s, minimum 30 s).
- **Media browser** — show the cloud album in the media browser (default on).
- **4G live view cameras** — add the live `camera` entities (default on).
- **TUTK SDK license key** — needed to open a live session (see *Live view*).
- **RTSP / NVR** — mode, cameras, triggers, port and login (see *RTSP for NVRs*).
