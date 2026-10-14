"""Render examples/dashboard.yaml to PNG screenshots with invented sample data.

    python examples/render_screenshots.py

Each view is drawn as HTML in Home Assistant's dark style from the dashboard
file itself (cards, names, icons, templates), with the placeholder readings
below — no real location, plate or account data — and captured with headless
Chrome or Edge into examples/images/.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jinja2
import yaml

HERE = Path(__file__).resolve().parent
OUT = HERE / "images"
WIDTH, HEIGHT = 1180, 1800
NOW = datetime(2026, 5, 18, 10, 24, tzinfo=timezone.utc)
P = "my_dashcam"


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


# --- invented sample readings ---------------------------------------------------------
STATES: dict[str, tuple[str, dict]] = {
    f"sensor.{P}_state": ("driving", {"friendly_name": "My dashcam State"}),
    f"binary_sensor.{P}_4g_link": ("on", {}),
    f"sensor.{P}_last_event": ("driving", {"recent": [
        {"event": "driving", "time": ago(minutes=12)},
        {"event": "parking", "time": ago(hours=3)},
        {"event": "collision", "time": ago(days=1, hours=2)},
        {"event": "standby", "time": ago(days=1, hours=6)},
        {"event": "driving", "time": ago(days=1, hours=7)},
    ]}),
    f"sensor.{P}_last_event_time": (ago(minutes=12), {}),
    f"binary_sensor.{P}_firmware_update_available": ("on", {"installed_version": "v9.0.1.72", "latest_version": "v9.0.1.75"}),
    f"sensor.{P}_firmware": ("v9.0.1.72", {"latest_version": "v9.0.1.75", "release_notes": "This firmware update: fixes common issues. Do not power off during the upgrade."}),
    f"image.{P}_last_view_front": (ago(minutes=12), {}),
    f"image.{P}_last_view_rear": (ago(minutes=11), {}),
    f"image.{P}_parked_photo_front": (ago(hours=3), {}),
    f"image.{P}_parked_photo_rear": (ago(hours=3), {}),
    f"image.{P}_urgent_event_front": (ago(days=1, hours=2), {}),
    f"image.{P}_urgent_event_rear": (ago(days=1, hours=2), {}),
    f"camera.{P}_live_front": ("idle", {"last_error": None}),
    f"camera.{P}_live_rear": ("idle", {}),
    f"select.{P}_live_camera": ("front", {}),
    f"device_tracker.{P}": ("not_home", {}),
    f"sensor.{P}_last_parked_photo": (ago(hours=3), {}),
    f"sensor.{P}_last_urgent_event": (ago(days=1, hours=2), {"friendly_name": "Last urgent event"}),
    f"sensor.{P}_geofences": ("2", {"geofences": [
        {"name": "Home", "radius": 2000, "notify_on": "enter", "enabled": True, "id": 10001},
        {"name": "Office", "radius": 800, "notify_on": "leave", "enabled": False, "id": 10002},
    ]}),
    f"binary_sensor.{P}_geofence_home": ("off", {"friendly_name": "My dashcam Geofence Home"}),
    f"binary_sensor.{P}_geofence_office": ("on", {"friendly_name": "My dashcam Geofence Office"}),
    f"sensor.{P}_sim_card": ("Activated", {}),
    f"sensor.{P}_data_used_this_month": ("412.5", {"friendly_name": "Data used this month", "unit": "MB"}),
    f"sensor.{P}_data_remaining": ("2587.5", {"friendly_name": "Data remaining", "unit": "MB"}),
    f"switch.{P}_status_change_notifications": ("on", {}),
    f"switch.{P}_upload_captured_pictures": ("on", {"read_at": ago(minutes=12)}),
    f"select.{P}_capture_sensitivity": ("medium", {}),
    f"switch.{P}_shut_down_15_min_after_parking": ("on", {}),
    f"select.{P}_rtsp_stream": ("trigger", {}),
    f"binary_sensor.{P}_rtsp_live_video": ("on", {
        "url": "rtsp://homeassistant.local:8565/ddpai_000000000001",
        "rear_url": "rtsp://homeassistant.local:8565/ddpai_000000000001_rear",
        "nvr_connected": True, "triggers": ["driving"], "error": None}),
    f"switch.{P}_rtsp_live": ("off", {}),
}
DISPLAY = {  # how a tile shows the state
    "driving": "Driving", "on": "On", "off": "Off", "idle": "Idle", "front": "Front", "rear": "Rear",
    "medium": "Medium", "trigger": "Only when triggered", "not_home": "Away",
}
ICONS = {  # default icon per entity when the card sets none
    "sensor.*_state": "mdi:car", "binary_sensor.*_4g_link": "mdi:signal-4g", "sensor.*_last_event": "mdi:history",
    "sensor.*_last_event_time": "mdi:clock-outline", "binary_sensor.*_firmware_update_available": "mdi:package-up",
    "sensor.*_firmware": "mdi:chip", "select.*_live_camera": "mdi:camera-switch", "device_tracker.*": "mdi:car-connected",
    "sensor.*_last_parked_photo": "mdi:image", "sensor.*_last_urgent_event": "mdi:alert-decagram",
    "sensor.*_sim_card": "mdi:sim", "switch.*_status_change_notifications": "mdi:bell-ring",
    "switch.*_upload_captured_pictures": "mdi:cloud-upload", "select.*_capture_sensitivity": "mdi:car-brake-alert",
    "switch.*_shut_down_15_min_after_parking": "mdi:power-sleep", "select.*_rtsp_stream": "mdi:cctv",
    "binary_sensor.*_rtsp_live_video": "mdi:video", "switch.*_rtsp_live": "mdi:record-circle",
    "sensor.*_geofences": "mdi:vector-square", "binary_sensor.*_geofence_*": "mdi:map-marker-radius",
    "sensor.*_data_*": "mdi:database",
}
TILE_COLOR = {"on": "#4caf50", "driving": "#03a9f4", "trigger": "#ff9800"}


def icon_for(entity: str) -> str:
    for pattern, icon in ICONS.items():
        if re.fullmatch(pattern.replace(".", r"\.").replace("*", ".*"), entity):
            return icon
    return "mdi:help-circle-outline"


def fmt_state(entity: str) -> str:
    state, attrs = STATES.get(entity, ("unknown", {}))
    if re.match(r"\d{4}-\d\d-\d\dT", state):
        delta = NOW - datetime.fromisoformat(state)
        minutes = int(delta.total_seconds() // 60)
        if minutes < 60:
            return f"{minutes} minutes ago"
        if minutes < 1440:
            return f"{minutes // 60} hours ago"
        return f"{minutes // 1440} day ago" if minutes < 2880 else f"{minutes // 1440} days ago"
    if attrs.get("unit"):
        return f"{float(state):,.1f} {attrs['unit']}"
    return DISPLAY.get(state, state)


# --- jinja for the markdown cards ------------------------------------------------------
class _State:
    def __init__(self, entity):
        self.entity_id = entity
        self.state, self.attributes = STATES[entity]
        self.last_changed = NOW - timedelta(minutes=12)


class _Domain:
    def __init__(self, domain):
        self.domain = domain

    def __getattr__(self, name):
        entity = f"{self.domain}.{name}"
        return _State(entity) if entity in STATES else None


class _States:
    def __getattr__(self, domain):
        return _Domain(domain)

    def __call__(self, entity):
        return STATES.get(entity, ("unknown", {}))[0]


def _ts(value):
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


ENV = jinja2.Environment()
ENV.globals.update(
    states=_States(),
    state_attr=lambda e, a: STATES.get(e, ("", {}))[1].get(a),
    is_state=lambda e, s: STATES.get(e, ("",))[0] == s,
    as_timestamp=_ts,
    now=lambda: NOW,
    relative_time=lambda d: "12 minutes",
    device_attr=lambda d, a: "My dashcam",
)
ENV.filters["as_timestamp"] = _ts
ENV.filters["timestamp_custom"] = lambda t, f: datetime.fromtimestamp(t, timezone.utc).strftime(f) if t else ""


def md(text: str) -> str:
    """The little markdown the dashboard uses: headings, bold, code, lists, tables."""
    out, table = [], []

    def inline(s):
        s = html.escape(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        return re.sub(r"\*([^*]+)\*", r"<i>\1</i>", s)

    def flush():
        if table:
            rows = [r for r in table if not re.fullmatch(r"\|[-| ]+\|", r)]
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            head = "".join(f"<th>{inline(c)}</th>" for c in cells[0])
            body = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in cells[1:])
            out.append(f"<table><tr>{head}</tr>{body}</table>")
            table.clear()

    for line in text.split("\n"):
        if line.startswith("|"):
            table.append(line)
            continue
        flush()
        if line.startswith("### "):
            out.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("- "):
            out.append(f"<li>{inline(line[2:])}</li>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    flush()
    return "".join(out)


# --- pictures: invented scenes, no real camera frames ----------------------------------
def scene(kind: str, caption: str) -> str:
    road = '<rect y="55%" width="100%" height="45%" fill="#4a4d52"/>' \
           '<polygon points="46,100 54,100 51,55 49,55" fill="#d9d9d9" opacity=".7"/>'
    sky = {"front": ("#7fb2e5", "#c6dcf2"), "rear": ("#8aa6c1", "#d3dee8"), "night": ("#1c2433", "#3a4558")}[kind]
    car = '<rect x="40" y="62" width="20" height="12" rx="2" fill="#e8e8e8"/><rect x="42" y="58" width="16" height="6" rx="2" fill="#bfc6cc"/>' if kind == "front" else ""
    return (
        f'<svg viewBox="0 0 100 100" preserveAspectRatio="none" class="scene">'
        f'<defs><linearGradient id="s{kind}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{sky[0]}"/>'
        f'<stop offset="1" stop-color="{sky[1]}"/></linearGradient></defs>'
        f'<rect width="100%" height="55%" fill="url(#s{kind})"/>{road}{car}'
        '<rect x="0" y="40" width="22" height="16" fill="#9aa3ab"/><rect x="78" y="36" width="22" height="20" fill="#8d969e"/>'
        f'</svg><div class="osd">DDPAI · {caption}</div>'
    )


def fake_map() -> str:
    streets = "".join(f'<line x1="0" y1="{y}" x2="100" y2="{y + 6}" stroke="#3b4048" stroke-width="1.6"/>' for y in (12, 34, 58, 80))
    streets += "".join(f'<line x1="{x}" y1="0" x2="{x - 8}" y2="100" stroke="#3b4048" stroke-width="1.2"/>' for x in (18, 44, 70, 92))
    return (
        '<svg viewBox="0 0 100 60" class="map" preserveAspectRatio="xMidYMid slice"><rect width="100" height="100" fill="#262a30"/>'
        '<rect x="52" y="38" width="30" height="20" fill="#2c3a2c"/>' + streets +
        '<polyline points="8,52 22,44 38,40 55,30 63,24" fill="none" stroke="#03a9f4" stroke-width="1.4" opacity=".8"/>'
        '<circle cx="63" cy="24" r="3.4" fill="#03a9f4" stroke="#fff" stroke-width=".8"/></svg>'
        '<div class="maplabel">Map (sample)</div>'
    )


# --- cards ----------------------------------------------------------------------------
def mdi(icon: str | None) -> str:
    return f'<span class="mdi {(icon or "mdi:help").replace(":", "-")}"></span>'


def visible(card) -> bool:
    for cond in card.get("visibility") or []:
        state = STATES.get(cond.get("entity"), ("unknown",))[0]
        if "state" in cond and state != cond["state"]:
            return False
        if "state_not" in cond and state == cond["state_not"]:
            return False
    return True


def tile(card) -> str:
    entity = card["entity"]
    name = card.get("name") or STATES.get(entity, ("", {}))[1].get("friendly_name", entity)
    state = STATES.get(entity, ("unknown",))[0]
    color = TILE_COLOR.get(state, "#9e9e9e")
    feats = ""
    for feat in card.get("features") or []:
        if feat.get("type") == "toggle":
            on = state == "on"
            feats += f'<div class="toggle"><span class="{"sel" if on else ""}">On</span><span class="{"" if on else "sel"}">Off</span></div>'
        if feat.get("type") == "select-options":
            feats += f'<div class="selectbox">{fmt_state(entity)} <span class="mdi mdi-menu-down"></span></div>'
    return (f'<div class="card tile"><div class="ticon" style="color:{color};background:{color}22">{mdi(card.get("icon") or icon_for(entity))}</div>'
            f'<div><div class="tname">{html.escape(name)}</div><div class="tstate">{html.escape(fmt_state(entity))}</div></div>{feats}</div>')


def render_card(card) -> str:
    if not visible(card):
        return ""
    kind = card.get("type")
    if kind == "heading":
        return f'<div class="heading">{mdi(card.get("icon"))}<span>{html.escape(card["heading"])}</span></div>'
    if kind == "tile":
        return tile(card)
    if kind == "markdown":
        text = ENV.from_string(card.get("content", "")).render()
        return f'<div class="card markdown">{md(text)}</div>' if text.strip() else ""
    if kind == "button":
        small = (card.get("grid_options") or {}).get("rows") == 1
        return (f'<div class="card button {"small" if small else ""}">{mdi(card.get("icon"))}'
                f'<div>{html.escape(card.get("name", ""))}</div></div>')
    if kind == "picture-entity":
        entity = card["entity"]
        lens = "rear" if "rear" in entity else "front"
        caption = card.get("name") or lens.title()
        state = f'<span class="pstate">{html.escape(fmt_state(entity))}</span>' if card.get("show_state") else ""
        return f'<div class="card picture">{scene(lens, caption)}<div class="pbar"><span>{html.escape(caption)}</span>{state}</div></div>'
    if kind == "map":
        return f'<div class="card mapcard">{fake_map()}</div>'
    if kind == "history-graph":
        return '<div class="card markdown"><p>History graph</p></div>'
    if kind == "custom:auto-entities":
        pattern = card["filter"]["include"][0]["entity_id"].replace(".", r"\.").replace("*", ".*")
        matches = [e for e in STATES if re.fullmatch(pattern, e)]
        if not matches and card.get("show_empty") is False:
            return ""
        inner = card.get("card", {})
        if inner.get("type") == "grid":
            opts = card["filter"]["include"][0].get("options", {})
            return '<div class="grid2">' + "".join(render_card({**opts, "entity": e}) for e in matches) + "</div>"
        rows = "".join(
            f'<div class="erow">{mdi(icon_for(e))}<span>{html.escape(STATES[e][1].get("friendly_name", e))}</span>'
            f'<b>{html.escape(fmt_state(e))}</b></div>' for e in matches)
        title = f'<div class="etitle">{html.escape(inner["title"])}</div>' if inner.get("title") else ""
        return f'<div class="card entities">{title}{rows}</div>'
    return ""


CSS = """
body{margin:0;background:#111318;font-family:Roboto,'Segoe UI',Arial,sans-serif;color:#e1e1e1}
.top{display:flex;align-items:center;justify-content:space-between;padding:18px 22px;border-bottom:1px solid #2a2d33;background:#1c1f24}
.top h1{font-size:19px;margin:0;font-weight:500}.tabs span{margin-left:28px;font-size:14px;color:#9aa0a6}
.tabs span.on{color:#03a9f4;border-bottom:2px solid #03a9f4;padding-bottom:8px}
.cols{display:grid;gap:18px;padding:22px}
.section{display:flex;flex-direction:column;gap:10px}
.heading{display:flex;align-items:center;gap:8px;font-size:16px;font-weight:500;margin:6px 0 2px}
.heading .mdi{color:#03a9f4;font-size:20px}
.card{background:#1c1f24;border-radius:12px;overflow:hidden}
.tile{display:flex;align-items:center;gap:14px;padding:12px 14px;flex-wrap:wrap}
.ticon{width:38px;height:38px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:21px}
.tname{font-size:14px;font-weight:500}.tstate{font-size:13px;color:#9aa0a6;margin-top:2px}
.toggle{width:100%;display:flex;gap:6px}.toggle span{flex:1;text-align:center;padding:8px;border-radius:8px;background:#262a30;font-size:13px}
.toggle span.sel{background:#03a9f4;color:#fff}
.selectbox{width:100%;padding:8px 10px;border-radius:8px;background:#262a30;font-size:13px;display:flex;justify-content:space-between}
.markdown{padding:12px 16px;font-size:13px;line-height:1.5;color:#c8cbd0}.markdown p{margin:4px 0}
.markdown h3{margin:2px 0;font-size:17px;color:#fff}.markdown li{margin-left:16px}
.markdown table{border-collapse:collapse;width:100%;margin:4px 0}.markdown td,.markdown th{border-bottom:1px solid #2f333a;padding:5px;text-align:left}
code{background:#262a30;padding:1px 5px;border-radius:4px;font-size:12px}
.button{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;padding:18px 8px;font-size:13px}
.button .mdi{font-size:30px;color:#03a9f4}.button.small{flex-direction:row;padding:12px}.button.small .mdi{font-size:20px}
.btns{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.picture{position:relative;height:190px}.scene{width:100%;height:100%;display:block}
.osd{position:absolute;left:10px;bottom:40px;font:600 11px monospace;color:#fff;opacity:.85}
.pbar{position:absolute;left:0;right:0;bottom:0;background:rgba(0,0,0,.55);padding:9px 12px;font-size:13px;display:flex;justify-content:space-between}
.pstate{color:#bbb}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}.grid2 .picture{height:130px}
.mapcard{position:relative;height:300px}.map{width:100%;height:100%}.maplabel{position:absolute;right:10px;bottom:8px;font-size:11px;color:#777}
.entities{padding:8px 14px}.etitle{font-size:15px;font-weight:500;margin:6px 0}
.erow{display:flex;align-items:center;gap:12px;padding:8px 0;font-size:13px}.erow span{flex:1}.erow .mdi{font-size:20px;color:#9aa0a6;flex:0}
.note{text-align:right;padding:0 22px 14px;font-size:11px;color:#666}
"""


def render_view(cfg, index) -> str:
    view = cfg["views"][index]
    tabs = "".join(f'<span class="{"on" if i == index else ""}">{html.escape(v["title"])}</span>' for i, v in enumerate(cfg["views"]))
    sections = view["sections"]
    columns = min(view.get("max_columns", 2), len(sections)) or 1
    cols = []
    for sec in sections:
        cards, buttons = [], []
        for card in sec["cards"]:
            rendered = render_card(card)
            if not rendered:
                continue
            if card.get("type") == "button" and card is not sec["cards"][-1] and (card.get("grid_options") or {}).get("columns") == 6:
                buttons.append(rendered)
                continue
            if buttons:
                cards.append(f'<div class="btns">{"".join(buttons)}</div>')
                buttons = []
            cards.append(rendered)
        if buttons:
            cards.append(f'<div class="btns">{"".join(buttons)}</div>')
        span = 2 if sec.get("column_span") == 2 and columns > 1 else 1
        cols.append(f'<div class="section" style="grid-column:span {span}">{"".join(cards)}</div>')
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@mdi/font@7.4.47/css/materialdesignicons.min.css">'
        f'<style>{CSS}</style></head><body><div class="top"><h1>DDPAI</h1><div class="tabs">{tabs}</div></div>'
        f'<div class="cols" style="grid-template-columns:repeat({columns},1fr)">{"".join(cols)}</div>'
        '<div class="note">Sample data</div></body></html>'
    )


def trim(path: Path) -> None:
    """Crop the empty page background below the last card (keeps a margin)."""
    from PIL import Image

    image = Image.open(path).convert("RGB")
    width, height = image.size
    background = image.getpixel((5, height - 5))
    bottom = height
    for y in range(height - 1, 0, -4):
        row = image.crop((0, y, width - 120, y + 1)).getcolors(64) or [(1, (0, 0, 0)), (1, (1, 1, 1))]
        if len(row) > 1 or row[0][1] != background:
            bottom = min(height, y + 24)
            break
    image.crop((0, 0, width, max(bottom, 400))).save(path, optimize=True)


def browser() -> str:
    for path in (shutil.which("chrome"), shutil.which("google-chrome"), shutil.which("msedge"),
                 r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
        if path and os.path.exists(path):
            return path
    sys.exit("Chrome or Edge is needed")


def main() -> None:
    cfg = yaml.safe_load((HERE / "dashboard.yaml").read_text(encoding="utf-8"))
    OUT.mkdir(exist_ok=True)
    exe = browser()
    tmp = Path(tempfile.mkdtemp())
    for index, view in enumerate(cfg["views"]):
        page = tmp / f"{view['path']}.html"
        page.write_text(render_view(cfg, index), encoding="utf-8")
        target = OUT / f"dashboard-{view['path']}.png"
        subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={WIDTH},{HEIGHT}",
                        "--virtual-time-budget=4000", f"--screenshot={target}", page.as_uri()],
                       check=True, capture_output=True, timeout=60)
        trim(target)
        print("wrote", target.relative_to(HERE.parent))
        time.sleep(0.2)


if __name__ == "__main__":
    main()
