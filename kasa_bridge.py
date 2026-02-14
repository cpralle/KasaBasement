import asyncio
import json
import os
import sys
import colorsys
from datetime import datetime, timezone
from collections import deque
import math
import time
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException, Form, Header
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import StreamingResponse
from kasa import Discover, Module
from kasa.iot import IotDevice

# Patch kasa timezone lookup to fall back to UTC for unrecognized timezone keys
# (e.g. "PST8PDT" which some routers set on devices but isn't always in tzdata).
import kasa.cachedzoneinfo as _kasa_tz_mod
_orig_get_zone_info = _kasa_tz_mod._get_zone_info
def _safe_get_zone_info(time_zone_str):
    try:
        return _orig_get_zone_info(time_zone_str)
    except Exception:
        from zoneinfo import ZoneInfo
        return ZoneInfo("UTC")
_kasa_tz_mod._get_zone_info = _safe_get_zone_info

# Optional local-auth credentials for newer Kasa firmware (NOT the cloud UI)
KASA_USERNAME = os.getenv("KASA_USERNAME")
KASA_PASSWORD = os.getenv("KASA_PASSWORD")
KASA_INTERFACE = os.getenv("KASA_INTERFACE")  # optional, e.g. "192.168.1.10" or interface name depending on python-kasa
KASA_SCENE_DISCOVERY_TIMEOUT = int(os.getenv("KASA_SCENE_DISCOVERY_TIMEOUT", "2"))  # seconds, used when we must fall back to broadcast discovery (reduced for faster response)
KASA_HOST_CONNECT_TIMEOUT = float(os.getenv("KASA_HOST_CONNECT_TIMEOUT", "0.5"))  # seconds, fast-path connect to saved IP (reduced for faster response - 0.5s should be enough for local network)
KASA_STARTUP_DISCOVERY_TIMEOUT = int(os.getenv("KASA_STARTUP_DISCOVERY_TIMEOUT", "8"))
KASA_PERIODIC_DISCOVERY_INTERVAL = int(os.getenv("KASA_PERIODIC_DISCOVERY_INTERVAL", "120"))  # seconds
KASA_SETTINGS_DISCOVERY_TIMEOUT = int(os.getenv("KASA_SETTINGS_DISCOVERY_TIMEOUT", "15"))  # seconds; Settings scan should be thorough

# Optional simple shared-secret for external triggers (e.g., Flic Hub HTTP request)
SCENE_TRIGGER_TOKEN = os.getenv("SCENE_TRIGGER_TOKEN")

# --- Data Models ---
class DeviceConfig(BaseModel):
    alias: str
    mac: str  # MAC address as primary identifier
    host: Optional[str] = None  # Cached IP address (optional, will be resolved dynamically)
    type: str  # 'bulb', 'plug', 'strip'

class SceneAction(BaseModel):
    device_alias: str
    action: str  # 'on', 'off', 'toggle'
    params: Optional[dict] = None

class Scene(BaseModel):
    name: str
    actions: List[SceneAction]
    # Rooms-based mapping: scenes reference a room's grid map instead of owning one.
    room_idx: Optional[int] = None
    # Per-scene dimming profile for derived scenes (_d1.._d4)
    dim_profile: str = "linear"  # "linear" | "aggressive"
    # Backward-compat: legacy per-scene grid map (migrated to rooms on startup).
    grid_map: Optional[List[Optional[SceneAction]]] = None

class Room(BaseModel):
    name: str
    rows: int = 8
    cols: int = 8
    grid_map: Optional[List[Optional[SceneAction]]] = None
    # Remember last active *base* scene for this room (e.g. "Scene2", not "Scene2_d1")
    active_scene: Optional[str] = None
    # Remember last dim level requested for this room via /api/<room>/dimming_d1..d4
    # Used so /api/<room>/cycle can respect the current dial position.
    active_dim: str = "d4"

class RoutineAction(BaseModel):
    kind: str  # "scene" | "group"
    # scene
    scene_name: Optional[str] = None
    # group (room)
    room_idx: Optional[int] = None
    group_action: Optional[str] = None  # "on" | "off" | "toggle"

class Routine(BaseModel):
    name: str
    time_hhmm: str  # "HH:MM" local time, daily
    enabled: bool = True
    actions: List[RoutineAction]
    last_run_date: Optional[str] = None  # "YYYY-MM-DD" local date

class Config(BaseModel):
    devices: List[DeviceConfig]
    scenes: List[Scene]
    rooms: List[Room] = []
    routines: List[Routine] = []

# --- Validation helpers ---
def _normalize_hex_color(c: str) -> str:
    c = str(c or "").strip()
    if not c:
        return ""
    if not c.startswith("#"):
        c = "#" + c
    return c.lower()

def validate_scene_actions(actions: list[SceneAction]):
    """Validate and lightly normalize scene action params (brightness/color/color_temp)."""
    for a in actions:
        if not a.params:
            continue
        p = dict(a.params)
        # Mutually exclusive: color (RGB) vs color_temp (white mode)
        if p.get("color") and p.get("color_temp") is not None:
            raise ValueError(f"{a.device_alias}: choose either color or color_temp (not both)")

        if "brightness" in p and p["brightness"] is not None:
            try:
                b = int(p["brightness"])
            except Exception:
                raise ValueError(f"{a.device_alias}: brightness must be an integer 0-100")
            if b < 0 or b > 100:
                raise ValueError(f"{a.device_alias}: brightness must be 0-100")
            p["brightness"] = b

        if "color" in p and p["color"]:
            c = _normalize_hex_color(p["color"])
            if len(c) != 7:
                raise ValueError(f"{a.device_alias}: color must be #RRGGBB")
            try:
                int(c[1:], 16)
            except Exception:
                raise ValueError(f"{a.device_alias}: color must be #RRGGBB hex")
            p["color"] = c

        if "color_temp" in p and p["color_temp"] is not None:
            try:
                k = int(p["color_temp"])
            except Exception:
                raise ValueError(f"{a.device_alias}: color_temp must be an integer (Kelvin)")
            # Clamp here so the config stays sane
            p["color_temp"] = clamp_color_temp_k(k)

        # Write back normalized params
        a.params = p if p else None

# --- App Setup ---
app = FastAPI(title="Kasa Basement Bridge")

def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """
    Persistent app directory.
    - normal python: directory containing this file
    - PyInstaller exe: directory containing the exe
    """
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    """
    Directory where bundled resources live.
    - normal python: same as app_dir()
    - PyInstaller onefile: sys._MEIPASS temp extraction dir
    """
    if _is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return app_dir()


def _resolve_templates_dir() -> str:
    """
    Find templates directory, checking multiple locations:
    1. Bundled in PyInstaller _MEIPASS (onefile builds)
    2. Next to the executable/script (fallback for manual deployment)
    """
    # Try bundle_dir first (handles _MEIPASS for frozen builds)
    bundled = bundle_dir() / "templates"
    if bundled.is_dir():
        return str(bundled.resolve())
    # Fallback to app_dir (next to exe or script)
    app_local = app_dir() / "templates"
    if app_local.is_dir():
        return str(app_local.resolve())
    # Return bundled path anyway (will fail with clear error if missing)
    return str(bundled.resolve())


TEMPLATES_DIR = _resolve_templates_dir()
CONFIG_PATH = str((app_dir() / "config.json").resolve())
_BUNDLED_CONFIG_PATH = str((bundle_dir() / "config.json").resolve())

# Initialize templates and add 'enumerate' to globals
templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals.update(enumerate=enumerate)

# Only create templates dir in dev mode (never in a frozen exe)
if (not _is_frozen()) and (not os.path.exists(TEMPLATES_DIR)):
    os.makedirs(TEMPLATES_DIR, exist_ok=True)

def load_config() -> Config:
    # If running as an exe and config.json isn't next to the exe yet, seed it from the bundled copy.
    if _is_frozen() and (not os.path.exists(CONFIG_PATH)) and os.path.exists(_BUNDLED_CONFIG_PATH):
        try:
            with open(_BUNDLED_CONFIG_PATH, "r") as src:
                data = src.read()
            with open(CONFIG_PATH, "w") as dst:
                dst.write(data)
        except Exception:
            pass

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                return Config(**json.load(f))
        except Exception:
            return Config(devices=[], scenes=[])
    return Config(devices=[], scenes=[])

def save_config(config: Config):
    with open(CONFIG_PATH, "w") as f:
        f.write(config.model_dump_json(indent=4))

config = load_config()

# Cache for last discovery results
last_discovery_cache = None
last_discovery_at: Optional[datetime] = None
last_discovery_error: Optional[str] = None
last_discovery_debug: Optional[dict] = None
discovery_lock = asyncio.Lock()
periodic_discovery_task: Optional[asyncio.Task] = None
routine_scheduler_task: Optional[asyncio.Task] = None

# Cache for device connections to avoid reconnecting on every button press
_device_connection_cache: dict[str, tuple[IotDevice, datetime]] = {}
_device_cache_lock = asyncio.Lock()
_device_cache_ttl = 30  # seconds - cache device connections for 30 seconds

# --- Last scene run (for Diagnostics UI) ---
last_scene_run: Optional[dict] = None
last_scene_run_lock = asyncio.Lock()

# --- Scene execution serialization ---
# Multiple triggers can arrive back-to-back (dashboard click + room cycle + routines).
# Without serialization, two scene runs can overlap and "fight", making it look like
# a trigger did nothing (its changes get immediately overwritten by the other run).
scene_execution_lock = asyncio.Lock()

# --- Diagnostics / Event logging ---
EVENT_LOG_MAX = int(os.getenv("EVENT_LOG_MAX", "200"))
event_log = deque(maxlen=EVENT_LOG_MAX)  # newest appended to the right
event_log_lock = asyncio.Lock()

# --- Simple server-sent events (SSE) bus for cross-tab updates ---
sse_clients: set[asyncio.Queue] = set()
sse_lock = asyncio.Lock()

async def publish_sse(event_type: str, payload: dict):
    msg = {"ts": datetime.now(timezone.utc).isoformat(), "type": event_type, "payload": payload}
    async with sse_lock:
        clients = list(sse_clients)
    # best-effort fanout
    for q in clients:
        try:
            q.put_nowait(msg)
        except Exception:
            pass

async def log_event(
    event_type: str,
    request: Optional[Request],
    details: Optional[dict] = None,
    *,
    client: Optional[str] = None,
    path: Optional[str] = None,
):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "client": client or (request.client.host if request and request.client else None),
        "path": path or (str(request.url.path) if request else None),
        "details": details or {},
    }
    async with event_log_lock:
        event_log.append(entry)

def ensure_test_scene():
    """Ensure a TestScene exists (empty actions) to validate Flic connectivity safely."""
    exists = any(s.name.lower() == "testscene" for s in config.scenes)
    if exists:
        return
    config.scenes.append(Scene(name="TestScene", actions=[]))
    save_config(config)

def _unique_room_name(base: str) -> str:
    base = (base or "Room").strip() or "Room"
    existing = {r.name.lower() for r in config.rooms}
    if base.lower() not in existing:
        return base
    i = 2
    while True:
        name = f"{base} {i}"
        if name.lower() not in existing:
            return name
        i += 1

def migrate_scene_maps_to_rooms():
    """
    One-time migration:
    - If a scene has legacy grid_map and no room_idx, create a room with that grid_map.
    - Assign scene.room_idx to the new room.
    - Clear scene.grid_map (legacy) so we don't keep duplicating data.
    """
    changed = False
    for s in config.scenes:
        if s.grid_map and s.room_idx is None:
            room_name = _unique_room_name(s.name)
            config.rooms.append(Room(name=room_name, grid_map=s.grid_map))
            s.room_idx = len(config.rooms) - 1
            s.grid_map = None
            changed = True
    if changed:
        save_config(config)

KASA_STATUS_CACHE_TTL = float(os.getenv("KASA_STATUS_CACHE_TTL", "3.0"))  # seconds
KASA_STATUS_UPDATE_TIMEOUT = float(os.getenv("KASA_STATUS_UPDATE_TIMEOUT", "2.5"))  # seconds per device update
device_status_cache = {"ts": 0.0, "data": []}
device_status_lock = asyncio.Lock()

# --- Helper Functions ---

def normalize_mac(mac: str) -> str:
    """Normalize MAC address to lowercase with colons."""
    mac_clean = mac.lower().replace(":", "").replace("-", "")
    if len(mac_clean) == 12:
        return ":".join([mac_clean[i:i+2] for i in range(0, 12, 2)])
    return mac.lower()

async def kasa_discover(timeout: int = 15):
    """
    Wrapper around python-kasa discovery.
    Tries to use optional credentials/interface when provided, and falls back
    gracefully if the installed python-kasa version doesn't support the args.
    """
    kwargs = {"discovery_timeout": timeout}
    if KASA_INTERFACE:
        kwargs["interface"] = KASA_INTERFACE
    if KASA_USERNAME and KASA_PASSWORD:
        kwargs["username"] = KASA_USERNAME
        kwargs["password"] = KASA_PASSWORD

    try:
        return await Discover.discover(**kwargs)
    except TypeError:
        # Fallback for older python-kasa versions that don't accept username/interface kwargs
        return await Discover.discover(discovery_timeout=timeout)

async def kasa_discover_single(host: str):
    """
    Best-effort single-device lookup by IP/host.
    Uses optional credentials/interface when supported by the installed python-kasa.
    """
    kwargs = {}
    if KASA_INTERFACE:
        kwargs["interface"] = KASA_INTERFACE
    if KASA_USERNAME and KASA_PASSWORD:
        kwargs["username"] = KASA_USERNAME
        kwargs["password"] = KASA_PASSWORD
    try:
        return await asyncio.wait_for(Discover.discover_single(host, **kwargs), timeout=KASA_HOST_CONNECT_TIMEOUT)
    except TypeError:
        # Older python-kasa versions may not support kwargs here
        return await asyncio.wait_for(Discover.discover_single(host), timeout=KASA_HOST_CONNECT_TIMEOUT)

async def discover_devices(timeout: int = 15) -> dict:
    """Discover all Kasa devices on the network and return a dict mapping MAC to device info."""
    global last_discovery_cache
    devices = {}
    try:
        # Increase timeout to find more devices on larger networks
        found_devices = await kasa_discover(timeout=timeout)
        # Some devices don't expose MAC until an update/auth handshake; fall back to direct connect by host.
        async def resolve_one(dev: IotDevice):
            host = getattr(dev, "host", None)
            try:
                await dev.update()
            except Exception:
                pass

            mac = getattr(dev, "mac", None)
            # If MAC is missing but we have an IP/host, try a direct lookup (often works better than broadcast object).
            if (not mac) and host:
                try:
                    direct = await kasa_discover_single(host)
                    try:
                        await direct.update()
                    except Exception:
                        pass
                    mac = getattr(direct, "mac", None) or mac
                    # Prefer richer metadata if direct connect succeeded.
                    if getattr(direct, "alias", None):
                        setattr(dev, "alias", getattr(direct, "alias"))
                    if getattr(direct, "device_type", None):
                        setattr(dev, "device_type", getattr(direct, "device_type"))
                    if get_light_module(direct) is not None or get_switch_module(direct) is not None:
                        # swap reference for type inference if modules are clearer
                        dev = direct
                except Exception:
                    pass

            if mac:
                mac_normalized = normalize_mac(mac)
                return mac_normalized, {
                    "alias": getattr(dev, "alias", mac_normalized),
                    "host": host,
                    "mac": mac_normalized,
                    "type": infer_config_device_type(dev),
                }
            return None

        results = await asyncio.gather(*(resolve_one(dev) for dev in found_devices.values()))
        for item in results:
            if not item:
                continue
            mac_normalized, info = item
            devices[mac_normalized] = info
        # Cache the results
        last_discovery_cache = devices
    except Exception as e:
        print(f"Discovery error: {e}")
    return devices

async def refresh_discovery_cache(*, timeout: int, update_config_hosts: bool) -> dict:
    """
    Refresh discovery cache and (optionally) update config.devices[].host based on MAC.
    This is the mechanism that keeps MAC->IP up to date across DHCP/network changes.
    """
    global last_discovery_cache, last_discovery_at, last_discovery_error, last_discovery_debug

    async with discovery_lock:
        try:
            discovered = await kasa_discover(timeout=timeout)
            new_cache: dict[str, dict] = {}
            debug = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "timeout": timeout,
                "broadcast_count": len(discovered),
                "broadcast_missing_mac": 0,
                "fallback_attempted": 0,
                "fallback_recovered": 0,
                "unrecovered": [],  # list of {alias,host,device_type}
            }

            async def resolve_one(dev: IotDevice):
                host = getattr(dev, "host", None)
                try:
                    await dev.update()
                except Exception:
                    pass

                mac = getattr(dev, "mac", None)
                if (not mac) and host:
                    # Some devices only yield MAC/metadata on direct connect.
                    debug["broadcast_missing_mac"] += 1
                    debug["fallback_attempted"] += 1
                    try:
                        direct = await kasa_discover_single(host)
                        try:
                            await direct.update()
                        except Exception:
                            pass
                        mac = getattr(direct, "mac", None) or mac
                        if mac:
                            debug["fallback_recovered"] += 1
                        # Prefer richer metadata if direct connect succeeded.
                        if getattr(direct, "alias", None):
                            setattr(dev, "alias", getattr(direct, "alias"))
                        if getattr(direct, "device_type", None):
                            setattr(dev, "device_type", getattr(direct, "device_type"))
                        if get_light_module(direct) is not None or get_switch_module(direct) is not None:
                            dev = direct
                    except Exception:
                        pass

                if not mac:
                    # Still couldn't resolve MAC -> can't show/add in UI; record for debugging.
                    if host:
                        try:
                            dt = getattr(getattr(dev, "device_type", None), "name", None)
                        except Exception:
                            dt = None
                        if len(debug["unrecovered"]) < 50:
                            debug["unrecovered"].append(
                                {"alias": getattr(dev, "alias", None), "host": host, "device_type": dt}
                            )
                    return None

                mac_norm = normalize_mac(mac)
                return mac_norm, {
                    "alias": getattr(dev, "alias", mac_norm),
                    "host": host,
                    "mac": mac_norm,
                    "type": infer_config_device_type(dev),
                }

            results = await asyncio.gather(*(resolve_one(dev) for dev in discovered.values()))
            for item in results:
                if not item:
                    continue
                mac_norm, info = item
                new_cache[mac_norm] = info

            last_discovery_cache = new_cache
            last_discovery_at = datetime.now(timezone.utc)
            last_discovery_error = None
            debug["cache_count"] = len(new_cache)
            last_discovery_debug = debug

            updated_hosts = 0
            if update_config_hosts:
                for dev_cfg in config.devices:
                    info = new_cache.get(normalize_mac(dev_cfg.mac))
                    if info and info.get("host") and dev_cfg.host != info["host"]:
                        dev_cfg.host = info["host"]
                        updated_hosts += 1
                if updated_hosts:
                    save_config(config)

            return {
                "status": "success",
                "discovered_count": len(new_cache),
                "updated_hosts": updated_hosts,
                "last_discovery_at": last_discovery_at.isoformat(),
                "debug": debug,
            }
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_discovery_error = str(e)
            return {"status": "error", "error": last_discovery_error}

async def periodic_discovery_loop():
    """Background refresh to handle DHCP/network changes without restart/manual scan."""
    while True:
        try:
            await refresh_discovery_cache(timeout=KASA_SCENE_DISCOVERY_TIMEOUT, update_config_hosts=True)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(KASA_PERIODIC_DISCOVERY_INTERVAL)

def verify_trigger_token(token: Optional[str]):
    """If SCENE_TRIGGER_TOKEN is set, require a matching token (query param or header)."""
    if not SCENE_TRIGGER_TOKEN:
        return
    if not token or token != SCENE_TRIGGER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


def _summarize_scene_results(res: dict) -> dict:
    results = (res or {}).get("results") or []
    ok = [r for r in results if r.get("status") == "success"]
    err = [r for r in results if r.get("status") != "success"]
    return {
        "scene": (res or {}).get("scene"),
        "device_success": len(ok),
        "device_error": len(err),
        "sample_errors": [{"device": e.get("device"), "message": e.get("message")} for e in err[:10]],
    }


async def _record_last_scene_run(*, request: Request, trigger: str, scene_name: str, res: Optional[dict] = None, error: Optional[str] = None):
    global last_scene_run
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        "scene_name": scene_name,
        "client": request.client.host if request and request.client else None,
        "path": str(request.url.path) if request else None,
        "ok": error is None,
        "error": error,
        "result": res,
        "summary": _summarize_scene_results(res) if res else None,
    }
    async with last_scene_run_lock:
        last_scene_run = entry
    # Also put it in the event log for troubleshooting/history.
    await log_event(
        "scene_run_result" if error is None else "scene_run_error",
        request,
        {"trigger": trigger, "scene_name": scene_name, "error": error, "summary": entry["summary"]},
    )

async def execute_scene(scene: Scene) -> dict:
    """
    Core scene execution logic used by dashboard + external triggers.
    Returns the same shape the dashboard expects: {status, scene, results}.
    """
    async with scene_execution_lock:
        # If this scene (or its dim variant) corresponds to a configured scene mapped to a room,
        # remember it as the room's active base scene so room dimming endpoints can work.
        try:
            base_name, _suffix = parse_dim_suffix(scene.name)
            base_scene = next((s for s in config.scenes if s.name.lower() == base_name.lower()), None)
            if base_scene and base_scene.room_idx is not None:
                if 0 <= base_scene.room_idx < len(config.rooms):
                    config.rooms[base_scene.room_idx].active_scene = base_scene.name
                    save_config(config)
        except Exception:
            pass

        # Broadcast target immediately so maps can update optimistically.
        await publish_sse("scene_target", {"scene": scene.name, "actions": [a.model_dump() for a in scene.actions]})

        results: list[dict] = []
        mac_to_device: dict[str, IotDevice] = {}

        async def run_one_action(action: SceneAction) -> dict:
            device_config = next((d for d in config.devices if d.alias == action.device_alias), None)
            if not device_config:
                return {"device": action.device_alias, "status": "error", "message": "Device not found in config"}

            try:
                device, source = await resolve_device_for_config(device_config, mac_to_device)
                if not device:
                    msg = "Device not reachable"
                    if device_config.host:
                        msg += f" (tried host {device_config.host} + MAC discovery)"
                    else:
                        msg += " (no cached host; MAC discovery required)"
                    return {"device": action.device_alias, "status": "error", "message": msg}

                # If this is a derived dim scene (e.g. *_d1/_d2/_d3), we may need to dim bulbs even when the
                # scene action omitted brightness (common when user only set color or on/off).
                # We do this by reading current brightness and scaling it.
                base_name, dim_suffix = parse_dim_suffix(scene.name)
                dim_mult = None
                if dim_suffix is not None and dim_suffix != "_d4":
                    base_scene_for_dim = next((s for s in config.scenes if s.name.lower() == base_name.lower()), None)
                    if base_scene_for_dim:
                        dim_mult = dim_multiplier(base_scene_for_dim, dim_suffix)
                if (
                    dim_mult is not None and dim_mult < 1.0
                    and action.action == "on"
                    and str(device_config.type).lower() == "bulb"
                ):
                    try:
                        p = dict(action.params or {})
                        if "brightness" not in p or p.get("brightness") is None:
                            # resolve_device_for_config already called update(), state is fresh
                            state = read_light_state(device) or {}
                            current_brightness = state.get("brightness")
                            if current_brightness is not None:
                                sb = scaled_brightness(int(current_brightness), float(dim_mult))
                                # If dimming says "off", convert the action accordingly
                                if sb == 0:
                                    action = SceneAction(device_alias=action.device_alias, action="off", params=None)
                                else:
                                    p["brightness"] = sb
                                    action = SceneAction(device_alias=action.device_alias, action="on", params=p)
                    except Exception:
                        # Best effort only; fall back to original action
                        pass

                if action.action == "toggle":
                    await toggle_device_power(device, skip_update=True)  # resolve_device_for_config already updated
                elif action.action == "on":
                    await device.turn_on()
                elif action.action == "off":
                    await device.turn_off()
                else:
                    return {"device": action.device_alias, "status": "error", "message": f"Unknown action: {action.action}"}

                if action.action == "on" and str(device_config.type).lower() == "bulb" and action.params:
                    light = get_light_module(device)
                    # White mode / color temperature: if provided, do NOT force RGB color mode.
                    if "color_temp" in action.params and action.params["color_temp"] is not None:
                        try:
                            await set_light_color_temp_k(device, kelvin=int(action.params["color_temp"]))
                        except Exception as e:
                            return {"device": action.device_alias, "status": "error", "message": f"color_temp failed: {e}"}
                    # IMPORTANT: set color FIRST, then brightness.
                    # Setting HSV often implicitly sets "value" (brightness) which can override a dimmed brightness
                    # and/or force brightness to 100 if we derive V from a full-brightness color swatch.
                    applied_brightness_via_hsv = False
                    if ("color_temp" not in action.params or action.params.get("color_temp") is None) and "color" in action.params and action.params["color"]:
                        hex_color = str(action.params["color"]).lstrip("#")
                        if len(hex_color) != 6:
                            raise ValueError("color must be 6 hex digits (e.g. #FF00AA)")
                        rgb = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
                        hsv = colorsys.rgb_to_hsv(rgb[0]/255.0, rgb[1]/255.0, rgb[2]/255.0)
                        hue = int(hsv[0] * 360)
                        saturation = int(hsv[1] * 100)
                        # If brightness is explicitly provided, apply it via HSV "value" to avoid an extra network call.
                        target_brightness = None
                        if "brightness" in action.params and action.params["brightness"] is not None:
                            try:
                                target_brightness = max(0, min(100, int(action.params["brightness"])))
                            except Exception:
                                target_brightness = None

                        if target_brightness is not None:
                            value = int(target_brightness)
                            applied_brightness_via_hsv = True
                        else:
                            # Preserve current brightness when applying color-only.
                            # resolve_device_for_config already called update(), state is fresh
                            current_brightness = None
                            try:
                                state = read_light_state(device) or {}
                                if isinstance(state, dict):
                                    current_brightness = state.get("brightness")
                            except Exception:
                                pass
                            value = int((current_brightness if current_brightness is not None else 100))
                        if light and getattr(light, "has_feature", None) and light.has_feature("hsv") and getattr(light, "set_hsv", None):
                            await light.set_hsv(hue, saturation, value)
                        else:
                            await device.set_hsv(hue, saturation, value)

                    # If we already applied brightness via HSV above, don't send a second brightness command.
                    if (not applied_brightness_via_hsv) and "brightness" in action.params and action.params["brightness"] is not None:
                        b = max(0, min(100, int(action.params["brightness"])))
                        if light and getattr(light, "set_brightness", None):
                            await light.set_brightness(b)
                        else:
                            await device.set_brightness(b)

                if device_config.host != device.host:
                    device_config.host = device.host

                return {"device": action.device_alias, "status": "success", "source": source, "host": device.host}
            except Exception as e:
                return {"device": action.device_alias, "status": "error", "message": str(e)}

        tasks = [run_one_action(a) for a in scene.actions]
        results = await asyncio.gather(*tasks)

        needs_discovery = any(
            r.get("status") == "error" and "MAC discovery" in str(r.get("message", ""))
            for r in results
        )
        if needs_discovery:
            try:
                discovered = await kasa_discover(timeout=KASA_SCENE_DISCOVERY_TIMEOUT)
                for dev in discovered.values():
                    try:
                        await dev.update()
                    except Exception:
                        pass
                    if getattr(dev, "mac", None):
                        mac_to_device[normalize_mac(dev.mac)] = dev
            except asyncio.CancelledError:
                raise HTTPException(status_code=499, detail="Request cancelled during device discovery")
            except Exception:
                pass

            retry_actions = [
                a for a, r in zip(scene.actions, results)
                if r.get("status") == "error" and "MAC discovery" in str(r.get("message", ""))
            ]
            if retry_actions:
                retry_results = await asyncio.gather(*[run_one_action(a) for a in retry_actions])
                it = iter(retry_results)
                results = [
                    (next(it) if (r.get("status") == "error" and "MAC discovery" in str(r.get("message", ""))) else r)
                    for r in results
                ]

        save_config(config)
        out = {"status": "success", "scene": scene.name, "results": results}
        # Broadcast event for map/dashboard syncing (multi-screen use)
        await publish_sse("scene_run", {"scene": scene.name, "results": results})
        return out

# --- Derived dim scenes (virtual; not stored in config / not shown in UI) ---

DIM_SUFFIXES = ("_d1", "_d2", "_d3", "_d4")
TOGGLE_SUFFIX = "_toggle"
DIM_PROFILES: dict[str, dict[str, float]] = {
    "linear": {"_d1": 0.25, "_d2": 0.50, "_d3": 0.75, "_d4": 1.00},
    "aggressive": {"_d1": 0.10, "_d2": 0.20, "_d3": 0.50, "_d4": 1.00},
}

def parse_dim_suffix(scene_name: str) -> tuple[str, Optional[str]]:
    """Return (base_name, suffix) if scene_name ends with _d1/_d2/_d3/_d4, else (scene_name, None)."""
    name = scene_name.strip()
    lower = name.lower()
    for suffix in DIM_SUFFIXES:
        if lower.endswith(suffix):
            return name[: -len(suffix)], suffix
    return name, None

def parse_toggle_suffix(scene_name: str) -> tuple[str, bool]:
    """Return (base_name, True) if scene_name ends with _toggle (case-insensitive)."""
    name = scene_name.strip()
    lower = name.lower()
    if lower.endswith(TOGGLE_SUFFIX):
        return name[: -len(TOGGLE_SUFFIX)], True
    return name, False

def dim_multiplier(scene: Scene, suffix: str) -> float:
    prof = (scene.dim_profile or "linear").strip().lower()
    table = DIM_PROFILES.get(prof) or DIM_PROFILES["linear"]
    return float(table.get(suffix, 1.0))

def is_reserved_scene_name(scene_name: str) -> bool:
    """Names reserved for virtual triggers (derived dim scenes + toggle)."""
    _, dim_suffix = parse_dim_suffix(scene_name)
    if dim_suffix is not None:
        return True
    _, is_toggle = parse_toggle_suffix(scene_name)
    return is_toggle

def is_derived_scene_name(scene_name: str) -> bool:
    return is_reserved_scene_name(scene_name)

def is_visible_scene(scene: Scene) -> bool:
    # Derived dim scenes are not visible/editable in the UI.
    return not is_derived_scene_name(scene.name)

def ensure_grid_map(obj) -> list[Optional[dict]]:
    """
    Normalize a .grid_map to a JSON-serializable list of length 64 containing dicts or None.
    """
    grid = getattr(obj, "grid_map", None) or []
    out: list[Optional[dict]] = []
    for cell in grid:
        if cell is None:
            out.append(None)
        else:
            # cell is SceneAction (pydantic) or dict
            if isinstance(cell, SceneAction):
                out.append(cell.model_dump())
            else:
                out.append(dict(cell))
    # pad/trim to configured size if present, else 64
    target = 64
    rows = getattr(obj, "rows", None)
    cols = getattr(obj, "cols", None)
    if isinstance(rows, int) and isinstance(cols, int) and rows > 0 and cols > 0:
        target = rows * cols
    if len(out) < target:
        out.extend([None] * (target - len(out)))
    elif len(out) > target:
        out = out[:target]
    return out

def _used_bounds(grid: list[Optional[SceneAction]], cols: int) -> tuple[int, int]:
    """Return (min_rows_needed, min_cols_needed) based on occupied cells."""
    max_r = -1
    max_c = -1
    for idx, cell in enumerate(grid):
        if cell is None:
            continue
        r = idx // cols
        c = idx % cols
        max_r = max(max_r, r)
        max_c = max(max_c, c)
    return (max_r + 1, max_c + 1)  # if none occupied => (0,0)

def _remap_grid(old_grid: list[Optional[SceneAction]], old_rows: int, old_cols: int, new_rows: int, new_cols: int) -> list[Optional[SceneAction]]:
    """Preserve (row,col) positions when resizing."""
    new_grid: list[Optional[SceneAction]] = [None] * (new_rows * new_cols)
    for idx, cell in enumerate(old_grid):
        if cell is None:
            continue
        r = idx // old_cols
        c = idx % old_cols
        if r >= new_rows or c >= new_cols:
            continue
        new_grid[r * new_cols + c] = cell
    return new_grid

def _remove_device_from_all_rooms(device_alias: str, *, except_room_idx: int, except_cell_idx: int):
    """Ensure a device alias appears in only one tile across all rooms."""
    alias_lower = device_alias.lower()
    for r_idx, room in enumerate(config.rooms):
        if not room.grid_map:
            continue
        new_grid: list[Optional[SceneAction]] = []
        changed = False
        for c_idx, cell in enumerate(room.grid_map):
            if cell is None:
                new_grid.append(None)
                continue
            if r_idx == except_room_idx and c_idx == except_cell_idx:
                new_grid.append(cell)
                continue
            if cell.device_alias.lower() == alias_lower:
                new_grid.append(None)
                changed = True
            else:
                new_grid.append(cell)
        if changed:
            room.grid_map = new_grid

def _room_device_aliases(room: Room) -> list[str]:
    """Return unique device aliases referenced by a room map."""
    aliases: list[str] = []
    seen: set[str] = set()
    grid = room.grid_map or []
    for cell in grid:
        if not cell:
            continue
        alias = cell.device_alias
        key = alias.lower()
        if key in seen:
            continue
        seen.add(key)
        aliases.append(alias)
    return aliases

def _find_scene_by_name_or_dim(name: str) -> Optional[Scene]:
    """Resolve a scene by name, supporting virtual _d1/_d2/_d3 dim suffix."""
    base_name, suffix = parse_dim_suffix(name)
    base_scene = next((s for s in config.scenes if s.name.lower() == base_name.lower()), None)
    if not base_scene:
        return None
    if suffix is None:
        return base_scene
    return derive_dimmed_scene(base_scene, suffix, dim_multiplier(base_scene, suffix))

async def is_scene_any_device_on(scene: Scene) -> bool:
    """Return True if any device referenced by the scene is currently on (best-effort)."""
    alias_to_cfg = {d.alias: d for d in config.devices}
    targets = [alias_to_cfg.get(a.device_alias) for a in scene.actions]
    targets = [t for t in targets if t]
    if not targets:
        return False

    mac_to_device: dict[str, IotDevice] = {}

    async def check_one(dev_cfg: DeviceConfig) -> bool:
        try:
            device, _source = await resolve_device_for_config(dev_cfg, mac_to_device)
            if not device:
                return False
            # resolve_device_for_config already calls update(), so state is fresh
            return bool(read_device_is_on(device))
        except Exception:
            return False

    # Use a very short overall timeout for the entire state check - don't block button presses!
    try:
        states = await asyncio.wait_for(asyncio.gather(*[check_one(d) for d in targets]), timeout=0.8)
        return any(states)
    except asyncio.TimeoutError:
        # If state check times out, assume off to allow toggle to proceed
        return False


async def is_scene_representative_on(scene: Scene) -> bool:
    """
    Fast heuristic for toggle: assume the scene's devices generally move together and
    check only ONE representative device (first configured action device we can resolve).
    This avoids N device updates on every button press.
    """
    alias_to_cfg = {d.alias: d for d in config.devices}
    mac_to_device: dict[str, IotDevice] = {}

    for a in scene.actions:
        dev_cfg = alias_to_cfg.get(a.device_alias)
        if not dev_cfg:
            continue
        try:
            device, _source = await resolve_device_for_config(dev_cfg, mac_to_device)
            if not device:
                continue
            # resolve_device_for_config already calls update(), so state is fresh
            return bool(read_device_is_on(device))
        except Exception:
            continue

    return False

async def run_routine(routine_idx: int) -> dict:
    """Execute a routine (manual or scheduled)."""
    if routine_idx < 0 or routine_idx >= len(config.routines):
        raise HTTPException(status_code=404, detail="Routine not found")
    r = config.routines[routine_idx]
    results: list[dict] = []
    for act in r.actions:
        if act.kind == "scene":
            if not act.scene_name:
                results.append({"kind": "scene", "status": "error", "message": "Missing scene_name"})
                continue
            scene = _find_scene_by_name_or_dim(act.scene_name)
            if not scene:
                results.append({"kind": "scene", "status": "error", "message": f"Scene not found: {act.scene_name}"})
                continue
            res = await execute_scene(scene)
            results.append({"kind": "scene", "scene": act.scene_name, "status": "success", "result": res})
        elif act.kind == "group":
            if act.room_idx is None or act.room_idx < 0 or act.room_idx >= len(config.rooms):
                results.append({"kind": "group", "status": "error", "message": "Invalid room"})
                continue
            if act.group_action not in ("on", "off", "toggle"):
                results.append({"kind": "group", "status": "error", "message": "Invalid group_action"})
                continue
            room = config.rooms[act.room_idx]
            aliases = _room_device_aliases(room)
            actions = [SceneAction(device_alias=a, action=act.group_action, params=None) for a in aliases]
            virtual = Scene(name=f"{r.name}:{room.name}:{act.group_action}", actions=actions)
            res = await execute_scene(virtual)
            results.append({"kind": "group", "room": room.name, "action": act.group_action, "status": "success", "result": res})
        else:
            results.append({"kind": act.kind, "status": "error", "message": "Unknown action kind"})

    # mark last run date (local)
    r.last_run_date = datetime.now().date().isoformat()
    save_config(config)
    await publish_sse("routine_run", {"routine": r.name, "results": results})
    return {"status": "success", "routine": r.name, "results": results}

async def routine_scheduler_loop():
    """Background loop: run enabled routines daily at HH:MM (local time)."""
    while True:
        try:
            now = datetime.now()
            today = now.date().isoformat()
            hhmm = now.strftime("%H:%M")
            for idx, r in enumerate(config.routines):
                if not r.enabled:
                    continue
                if r.time_hhmm != hhmm:
                    continue
                if r.last_run_date == today:
                    continue
                # Mark as run IMMEDIATELY to prevent double-triggering if loop runs again
                # before the background task completes
                r.last_run_date = today
                save_config(config)
                # Capture idx in a default argument to avoid race condition if config.routines
                # is modified before the task executes
                asyncio.create_task(run_routine(idx))
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(5)

def scaled_brightness(parent_brightness: int, dim_mult: float) -> int:
    """
    Apply dim multiplier to brightness with rounding and special-case behavior:
    - brightness is 0-100
    - round half up
    - if parent_brightness == 1 and dim_mult < 1.0 -> treat as off (return 0)
    - otherwise clamp to at least 1 when dimmed result is non-zero
    """
    parent_brightness = max(0, min(100, int(parent_brightness)))
    if parent_brightness == 0:
        return 0
    if parent_brightness == 1 and dim_mult < 1.0:
        return 0

    raw = parent_brightness * dim_mult
    # round half up
    rounded = int(math.floor(raw + 0.5))
    if rounded <= 0:
        return 1
    return max(1, min(100, rounded))

def derive_dimmed_scene(base_scene: Scene, suffix: str, dim_mult: float) -> Scene:
    """
    Build a derived Scene object where bulb brightness is scaled.
    If a bulb brightness scales to 0 => that device action becomes 'off'.
    """
    new_actions: list[SceneAction] = []
    for a in base_scene.actions:
        # Copy action shallowly
        action_dict = a.model_dump()
        params = dict(action_dict.get("params") or {})

        # Only scale for bulb actions that are "on" and have an explicit brightness param.
        if a.action == "on" and "brightness" in params and params["brightness"] is not None:
            try:
                pb = int(params["brightness"])
                sb = scaled_brightness(pb, dim_mult)
                if sb == 0:
                    action_dict["action"] = "off"
                    action_dict["params"] = None
                else:
                    params["brightness"] = sb
                    action_dict["params"] = params
            except Exception:
                # If brightness isn't parseable, leave it untouched.
                action_dict["params"] = params if params else None
        else:
            action_dict["params"] = params if params else None

        new_actions.append(SceneAction(**action_dict))

    # Preserve room mapping so dim runs still count as "active scene" for that room.
    return Scene(name=f"{base_scene.name}{suffix}", actions=new_actions, room_idx=base_scene.room_idx)

async def get_device_by_mac(mac: str) -> Optional[IotDevice]:
    """Find and return a device by MAC address."""
    mac_normalized = normalize_mac(mac)
    try:
        found_devices = await kasa_discover(timeout=15)
        for dev in found_devices.values():
            await dev.update()
            if dev.mac:
                dev_mac_normalized = normalize_mac(dev.mac)
                if dev_mac_normalized == mac_normalized:
                    return dev
    except asyncio.CancelledError:
        # Let callers decide how to handle request cancellation/shutdown.
        raise
    except Exception as e:
        print(f"Error finding device by MAC {mac}: {e}")
    return None

def get_light_module(device: IotDevice):
    """
    Best-effort accessor for the Light module to avoid deprecated device-level helpers.
    Returns None if the device doesn't expose a Light module.
    """
    try:
        return device.modules.get(Module.Light)
    except Exception:
        return None

def get_switch_module(device: IotDevice):
    """Best-effort accessor for the Switch module."""
    try:
        return device.modules.get(Module.Switch)
    except Exception:
        return None


def infer_config_device_type(device: IotDevice) -> str:
    """
    Normalize python-kasa device information into this app's config types:
    - 'bulb' (Light module)
    - 'strip' (power strip / has children)
    - 'plug' (switch/outlet)
    Defaults to 'plug' for unknown switch-like devices so the UI/control path is safe.
    """
    try:
        if get_light_module(device) is not None:
            return "bulb"
    except Exception:
        pass

    # Power strips often expose children outlets
    try:
        children = getattr(device, "children", None)
        if children:
            return "strip"
    except Exception:
        pass

    try:
        if get_switch_module(device) is not None:
            return "plug"
    except Exception:
        pass

    # Fallback: inspect reported device_type name if present
    try:
        dt = getattr(getattr(device, "device_type", None), "name", "") or ""
        dt_lower = str(dt).lower()
        if "strip" in dt_lower:
            return "strip"
        if "bulb" in dt_lower or "light" in dt_lower:
            return "bulb"
        if "plug" in dt_lower or "switch" in dt_lower or "outlet" in dt_lower:
            return "plug"
    except Exception:
        pass

    return "plug"

async def toggle_device_power(device: IotDevice, *, skip_update: bool = False):
    """
    Toggle device power in a way that works across python-kasa device/module APIs.
    Some devices don't implement device.toggle(); prefer reading is_on + turn_on/off.

    Args:
        skip_update: If True, skip the device.update() call (use when device state is already fresh)
    """
    if not skip_update:
        await device.update()

    # Prefer device-level is_on + turn_on/off
    try:
        is_on_val = getattr(device, "is_on", None)
        if isinstance(is_on_val, bool):
            if is_on_val:
                await device.turn_off()
            else:
                await device.turn_on()
            return
    except Exception:
        pass

    # Fallback to module-level (Light / Switch)
    for mod in (get_light_module(device), get_switch_module(device)):
        if not mod:
            continue
        try:
            is_on_val = getattr(mod, "is_on", None)
            if isinstance(is_on_val, bool):
                if is_on_val and getattr(mod, "turn_off", None):
                    await mod.turn_off()
                    return
                if (not is_on_val) and getattr(mod, "turn_on", None):
                    await mod.turn_on()
                    return
        except Exception:
            pass

    raise AttributeError("toggle not supported (missing is_on + turn_on/off)")

def hsv_to_hex(hue: int, saturation: int, value: int) -> str:
    """Convert Kasa HSV (0-360,0-100,0-100) to #RRGGBB."""
    h = max(0, min(360, int(hue))) / 360.0
    s = max(0, min(100, int(saturation))) / 100.0
    v = max(0, min(100, int(value))) / 100.0
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))

def hex_apply_brightness(hex_color: str, brightness: int) -> str:
    """Apply brightness (0-100) to a hex color by scaling RGB linearly."""
    c = str(hex_color).lstrip("#")
    if len(c) != 6:
        return "#000000"
    try:
        r = int(c[0:2], 16)
        g = int(c[2:4], 16)
        b = int(c[4:6], 16)
        scale = max(0, min(100, int(brightness))) / 100.0
        r2 = int(r * scale)
        g2 = int(g * scale)
        b2 = int(b * scale)
        return "#{:02x}{:02x}{:02x}".format(r2, g2, b2)
    except Exception:
        return "#000000"

def read_device_is_on(device: IotDevice) -> Optional[bool]:
    """Best-effort read of on/off state."""
    v = getattr(device, "is_on", None)
    if isinstance(v, bool):
        return v
    for mod in (get_light_module(device), get_switch_module(device)):
        if not mod:
            continue
        mv = getattr(mod, "is_on", None)
        if isinstance(mv, bool):
            return mv
    return None

def read_light_state(device: IotDevice) -> dict:
    """
    Best-effort read of bulb settings:
    - brightness (0-100)
    - hsv (hue/sat/val) and derived colors (#RRGGBB)
    - color_temp (Kelvin) when supported
    Returns dict possibly containing: brightness, color_full, color_current, hue, saturation, value, color_temp.
    """
    # This function is intentionally defensive: callers rely on it for "best-effort"
    # reads during scene runs, and we never want missing attributes/modules to crash
    # the whole scene execution.
    try:
        light = get_light_module(device)
        if not light:
            return {}

        out: dict = {}

        # Brightness
        b = getattr(light, "brightness", None)
        if b is None:
            b = getattr(device, "brightness", None)
        if b is not None:
            try:
                out["brightness"] = max(0, min(100, int(b)))
            except Exception:
                pass

        # HSV / Color
        hsv = getattr(light, "hsv", None)
        if hsv is None:
            hsv = getattr(device, "hsv", None)
        if hsv is not None:
            try:
                hue, sat, val = hsv
                out["hue"] = int(hue)
                out["saturation"] = int(sat)
                out["value"] = int(val)
                # Full-brightness color and current brightness-applied color
                out["color_full"] = hsv_to_hex(hue, sat, 100)
                out["color_current"] = hsv_to_hex(hue, sat, val)
            except Exception:
                pass

        # Color temperature (Kelvin) / "warmth" / "white"
        # Different device firmwares expose different attribute names; try a few.
        for attr in ("color_temp", "color_temperature", "temperature", "color_temp_kelvin", "kelvin"):
            v = getattr(light, attr, None)
            if v is None:
                v = getattr(device, attr, None)
            if v is None:
                continue
            try:
                out["color_temp"] = int(v)
                break
            except Exception:
                continue

        return out
    except Exception:
        return {}

def clamp_color_temp_k(k: int, light=None) -> int:
    """
    Clamp color temperature to the device range if available, else a sane default.
    Most Kasa tunable whites are ~2500K-9000K.
    """
    try:
        k = int(k)
    except Exception:
        k = 0
    # device-provided range if exposed
    rng = None
    if light is not None:
        rng = getattr(light, "color_temp_range", None) or getattr(light, "color_temperature_range", None)
    if rng and isinstance(rng, (list, tuple)) and len(rng) == 2:
        try:
            lo = int(rng[0])
            hi = int(rng[1])
            if lo > hi:
                lo, hi = hi, lo
            return max(lo, min(hi, k))
        except Exception:
            pass
    return max(2500, min(9000, k))

async def set_light_color_temp_k(device: IotDevice, *, kelvin: int):
    """Best-effort set of bulb color temperature (Kelvin) using Light module APIs."""
    light = get_light_module(device)
    if not light:
        raise AttributeError("Light module not available")
    k = clamp_color_temp_k(kelvin, light)
    # Prefer module methods when present
    for method in ("set_color_temp", "set_color_temperature", "set_temperature", "set_color_temp_kelvin"):
        fn = getattr(light, method, None)
        if fn:
            await fn(k)
            return
    # Some devices expose it on device object (older APIs)
    for method in ("set_color_temp", "set_color_temperature", "set_temperature"):
        fn = getattr(device, method, None)
        if fn:
            await fn(k)
            return
    raise AttributeError("Color temperature not supported by this bulb")

async def compute_device_statuses() -> list[dict]:
    """
    Compute status for all configured devices.
    Returns list sorted by alias.
    """
    mac_to_device: dict[str, IotDevice] = {}

    async def one(dev_cfg: DeviceConfig) -> dict:
        base = {
            "alias": dev_cfg.alias,
            "mac": dev_cfg.mac,
            "host": dev_cfg.host,
            "type": dev_cfg.type,
            "reachable": False,
            "is_on": None,
        }
        try:
            device, source = await resolve_device_for_config(dev_cfg, mac_to_device)
            if not device:
                return {**base, "reachable": False}
            # Bound update time per device
            try:
                await asyncio.wait_for(device.update(), timeout=KASA_STATUS_UPDATE_TIMEOUT)
            except Exception:
                # even if update fails, we may still have partial info
                pass

            is_on = read_device_is_on(device)
            out = {**base, "reachable": True, "is_on": is_on, "source": source, "host": getattr(device, "host", dev_cfg.host)}

            if str(dev_cfg.type).lower() == "bulb":
                state = read_light_state(device)
                brightness = None
                if "brightness" in state:
                    brightness = state["brightness"]
                # prefer explicit brightness if available, else infer from hsv val not reliable
                out["brightness"] = brightness
                out["color_full"] = state.get("color_full")
                out["color_current"] = state.get("color_current")
                out["color_temp"] = state.get("color_temp")
                # If we have full color + brightness but no current color, compute it
                if out.get("color_full") and out.get("brightness") is not None and not out.get("color_current"):
                    out["color_current"] = hex_apply_brightness(out["color_full"], out["brightness"])

            return out
        except Exception as e:
            return {**base, "reachable": False, "error": str(e)}

    statuses = await asyncio.gather(*[one(d) for d in config.devices])
    statuses.sort(key=lambda d: str(d.get("alias", "")).lower())
    return statuses

async def resolve_device_for_config(
    device_config: DeviceConfig,
    mac_to_device: dict[str, IotDevice],
) -> tuple[Optional[IotDevice], str]:
    """
    Resolve a device using the best available method:
    1) Cached connection (fastest - avoids reconnecting)
    2) Already-discovered MAC mapping (fast)
    3) Fallback to cached IP/host in config (works even when UDP discovery is blocked)
    Returns (device, source) where source is 'mac' or 'host'.
    """
    mac_norm = normalize_mac(device_config.mac)
    
    # Check connection cache first (fastest path)
    async with _device_cache_lock:
        if mac_norm in _device_connection_cache:
            cached_dev, cached_time = _device_connection_cache[mac_norm]
            age = (datetime.now(timezone.utc) - cached_time).total_seconds()
            # Only use cache if not expired AND device has valid modules
            if age < _device_cache_ttl and getattr(cached_dev, "modules", None) is not None:
                return cached_dev, "cache"
            else:
                # Cache expired or invalid, remove it
                del _device_connection_cache[mac_norm]
    
    # Fast path: if we have a cached IP, try it first (much faster than broadcast discovery)
    if device_config.host:
        try:
            # Use shorter timeout for faster response
            dev = await asyncio.wait_for(kasa_discover_single(device_config.host), timeout=KASA_HOST_CONNECT_TIMEOUT)
            # IMPORTANT: Must call update() to populate device.modules before caching/using.
            # Without this, device.modules is None and set_hsv/set_brightness fail.
            try:
                await dev.update()
            except Exception:
                pass
            # Only cache if the device has valid modules (update succeeded)
            if getattr(dev, "modules", None) is not None:
                async with _device_cache_lock:
                    _device_connection_cache[mac_norm] = (dev, datetime.now(timezone.utc))
                return dev, "host"
            # If modules is None, don't cache - fall through to MAC discovery
        except (asyncio.TimeoutError, Exception):
            # If host connect fails, we can still try MAC discovery if available.
            pass

    if mac_norm in mac_to_device:
        dev = mac_to_device[mac_norm]
        # Only cache and use if device has valid modules
        if getattr(dev, "modules", None) is not None:
            async with _device_cache_lock:
                _device_connection_cache[mac_norm] = (dev, datetime.now(timezone.utc))
            return dev, "mac"

    return None, "mac"

# --- Routes ---

@app.on_event("startup")
async def on_startup():
    """Startup discovery to refresh MAC->IP, plus periodic refresh to handle network changes."""
    global periodic_discovery_task
    try:
        print(
            "Startup:",
            {
                "frozen": bool(getattr(sys, "frozen", False)),
                "app_dir": str(app_dir()),
                "bundle_dir": str(bundle_dir()),
                "config_path": CONFIG_PATH,
                "templates_dir": TEMPLATES_DIR,
                "rooms": len(getattr(config, "rooms", []) or []),
                "devices": len(getattr(config, "devices", []) or []),
                "scenes": len(getattr(config, "scenes", []) or []),
            },
        )
    except Exception:
        pass
    # Don't block startup; run initial refresh in the background.
    asyncio.create_task(refresh_discovery_cache(timeout=KASA_STARTUP_DISCOVERY_TIMEOUT, update_config_hosts=True))
    periodic_discovery_task = asyncio.create_task(periodic_discovery_loop())
    global routine_scheduler_task
    routine_scheduler_task = asyncio.create_task(routine_scheduler_loop())
    ensure_test_scene()
    migrate_scene_maps_to_rooms()

@app.on_event("shutdown")
async def on_shutdown():
    global periodic_discovery_task
    if periodic_discovery_task:
        periodic_discovery_task.cancel()
        periodic_discovery_task = None
    global routine_scheduler_task
    if routine_scheduler_task:
        routine_scheduler_task.cancel()
        routine_scheduler_task = None

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main dashboard showing scenes and device status."""
    # Sort devices alphabetically by alias
    sorted_devices = sorted(config.devices, key=lambda d: d.alias.lower())
    visible_scenes = [s for s in config.scenes if is_visible_scene(s)]
    return templates.TemplateResponse("index.html", {
        "request": request,
        "config": config,
        "sorted_devices": sorted_devices
        ,
        "visible_scenes": visible_scenes
    })

@app.get("/diagnostics", response_class=HTMLResponse)
async def diagnostics_page(request: Request):
    """Diagnostics page (shows recent trigger events, e.g. from Flic Hub)."""
    return templates.TemplateResponse("diagnostics.html", {
        "request": request,
        "event_log_max": EVENT_LOG_MAX,
    })

@app.get("/api/diagnostics/last-scene-run")
async def api_last_scene_run():
    async with last_scene_run_lock:
        return {"last_scene_run": last_scene_run}

@app.get("/api/diagnostics/events")
async def api_diagnostics_events():
    """Return recent events (newest last)."""
    async with event_log_lock:
        return {"events": list(event_log)}

@app.post("/api/diagnostics/clear")
async def api_diagnostics_clear():
    async with event_log_lock:
        event_log.clear()
    return {"status": "success"}

@app.get("/api/events")
async def api_events(request: Request):
    """
    Server-Sent Events stream used by map/dashboard cross-tab sync.
    Clients receive JSON messages like: {ts,type,payload}
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    async with sse_lock:
        sse_clients.add(q)

    async def event_gen():
        try:
            # Initial hello
            yield "event: hello\ndata: {}\n\n"
            while True:
                # Stop if client disconnected
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"event: {msg.get('type','message')}\ndata: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    # keepalive
                    yield "event: ping\ndata: {}\n\n"
        finally:
            async with sse_lock:
                sse_clients.discard(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream")

@app.get("/api/device-status")
async def api_device_status(force: int = 0):
    """
    Return current device status (cached briefly for responsiveness).
    Query param: force=1 to bypass cache.
    """
    now = time.time()
    async with device_status_lock:
        if not force and (now - device_status_cache["ts"] < KASA_STATUS_CACHE_TTL):
            return {"ts": device_status_cache["ts"], "cached": True, "devices": device_status_cache["data"]}

        data = await compute_device_statuses()
        device_status_cache["ts"] = now
        device_status_cache["data"] = data
        return {"ts": now, "cached": False, "devices": data}

@app.post("/api/device/{mac}/set")
async def api_set_device(mac: str, request: Request):
    """
    Set device state by MAC.
    JSON body supports:
      { "power": "on"|"off"|"toggle", "brightness": 0-100, "color": "#RRGGBB", "color_temp": 2500-9000 }
    """
    mac_norm = normalize_mac(mac)
    dev_cfg = next((d for d in config.devices if normalize_mac(d.mac) == mac_norm), None)
    if not dev_cfg:
        raise HTTPException(status_code=404, detail="Device not found")

    payload = await request.json()
    power = payload.get("power")
    brightness = payload.get("brightness")
    color = payload.get("color")
    color_temp = payload.get("color_temp")

    if power not in (None, "on", "off", "toggle"):
        raise HTTPException(status_code=400, detail="power must be on/off/toggle")

    if brightness is not None:
        try:
            brightness = int(brightness)
        except Exception:
            raise HTTPException(status_code=400, detail="brightness must be an integer 0-100")
        if brightness < 0 or brightness > 100:
            raise HTTPException(status_code=400, detail="brightness must be 0-100")

    if color is not None:
        c = str(color).strip()
        if not c.startswith("#"):
            c = "#" + c
        if len(c) != 7:
            raise HTTPException(status_code=400, detail="color must be #RRGGBB")
        try:
            int(c[1:], 16)
        except Exception:
            raise HTTPException(status_code=400, detail="color must be #RRGGBB hex")
        color = c.lower()

    if color_temp is not None:
        try:
            color_temp = int(color_temp)
        except Exception:
            raise HTTPException(status_code=400, detail="color_temp must be an integer (Kelvin)")

    # Broadcast optimistic target immediately
    await publish_sse("device_target", {"alias": dev_cfg.alias, "mac": dev_cfg.mac, "power": power, "brightness": brightness, "color": color, "color_temp": color_temp})

    mac_to_device: dict[str, IotDevice] = {}
    device, source = await resolve_device_for_config(dev_cfg, mac_to_device)
    if not device:
        await publish_sse("device_applied", {"alias": dev_cfg.alias, "mac": dev_cfg.mac, "ok": False, "message": "Unable to reach device"})
        raise HTTPException(status_code=503, detail="Unable to reach device")

    try:
        await device.update()

        # Power
        if power == "on":
            await device.turn_on()
        elif power == "off":
            await device.turn_off()
        elif power == "toggle":
            await toggle_device_power(device, skip_update=True)  # already updated above

        # Bulb-only settings
        if str(dev_cfg.type).lower() == "bulb":
            light = get_light_module(device)

            # White mode: set color temperature (Kelvin). Prefer this over RGB if both provided.
            if color_temp is not None:
                await set_light_color_temp_k(device, kelvin=color_temp)

            # Color: set HSV and then restore brightness if provided (to avoid forcing V=100)
            if color is not None and color_temp is None:
                hex_color = color.lstrip("#")
                rgb = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
                hsv = colorsys.rgb_to_hsv(rgb[0]/255.0, rgb[1]/255.0, rgb[2]/255.0)
                hue = int(hsv[0] * 360)
                saturation = int(hsv[1] * 100)
                # Use current brightness if known, else 100, then reapply brightness below if requested
                current_brightness = None
                try:
                    state = read_light_state(device) or {}
                    if isinstance(state, dict):
                        current_brightness = state.get("brightness")
                except Exception:
                    pass
                value = int((current_brightness if current_brightness is not None else 100))
                if light and getattr(light, "has_feature", None) and light.has_feature("hsv") and getattr(light, "set_hsv", None):
                    await light.set_hsv(hue, saturation, value)
                else:
                    await device.set_hsv(hue, saturation, value)

            if brightness is not None:
                if light and getattr(light, "set_brightness", None):
                    await light.set_brightness(brightness)
                else:
                    await device.set_brightness(brightness)

        # Update cached host if it changed
        if dev_cfg.host != device.host:
            dev_cfg.host = device.host
            save_config(config)

        # Bust status cache so dashboard updates immediately
        async with device_status_lock:
            device_status_cache["ts"] = 0.0

        await publish_sse("device_applied", {"alias": dev_cfg.alias, "mac": dev_cfg.mac, "ok": True})
        return {"status": "success", "device": dev_cfg.alias, "source": source, "host": device.host}
    except HTTPException:
        raise
    except Exception as e:
        await publish_sse("device_applied", {"alias": dev_cfg.alias, "mac": dev_cfg.mac, "ok": False, "message": str(e)})
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/run-scene/{scene_idx}")
async def run_scene(scene_idx: int):
    """Executes a defined scene."""
    if scene_idx < 0 or scene_idx >= len(config.scenes):
        raise HTTPException(status_code=404, detail="Scene not found")
    
    scene = config.scenes[scene_idx]
    return await execute_scene(scene)

@app.get("/api/scenes")
async def api_scenes():
    """List scenes (for integrations like Flic)."""
    scenes_out = [{"index": i, "name": s.name} for i, s in enumerate(config.scenes) if is_visible_scene(s)]
    # Also expose derived dim-scene names (virtual) for integrations.
    for s in [s for s in config.scenes if is_visible_scene(s)]:
        if s.name.lower() == "testscene":
            continue
        for suffix in DIM_SUFFIXES:
            scenes_out.append({"index": None, "name": f"{s.name}{suffix}"})
        scenes_out.append({"index": None, "name": f"{s.name}{TOGGLE_SUFFIX}"})
    return {"scenes": scenes_out}

@app.post("/api/capture-scene-actions")
async def api_capture_scene_actions(request: Request):
    """
    Capture current device states for a set of device aliases and return SceneAction list.
    Body JSON:
      { "device_aliases": ["Basement LED 01", ...] }
    """
    payload = await request.json()
    aliases = payload.get("device_aliases") or []
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        raise HTTPException(status_code=400, detail="device_aliases must be a list of strings")

    # Resolve configured devices
    alias_to_cfg = {d.alias: d for d in config.devices}
    targets = [alias_to_cfg.get(a) for a in aliases]
    targets = [t for t in targets if t]
    if not targets:
        return {"actions": []}

    # Try to capture quickly via host; discovery only if needed (same strategy as scene run)
    mac_to_device: dict[str, IotDevice] = {}

    async def capture_one(dev_cfg: DeviceConfig) -> dict:
        try:
            device, _source = await resolve_device_for_config(dev_cfg, mac_to_device)
            if not device:
                return {"device_alias": dev_cfg.alias, "action": "off", "params": None}

            await device.update()
            is_on = read_device_is_on(device)
            if is_on is False:
                return {"device_alias": dev_cfg.alias, "action": "off", "params": None}

            # On/unknown => store as "on"
            params = None
            if str(dev_cfg.type).lower() == "bulb":
                state = read_light_state(device)
                # Store scene params in the format the app expects:
                # - params.color is the FULL-brightness color
                # - params.brightness is the intended brightness (0-100)
                if state:
                    p: dict = {}
                    if "brightness" in state and state["brightness"] is not None:
                        p["brightness"] = state["brightness"]
                    # Prefer white mode if saturation is 0 and color_temp exists; otherwise store RGB color.
                    if state.get("color_temp") is not None and (state.get("saturation") in (0, "0", None) or state.get("saturation") == 0):
                        p["color_temp"] = state["color_temp"]
                    else:
                        if "color_full" in state and state["color_full"]:
                            p["color"] = state["color_full"]
                        elif "color_current" in state and state["color_current"]:
                            # Fallback: we only have current, still store it
                            p["color"] = state["color_current"]
                    params = p if p else None

            return {"device_alias": dev_cfg.alias, "action": "on", "params": params}
        except Exception:
            return {"device_alias": dev_cfg.alias, "action": "off", "params": None}

    captured = await asyncio.gather(*[capture_one(d) for d in targets])
    return {"actions": captured}

@app.get("/api/trigger/scene/{scene_name}")
async def api_trigger_scene_get(
    request: Request,
    scene_name: str,
    token: Optional[str] = None,
    x_token: Optional[str] = Header(None, alias="X-Token"),
):
    """Trigger a scene by name (Flic-friendly GET)."""
    # Log *all* incoming trigger hits for connectivity diagnostics, even if auth fails.
    await log_event(
        "flic_trigger",
        request,
        {"scene": scene_name, "method": "GET", "has_token": bool(token or x_token)},
    )
    verify_trigger_token(token or x_token)
    run_scene: Optional[Scene] = None

    # Virtual toggle scene: <SceneName>_toggle
    base_name_toggle, is_toggle = parse_toggle_suffix(scene_name)
    if is_toggle:
        base_scene = next((s for s in config.scenes if s.name.lower() == base_name_toggle.lower()), None)
        if not base_scene:
            raise HTTPException(status_code=404, detail="Scene not found")
        # Heuristic: check only one representative device for speed/stability.
        any_on = await is_scene_representative_on(base_scene)
        await log_event("scene_toggle_state", request, {"scene": base_scene.name, "any_on": any_on})
        if any_on:
            # Turn off all devices referenced by the scene
            off_actions = [SceneAction(device_alias=a.device_alias, action="off", params=None) for a in base_scene.actions]
            run_scene = Scene(
                name=f"{base_scene.name}{TOGGLE_SUFFIX}:off",
                actions=off_actions,
                room_idx=base_scene.room_idx,
                dim_profile=base_scene.dim_profile,
            )
            # Clear active scene for the room when toggling off
            if base_scene.room_idx is not None and 0 <= base_scene.room_idx < len(config.rooms):
                config.rooms[base_scene.room_idx].active_scene = None
        else:
            # Off -> set scene (full scene brightness, i.e., base scene)
            run_scene = base_scene
    else:
        base_name, suffix = parse_dim_suffix(scene_name)
        base_scene = next((s for s in config.scenes if s.name.lower() == base_name.lower()), None)
        if not base_scene:
            raise HTTPException(status_code=404, detail="Scene not found")
        if suffix is None:
            run_scene = base_scene
        else:
            # Derived dim scene is virtual: compute on the fly.
            run_scene = derive_dimmed_scene(base_scene, suffix, dim_multiplier(base_scene, suffix))

    try:
        res = await execute_scene(run_scene)
        await _record_last_scene_run(request=request, trigger="api_trigger_scene_get", scene_name=scene_name, res=res)
        return res
    except HTTPException as e:
        await _record_last_scene_run(request=request, trigger="api_trigger_scene_get", scene_name=scene_name, error=str(e.detail))
        raise
    except Exception as e:
        await _record_last_scene_run(request=request, trigger="api_trigger_scene_get", scene_name=scene_name, error=str(e))
        raise

@app.post("/api/trigger/scene/{scene_name}")
async def api_trigger_scene_post(request: Request, scene_name: str, token: Optional[str] = Form(None)):
    """Trigger a scene by name (Flic-friendly POST)."""
    await log_event("flic_trigger", request, {"scene": scene_name, "method": "POST", "has_token": bool(token)})
    verify_trigger_token(token)

    run_scene: Optional[Scene] = None

    base_name_toggle, is_toggle = parse_toggle_suffix(scene_name)
    if is_toggle:
        base_scene = next((s for s in config.scenes if s.name.lower() == base_name_toggle.lower()), None)
        if not base_scene:
            raise HTTPException(status_code=404, detail="Scene not found")
        # Heuristic: check only one representative device for speed/stability.
        any_on = await is_scene_representative_on(base_scene)
        await log_event("scene_toggle_state", request, {"scene": base_scene.name, "any_on": any_on})
        if any_on:
            off_actions = [SceneAction(device_alias=a.device_alias, action="off", params=None) for a in base_scene.actions]
            run_scene = Scene(
                name=f"{base_scene.name}{TOGGLE_SUFFIX}:off",
                actions=off_actions,
                room_idx=base_scene.room_idx,
                dim_profile=base_scene.dim_profile,
            )
            if base_scene.room_idx is not None and 0 <= base_scene.room_idx < len(config.rooms):
                config.rooms[base_scene.room_idx].active_scene = None
        else:
            run_scene = base_scene
    else:
        base_name, suffix = parse_dim_suffix(scene_name)
        base_scene = next((s for s in config.scenes if s.name.lower() == base_name.lower()), None)
        if not base_scene:
            raise HTTPException(status_code=404, detail="Scene not found")
        if suffix is None:
            run_scene = base_scene
        else:
            run_scene = derive_dimmed_scene(base_scene, suffix, dim_multiplier(base_scene, suffix))

    try:
        res = await execute_scene(run_scene)
        await _record_last_scene_run(request=request, trigger="api_trigger_scene_post", scene_name=scene_name, res=res)
        return res
    except HTTPException as e:
        await _record_last_scene_run(request=request, trigger="api_trigger_scene_post", scene_name=scene_name, error=str(e.detail))
        raise
    except Exception as e:
        await _record_last_scene_run(request=request, trigger="api_trigger_scene_post", scene_name=scene_name, error=str(e))
        raise

@app.get("/api/trigger/scene-index/{scene_idx}")
async def api_trigger_scene_index_get(
    request: Request,
    scene_idx: int,
    token: Optional[str] = None,
    x_token: Optional[str] = Header(None, alias="X-Token"),
):
    """Trigger a scene by index (Flic-friendly GET)."""
    await log_event(
        "flic_trigger",
        request,
        {"scene_index": scene_idx, "method": "GET", "has_token": bool(token or x_token)},
    )
    verify_trigger_token(token or x_token)
    if scene_idx < 0 or scene_idx >= len(config.scenes):
        raise HTTPException(status_code=404, detail="Scene not found")
    scene = config.scenes[scene_idx]
    try:
        res = await execute_scene(scene)
        await _record_last_scene_run(request=request, trigger="api_trigger_scene_index_get", scene_name=str(scene_idx), res=res)
        return res
    except HTTPException as e:
        await _record_last_scene_run(request=request, trigger="api_trigger_scene_index_get", scene_name=str(scene_idx), error=str(e.detail))
        raise
    except Exception as e:
        await _record_last_scene_run(request=request, trigger="api_trigger_scene_index_get", scene_name=str(scene_idx), error=str(e))
        raise

# --- Room dimming endpoints (virtual) ---
def _get_room_by_name(room_name: str) -> tuple[int, Room]:
    name = (room_name or "").strip().lower()
    for idx, r in enumerate(config.rooms):
        if r.name.strip().lower() == name:
            return idx, r
    raise HTTPException(status_code=404, detail="Room not found")

def _get_base_scenes_for_room(room_idx: int) -> list[Scene]:
    return [s for s in config.scenes if s.room_idx == room_idx and is_visible_scene(s)]

@app.get("/api/{room_name}/cycle")
async def api_room_cycle_get(
    request: Request,
    room_name: str,
    token: Optional[str] = None,
    x_token: Optional[str] = Header(None, alias="X-Token"),
):
    """Cycle to the next scene mapped to a room."""
    await log_event("room_cycle_trigger", request, {"room": room_name, "method": "GET", "has_token": bool(token or x_token)})
    verify_trigger_token(token or x_token)
    try:
        return await _run_room_cycle(request, room_name)
    except HTTPException as e:
        await log_event("room_cycle_error", request, {"room": room_name, "status": e.status_code, "detail": e.detail})
        raise
    except Exception as e:
        await log_event("room_cycle_error", request, {"room": room_name, "status": 500, "detail": str(e)})
        raise

@app.post("/api/{room_name}/cycle")
async def api_room_cycle_post(request: Request, room_name: str, token: Optional[str] = Form(None)):
    await log_event("room_cycle_trigger", request, {"room": room_name, "method": "POST", "has_token": bool(token)})
    verify_trigger_token(token)
    try:
        return await _run_room_cycle(request, room_name)
    except HTTPException as e:
        await log_event("room_cycle_error", request, {"room": room_name, "status": e.status_code, "detail": e.detail})
        raise
    except Exception as e:
        await log_event("room_cycle_error", request, {"room": room_name, "status": 500, "detail": str(e)})
        raise

async def _run_room_cycle(request: Request, room_name: str) -> dict:
    room_idx, room = _get_room_by_name(room_name)
    mapped = _get_base_scenes_for_room(room_idx)
    if not mapped:
        raise HTTPException(status_code=409, detail="No scenes mapped to this room")

    # Acquire the scene execution lock BEFORE determining the next scene.
    # This prevents race conditions where two rapid cycle requests both read the same
    # active_scene and try to advance to the same next scene.
    async with scene_execution_lock:
        # Determine next scene based on room.active_scene (base scene name).
        active_lower = (room.active_scene or "").strip().lower()
        names = [s.name for s in mapped]
        # Normalize names to avoid subtle whitespace/case mismatches causing cycling to "stick" on index 0.
        idx_map = {s.name.strip().lower(): i for i, s in enumerate(mapped)}

        if active_lower in idx_map:
            next_idx = (idx_map[active_lower] + 1) % len(mapped)
        else:
            next_idx = 0

        chosen = mapped[next_idx]
        # Respect the room's last dim level (dial position) when cycling.
        # If active_dim is missing/invalid, default to full brightness (d4).
        dim = (getattr(room, "active_dim", None) or "d4").strip().lower()
        suffix = f"_{dim}"
        if suffix in DIM_SUFFIXES and suffix != "_d4":
            run_scene = derive_dimmed_scene(chosen, suffix, dim_multiplier(chosen, suffix))
        else:
            run_scene = chosen

        # Update active_scene immediately while we hold the lock
        room.active_scene = chosen.name
        save_config(config)

        await log_event(
            "room_cycle_chosen",
            request,
            {
                "room": room.name,
                "room_idx": room_idx,
                "active_scene": room.active_scene,
                "active_dim": getattr(room, "active_dim", None),
                "mapped_scenes": names,
                "chosen_scene": chosen.name,
                "run_scene": run_scene.name,
            },
        )

    # Execute scene outside the lock to avoid holding it during slow device operations
    res = await execute_scene(run_scene)
    try:
        results = res.get("results") or []
        ok = sum(1 for r in results if r.get("status") == "success")
        err = sum(1 for r in results if r.get("status") == "error")
        sample_errors = [
            {"device": r.get("device"), "message": r.get("message")}
            for r in results
            if r.get("status") == "error"
        ][:5]
        await log_event(
            "room_cycle_result",
            request,
            {"room": room.name, "scene": chosen.name, "device_success": ok, "device_error": err, "sample_errors": sample_errors},
        )
    except Exception:
        pass
    return res

@app.get("/api/{room_name}/dimming_{dim}")
async def api_room_dimming_get(
    request: Request,
    room_name: str,
    dim: str,
    token: Optional[str] = None,
    x_token: Optional[str] = Header(None, alias="X-Token"),
):
    """Dimming endpoint per room. Example: /api/Home/dimming_d2 -> runs <activeScene>_d2."""
    await log_event("room_dimming_trigger", request, {"room": room_name, "dim": dim, "method": "GET", "has_token": bool(token or x_token)})
    verify_trigger_token(token or x_token)
    try:
        return await _run_room_dimming(request, room_name, dim)
    except HTTPException as e:
        await log_event("room_dimming_error", request, {"room": room_name, "dim": dim, "status": e.status_code, "detail": e.detail})
        raise
    except Exception as e:
        await log_event("room_dimming_error", request, {"room": room_name, "dim": dim, "status": 500, "detail": str(e)})
        raise

@app.post("/api/{room_name}/dimming_{dim}")
async def api_room_dimming_post(request: Request, room_name: str, dim: str, token: Optional[str] = Form(None)):
    await log_event("room_dimming_trigger", request, {"room": room_name, "dim": dim, "method": "POST", "has_token": bool(token)})
    verify_trigger_token(token)
    try:
        return await _run_room_dimming(request, room_name, dim)
    except HTTPException as e:
        await log_event("room_dimming_error", request, {"room": room_name, "dim": dim, "status": e.status_code, "detail": e.detail})
        raise
    except Exception as e:
        await log_event("room_dimming_error", request, {"room": room_name, "dim": dim, "status": 500, "detail": str(e)})
        raise

async def _run_room_dimming(request: Request, room_name: str, dim: str) -> dict:
    dim = (dim or "").lower()
    suffix = f"_{dim}"
    if suffix not in DIM_SUFFIXES:
        raise HTTPException(status_code=400, detail="dim must be d1, d2, d3, or d4")

    room_idx, room = _get_room_by_name(room_name)
    # Persist last dim level for the room so future cycles respect the dial position.
    try:
        room.active_dim = dim
        save_config(config)
    except Exception:
        pass
    mapped_names = [s.name for s in _get_base_scenes_for_room(room_idx)]
    await log_event(
        "room_dimming_state",
        request,
        {
            "room": room.name,
            "room_idx": room_idx,
            "dim": dim,
            "active_dim": getattr(room, "active_dim", None),
            "active_scene": room.active_scene,
            "mapped_scenes": mapped_names,
        },
    )

    # Determine the active base scene for this room.
    base_scene: Optional[Scene] = None
    if room.active_scene:
        base_scene = next((s for s in config.scenes if s.name.lower() == room.active_scene.lower()), None)
        if base_scene and base_scene.room_idx != room_idx:
            base_scene = None

    if not base_scene:
        mapped = _get_base_scenes_for_room(room_idx)
        if len(mapped) == 1:
            base_scene = mapped[0]
        elif len(mapped) == 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "No scenes are mapped to this room. Assign a Room to a scene on the Scenes page first.",
                    "room": room.name,
                },
            )
        else:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "No active scene remembered for this room. Trigger one of the mapped scenes first.",
                    "mapped_scenes": mapped_names,
                },
            )

    mult = dim_multiplier(base_scene, suffix)
    dimmed = derive_dimmed_scene(base_scene, suffix, mult)
    # Keep room active base scene as the base (not dim variant)
    room.active_scene = base_scene.name
    await log_event(
        "room_dimming_chosen",
        request,
        {"room": room.name, "dim": dim, "base_scene": base_scene.name, "run_scene": dimmed.name},
    )
    res = await execute_scene(dimmed)
    try:
        results = res.get("results") or []
        ok = sum(1 for r in results if r.get("status") == "success")
        err = sum(1 for r in results if r.get("status") == "error")
        sample_errors = [
            {"device": r.get("device"), "message": r.get("message")}
            for r in results
            if r.get("status") == "error"
        ][:5]
        await log_event(
            "room_dimming_result",
            request,
            {
                "room": room.name,
                "dim": dim,
                "scene": res.get("scene"),
                "device_success": ok,
                "device_error": err,
                "sample_errors": sample_errors,
            },
        )
    except Exception:
        pass
    return res

@app.post("/api/test-device/{device_idx}")
async def api_test_device(device_idx: int, action: str = Form(...), brightness: Optional[int] = Form(None), color: Optional[str] = Form(None)):
    """Diagnostics endpoint: perform a simple action on one configured device and return details."""
    if device_idx >= len(config.devices):
        raise HTTPException(status_code=404, detail="Device not found")

    dev_cfg = config.devices[device_idx]

    # Discover once and use MAC map + host fallback
    try:
        discovered = await kasa_discover(timeout=10)
        mac_to_device: dict[str, IotDevice] = {}
        for dev in discovered.values():
            try:
                await dev.update()
            except Exception:
                pass
            if getattr(dev, "mac", None):
                mac_to_device[normalize_mac(dev.mac)] = dev
    except Exception:
        mac_to_device = {}

    device, source = await resolve_device_for_config(dev_cfg, mac_to_device)
    if not device:
        raise HTTPException(status_code=503, detail="Unable to reach device (MAC discovery + host fallback failed)")

    try:
        await device.update()
        if action == "on":
            await device.turn_on()
        elif action == "off":
            await device.turn_off()
        elif action == "toggle":
            await toggle_device_power(device, skip_update=True)  # already updated above
        else:
            raise HTTPException(status_code=400, detail="Invalid action")

        # Optional bulb controls
        if str(dev_cfg.type).lower() == "bulb":
            light = get_light_module(device)
            if brightness is not None:
                b = max(0, min(100, int(brightness)))
                if light and getattr(light, "set_brightness", None):
                    await light.set_brightness(b)
                else:
                    await device.set_brightness(b)
            if color:
                hex_color = str(color).lstrip("#")
                if len(hex_color) != 6:
                    raise HTTPException(status_code=400, detail="color must be 6 hex digits (e.g. #FF00AA)")
                rgb = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
                hsv = colorsys.rgb_to_hsv(rgb[0]/255.0, rgb[1]/255.0, rgb[2]/255.0)
                hue = int(hsv[0] * 360)
                saturation = int(hsv[1] * 100)
                value = int(hsv[2] * 100)
                if light and getattr(light, "has_feature", None) and light.has_feature("hsv") and getattr(light, "set_hsv", None):
                    await light.set_hsv(hue, saturation, value)
                else:
                    await device.set_hsv(hue, saturation, value)

        # Update cached host
        if dev_cfg.host != device.host:
            dev_cfg.host = device.host
            save_config(config)

        return {
            "status": "success",
            "device": dev_cfg.alias,
            "source": source,
            "host": device.host,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    """Configuration page."""
    # Get configured MAC addresses to filter them out
    configured_macs = {normalize_mac(dev.mac) for dev in config.devices}
    
    # Get last discovery results if available
    discovered_devices = []
    if last_discovery_cache:
        for mac, device_info in last_discovery_cache.items():
            if mac not in configured_macs:  # Only show unconfigured devices
                discovered_devices.append(device_info)
        # Sort discovered devices alphabetically by alias
        discovered_devices.sort(key=lambda d: d["alias"].lower())
    
    # Sort configured devices alphabetically by alias
    sorted_devices = sorted(config.devices, key=lambda d: d.alias.lower())
    
    # Check for error message
    error = request.query_params.get("error", "")
    
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "config": config,
        "sorted_devices": sorted_devices,
        "discovered_devices": discovered_devices,
        "error": error,
        "last_discovery_at": last_discovery_at.isoformat() if last_discovery_at else None,
        "last_discovery_error": last_discovery_error,
        "last_discovery_debug": last_discovery_debug,
    })

@app.post("/api/rescan")
async def api_rescan():
    """Force a discovery refresh now and update configured device IPs."""
    return await refresh_discovery_cache(timeout=KASA_SETTINGS_DISCOVERY_TIMEOUT, update_config_hosts=True)

@app.get("/api/discover")
async def api_discover():
    """
    API endpoint used by Settings UI.
    Important: the UI already calls /api/rescan first, so this should return the
    refreshed cache (fast, consistent) rather than running a second discovery pass.
    """
    devices = last_discovery_cache or await discover_devices()

    # Filter out already configured devices
    configured_macs = {normalize_mac(dev.mac) for dev in config.devices}
    unconfigured = [dev for mac, dev in devices.items() if mac not in configured_macs]
    # Sort alphabetically by alias
    unconfigured.sort(key=lambda d: d["alias"].lower())
    return {"devices": unconfigured}

@app.get("/api/discovery-debug")
async def api_discovery_debug():
    """Return latest discovery debug snapshot (from /api/rescan refresh)."""
    return {
        "last_discovery_at": last_discovery_at.isoformat() if last_discovery_at else None,
        "last_discovery_error": last_discovery_error,
        "debug": last_discovery_debug,
        "cache_count": len(last_discovery_cache) if last_discovery_cache else 0,
    }

@app.post("/add-device")
async def add_device(alias: str = Form(...), mac: str = Form(...), dev_type: str = Form(...)):
    """Add a device by MAC address. Optionally discovers current IP."""
    mac_formatted = normalize_mac(mac)
    
    # Check for duplicates
    configured_macs = {normalize_mac(dev.mac) for dev in config.devices}
    if mac_formatted in configured_macs:
        return RedirectResponse(url="/settings?error=duplicate", status_code=303)
    
    # Skip IP discovery to speed up - will be resolved when needed
    # Check cache first for faster lookup
    current_ip = None
    if last_discovery_cache and mac_formatted in last_discovery_cache:
        current_ip = last_discovery_cache[mac_formatted]["host"]
    
    config.devices.append(DeviceConfig(alias=alias, mac=mac_formatted, host=current_ip, type=dev_type))
    save_config(config)
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/add-devices-batch")
async def add_devices_batch(request: Request):
    """Add multiple devices at once."""
    form_data = await request.form()
    devices_data = json.loads(form_data.get("devices", "[]"))
    
    # Get existing MAC addresses to check for duplicates
    configured_macs = {normalize_mac(dev.mac) for dev in config.devices}
    added_count = 0
    
    for device_data in devices_data:
        mac_formatted = normalize_mac(device_data["mac"])
        
        # Skip if already configured
        if mac_formatted in configured_macs:
            continue
        
        current_ip = None
        if last_discovery_cache and mac_formatted in last_discovery_cache:
            current_ip = last_discovery_cache[mac_formatted]["host"]
        
        config.devices.append(DeviceConfig(
            alias=device_data["alias"],
            mac=mac_formatted,
            host=current_ip,
            type=device_data["type"]
        ))
        configured_macs.add(mac_formatted)  # Track newly added
        added_count += 1
    
    if added_count > 0:
        save_config(config)
    
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/delete-device/{device_idx}")
async def delete_device(device_idx: int):
    """Delete a device by index."""
    if device_idx >= len(config.devices):
        raise HTTPException(status_code=404, detail="Device not found")
    
    config.devices.pop(device_idx)
    save_config(config)
    return RedirectResponse(url="/settings", status_code=303)

@app.get("/scenes", response_class=HTMLResponse)
async def scenes_page(request: Request):
    """Scene creation/management page."""
    # Sort devices alphabetically by alias
    sorted_devices = sorted(config.devices, key=lambda d: d.alias.lower())
    visible_scenes = [{"idx": i, "scene": s} for i, s in enumerate(config.scenes) if is_visible_scene(s)]
    devices_js = [{"alias": d.alias, "type": d.type} for d in sorted_devices]

    edit_idx_raw = request.query_params.get("edit")
    edit_idx: Optional[int] = None
    edit_scene: Optional[Scene] = None
    edit_actions_js: Optional[list[dict]] = None
    if edit_idx_raw is not None:
        try:
            edit_idx = int(edit_idx_raw)
            if edit_idx < 0 or edit_idx >= len(config.scenes):
                edit_idx = None
            else:
                edit_scene = config.scenes[edit_idx]
                if not is_visible_scene(edit_scene):
                    edit_scene = None
                    edit_idx = None
                else:
                    # Jinja's tojson can't serialize Pydantic models directly; pass plain dicts.
                    edit_actions_js = [a.model_dump() for a in edit_scene.actions]
        except Exception:
            edit_idx = None
            edit_scene = None
            edit_actions_js = None

    rooms_for_select = [{"idx": i, "name": r.name} for i, r in enumerate(config.rooms)]
    rooms_for_select.sort(key=lambda r: r["name"].lower())

    return templates.TemplateResponse("scenes.html", {
        "request": request,
        "config": config,
        "sorted_devices": sorted_devices,
        "visible_scenes": visible_scenes,
        "devices_js": devices_js,
        "edit_idx": edit_idx,
        "edit_scene": edit_scene,
        "edit_actions_js": edit_actions_js,
        "rooms": rooms_for_select,
    })

@app.post("/update-scene/{scene_idx}")
async def update_scene(scene_idx: int, request: Request):
    """Update an existing scene (by index)."""
    if scene_idx < 0 or scene_idx >= len(config.scenes):
        raise HTTPException(status_code=404, detail="Scene not found")

    current = config.scenes[scene_idx]
    if not is_visible_scene(current):
        raise HTTPException(status_code=400, detail="Cannot edit derived scenes")

    form_data = await request.form()
    scene_name = form_data.get("name", "")
    actions_json = form_data.get("actions", "[]")
    room_idx_raw = form_data.get("room_idx")
    dim_profile = str(form_data.get("dim_profile", "linear") or "linear").strip().lower()

    try:
        actions_data = json.loads(actions_json)
        actions = [SceneAction(**action) for action in actions_data]
        validate_scene_actions(actions)

        base_name, suffix = parse_dim_suffix(scene_name)
        if suffix is not None or is_reserved_scene_name(scene_name):
            raise ValueError("Scene names cannot end with _d1.._d4 or _toggle (reserved for virtual triggers)")

        # Enforce unique names among visible scenes (case-insensitive), excluding self
        for i, s in enumerate(config.scenes):
            if i == scene_idx:
                continue
            if not is_visible_scene(s):
                continue
            if s.name.lower() == scene_name.lower():
                raise ValueError("A scene with this name already exists")

        # Room assignment
        room_idx: Optional[int] = None
        if room_idx_raw not in (None, "", "none"):
            try:
                room_idx = int(room_idx_raw)
            except Exception:
                raise ValueError("Invalid room")
            if room_idx < 0 or room_idx >= len(config.rooms):
                raise ValueError("Invalid room")

        if dim_profile not in DIM_PROFILES:
            raise ValueError("Invalid dimming profile")

        # Preserve existing legacy grid_map, but new UI uses rooms
        config.scenes[scene_idx] = Scene(
            name=scene_name,
            actions=actions,
            grid_map=current.grid_map,
            room_idx=room_idx,
            dim_profile=dim_profile,
        )
        save_config(config)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid scene data: {str(e)}")

    return RedirectResponse(url="/scenes", status_code=303)

@app.post("/create-scene")
async def create_scene(request: Request):
    """Create a new scene."""
    form_data = await request.form()
    scene_name = form_data.get("name", "")
    actions_json = form_data.get("actions", "[]")
    room_idx_raw = form_data.get("room_idx")
    dim_profile = str(form_data.get("dim_profile", "linear") or "linear").strip().lower()
    
    try:
        actions_data = json.loads(actions_json)
        actions = [SceneAction(**action) for action in actions_data]
        validate_scene_actions(actions)
        # Prevent creating names that collide with derived dim scenes
        base_name, suffix = parse_dim_suffix(scene_name)
        if suffix is not None or is_reserved_scene_name(scene_name):
            raise ValueError("Scene names cannot end with _d1.._d4 or _toggle (reserved for virtual triggers)")

        # Enforce unique names among visible scenes (case-insensitive)
        for s in config.scenes:
            if not is_visible_scene(s):
                continue
            if s.name.lower() == scene_name.lower():
                raise ValueError("A scene with this name already exists")
        room_idx: Optional[int] = None
        if room_idx_raw not in (None, "", "none"):
            room_idx = int(room_idx_raw)
            if room_idx < 0 or room_idx >= len(config.rooms):
                raise ValueError("Invalid room")

        if dim_profile not in DIM_PROFILES:
            raise ValueError("Invalid dimming profile")

        scene = Scene(name=scene_name, actions=actions, room_idx=room_idx, dim_profile=dim_profile)
        config.scenes.append(scene)
        save_config(config)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid scene data: {str(e)}")
    
    return RedirectResponse(url="/scenes", status_code=303)

@app.post("/delete-scene/{scene_idx}")
async def delete_scene(scene_idx: int):
    """Delete a scene."""
    if scene_idx < 0 or scene_idx >= len(config.scenes):
        raise HTTPException(status_code=404, detail="Scene not found")
    
    config.scenes.pop(scene_idx)
    save_config(config)
    return RedirectResponse(url="/scenes", status_code=303)

# --- Rooms / Map ---

@app.get("/map", response_class=HTMLResponse)
async def rooms_page(request: Request):
    rooms = [{"idx": i, "name": r.name, "rows": r.rows, "cols": r.cols} for i, r in enumerate(config.rooms)]
    rooms.sort(key=lambda r: r["name"].lower())
    return templates.TemplateResponse("rooms.html", {"request": request, "rooms": rooms})

# --- Routines ---

@app.get("/routines", response_class=HTMLResponse)
async def routines_page(request: Request):
    edit_idx_raw = request.query_params.get("edit")
    edit_idx: Optional[int] = None
    edit_routine: Optional[Routine] = None
    edit_actions_js: Optional[list[dict]] = None
    if edit_idx_raw is not None:
        try:
            edit_idx = int(edit_idx_raw)
            if 0 <= edit_idx < len(config.routines):
                edit_routine = config.routines[edit_idx]
                edit_actions_js = [a.model_dump() for a in edit_routine.actions]
            else:
                edit_idx = None
        except Exception:
            edit_idx = None

    routines = [{"idx": i, **r.model_dump()} for i, r in enumerate(config.routines)]
    routines.sort(key=lambda r: r["name"].lower())

    scenes_js = [s.name for s in config.scenes if is_visible_scene(s)]
    scenes_js.sort(key=lambda s: s.lower())
    # include dim variants for convenience
    dim = []
    for s in scenes_js:
        if s.lower() == "testscene":
            continue
        dim.extend([f"{s}_d1", f"{s}_d2", f"{s}_d3", f"{s}_d4"])
    scenes_js = scenes_js + dim

    rooms_js = [{"idx": i, "name": r.name} for i, r in enumerate(config.rooms)]
    rooms_js.sort(key=lambda r: r["name"].lower())

    return templates.TemplateResponse("routines.html", {
        "request": request,
        "routines": routines,
        "scenes_js": scenes_js,
        "rooms_js": rooms_js,
        "edit_idx": edit_idx,
        "edit_routine": edit_routine,
        "edit_actions_js": edit_actions_js,
    })

@app.post("/routines/create")
async def create_routine(request: Request):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    time_hhmm = str(form.get("time_hhmm", "")).strip()
    enabled = form.get("enabled") == "1"
    actions_json = str(form.get("actions_json", "[]"))
    try:
        actions_data = json.loads(actions_json)
        actions = [RoutineAction(**a) for a in actions_data]
        if not name:
            raise ValueError("Missing name")
        if not time_hhmm or len(time_hhmm) != 5 or time_hhmm[2] != ":":
            raise ValueError("Invalid time")
        config.routines.append(Routine(name=name, time_hhmm=time_hhmm, enabled=enabled, actions=actions))
        save_config(config)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url="/routines", status_code=303)

@app.post("/routines/update/{routine_idx}")
async def update_routine(routine_idx: int, request: Request):
    if routine_idx < 0 or routine_idx >= len(config.routines):
        raise HTTPException(status_code=404, detail="Routine not found")
    form = await request.form()
    name = str(form.get("name", "")).strip()
    time_hhmm = str(form.get("time_hhmm", "")).strip()
    enabled = form.get("enabled") == "1"
    actions_json = str(form.get("actions_json", "[]"))
    try:
        actions_data = json.loads(actions_json)
        actions = [RoutineAction(**a) for a in actions_data]
        if not name:
            raise ValueError("Missing name")
        if not time_hhmm or len(time_hhmm) != 5 or time_hhmm[2] != ":":
            raise ValueError("Invalid time")
        config.routines[routine_idx] = Routine(
            name=name,
            time_hhmm=time_hhmm,
            enabled=enabled,
            actions=actions,
            last_run_date=config.routines[routine_idx].last_run_date,
        )
        save_config(config)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url="/routines", status_code=303)

@app.post("/routines/delete/{routine_idx}")
async def delete_routine(routine_idx: int):
    if routine_idx < 0 or routine_idx >= len(config.routines):
        raise HTTPException(status_code=404, detail="Routine not found")
    config.routines.pop(routine_idx)
    save_config(config)
    return RedirectResponse(url="/routines", status_code=303)

@app.post("/routines/run/{routine_idx}")
async def run_routine_now(routine_idx: int):
    await run_routine(routine_idx)
    return RedirectResponse(url="/routines", status_code=303)

@app.post("/map/create-room")
async def create_room(request: Request):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        return RedirectResponse(url="/map", status_code=303)

    if any(r.name.lower() == name.lower() for r in config.rooms):
        return RedirectResponse(url="/map", status_code=303)

    config.rooms.append(Room(name=name, rows=8, cols=8, grid_map=[None] * 64))
    save_config(config)
    return RedirectResponse(url="/map", status_code=303)

@app.post("/map/rename-room/{room_idx}")
async def rename_room(room_idx: int, request: Request):
    if room_idx < 0 or room_idx >= len(config.rooms):
        raise HTTPException(status_code=404, detail="Room not found")
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        return RedirectResponse(url="/map", status_code=303)
    # enforce unique name
    for i, r in enumerate(config.rooms):
        if i == room_idx:
            continue
        if r.name.lower() == name.lower():
            return RedirectResponse(url="/map", status_code=303)
    config.rooms[room_idx].name = name
    save_config(config)
    return RedirectResponse(url="/map", status_code=303)

@app.post("/map/delete-room/{room_idx}")
async def delete_room(room_idx: int):
    if room_idx < 0 or room_idx >= len(config.rooms):
        raise HTTPException(status_code=404, detail="Room not found")

    # Unassign from scenes
    for s in config.scenes:
        if s.room_idx == room_idx:
            s.room_idx = None
        elif s.room_idx is not None and s.room_idx > room_idx:
            s.room_idx -= 1

    config.rooms.pop(room_idx)
    save_config(config)
    return RedirectResponse(url="/map", status_code=303)

@app.get("/map/room", response_class=HTMLResponse)
async def room_map_page(request: Request):
    idx_raw = request.query_params.get("room")
    if idx_raw is None:
        raise HTTPException(status_code=400, detail="Missing room index")
    try:
        room_idx = int(idx_raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid room index")
    if room_idx < 0 or room_idx >= len(config.rooms):
        raise HTTPException(status_code=404, detail="Room not found")

    room = config.rooms[room_idx]
    sorted_devices = sorted(config.devices, key=lambda d: d.alias.lower())
    devices_js = [{"alias": d.alias, "type": d.type} for d in sorted_devices]
    grid_js = ensure_grid_map(room)
    return templates.TemplateResponse("room_map.html", {
        "request": request,
        "room_idx": room_idx,
        "room_name": room.name,
        "room_rows": room.rows,
        "room_cols": room.cols,
        "devices_js": devices_js,
        "grid_js": grid_js,
    })

@app.post("/api/rooms/{room_idx}/resize")
async def api_resize_room(room_idx: int, request: Request):
    if room_idx < 0 or room_idx >= len(config.rooms):
        raise HTTPException(status_code=404, detail="Room not found")
    room = config.rooms[room_idx]
    payload = await request.json()
    rows = payload.get("rows")
    cols = payload.get("cols")
    try:
        rows = int(rows)
        cols = int(cols)
    except Exception:
        raise HTTPException(status_code=400, detail="rows and cols must be integers")
    if rows < 1 or cols < 1 or rows > 20 or cols > 20:
        raise HTTPException(status_code=400, detail="rows/cols must be between 1 and 20")

    old_rows = int(room.rows)
    old_cols = int(room.cols)
    old_grid = room.grid_map or ([None] * (old_rows * old_cols))
    # normalize old grid length
    if len(old_grid) < old_rows * old_cols:
        old_grid = old_grid + [None] * ((old_rows * old_cols) - len(old_grid))
    elif len(old_grid) > old_rows * old_cols:
        old_grid = old_grid[: old_rows * old_cols]

    min_rows, min_cols = _used_bounds(old_grid, old_cols)
    if rows < min_rows or cols < min_cols:
        raise HTTPException(status_code=400, detail=f"Cannot shrink below used tiles (min rows={min_rows}, min cols={min_cols})")

    room.grid_map = _remap_grid(old_grid, old_rows, old_cols, rows, cols)
    room.rows = rows
    room.cols = cols
    save_config(config)
    return {"status": "success", "rows": rows, "cols": cols}

@app.get("/api/rooms/{room_idx}/map")
async def api_get_room_map(room_idx: int):
    if room_idx < 0 or room_idx >= len(config.rooms):
        raise HTTPException(status_code=404, detail="Room not found")
    room = config.rooms[room_idx]
    return {"room": room.name, "grid": ensure_grid_map(room)}

@app.post("/api/rooms/{room_idx}/map")
async def api_set_room_map(room_idx: int, request: Request):
    if room_idx < 0 or room_idx >= len(config.rooms):
        raise HTTPException(status_code=404, detail="Room not found")
    room = config.rooms[room_idx]

    payload = await request.json()
    grid = payload.get("grid")
    expected_len = int(room.rows) * int(room.cols)
    if not isinstance(grid, list) or len(grid) != expected_len:
        raise HTTPException(status_code=400, detail=f"grid must be a list of length {expected_len}")

    new_grid: list[Optional[SceneAction]] = []
    for cell in grid:
        if cell is None:
            new_grid.append(None)
        elif isinstance(cell, dict):
            try:
                new_grid.append(SceneAction(**cell))
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid cell: {e}")
        else:
            raise HTTPException(status_code=400, detail="Each grid cell must be null or an object")

    # Enforce uniqueness across all rooms: device can exist in only one tile globally.
    for idx, cell in enumerate(new_grid):
        if cell is None:
            continue
        _remove_device_from_all_rooms(cell.device_alias, except_room_idx=room_idx, except_cell_idx=idx)

    room.grid_map = new_grid
    save_config(config)
    return {"status": "success"}

@app.get("/scenes/map", response_class=HTMLResponse)
async def scene_map_page(request: Request):
    """Deprecated: scene mapping moved to Rooms. Keep route for compatibility."""
    return RedirectResponse(url="/map", status_code=303)

@app.get("/api/scenes/{scene_idx}/map")
async def api_get_scene_map(scene_idx: int):
    raise HTTPException(status_code=410, detail="Scene maps moved to rooms. Use /api/rooms/{room_idx}/map")

@app.post("/api/scenes/{scene_idx}/map")
async def api_set_scene_map(scene_idx: int, request: Request):
    raise HTTPException(status_code=410, detail="Scene maps moved to rooms. Use /api/rooms/{room_idx}/map")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)