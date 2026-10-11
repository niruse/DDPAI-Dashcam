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
- Remote live view and remote settings over 4G are not part of this integration.

## Options

- **Poll interval** — how often state and position are read (default 60 s, minimum 30 s).
- **Media browser** — show the cloud album in the media browser (default on).
