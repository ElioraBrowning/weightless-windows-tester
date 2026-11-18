"""Shared persistent configuration and per-app memory store for Woolbur & Weightless Tester."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_LOCK = threading.RLock()
_CONFIG_MTIME_NS: Optional[int] = None
_CONFIG_CACHE: Optional[dict[str, Any]] = None
_MEMORY_MTIME_NS: Optional[int] = None
_MEMORY_CACHE: Optional[dict[str, Any]] = None

DEFAULT_CONFIG: dict[str, Any] = {
    "appearance": {
        "accessory_set": "auto",  # auto | nerd | detective | punk | goth | happy | none
        "mood": "auto",  # auto | nerd | detective | punk | goth | happy
        "scale": 1.0,
        "opacity": 0.95,
        "show_hud": True,
        "show_woolbur": True,
        "follow_cursor": True,
        "accent_color": "#38bdf8",
        "wool_color": "#fffbf5",
        "horn_color": "#fde68a",
    },
    "settings": {
        "default_screenshot_scale": 0.5,
        "default_screenshot_format": "jpeg",
        "default_grid": True,
        "force_fullscreen": True,
        "triple_esc_window_seconds": 1.8,
        "assist_move_threshold_px": 45.0,
        "auto_save_app_memory": True,
    },
}


def get_data_dir() -> Path:
    custom = os.environ.get("DESKTOP_TESTER_DATA_DIR", "").strip()
    base = Path(custom).expanduser() if custom else Path.home() / ".local_desktop_agent"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    return get_data_dir() / "woolbur_config.json"


def app_memory_path() -> Path:
    return get_data_dir() / "app_memory.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _clamp_float(val: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


def _normalize_hex(val: Any, default: str) -> str:
    s = str(val or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", s):
        return s.lower()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", s):
        r, g, b = s[1], s[2], s[3]
        return f"#{r}{r}{g}{g}{b}{b}".lower()
    return default


def parse_hex_rgba(hex_str: Any, default_rgb: tuple[int, int, int], alpha: int = 255) -> tuple[int, int, int, int]:
    norm = _normalize_hex(hex_str, "")
    if len(norm) == 7:
        return (int(norm[1:3], 16), int(norm[3:5], 16), int(norm[5:7], 16), int(alpha))
    return (default_rgb[0], default_rgb[1], default_rgb[2], int(alpha))


def _normalize_config(raw: Any) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg
    app_in = raw.get("appearance")
    if isinstance(app_in, dict):
        valid_acc = ("auto", "nerd", "detective", "punk", "goth", "happy", "none")
        acc_in = str(app_in.get("accessory_set", "")).strip().lower()
        mood = str(app_in.get("mood", cfg["appearance"]["mood"])).strip().lower()
        if mood in ("auto", "nerd", "detective", "punk", "goth", "happy"):
            cfg["appearance"]["mood"] = mood
        if acc_in in valid_acc:
            cfg["appearance"]["accessory_set"] = acc_in
            if "mood" not in app_in and acc_in in ("auto", "nerd", "detective", "punk", "goth", "happy"):
                cfg["appearance"]["mood"] = acc_in
        elif mood in valid_acc:
            cfg["appearance"]["accessory_set"] = mood
        cfg["appearance"]["scale"] = round(_clamp_float(app_in.get("scale"), 0.65, 1.6, 1.0), 2)
        cfg["appearance"]["opacity"] = round(_clamp_float(app_in.get("opacity"), 0.4, 1.0, 0.95), 2)
        for key in ("show_hud", "show_woolbur", "follow_cursor"):
            if key in app_in:
                cfg["appearance"][key] = bool(app_in[key])
        cfg["appearance"]["accent_color"] = _normalize_hex(
            app_in.get("accent_color"), cfg["appearance"]["accent_color"]
        )
        cfg["appearance"]["wool_color"] = _normalize_hex(
            app_in.get("wool_color"), cfg["appearance"]["wool_color"]
        )
        cfg["appearance"]["horn_color"] = _normalize_hex(
            app_in.get("horn_color"), cfg["appearance"]["horn_color"]
        )

    set_in = raw.get("settings")
    if isinstance(set_in, dict):
        cfg["settings"]["default_screenshot_scale"] = round(
            _clamp_float(set_in.get("default_screenshot_scale"), 0.25, 1.0, 0.5), 2
        )
        fmt = str(set_in.get("default_screenshot_format", "jpeg")).strip().lower()
        if fmt in ("jpeg", "png"):
            cfg["settings"]["default_screenshot_format"] = fmt
        for key in ("default_grid", "force_fullscreen", "auto_save_app_memory"):
            if key in set_in:
                cfg["settings"][key] = bool(set_in[key])
        cfg["settings"]["triple_esc_window_seconds"] = round(
            _clamp_float(set_in.get("triple_esc_window_seconds"), 0.8, 4.0, 1.8), 2
        )
        cfg["settings"]["assist_move_threshold_px"] = round(
            _clamp_float(set_in.get("assist_move_threshold_px"), 20.0, 150.0, 45.0), 1
        )
    return cfg


def load_config_cached() -> dict[str, Any]:
    """Load woolbur_config.json with stat() mtime caching, creating defaults on first run."""
    global _CONFIG_MTIME_NS, _CONFIG_CACHE
    path = config_path()
    with _LOCK:
        try:
            st = path.stat()
            if _CONFIG_CACHE is not None and _CONFIG_MTIME_NS == st.st_mtime_ns:
                return _CONFIG_CACHE
            raw = json.loads(path.read_text(encoding="utf-8"))
            cfg = _normalize_config(raw)
            _CONFIG_CACHE = cfg
            _CONFIG_MTIME_NS = st.st_mtime_ns
            return cfg
        except FileNotFoundError:
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            try:
                _atomic_write_json(path, cfg)
                _CONFIG_MTIME_NS = path.stat().st_mtime_ns
            except OSError:
                _CONFIG_MTIME_NS = None
            _CONFIG_CACHE = cfg
            return cfg
        except Exception:
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            _CONFIG_CACHE = cfg
            return cfg


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge partial appearance/settings updates atomically into woolbur_config.json."""
    global _CONFIG_MTIME_NS, _CONFIG_CACHE
    with _LOCK:
        curr = copy.deepcopy(load_config_cached())
        for sec in ("appearance", "settings"):
            if isinstance(patch.get(sec), dict):
                curr[sec].update(patch[sec])
        norm = _normalize_config(curr)
        p = config_path()
        _atomic_write_json(p, norm)
        _CONFIG_CACHE = norm
        _CONFIG_MTIME_NS = p.stat().st_mtime_ns
        return norm


def canonical_app_id(raw_name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(raw_name or "").strip().lower()).strip("._-")
    return cleaned or "unknown_app"


def load_app_memory() -> dict[str, Any]:
    """Load app_memory.json with stat() mtime caching."""
    global _MEMORY_MTIME_NS, _MEMORY_CACHE
    path = app_memory_path()
    with _LOCK:
        try:
            st = path.stat()
            if _MEMORY_CACHE is not None and _MEMORY_MTIME_NS == st.st_mtime_ns:
                return copy.deepcopy(_MEMORY_CACHE)
            raw = json.loads(path.read_text(encoding="utf-8"))
            apps = raw.get("apps") if isinstance(raw, dict) and isinstance(raw.get("apps"), dict) else {}
            data = {"apps": apps}
            _MEMORY_CACHE = data
            _MEMORY_MTIME_NS = st.st_mtime_ns
            return copy.deepcopy(data)
        except FileNotFoundError:
            data = {"apps": {}}
            try:
                _atomic_write_json(path, data)
                _MEMORY_MTIME_NS = path.stat().st_mtime_ns
            except OSError:
                _MEMORY_MTIME_NS = None
            _MEMORY_CACHE = data
            return copy.deepcopy(data)
        except Exception:
            return {"apps": {}}


def _match_app_key(
    apps: dict[str, Any],
    app_name: Optional[str] = None,
    process_name: Optional[str] = None,
    title: Optional[str] = None,
) -> Optional[str]:
    needles = [s.strip().lower() for s in (app_name, process_name) if s and s.strip()]
    for needle in needles:
        cid = canonical_app_id(needle)
        if cid in apps:
            return cid
        stem = needle.removesuffix(".exe")
        if stem in apps:
            return stem
        for key, entry in apps.items():
            if not isinstance(entry, dict):
                continue
            procs = [str(p).strip().lower() for p in entry.get("process_names", [])]
            disp = str(entry.get("display_name", "")).strip().lower()
            pats = [str(p).strip().lower() for p in entry.get("title_patterns", [])]
            if (
                needle in procs
                or stem in [p.removesuffix(".exe") for p in procs]
                or needle == disp
                or any(p and (p == needle or p in needle) for p in pats)
            ):
                return key
    t_low = (title or "").strip().lower()
    if t_low:
        for key, entry in apps.items():
            if not isinstance(entry, dict):
                continue
            disp = str(entry.get("display_name", "")).strip().lower()
            pats = [str(p).strip().lower() for p in entry.get("title_patterns", [])]
            if (disp and disp in t_low) or any(p and p in t_low for p in pats):
                return key
    return None


def find_app_memory(
    app_name: Optional[str] = None,
    process_name: Optional[str] = None,
    title: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    data = load_app_memory()
    apps = data["apps"]
    key = _match_app_key(apps, app_name=app_name, process_name=process_name, title=title)
    if key is None:
        return None
    entry = copy.deepcopy(apps[key])
    entry["app_id"] = key
    return entry


def upsert_app_memory(
    app_name: str,
    display_name: Optional[str] = None,
    process_names: Optional[list[str]] = None,
    title_patterns: Optional[list[str]] = None,
    add_note: Optional[str] = None,
    remove_note: Optional[str] = None,
    coordinates: Optional[dict[str, Any]] = None,
    remove_coordinates: Optional[list[str]] = None,
    hotkeys: Optional[dict[str, str]] = None,
    settings: Optional[dict[str, Any]] = None,
    clear: bool = False,
) -> dict[str, Any]:
    """Create, update, or clear a per-app memory record atomically in app_memory.json."""
    global _MEMORY_MTIME_NS, _MEMORY_CACHE
    with _LOCK:
        data = load_app_memory()
        apps = data["apps"]
        key = _match_app_key(apps, app_name=app_name) or canonical_app_id(app_name)
        if clear:
            removed = apps.pop(key, None)
            p = app_memory_path()
            _atomic_write_json(p, data)
            _MEMORY_CACHE = data
            _MEMORY_MTIME_NS = p.stat().st_mtime_ns
            return {"app_id": key, "cleared": removed is not None}

        entry = apps.get(key)
        if not isinstance(entry, dict):
            default_disp = app_name.removesuffix(".exe").removesuffix(".EXE").strip() or key
            entry = {
                "app_id": key,
                "display_name": display_name or default_disp,
                "process_names": [app_name] if app_name.lower().endswith(".exe") else [],
                "title_patterns": [default_disp],
                "notes": [],
                "coordinates": {},
                "hotkeys": {},
                "settings": {},
                "updated_at": "",
            }
        if display_name:
            entry["display_name"] = display_name.strip()
        if process_names:
            existing_p = {str(x).strip(): x for x in entry.get("process_names", []) if str(x).strip()}
            for pn in process_names:
                clean = str(pn).strip()
                if clean:
                    existing_p[clean] = clean
            entry["process_names"] = list(existing_p.keys())
        if title_patterns:
            existing_t = list(dict.fromkeys([*entry.get("title_patterns", []), *[str(t).strip() for t in title_patterns if str(t).strip()]]))
            entry["title_patterns"] = existing_t

        notes = [str(n).strip() for n in entry.get("notes", []) if str(n).strip()]
        if remove_note:
            rem_low = remove_note.strip().lower()
            notes = [n for n in notes if n.lower() != rem_low and rem_low not in n.lower()]
        if add_note:
            clean_note = " ".join(str(add_note).split()).strip()
            if clean_note and clean_note not in notes:
                notes.append(clean_note)
        entry["notes"] = notes

        coords = dict(entry.get("coordinates") or {})
        if remove_coordinates:
            for rk in remove_coordinates:
                coords.pop(str(rk), None)
        if coordinates:
            for ck, cv in coordinates.items():
                if isinstance(cv, dict) and "x" in cv and "y" in cv:
                    item = {"x": int(cv["x"]), "y": int(cv["y"])}
                    if cv.get("label"):
                        item["label"] = str(cv["label"])
                    if cv.get("description"):
                        item["description"] = str(cv["description"]).strip()
                    coords[str(ck)] = item
                elif isinstance(cv, (list, tuple)) and len(cv) >= 2:
                    coords[str(ck)] = {"x": int(cv[0]), "y": int(cv[1])}
        entry["coordinates"] = coords

        hk = dict(entry.get("hotkeys") or {})
        if hotkeys:
            for hk_name, hk_val in hotkeys.items():
                if hk_val is None or str(hk_val).strip() == "":
                    hk.pop(str(hk_name), None)
                else:
                    hk[str(hk_name)] = str(hk_val).strip()
        entry["hotkeys"] = hk

        st_map = dict(entry.get("settings") or {})
        if settings:
            st_map.update(settings)
        entry["settings"] = st_map

        entry["app_id"] = key
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        apps[key] = entry
        p = app_memory_path()
        _atomic_write_json(p, data)
        _MEMORY_CACHE = data
        _MEMORY_MTIME_NS = p.stat().st_mtime_ns
        return copy.deepcopy(entry)
