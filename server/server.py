"""Local Desktop Vision & Cursor Control MCP Server for Windows.

Provides OS-level screen inspection, gridded screenshots with 1 GB storage alerts,
offline Windows.Media.Ocr text localization, window management, cursor/keyboard
control, clipboard operations, multi-strategy file drag-and-drop, and a hardware
emergency abort listener.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any, Literal, Optional


if sys.platform != "win32":
    raise RuntimeError("desktop-tester MCP server requires Windows 10/11.")


def _enable_per_monitor_v2_dpi() -> str:
    """Enable Per-Monitor v2 DPI awareness before importing GUI modules."""
    u32 = ctypes.windll.user32
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (DPI_AWARENESS_CONTEXT)-4
        u32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per_monitor_v2"
    except Exception:
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per_monitor_v1"
    except Exception:
        pass
    try:
        u32.SetProcessDPIAware()
        return "system_dpi_aware"
    except Exception:
        return "unaware"


DPI_MODE = _enable_per_monitor_v2_dpi()

import mss
import pyautogui
import pyperclip
from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent
from PIL import Image, ImageDraw, ImageFont
from pynput import keyboard as pynput_keyboard
import winrt.windows.globalization as winrt_glob
import winrt.windows.graphics.imaging as winrt_imaging
import winrt.windows.media.ocr as winrt_ocr
import winrt.windows.storage.streams as winrt_streams


# ---------------------------------------------------------------------------
# Paths, Logging, and Failsafe Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(
    os.environ.get("DESKTOP_TESTER_DATA_DIR", Path.home() / ".local_desktop_agent")
).expanduser().resolve()
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
LOGS_DIR = BASE_DIR / "logs"
STORAGE_STATE_PATH = BASE_DIR / "storage_state.json"

SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("desktop_vision_mcp")
logger.setLevel(logging.INFO)
if not logger.handlers:
    file_handler = RotatingFileHandler(
        LOGS_DIR / "server.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(file_handler)

# Disable PyAutoGUI corner failsafe per user hardware requirement (mouse drift).
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.02

ONE_GIB = 1024 ** 3


# ---------------------------------------------------------------------------
# Win32 Constants & Structs (64-bit Safe)
# ---------------------------------------------------------------------------

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

SW_RESTORE = 9
MONITORINFOF_PRIMARY = 1

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_TAB = 0x09
VK_MENU = 0x12

WM_DROPFILES = 0x0233
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
PAGE_READWRITE = 0x04


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


class DROPFILES(ctypes.Structure):
    _fields_ = [
        ("pFiles", wintypes.DWORD),
        ("pt", wintypes.POINT),
        ("fNC", wintypes.BOOL),
        ("fWide", wintypes.BOOL),
    ]


# ---------------------------------------------------------------------------
# Emergency Hardware Abort Listener (Pause or Ctrl+Shift+Esc / Ctrl+Alt+Esc)
# ---------------------------------------------------------------------------

_ABORT_EVENT = threading.Event()
_ABORT_LOCK = threading.Lock()
_LAST_ABORT_TIME: float = 0.0
_PRESSED_KEYS: set[Any] = set()
_ABORT_LISTENER_STARTED = False


def _is_ctrl_pressed() -> bool:
    return any(
        k in _PRESSED_KEYS
        for k in (
            pynput_keyboard.Key.ctrl,
            pynput_keyboard.Key.ctrl_l,
            pynput_keyboard.Key.ctrl_r,
        )
    )


def _is_shift_pressed() -> bool:
    return any(
        k in _PRESSED_KEYS
        for k in (
            pynput_keyboard.Key.shift,
            pynput_keyboard.Key.shift_l,
            pynput_keyboard.Key.shift_r,
        )
    )


def _is_alt_pressed() -> bool:
    return any(
        k in _PRESSED_KEYS
        for k in (
            pynput_keyboard.Key.alt,
            pynput_keyboard.Key.alt_l,
            pynput_keyboard.Key.alt_r,
            pynput_keyboard.Key.alt_gr,
        )
    )


def _trigger_abort(reason: str) -> None:
    global _LAST_ABORT_TIME
    with _ABORT_LOCK:
        _ABORT_EVENT.set()
        _LAST_ABORT_TIME = time.time()
    logger.warning("EMERGENCY ABORT triggered: %s", reason)


def _on_key_press(key: Any) -> None:
    _PRESSED_KEYS.add(key)
    if key == pynput_keyboard.Key.pause:
        _trigger_abort("Pause key")
    elif key == pynput_keyboard.Key.esc and _is_ctrl_pressed() and (
        _is_shift_pressed() or _is_alt_pressed()
    ):
        _trigger_abort("Ctrl+Shift/Alt+Esc")


def _on_key_release(key: Any) -> None:
    _PRESSED_KEYS.discard(key)


def _ensure_abort_listener() -> None:
    global _ABORT_LISTENER_STARTED
    if _ABORT_LISTENER_STARTED:
        return
    try:
        listener = pynput_keyboard.Listener(
            on_press=_on_key_press,
            on_release=_on_key_release,
            daemon=True,
        )
        listener.start()
        _ABORT_LISTENER_STARTED = True
        logger.info("Emergency abort keyboard listener started (Pause / Ctrl+Shift+Esc / Ctrl+Alt+Esc).")
    except Exception as exc:
        logger.warning("Failed to start emergency abort listener: %s", exc)


def _begin_input_action() -> None:
    """Clear any prior abort flag before beginning a new input action."""
    _ensure_abort_listener()
    with _ABORT_LOCK:
        _ABORT_EVENT.clear()


def _check_abort() -> None:
    """Raise RuntimeError immediately if an emergency abort was triggered."""
    if _ABORT_EVENT.is_set():
        raise RuntimeError(
            "EMERGENCY ABORT triggered via hardware hotkey (Pause / Ctrl+Shift+Esc / Ctrl+Alt+Esc). "
            "Input sequence halted immediately."
        )


# ---------------------------------------------------------------------------
# Monitor Discovery, Capture, Grid Overlay, and Storage Monitoring
# ---------------------------------------------------------------------------

def _get_win32_primary_rects() -> set[tuple[int, int, int, int]]:
    """Return the set of primary monitor (left, top, width, height) tuples."""
    primaries: set[tuple[int, int, int, int]] = set()
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )

    def _callback(hmonitor, _hdc, _lprect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            if info.dwFlags & MONITORINFOF_PRIMARY:
                rc = info.rcMonitor
                primaries.add(
                    (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top)
                )
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_callback), 0)
    return primaries


def _create_mss():
    """Return an mss capture instance without deprecation warnings."""
    mss_cls = getattr(mss, "MSS", None) or mss.mss
    return mss_cls()


def _collect_monitor_layout() -> dict[str, Any]:
    """Collect virtual desktop bounds and indexed monitor descriptors.

    Indexing convention:
    - monitor_index = 0: Primary monitor
    - monitor_index = 1..N: Individual monitors (mss physical display list)
    - monitor_index = -1: All monitors stitched together (virtual desktop)
    """
    primaries = _get_win32_primary_rects()
    with _create_mss() as sct:
        raw_mons = sct.monitors
        virtual = {
            "monitor_index": -1,
            "left": int(raw_mons[0]["left"]),
            "top": int(raw_mons[0]["top"]),
            "width": int(raw_mons[0]["width"]),
            "height": int(raw_mons[0]["height"]),
            "is_virtual_all": True,
        }
        physical: list[dict[str, Any]] = []
        primary_rect: Optional[dict[str, Any]] = None
        for idx, m in enumerate(raw_mons[1:], start=1):
            rect_tuple = (
                int(m["left"]),
                int(m["top"]),
                int(m["width"]),
                int(m["height"]),
            )
            is_prim = rect_tuple in primaries or (not primaries and idx == 1)
            entry = {
                "monitor_index": idx,
                "left": rect_tuple[0],
                "top": rect_tuple[1],
                "width": rect_tuple[2],
                "height": rect_tuple[3],
                "is_primary": is_prim,
            }
            physical.append(entry)
            if is_prim and primary_rect is None:
                primary_rect = {
                    "monitor_index": 0,
                    "physical_index": idx,
                    "left": entry["left"],
                    "top": entry["top"],
                    "width": entry["width"],
                    "height": entry["height"],
                    "is_primary": True,
                }
        if primary_rect is None and physical:
            first = physical[0]
            primary_rect = {
                "monitor_index": 0,
                "physical_index": 1,
                "left": first["left"],
                "top": first["top"],
                "width": first["width"],
                "height": first["height"],
                "is_primary": True,
            }
    return {
        "dpi_mode": DPI_MODE,
        "virtual_screen": virtual,
        "primary_monitor": primary_rect,
        "monitors": physical,
    }


def _resolve_capture_bounds(
    monitor_index: int = 0, region: Optional[tuple[int, int, int, int]] = None
) -> dict[str, int]:
    """Resolve global desktop capture bounds from monitor_index or region."""
    if region is not None:
        if len(region) != 4:
            raise ValueError("region must be a 4-element tuple/list (x, y, width, height)")
        x, y, w, h = (int(v) for v in region)
        if w <= 0 or h <= 0:
            raise ValueError(f"region width and height must be positive, got w={w}, h={h}")
        return {"left": x, "top": y, "width": w, "height": h}

    layout = _collect_monitor_layout()
    if monitor_index == -1:
        v = layout["virtual_screen"]
        return {"left": v["left"], "top": v["top"], "width": v["width"], "height": v["height"]}
    if monitor_index == 0:
        p = layout["primary_monitor"]
        if p is None:
            raise RuntimeError("No active monitors found")
        return {"left": p["left"], "top": p["top"], "width": p["width"], "height": p["height"]}
    for mon in layout["monitors"]:
        if mon["monitor_index"] == monitor_index:
            return {
                "left": mon["left"],
                "top": mon["top"],
                "width": mon["width"],
                "height": mon["height"],
            }
    available = [m["monitor_index"] for m in layout["monitors"]]
    raise ValueError(
        f"Invalid monitor_index={monitor_index}. Use 0 (primary), -1 (all monitors), "
        f"or one of {available}."
    )


def _capture_region_rgba(bounds: dict[str, int]) -> Image.Image:
    """Capture a global desktop rectangle as an RGBA PIL Image."""
    with _create_mss() as sct:
        shot = sct.grab(bounds)
        return Image.frombytes("RGBA", shot.size, shot.bgra, "raw", "BGRA")


def _load_font(size: int = 13) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for candidate in ("consola.ttf", "arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_coordinate_grid(
    img: Image.Image, origin_x: int, origin_y: int, grid_interval: int
) -> Image.Image:
    """Overlay pixel grid lines and global (x, y) markers onto the screenshot."""
    interval = max(20, int(grid_interval))
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _load_font(12)

    width, height = img.size
    major_color = (255, 64, 64, 155)
    sub_color = (255, 165, 0, 70)
    bg_color = (15, 15, 20, 210)
    text_color = (255, 255, 120, 255)

    # Optional subtle sub-grid at half interval for finer visual reading
    if interval >= 80:
        half = interval // 2
        for lx in range(half, width, interval):
            draw.line([(lx, 0), (lx, height)], fill=sub_color, width=1)
        for ly in range(half, height, interval):
            draw.line([(0, ly), (width, ly)], fill=sub_color, width=1)

    # Major vertical grid lines & top/bottom X coordinate tags
    for lx in range(0, width, interval):
        gx = origin_x + lx
        draw.line([(lx, 0), (lx, height)], fill=major_color, width=1)
        label = str(gx)
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = min(max(lx + 2, 2), max(2, width - tw - 6))
        draw.rectangle([tx - 2, 2, tx + tw + 4, 2 + th + 4], fill=bg_color)
        draw.text((tx + 1, 3), label, font=font, fill=text_color)

    # Major horizontal grid lines & left-edge Y coordinate tags
    for ly in range(0, height, interval):
        gy = origin_y + ly
        draw.line([(0, ly), (width, ly)], fill=major_color, width=1)
        if ly == 0:
            continue
        label = str(gy)
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        ty = min(max(ly + 2, 2), max(2, height - th - 6))
        draw.rectangle([2, ty - 2, 2 + tw + 6, ty + th + 4], fill=bg_color)
        draw.text((4, ty), label, font=font, fill=text_color)

    # Intersection markers every 2x major intervals so deep interior points are easy to read
    step = interval * 2
    for lx in range(step, width - 80, step):
        for ly in range(step, height - 30, step):
            gx = origin_x + lx
            gy = origin_y + ly
            label = f"{gx},{gy}"
            bbox = draw.textbbox((0, 0), label, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.rectangle([lx + 2, ly + 2, lx + tw + 6, ly + th + 5], fill=(15, 15, 20, 165))
            draw.text((lx + 4, ly + 3), label, font=font, fill=(140, 255, 180, 235))

    return Image.alpha_composite(img, overlay)


def _check_screenshot_storage() -> dict[str, Any]:
    """Compute screenshot storage size and emit alerts on every 1 GiB increment."""
    total_bytes = 0
    file_count = 0
    for entry in SCREENSHOTS_DIR.rglob("*"):
        if entry.is_file():
            try:
                total_bytes += entry.stat().st_size
                file_count += 1
            except OSError:
                pass

    current_gb_tier = int(total_bytes // ONE_GIB)
    last_alerted_tier = 0
    if STORAGE_STATE_PATH.exists():
        try:
            state = json.loads(STORAGE_STATE_PATH.read_text(encoding="utf-8"))
            last_alerted_tier = int(state.get("last_alerted_gb_tier", 0))
        except Exception:
            last_alerted_tier = 0

    crossed_new_tier = current_gb_tier >= 1 and current_gb_tier > last_alerted_tier
    if crossed_new_tier:
        try:
            STORAGE_STATE_PATH.write_text(
                json.dumps(
                    {
                        "last_alerted_gb_tier": current_gb_tier,
                        "updated_at": datetime.now().isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Failed to update storage_state.json: %s", exc)
    elif current_gb_tier < last_alerted_tier:
        # User cleaned up screenshots; reset tracked tier so future 1 GB crossings alert again.
        try:
            STORAGE_STATE_PATH.write_text(
                json.dumps(
                    {
                        "last_alerted_gb_tier": current_gb_tier,
                        "updated_at": datetime.now().isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    total_mb = round(total_bytes / (1024 ** 2), 2)
    total_gb = round(total_bytes / ONE_GIB, 4)
    alert_msg: Optional[str] = None
    if crossed_new_tier:
        alert_msg = (
            f"STORAGE ALERT: Cumulative screenshot storage in '{SCREENSHOTS_DIR}' "
            f"just crossed {current_gb_tier} GB ({total_gb:.2f} GiB across {file_count} files). "
            "Inform the user so they can archive or prune old screenshots if desired."
        )
    elif current_gb_tier >= 1:
        alert_msg = (
            f"STORAGE NOTICE: Screenshot directory currently holds {total_gb:.2f} GiB "
            f"({file_count} files, tier >= {current_gb_tier} GB)."
        )

    return {
        "directory": str(SCREENSHOTS_DIR),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "total_mb": total_mb,
        "total_gb": total_gb,
        "current_gb_tier": current_gb_tier,
        "crossed_new_1gb_increment": crossed_new_tier,
        "alert": alert_msg,
    }


# ---------------------------------------------------------------------------
# Window Enumeration & Focus Helpers
# ---------------------------------------------------------------------------

def _enum_visible_windows(include_minimized: bool = True) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        is_minimized = bool(user32.IsIconic(hwnd))
        if is_minimized and not include_minimized:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True

        rc = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        windows.append(
            {
                "hwnd": int(hwnd),
                "pid": int(pid.value),
                "title": title,
                "is_minimized": is_minimized,
                "bounds": {
                    "x": int(rc.left),
                    "y": int(rc.top),
                    "w": int(rc.right - rc.left),
                    "h": int(rc.bottom - rc.top),
                },
            }
        )
        return True

    user32.EnumWindows(WNDENUMPROC(_callback), 0)
    return windows


# ---------------------------------------------------------------------------
# SendInput Unicode Typing & Key Normalization
# ---------------------------------------------------------------------------

def _send_unicode_char(ch: str) -> None:
    """Send a single character using Win32 SendInput KEYEVENTF_UNICODE."""
    if ch == "\n":
        pyautogui.press("enter")
        return
    if ch == "\t":
        pyautogui.press("tab")
        return

    code = ord(ch)
    # Encode astral characters (> 0xFFFF) as UTF-16 surrogate pairs
    units = (
        [int.from_bytes(b, "little") for b in (ch.encode("utf-16-le")[0:2], ch.encode("utf-16-le")[2:4])]
        if code > 0xFFFF
        else [code]
    )
    events: list[INPUT] = []
    for unit in units:
        down = INPUT(type=INPUT_KEYBOARD)
        down.union.ki = KEYBDINPUT(wVk=0, wScan=unit, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=0)
        up = INPUT(type=INPUT_KEYBOARD)
        up.union.ki = KEYBDINPUT(
            wVk=0,
            wScan=unit,
            dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
            time=0,
            dwExtraInfo=0,
        )
        events.extend((down, up))

    arr = (INPUT * len(events))(*events)
    user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


_KEY_ALIASES = {
    "control": "ctrl",
    "lctrl": "ctrlleft",
    "rctrl": "ctrlright",
    "escape": "esc",
    "return": "enter",
    "del": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "win": "winleft",
    "super": "winleft",
    "cmd": "winleft",
    "alt_l": "altleft",
    "alt_r": "altright",
    "spacebar": "space",
}


def _normalize_key_name(part: str) -> str:
    cleaned = part.strip().lower()
    return _KEY_ALIASES.get(cleaned, cleaned)


# ---------------------------------------------------------------------------
# File Drag-and-Drop Helpers
# ---------------------------------------------------------------------------

def _build_dropfiles_blob(file_paths: list[Path], client_x: int = 0, client_y: int = 0) -> bytes:
    """Construct a Win32 DROPFILES struct followed by double-null UTF-16LE paths."""
    header = DROPFILES(
        pFiles=ctypes.sizeof(DROPFILES),
        pt=wintypes.POINT(client_x, client_y),
        fNC=False,
        fWide=True,
    )
    joined = "\0".join(str(p) for p in file_paths) + "\0\0"
    return bytes(header) + joined.encode("utf-16-le")


def _drop_via_wm_dropfiles(target_path: Path, target_x: int, target_y: int) -> dict[str, Any]:
    """Post WM_DROPFILES to the Win32 window under (target_x, target_y)."""
    pt = wintypes.POINT(int(target_x), int(target_y))
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        raise RuntimeError(f"No window found at ({target_x}, {target_y})")

    client_pt = wintypes.POINT(int(target_x), int(target_y))
    user32.ScreenToClient(hwnd, ctypes.byref(client_pt))
    blob = _build_dropfiles_blob([target_path], client_pt.x, client_pt.y)

    # Get target process id and allocate inside target address space when allowed,
    # noting GlobalAlloc HGLOBAL works for WM_DROPFILES inside same-session apps on Windows
    # via kernel32 GlobalAlloc (Explorer/Win32 marshaling handles WM_DROPFILES HGLOBAL).
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

    hglobal = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(blob))
    if not hglobal:
        raise ctypes.WinError()
    ptr = kernel32.GlobalLock(hglobal)
    if not ptr:
        kernel32.GlobalFree(hglobal)
        raise ctypes.WinError()
    try:
        ctypes.memmove(ptr, blob, len(blob))
    finally:
        kernel32.GlobalUnlock(hglobal)

    posted = user32.PostMessageW(hwnd, WM_DROPFILES, hglobal, 0)
    if not posted:
        kernel32.GlobalFree(hglobal)
        raise RuntimeError("PostMessageW(WM_DROPFILES) failed")

    title_buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, title_buf, 256)
    class_buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_buf, 256)

    return {
        "method_used": "wm_dropfiles",
        "posted": True,
        "target_hwnd": int(hwnd),
        "target_window_title": title_buf.value,
        "target_window_class": class_buf.value,
        "file_path": str(target_path),
        "target_point": {"x": target_x, "y": target_y},
        "note": (
            "WM_DROPFILES posted to target window. If the target is a Chromium/Electron/web "
            "drop zone that ignores WM_DROPFILES, retry with method='explorer' or method='clipboard'."
        ),
    }


def _drop_via_clipboard_cf_hdrop(target_path: Path, target_x: int, target_y: int) -> dict[str, Any]:
    """Place a file path into Windows clipboard as CF_HDROP, click drop zone, and paste."""
    blob = _build_dropfiles_blob([target_path], 0, 0)
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    hglobal = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(blob))
    if not hglobal:
        raise ctypes.WinError()
    ptr = kernel32.GlobalLock(hglobal)
    ctypes.memmove(ptr, blob, len(blob))
    kernel32.GlobalUnlock(hglobal)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(hglobal)
        raise RuntimeError("Failed to open Windows clipboard")
    try:
        user32.EmptyClipboard()
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        if not user32.SetClipboardData(CF_HDROP, hglobal):
            kernel32.GlobalFree(hglobal)
            raise RuntimeError("SetClipboardData(CF_HDROP) failed")
    finally:
        user32.CloseClipboard()

    _check_abort()
    pyautogui.click(x=int(target_x), y=int(target_y))
    time.sleep(0.1)
    _check_abort()
    pyautogui.hotkey("ctrl", "v")
    return {
        "method_used": "clipboard",
        "file_path": str(target_path),
        "target_point": {"x": target_x, "y": target_y},
    }


async def _run_ocr_on_image(img: Image.Image, origin_x: int, origin_y: int) -> dict[str, Any]:
    """Run offline Windows.Media.Ocr on a PIL Image and translate coords to global desktop."""
    rgba = img.convert("RGBA")
    r, g, b, a = rgba.split()
    bgra_bytes = Image.merge("RGBA", (b, g, r, a)).tobytes()

    writer = winrt_streams.DataWriter()
    writer.write_bytes(bgra_bytes)
    ibuf = writer.detach_buffer()

    bitmap = winrt_imaging.SoftwareBitmap(
        winrt_imaging.BitmapPixelFormat.BGRA8,
        rgba.width,
        rgba.height,
        winrt_imaging.BitmapAlphaMode.PREMULTIPLIED,
    )
    bitmap.copy_from_buffer(ibuf)

    engine = winrt_ocr.OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        engine = winrt_ocr.OcrEngine.try_create_from_language(winrt_glob.Language("en-US"))
    if engine is None:
        raise RuntimeError(
            "Windows.Media.Ocr could not initialize an OCR engine. "
            "Ensure at least one Windows OCR language pack (e.g. en-US) is installed."
        )

    result = await engine.recognize_async(bitmap)
    lines_out: list[dict[str, Any]] = []
    word_count = 0

    for line in result.lines:
        words_out: list[dict[str, Any]] = []
        min_x: Optional[float] = None
        min_y: Optional[float] = None
        max_r: Optional[float] = None
        max_b: Optional[float] = None

        for word in line.words:
            rect = word.bounding_rect
            wx = int(round(origin_x + rect.x))
            wy = int(round(origin_y + rect.y))
            ww = int(round(rect.width))
            wh = int(round(rect.height))
            words_out.append(
                {
                    "text": word.text,
                    "x": wx,
                    "y": wy,
                    "w": ww,
                    "h": wh,
                    "center_x": wx + ww // 2,
                    "center_y": wy + wh // 2,
                }
            )
            word_count += 1
            rx2 = rect.x + rect.width
            ry2 = rect.y + rect.height
            min_x = rect.x if min_x is None else min(min_x, rect.x)
            min_y = rect.y if min_y is None else min(min_y, rect.y)
            max_r = rx2 if max_r is None else max(max_r, rx2)
            max_b = ry2 if max_b is None else max(max_b, ry2)

        if min_x is not None and min_y is not None and max_r is not None and max_b is not None:
            lx = int(round(origin_x + min_x))
            ly = int(round(origin_y + min_y))
            lw = int(round(max_r - min_x))
            lh = int(round(max_b - min_y))
        else:
            lx, ly, lw, lh = origin_x, origin_y, 0, 0

        lines_out.append(
            {
                "text": line.text,
                "x": lx,
                "y": ly,
                "w": lw,
                "h": lh,
                "center_x": lx + lw // 2,
                "center_y": ly + lh // 2,
                "words": words_out,
            }
        )

    return {
        "origin": {
            "x": origin_x,
            "y": origin_y,
            "w": rgba.width,
            "h": rgba.height,
        },
        "language": engine.recognizer_language.language_tag if engine.recognizer_language else "unknown",
        "full_text": result.text,
        "line_count": len(lines_out),
        "word_count": word_count,
        "lines": lines_out,
    }


def _open_explorer_and_select(target_path: Path) -> None:
    """Open Windows Explorer with target_path selected via SHOpenFolderAndSelectItems."""
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    pidl = ctypes.c_void_p()
    try:
        shell32.SHParseDisplayName.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        shell32.SHParseDisplayName.restype = ctypes.c_long
        hr = shell32.SHParseDisplayName(str(target_path), None, ctypes.byref(pidl), 0, None)
        if hr == 0 and pidl.value:
            shell32.SHOpenFolderAndSelectItems.argtypes = [
                ctypes.c_void_p,
                wintypes.UINT,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
            hr_open = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
            if hr_open == 0:
                return
    except Exception as exc:
        logger.warning("SHOpenFolderAndSelectItems failed, falling back to explorer.exe: %s", exc)
    finally:
        if pidl.value:
            ole32.CoTaskMemFree(pidl)
        ole32.CoUninitialize()
    subprocess.Popen(["explorer.exe", f'/select,"{target_path}"'])


async def _drop_via_explorer_drag(target_path: Path, target_x: int, target_y: int) -> dict[str, Any]:
    """Open Explorer with the file selected, locate it via OCR, and physically drag it."""
    before_hwnds = {w["hwnd"] for w in _enum_visible_windows(include_minimized=True)}
    _open_explorer_and_select(target_path)
    time.sleep(1.0)
    _check_abort()

    folder_name = target_path.parent.name or str(target_path.parent)
    after_windows = _enum_visible_windows(include_minimized=True)
    explorer_win = None
    for w in after_windows:
        if w["hwnd"] not in before_hwnds and folder_name.lower() in w["title"].lower():
            explorer_win = w
            break
    if explorer_win is None:
        for w in after_windows:
            if folder_name.lower() in w["title"].lower():
                explorer_win = w
                break
    if explorer_win is None:
        raise RuntimeError("Could not locate opened File Explorer window for physical drag.")

    b = explorer_win["bounds"]
    # Position Explorer away from target_x/target_y if overlapping, or inspect directly
    img = _capture_region_rgba({"left": b["x"], "top": b["y"], "width": b["w"], "height": b["h"]})
    ocr_res = await _run_ocr_on_image(img, b["x"], b["y"])

    target_name = target_path.name.lower()
    target_stem = target_path.stem.lower()
    src_pt: Optional[tuple[int, int]] = None
    for line in ocr_res["lines"]:
        lt = line["text"].lower()
        if target_name in lt or target_stem in lt:
            # Avoid matching the Explorer title bar or address bar near top 90px
            if line["y"] > b["y"] + 90:
                src_pt = (line["center_x"], line["center_y"])
                break

    if src_pt is None:
        raise RuntimeError(
            f"Opened Explorer for '{target_path.name}', but could not locate the file row via OCR."
        )

    _drag_between(src_pt[0], src_pt[1], int(target_x), int(target_y), duration=0.6, button="left")
    return {
        "method_used": "explorer",
        "file_path": str(target_path),
        "source_point": {"x": src_pt[0], "y": src_pt[1]},
        "target_point": {"x": target_x, "y": target_y},
    }


def _drag_between(
    x1: int, y1: int, x2: int, y2: int, duration: float = 0.4, button: str = "left"
) -> None:
    """Interpolated drag with abort checks on each step and safe mouseUp cleanup."""
    _check_abort()
    pyautogui.moveTo(x1, y1)
    pyautogui.mouseDown(x=x1, y=y1, button=button)
    try:
        steps = max(10, int(max(0.1, duration) * 40))
        step_sleep = max(0.005, duration / steps)
        for i in range(1, steps + 1):
            _check_abort()
            t = i / steps
            nx = int(round(x1 + (x2 - x1) * t))
            ny = int(round(y1 + (y2 - y1) * t))
            pyautogui.moveTo(nx, ny)
            time.sleep(step_sleep)
    finally:
        pyautogui.mouseUp(x=x2, y=y2, button=button)


# ---------------------------------------------------------------------------
# MCP Server Definition & Tools
# ---------------------------------------------------------------------------

mcp = MCPServer("desktop-vision")


@mcp.tool()
def get_screen_resolution() -> dict[str, Any]:
    """Return physical pixel dimensions across connected monitors and virtual screen.

    monitor_index=0 is the primary monitor, 1..N are individual monitors, and -1 is
    the full virtual screen across all monitors.
    """
    return _collect_monitor_layout()


@mcp.tool()
def take_screenshot(
    show_grid: bool = True,
    grid_interval: int = 100,
    monitor_index: int = 0,
    include_image_content: bool = False,
) -> list[TextContent | ImageContent]:
    """Capture a screenshot, optionally overlay a global (x, y) pixel grid, save permanently,
    and check cumulative screenshot storage (alerting on every 1 GB increment).

    Args:
        show_grid: Overlay coordinate grid lines and global pixel coordinate markers.
        grid_interval: Pixel spacing between major grid lines (minimum 20).
        monitor_index: 0 for primary monitor, 1..N for specific monitors, -1 for all monitors.
        include_image_content: If True, also attach inline base64 ImageContent (default False;
            prefer inspecting the saved file_path with read_file to keep context compact).
    """
    bounds = _resolve_capture_bounds(monitor_index=monitor_index)
    img = _capture_region_rgba(bounds)
    if show_grid:
        img = _draw_coordinate_grid(img, bounds["left"], bounds["top"], grid_interval)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    mon_label = "all" if monitor_index == -1 else str(monitor_index)
    out_path = SCREENSHOTS_DIR / f"screenshot_{ts}_mon{mon_label}.png"
    img.convert("RGB").save(out_path, format="PNG", optimize=True)

    storage = _check_screenshot_storage()
    payload = {
        "file_path": str(out_path),
        "monitor_index": monitor_index,
        "bounds": bounds,
        "grid_shown": show_grid,
        "grid_interval": max(20, int(grid_interval)) if show_grid else None,
        "dpi_mode": DPI_MODE,
        "storage": storage,
    }

    blocks: list[TextContent | ImageContent] = [
        TextContent(type="text", text=json.dumps(payload, indent=2))
    ]
    if include_image_content:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        blocks.append(ImageContent(type="image", data=b64, mimeType="image/png"))
    return blocks


@mcp.tool()
async def read_text_from_screen(
    region: Optional[list[int]] = None,
    monitor_index: int = 0,
) -> dict[str, Any]:
    """Run 100% offline Windows native OCR (Windows.Media.Ocr) on the screen or a region.

    Args:
        region: Optional global rectangle [x, y, width, height]. If omitted, captures monitor_index.
        monitor_index: Monitor to scan when region is omitted (0=primary, -1=all monitors).

    Returns detected lines and words with global desktop bounding boxes (x, y, w, h)
    and pre-computed click centers (center_x, center_y).
    """
    region_tuple = tuple(region) if region is not None else None
    bounds = _resolve_capture_bounds(monitor_index=monitor_index, region=region_tuple)
    img = _capture_region_rgba(bounds)
    return await _run_ocr_on_image(img, bounds["left"], bounds["top"])


@mcp.tool()
def list_windows(include_minimized: bool = True) -> dict[str, Any]:
    """List visible desktop windows with non-empty titles, hwnd, pid, and global bounds."""
    wins = _enum_visible_windows(include_minimized=include_minimized)
    return {"count": len(wins), "windows": wins}


@mcp.tool()
def focus_window(window_title: str) -> dict[str, Any]:
    """Find a window matching window_title, restore if minimized, and bring to foreground."""
    wins = _enum_visible_windows(include_minimized=True)
    needle = window_title.strip().lower()
    matches = [w for w in wins if needle in w["title"].lower()]
    if not matches:
        try:
            pattern = re.compile(window_title, re.IGNORECASE)
            matches = [w for w in wins if pattern.search(w["title"])]
        except re.error:
            matches = []

    if not matches:
        return {
            "success": False,
            "error": f"No visible window matched '{window_title}'.",
            "available_titles": [w["title"] for w in wins[:25]],
        }

    # Prefer exact match if multiple substring matches exist
    exact = [w for w in matches if w["title"].lower() == needle]
    target = exact[0] if exact else matches[0]
    hwnd = target["hwnd"]

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.1)

    fg_hwnd = user32.GetForegroundWindow()
    curr_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)

    attached_fg = False
    attached_target = False
    try:
        if fg_tid and fg_tid != curr_tid:
            attached_fg = bool(user32.AttachThreadInput(curr_tid, fg_tid, True))
        if target_tid and target_tid != curr_tid:
            attached_target = bool(user32.AttachThreadInput(curr_tid, target_tid, True))

        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
        if not ok:
            # Windows foreground lock workaround: synthesize brief Alt press/release
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            user32.BringWindowToTop(hwnd)
            ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached_target and target_tid:
            user32.AttachThreadInput(curr_tid, target_tid, False)
        if attached_fg and fg_tid:
            user32.AttachThreadInput(curr_tid, fg_tid, False)

    time.sleep(0.15)
    rc = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    return {
        "success": True,
        "focused": user32.GetForegroundWindow() == hwnd,
        "hwnd": hwnd,
        "title": target["title"],
        "bounds": {
            "x": int(rc.left),
            "y": int(rc.top),
            "w": int(rc.right - rc.left),
            "h": int(rc.bottom - rc.top),
        },
    }


@mcp.tool()
def mouse_click(
    x: int,
    y: int,
    button: Literal["left", "right", "middle"] = "left",
    clicks: int = 1,
) -> dict[str, Any]:
    """Move cursor to global (x, y) and click with the specified mouse button."""
    _begin_input_action()
    count = max(1, int(clicks))
    for idx in range(count):
        _check_abort()
        pyautogui.click(x=int(x), y=int(y), button=button)
        if idx + 1 < count:
            time.sleep(0.08)
    return {"clicked": True, "x": int(x), "y": int(y), "button": button, "clicks": count}


@mcp.tool()
def mouse_move(x: int, y: int, duration: float = 0.1) -> dict[str, Any]:
    """Move the cursor smoothly to global (x, y)."""
    _begin_input_action()
    _check_abort()
    pyautogui.moveTo(int(x), int(y), duration=max(0.0, float(duration)))
    pos = pyautogui.position()
    return {"moved": True, "x": int(pos.x), "y": int(pos.y)}


@mcp.tool()
def mouse_drag(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration: float = 0.4,
    button: Literal["left", "right", "middle"] = "left",
) -> dict[str, Any]:
    """Press and drag the mouse from global (x1, y1) to (x2, y2)."""
    _begin_input_action()
    _drag_between(int(x1), int(y1), int(x2), int(y2), duration=float(duration), button=button)
    return {
        "dragged": True,
        "from": {"x": int(x1), "y": int(y1)},
        "to": {"x": int(x2), "y": int(y2)},
        "button": button,
    }


@mcp.tool()
def type_text(text: str, delay_between_keys: float = 0.05) -> dict[str, Any]:
    """Type text character-by-character into the focused control using Unicode SendInput."""
    _begin_input_action()
    delay = max(0.0, float(delay_between_keys))
    typed = 0
    for ch in text:
        _check_abort()
        _send_unicode_char(ch)
        typed += 1
        if delay > 0:
            time.sleep(delay)
    return {"typed_chars": typed}


@mcp.tool()
def press_key(key: str) -> dict[str, Any]:
    """Press a single key ('enter', 'tab', 'esc') or '+'-separated hotkey ('ctrl+a', 'alt+tab')."""
    _begin_input_action()
    _check_abort()
    raw = key.strip()
    if "+" in raw:
        parts = [_normalize_key_name(p) for p in raw.split("+") if p.strip()]
        if not parts:
            raise ValueError(f"Invalid hotkey specification: '{key}'")
        pyautogui.hotkey(*parts)
        return {"pressed_hotkey": parts}
    norm = _normalize_key_name(raw)
    pyautogui.press(norm)
    return {"pressed_key": norm}


@mcp.tool()
def set_clipboard(text: str) -> dict[str, Any]:
    """Set the Windows system clipboard text."""
    pyperclip.copy(text)
    return {"copied": True, "length": len(text)}


@mcp.tool()
def get_clipboard() -> dict[str, Any]:
    """Read the current Windows system clipboard text."""
    text = pyperclip.paste()
    return {"text": text, "length": len(text)}


@mcp.tool()
async def drag_and_drop_file(
    file_path: str,
    target_x: int,
    target_y: int,
    method: Literal["auto", "wm_dropfiles", "explorer", "clipboard"] = "auto",
) -> dict[str, Any]:
    """Simulate dropping a local file into an application's drop zone at (target_x, target_y).

    Args:
        file_path: Local path to the file to drop.
        target_x: Global X coordinate of the target drop zone.
        target_y: Global Y coordinate of the target drop zone.
        method: 'auto' (tries WM_DROPFILES), 'wm_dropfiles' (Win32 message injection),
            'explorer' (physical Explorer SHOpenFolderAndSelectItems drag for Chromium/Electron),
            or 'clipboard' (CF_HDROP paste at target point).
    """
    _begin_input_action()
    _check_abort()
    resolved = Path(file_path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"File does not exist: {resolved}")

    if method in ("auto", "wm_dropfiles"):
        return _drop_via_wm_dropfiles(resolved, int(target_x), int(target_y))
    if method == "clipboard":
        return _drop_via_clipboard_cf_hdrop(resolved, int(target_x), int(target_y))
    if method == "explorer":
        return await _drop_via_explorer_drag(resolved, int(target_x), int(target_y))
    raise ValueError(f"Unsupported drag_and_drop_file method: '{method}'")


if __name__ == "__main__":
    _ensure_abort_listener()
    logger.info("Starting desktop-vision MCP server (DPI mode: %s)", DPI_MODE)
    mcp.run(transport="stdio")
