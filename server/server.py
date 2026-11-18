"""Local Desktop Vision & Cursor Control MCP Server for Windows.

Provides OS-level screen inspection, gridded screenshots with 1 GB storage alerts,
offline Windows.Media.Ocr text localization, window management, cursor/keyboard
control, clipboard operations, multi-strategy file drag-and-drop, and a hardware
emergency abort listener.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
from ctypes import wintypes
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import queue
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
import winrt.windows.globalization as winrt_glob
import winrt.windows.graphics.imaging as winrt_imaging
import winrt.windows.media.ocr as winrt_ocr
import winrt.windows.storage.streams as winrt_streams

from overlay_ui import (
    ALL_OVERLAY_CLASSES,
    _WoolburOverlayManager,
    detect_exclusive_fullscreen,
    get_cursor_pos,
)
from woolbur_store import (
    find_app_memory,
    load_app_memory,
    load_config_cached,
    upsert_app_memory,
)


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
pyautogui.PAUSE = 0.005

ONE_GIB = 1024 ** 3


# ---------------------------------------------------------------------------
# Win32 Constants & Structs (64-bit Safe)
# ---------------------------------------------------------------------------

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

SW_RESTORE = 9
MONITORINFOF_PRIMARY = 1

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

KEYEVENTF_EXTENDEDKEY = 0x0001
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

_BUBBLE_OVERLAY: Any = None


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
# Low-Level Mouse Hook (Suppresses physical mouse/touchpad while agent works)
# & Hardware Emergency Abort Listener (Triple-Esc / Pause / Ctrl+Shift+Esc)
# ---------------------------------------------------------------------------

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
LLMHF_INJECTED = 0x00000001
LLMHF_LOWER_IL_INJECTED = 0x00000002
LLKHF_LOWER_IL_INJECTED = 0x00000002
LLKHF_INJECTED = 0x00000010
LLKHF_ALTDOWN = 0x00000020
LLKHF_UP = 0x00000080
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
VK_SHIFT_KEY = 0x10
VK_CONTROL_KEY = 0x11
VK_MENU_KEY = 0x12
VK_PAUSE_KEY = 0x13
VK_ESCAPE_KEY = 0x1B


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, ctypes.c_void_p)

_ABORT_EVENT = threading.Event()
_ABORT_LOCK = threading.Lock()
_LAST_ABORT_TIME: float = 0.0
_ABORT_TTL_SECONDS: float = 60.0
_ESC_PRESS_TIMES: list[float] = []
_ESC_IS_DOWN = False
_ABORT_LISTENER_STARTED = False

_MOUSE_HOOK = None
_KB_HOOK = None
_HOOK_PROC_INST = None
_KB_HOOK_PROC_INST = None
_PHYSICAL_MOUSE_BLOCKED = False
_MOUSE_BLOCK_LOCK = threading.Lock()
_LAST_BLOCK_TIME: float = 0.0
_MAX_BLOCK_DURATION: float = 15.0
_MOUSE_HOOK_THREAD_STARTED = False
_MOUSE_HOOK_STOP_EVENT = threading.Event()
_ASSIST_ANCHOR: Optional[tuple[int, int]] = None
_ASSIST_LAST_MOVE_TS: float = 0.0
_ASSIST_MOVE_THRESHOLD_PX: float = 45.0

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HANDLE
user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, ctypes.c_void_p]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


def _trigger_abort(reason: str) -> None:
    global _LAST_ABORT_TIME, _PHYSICAL_MOUSE_BLOCKED
    with _ABORT_LOCK:
        _ABORT_EVENT.set()
        _LAST_ABORT_TIME = time.time()
    # Immediately unblock physical mouse so the user has full hardware control
    with _MOUSE_BLOCK_LOCK:
        _PHYSICAL_MOUSE_BLOCKED = False

    def _notify() -> None:
        try:
            if _BUBBLE_OVERLAY is not None:
                _BUBBLE_OVERLAY.show(
                    "ABORTED — Physical mouse restored",
                    status_type="error",
                    auto_hide_seconds=3.5,
                    clear_intent=True,
                )
            logger.warning(
                "EMERGENCY ABORT triggered: %s (physical mouse unblocked immediately)",
                reason,
            )
        except Exception:
            pass

    threading.Thread(target=_notify, daemon=True).start()


def _low_level_keyboard_proc(nCode: int, wParam: wintypes.WPARAM, lParam: ctypes.c_void_p) -> int:
    global _ESC_IS_DOWN, _ESC_PRESS_TIMES
    if nCode >= 0:
        try:
            kb = KBDLLHOOKSTRUCT.from_address(lParam)
            vk = int(kb.vkCode)
            is_injected = (kb.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED)) != 0
            if not is_injected:
                is_up = wParam in (WM_KEYUP, WM_SYSKEYUP) or bool(kb.flags & LLKHF_UP)
                is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN) and not bool(kb.flags & LLKHF_UP)
                if vk == VK_ESCAPE_KEY:
                    if is_up:
                        _ESC_IS_DOWN = False
                    elif is_down and not _ESC_IS_DOWN:
                        _ESC_IS_DOWN = True
                        ctrl = (user32.GetAsyncKeyState(VK_CONTROL_KEY) & 0x8000) != 0
                        shift = (user32.GetAsyncKeyState(VK_SHIFT_KEY) & 0x8000) != 0
                        alt = ((user32.GetAsyncKeyState(VK_MENU_KEY) & 0x8000) != 0) or bool(
                            kb.flags & LLKHF_ALTDOWN
                        )
                        if ctrl and (shift or alt):
                            _ESC_PRESS_TIMES.clear()
                            _trigger_abort("Ctrl+Shift/Alt+Esc pressed")
                        else:
                            try:
                                esc_win = float(
                                    load_config_cached()["settings"].get(
                                        "triple_esc_window_seconds", 1.8
                                    )
                                )
                            except Exception:
                                esc_win = 1.8
                            now = time.monotonic()
                            _ESC_PRESS_TIMES = [t for t in _ESC_PRESS_TIMES if now - t <= esc_win]
                            _ESC_PRESS_TIMES.append(now)
                            tap_count = len(_ESC_PRESS_TIMES)
                            if tap_count >= 3:
                                _ESC_PRESS_TIMES.clear()
                                _trigger_abort(
                                    f"Triple-Esc emergency abort (Esc pressed 3 times within {esc_win:.1f}s)"
                                )
                            elif tap_count == 2:
                                threading.Thread(
                                    target=lambda w=esc_win: logger.info(
                                        f"Hardware Esc tap 2/3 detected (press Esc once more within {w:.1f}s to abort)."
                                    ),
                                    daemon=True,
                                ).start()
                elif vk == VK_PAUSE_KEY and is_down:
                    _trigger_abort("Pause/Break key pressed")
                elif vk == 0x57 and is_down:
                    alt = ((user32.GetAsyncKeyState(VK_MENU_KEY) & 0x8000) != 0) or bool(
                        kb.flags & LLKHF_ALTDOWN
                    )
                    ctrl = (user32.GetAsyncKeyState(VK_CONTROL_KEY) & 0x8000) != 0
                    if (
                        alt
                        and not ctrl
                        and _BUBBLE_OVERLAY is not None
                        and _BUBBLE_OVERLAY.is_session_active()
                    ):
                        threading.Thread(
                            target=lambda: _BUBBLE_OVERLAY.open_steer_prompt(
                                pointer=get_cursor_pos()
                            ),
                            daemon=True,
                        ).start()
        except Exception:
            pass
    return user32.CallNextHookEx(_KB_HOOK, nCode, wParam, lParam)


_KB_HOOK_PROC_INST = HOOKPROC(_low_level_keyboard_proc)


def _ensure_abort_listener() -> None:
    global _ABORT_LISTENER_STARTED
    if _ABORT_LISTENER_STARTED:
        return
    _ensure_mouse_hook()
    _ABORT_LISTENER_STARTED = bool(_KB_HOOK)


def _low_level_mouse_proc(nCode: int, wParam: wintypes.WPARAM, lParam: ctypes.c_void_p) -> int:
    global _PHYSICAL_MOUSE_BLOCKED, _ASSIST_ANCHOR, _ASSIST_LAST_MOVE_TS
    if nCode >= 0:
        info = MSLLHOOKSTRUCT.from_address(lParam)
        is_injected = (info.flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED)) != 0
        if _PHYSICAL_MOUSE_BLOCKED:
            # Watchdog: auto-release if blocked longer than MAX_BLOCK_DURATION
            if time.time() - _LAST_BLOCK_TIME > _MAX_BLOCK_DURATION:
                _PHYSICAL_MOUSE_BLOCKED = False
                logger.warning("Physical mouse suppression auto-released due to watchdog timeout.")
            elif not is_injected:
                _ASSIST_ANCHOR = None
                return 1
        else:
            if is_injected:
                _ASSIST_ANCHOR = (int(info.pt.x), int(info.pt.y))
                _ASSIST_LAST_MOVE_TS = time.monotonic()
            elif (
                _BUBBLE_OVERLAY is not None
                and _BUBBLE_OVERLAY.is_session_active()
                and not _BUBBLE_OVERLAY.is_prompt_open()
            ):
                px, py = int(info.pt.x), int(info.pt.y)
                if wParam == WM_MOUSEMOVE:
                    now_m = time.monotonic()
                    if (
                        _ASSIST_ANCHOR is None
                        or ((now_m - _ASSIST_LAST_MOVE_TS > 1.5) and not _BUBBLE_OVERLAY.is_assisting())
                    ):
                        _ASSIST_ANCHOR = (px, py)
                    _ASSIST_LAST_MOVE_TS = now_m
                    try:
                        move_thresh = float(
                            load_config_cached()["settings"].get(
                                "assist_move_threshold_px", _ASSIST_MOVE_THRESHOLD_PX
                            )
                        )
                    except Exception:
                        move_thresh = _ASSIST_MOVE_THRESHOLD_PX
                    dist = math.hypot(px - _ASSIST_ANCHOR[0], py - _ASSIST_ANCHOR[1])
                    if dist >= move_thresh:
                        _ASSIST_ANCHOR = (px, py)
                        _BUBBLE_OVERLAY.on_physical_assist_move(px, py)
                elif wParam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN):
                    if not _BUBBLE_OVERLAY.point_in_interactive_ui(px, py):
                        _BUBBLE_OVERLAY.on_physical_assist_click(
                            px, py, "left" if wParam == WM_LBUTTONDOWN else "right"
                        )
    return user32.CallNextHookEx(_MOUSE_HOOK, nCode, wParam, lParam)


_HOOK_PROC_INST = HOOKPROC(_low_level_mouse_proc)


def _mouse_hook_worker(ready_ev: threading.Event) -> None:
    global _MOUSE_HOOK, _KB_HOOK, _ABORT_LISTENER_STARTED
    _MOUSE_HOOK = user32.SetWindowsHookExW(WH_MOUSE_LL, _HOOK_PROC_INST, None, 0)
    if not _MOUSE_HOOK:
        logger.warning("Failed to install WH_MOUSE_LL hook (Error %d).", ctypes.GetLastError())
    else:
        logger.info("Low-level WH_MOUSE_LL mouse hook installed successfully.")
    _KB_HOOK = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _KB_HOOK_PROC_INST, None, 0)
    if not _KB_HOOK:
        logger.warning("Failed to install WH_KEYBOARD_LL hook (Error %d).", ctypes.GetLastError())
    else:
        _ABORT_LISTENER_STARTED = True
        logger.info("Low-level WH_KEYBOARD_LL abort & Alt+W steering hook installed successfully.")
    ready_ev.set()
    msg = wintypes.MSG()
    while not _MOUSE_HOOK_STOP_EVENT.is_set():
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.005)
    if _KB_HOOK:
        user32.UnhookWindowsHookEx(_KB_HOOK)
        _KB_HOOK = None
        _ABORT_LISTENER_STARTED = False
        logger.info("WH_KEYBOARD_LL keyboard hook uninstalled.")
    if _MOUSE_HOOK:
        user32.UnhookWindowsHookEx(_MOUSE_HOOK)
        _MOUSE_HOOK = None
        logger.info("WH_MOUSE_LL mouse hook uninstalled.")


def _ensure_mouse_hook() -> None:
    global _MOUSE_HOOK_THREAD_STARTED
    if _MOUSE_HOOK_THREAD_STARTED:
        return
    _MOUSE_HOOK_THREAD_STARTED = True
    ready_ev = threading.Event()
    th = threading.Thread(target=_mouse_hook_worker, args=(ready_ev,), daemon=True)
    th.start()
    ready_ev.wait(2.0)


@contextlib.contextmanager
def _suppress_physical_mouse(duration_cap: float = 15.0):
    """Context manager to suppress physical mousepad/mouse input while allowing synthetic input."""
    global _PHYSICAL_MOUSE_BLOCKED, _LAST_BLOCK_TIME, _MAX_BLOCK_DURATION
    _ensure_mouse_hook()
    _ensure_abort_listener()
    _check_abort()
    with _MOUSE_BLOCK_LOCK:
        _LAST_BLOCK_TIME = time.time()
        _MAX_BLOCK_DURATION = max(1.0, float(duration_cap))
        _PHYSICAL_MOUSE_BLOCKED = True
    try:
        yield
    finally:
        with _MOUSE_BLOCK_LOCK:
            _PHYSICAL_MOUSE_BLOCKED = False


def _begin_input_action() -> None:
    """Ensure hardware hooks are active and reject input if an emergency abort is latched."""
    _ensure_mouse_hook()
    _ensure_abort_listener()
    _check_abort()


def _check_abort() -> None:
    """Raise RuntimeError immediately if an emergency abort is latched."""
    if _ABORT_EVENT.is_set():
        with _ABORT_LOCK:
            if time.time() - _LAST_ABORT_TIME >= _ABORT_TTL_SECONDS:
                _ABORT_EVENT.clear()
                return
        raise RuntimeError(
            "EMERGENCY ABORT triggered via hardware hotkey (Triple-Esc / Pause / Ctrl+Shift+Esc). "
            "Input sequence halted and physical mouse restored immediately. "
            "STOP all automation actions and ask the user how to proceed; on a new user instruction, "
            "call show_status_bubble(..., status_type='intent', reset_abort=True) to clear."
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
    """Capture a global desktop rectangle as an RGBA PIL Image (hiding the bubble briefly)."""
    with _BUBBLE_OVERLAY.suspend_for_capture():
        with _create_mss() as sct:
            shot = sct.grab(bounds)
            return Image.frombytes("RGBA", shot.size, shot.bgra, "raw", "BGRA")


def _capture_region_rgb(bounds: dict[str, int]) -> Image.Image:
    """Fast capture directly into RGB (avoids RGBA conversion when no grid overlay is needed)."""
    with _BUBBLE_OVERLAY.suspend_for_capture():
        with _create_mss() as sct:
            shot = sct.grab(bounds)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


_FONT_CACHE: dict[tuple[tuple[str, ...], int], ImageFont.ImageFont | ImageFont.FreeTypeFont] = {}
_FONT_CACHE_LOCK = threading.Lock()
_GRID_OVERLAY_CACHE: dict[tuple[int, int, int, int, int], Image.Image] = {}
_GRID_OVERLAY_LOCK = threading.Lock()
_STORAGE_LOCK = threading.Lock()
_STORAGE_CACHE: Optional[list[int]] = None  # [total_bytes, file_count]


def _load_font(
    size: int = 13,
    candidates: tuple[str, ...] = ("consola.ttf", "arial.ttf", "segoeui.ttf"),
) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    key = (candidates, int(size))
    with _FONT_CACHE_LOCK:
        cached = _FONT_CACHE.get(key)
        if cached is not None:
            return cached
        loaded: Optional[ImageFont.ImageFont | ImageFont.FreeTypeFont] = None
        for candidate in candidates:
            try:
                loaded = ImageFont.truetype(candidate, size)
                break
            except Exception:
                continue
        if loaded is None:
            loaded = ImageFont.load_default()
        _FONT_CACHE[key] = loaded
        return loaded


def _get_grid_overlay(
    width: int,
    height: int,
    origin_x: int,
    origin_y: int,
    interval: int,
    physical_width: Optional[int] = None,
    physical_height: Optional[int] = None,
) -> Image.Image:
    """Return a cached transparent RGBA coordinate-grid overlay scaled cleanly with physical labels."""
    phys_w = max(1, int(physical_width or width))
    phys_h = max(1, int(physical_height or height))
    key = (int(width), int(height), int(origin_x), int(origin_y), int(interval), phys_w, phys_h)
    with _GRID_OVERLAY_LOCK:
        cached = _GRID_OVERLAY_CACHE.get(key)
        if cached is not None:
            return cached

    sx = width / float(phys_w)
    sy = height / float(phys_h)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _load_font(12)
    major_color = (255, 64, 64, 155)
    sub_color = (255, 165, 0, 70)
    bg_color = (15, 15, 20, 210)
    text_color = (255, 255, 120, 255)

    if interval * sx >= 36 and interval * sy >= 36 and interval >= 80:
        half = interval // 2
        for px in range(half, phys_w, interval):
            lx = min(width - 1, int(round(px * sx)))
            draw.line([(lx, 0), (lx, height)], fill=sub_color, width=1)
        for py in range(half, phys_h, interval):
            ly = min(height - 1, int(round(py * sy)))
            draw.line([(0, ly), (width, ly)], fill=sub_color, width=1)

    label_stride = 2 if (interval * sx < 42) else 1
    for idx, px in enumerate(range(0, phys_w, interval)):
        lx = min(width - 1, int(round(px * sx)))
        gx = origin_x + px
        draw.line([(lx, 0), (lx, height)], fill=major_color, width=1)
        if idx % label_stride == 0:
            label = str(gx)
            bbox = draw.textbbox((0, 0), label, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            tx = min(max(lx + 2, 2), max(2, width - tw - 6))
            draw.rectangle([tx - 2, 2, tx + tw + 4, 2 + th + 4], fill=bg_color)
            draw.text((tx + 1, 3), label, font=font, fill=text_color)

    y_stride = 2 if (interval * sy < 24) else 1
    for idx, py in enumerate(range(0, phys_h, interval)):
        ly = min(height - 1, int(round(py * sy)))
        gy = origin_y + py
        draw.line([(0, ly), (width, ly)], fill=major_color, width=1)
        if py == 0 or (idx % y_stride != 0):
            continue
        label = str(gy)
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        ty = min(max(ly + 2, 2), max(2, height - th - 6))
        draw.rectangle([2, ty - 2, 2 + tw + 6, ty + th + 4], fill=bg_color)
        draw.text((4, ty), label, font=font, fill=text_color)

    mult = 3 if (interval * sx < 44) else 2
    step = interval * mult
    x_margin = max(40, int(round(80 / max(sx, 0.25))))
    y_margin = max(18, int(round(30 / max(sy, 0.25))))
    for px in range(step, max(step, phys_w - x_margin), step):
        for py in range(step, max(step, phys_h - y_margin), step):
            lx = min(width - 1, int(round(px * sx)))
            ly = min(height - 1, int(round(py * sy)))
            gx = origin_x + px
            gy = origin_y + py
            label = f"{gx},{gy}"
            bbox = draw.textbbox((0, 0), label, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.rectangle([lx + 2, ly + 2, lx + tw + 6, ly + th + 5], fill=(15, 15, 20, 165))
            draw.text((lx + 4, ly + 3), label, font=font, fill=(140, 255, 180, 235))

    with _GRID_OVERLAY_LOCK:
        if len(_GRID_OVERLAY_CACHE) >= 16:
            _GRID_OVERLAY_CACHE.pop(next(iter(_GRID_OVERLAY_CACHE)))
        _GRID_OVERLAY_CACHE[key] = overlay
    return overlay


def _draw_coordinate_grid(
    img: Image.Image,
    origin_x: int,
    origin_y: int,
    grid_interval: int,
    physical_width: Optional[int] = None,
    physical_height: Optional[int] = None,
) -> Image.Image:
    """Composite the cached pixel grid overlay onto the screenshot."""
    interval = max(20, int(grid_interval))
    overlay = _get_grid_overlay(
        img.width,
        img.height,
        origin_x,
        origin_y,
        interval,
        physical_width=physical_width,
        physical_height=physical_height,
    )
    base = img if img.mode == "RGBA" else img.convert("RGBA")
    return Image.alpha_composite(base, overlay)


def _check_screenshot_storage(added_bytes: int = 0) -> dict[str, Any]:
    """Track cumulative screenshot storage incrementally and alert on every 1 GiB increment."""
    global _STORAGE_CACHE
    with _STORAGE_LOCK:
        if _STORAGE_CACHE is None:
            total_bytes = 0
            file_count = 0
            for entry in SCREENSHOTS_DIR.rglob("*"):
                if entry.is_file():
                    try:
                        total_bytes += entry.stat().st_size
                        file_count += 1
                    except OSError:
                        pass
            _STORAGE_CACHE = [total_bytes, file_count]
        elif added_bytes > 0:
            _STORAGE_CACHE[0] += int(added_bytes)
            _STORAGE_CACHE[1] += 1
        total_bytes, file_count = _STORAGE_CACHE[0], _STORAGE_CACHE[1]

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
# Woolbur Dual-Overlay Manager & User Cooperation Helpers
# ---------------------------------------------------------------------------

_BUBBLE_OVERLAY = _WoolburOverlayManager()


def _cooperate_before_input() -> Optional[dict[str, Any]]:
    """Pause if user is steering/assisting, and abort stale input if guidance arrived."""
    _ensure_abort_listener()
    _ensure_mouse_hook()
    events = _BUBBLE_OVERLAY.wait_for_cooperation(abort_check=_check_abort)
    if events:
        return {
            "executed": False,
            "paused_for_user_guidance": True,
            "user_guidance": events,
            "note": (
                "The user interacted (moved/clicked mouse to help or sent Alt+W guidance) "
                "before this input ran. Acknowledge their help in Woolbur's bubble, ask "
                "'why' via ask_woolbur only if their intent is unclear, and adjust your next action."
            ),
        }
    return None


def _cooperate_before_observe() -> list[dict[str, Any]]:
    """Wait if a steering prompt or active assist move is underway, returning pending guidance."""
    _ensure_abort_listener()
    _ensure_mouse_hook()
    return _BUBBLE_OVERLAY.wait_for_cooperation(abort_check=_check_abort)


def _attach_user_guidance(payload: dict[str, Any], events: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """Attach any buffered user guidance events to a tool response dict."""
    combined = list(events or []) + _BUBBLE_OVERLAY.pop_pending_user_events()
    if combined:
        payload["user_guidance"] = combined
    return payload


@contextlib.contextmanager
def _bubble_action_scope(
    action_text: str,
    finish_to_thinking: bool = True,
    step_summary: Optional[str] = None,
):
    """Display an acting status with Woolbur following the cursor, then hop back to HUD."""
    _ensure_abort_listener()
    _ensure_mouse_hook()
    with _BUBBLE_OVERLAY._state_lock:
        prev_type = _BUBBLE_OVERLAY._last_status_type
        prev_msg = _BUBBLE_OVERLAY._last_message
    _BUBBLE_OVERLAY.show(
        action_text,
        status_type="acting",
        auto_hide_seconds=0.0,
        combine_intent=True,
    )
    try:
        yield
    except Exception as exc:
        if not _ABORT_EVENT.is_set():
            _BUBBLE_OVERLAY.show(
                f"Error: {str(exc)[:100]}",
                status_type="error",
                auto_hide_seconds=5.0,
            )
        raise
    else:
        if not _ABORT_EVENT.is_set():
            if finish_to_thinking:
                _BUBBLE_OVERLAY.show(
                    "Thinking / planning next action...",
                    status_type="thinking",
                    auto_hide_seconds=0.0,
                    combine_intent=True,
                    completed_step=step_summary or action_text,
                )
            else:
                if prev_type == "complete" and prev_msg:
                    done_msg = (
                        prev_msg
                        if "Ready in Antigravity" in prev_msg
                        else f"{prev_msg}  \u2022  Ready in Antigravity"
                    )
                else:
                    done_msg = "Ready in Antigravity"
                _BUBBLE_OVERLAY.show(
                    done_msg,
                    status_type="complete",
                    auto_hide_seconds=4.0,
                    clear_intent=True,
                    combine_intent=False,
                )


# ---------------------------------------------------------------------------
# Window Enumeration & Focus Helpers
# ---------------------------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int


def _get_process_name(pid: int) -> str:
    """Resolve the executable basename (e.g., 'Antigravity.exe') for a PID."""
    if not pid:
        return ""
    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not hproc:
        return ""
    try:
        size = wintypes.DWORD(512)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(hproc)


def _enum_visible_windows(include_minimized: bool = True) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    own_hwnds = _BUBBLE_OVERLAY.own_hwnds()

    def _callback(hwnd, _lparam):
        if int(hwnd) in own_hwnds:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        class_buf = ctypes.create_unicode_buffer(64)
        if user32.GetClassNameW(hwnd, class_buf, 64) > 0 and class_buf.value in ALL_OVERLAY_CLASSES:
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
        pid_int = int(pid.value)
        windows.append(
            {
                "hwnd": int(hwnd),
                "pid": pid_int,
                "process_name": _get_process_name(pid_int),
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
# SendInput Native Win32 Mouse, Keyboard & Unicode Helpers
# ---------------------------------------------------------------------------

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
user32.VkKeyScanW.restype = ctypes.c_short


def _to_virtual_abs(x: int, y: int) -> tuple[int, int]:
    """Convert global desktop pixel coordinates (x, y) to 0..65535 SendInput virtual coords."""
    vx = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    vy = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    vw = max(1, int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)) or int(user32.GetSystemMetrics(0)) or 1920)
    vh = max(1, int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)) or int(user32.GetSystemMetrics(1)) or 1080)
    nx = max(0, min(65535, int(((int(x) - vx) + 0.5) * 65536.0 / vw)))
    ny = max(0, min(65535, int(((int(y) - vy) + 0.5) * 65536.0 / vh)))
    return nx, ny


def _send_mouse_move(x: int, y: int) -> None:
    """Move cursor via native SendInput (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | VIRTUALDESK)."""
    nx, ny = _to_virtual_abs(x, y)
    ev = INPUT(type=INPUT_MOUSE)
    ev.union.mi = MOUSEINPUT(
        dx=nx,
        dy=ny,
        mouseData=0,
        dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
        time=0,
        dwExtraInfo=0,
    )
    arr = (INPUT * 1)(ev)
    user32.SendInput(1, arr, ctypes.sizeof(INPUT))


def _send_mouse_button(
    x: int,
    y: int,
    button: Literal["left", "right", "middle"] = "left",
    down: bool = True,
) -> None:
    """Send a mouse button down/up event at (x, y) via native Win32 SendInput."""
    flag_map = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }
    if button not in flag_map:
        raise ValueError(f"Unsupported mouse button: '{button}'")
    flag = flag_map[button][0] if down else flag_map[button][1]
    nx, ny = _to_virtual_abs(x, y)
    ev = INPUT(type=INPUT_MOUSE)
    ev.union.mi = MOUSEINPUT(
        dx=nx,
        dy=ny,
        mouseData=0,
        dwFlags=flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
        time=0,
        dwExtraInfo=0,
    )
    arr = (INPUT * 1)(ev)
    user32.SendInput(1, arr, ctypes.sizeof(INPUT))


def _char_to_unicode_events(ch: str) -> list[INPUT]:
    """Encode one character (including astral UTF-16 surrogates) as SendInput events."""
    code = ord(ch)
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
    return events


def _send_unicode_char(ch: str) -> None:
    """Send a single character using Win32 SendInput KEYEVENTF_UNICODE."""
    if ch == "\n":
        _send_key_press("enter", hold_seconds=0.02)
        return
    if ch == "\t":
        _send_key_press("tab", hold_seconds=0.02)
        return

    events = _char_to_unicode_events(ch)
    arr = (INPUT * len(events))(*events)
    user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def _send_unicode_batch(text: str, chunk_size: int = 64) -> int:
    """Send text via batched SendInput arrays, splitting on Enter/Tab controls."""
    typed = 0
    buf: list[str] = []

    def _flush() -> None:
        nonlocal typed
        if not buf:
            return
        segment = "".join(buf)
        buf.clear()
        for start in range(0, len(segment), chunk_size):
            _check_abort()
            part = segment[start : start + chunk_size]
            events: list[INPUT] = []
            for ch in part:
                events.extend(_char_to_unicode_events(ch))
            if events:
                arr = (INPUT * len(events))(*events)
                user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
            typed += len(part)

    for ch in text:
        _check_abort()
        if ch in ("\n", "\t"):
            _flush()
            _send_key_press("enter" if ch == "\n" else "tab", hold_seconds=0.02)
            typed += 1
        else:
            buf.append(ch)
    _flush()
    return typed


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

_VK_MAP: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "shift": 0x10,
    "shiftleft": 0xA0,
    "shiftright": 0xA1,
    "ctrl": 0x11,
    "ctrlleft": 0xA2,
    "ctrlright": 0xA3,
    "alt": 0x12,
    "altleft": 0xA4,
    "altright": 0xA5,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "winleft": 0x5B,
    "winright": 0x5C,
    "apps": 0x5D,
    "numlock": 0x90,
    "scrolllock": 0x91,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{chr(c): c for c in range(ord("a"), ord("z") + 1)},
    **{chr(c): c for c in range(ord("0"), ord("9") + 1)},
}
# Make uppercase lookups align with VK_A..VK_Z (0x41..0x5A)
for _c in range(ord("a"), ord("z") + 1):
    _VK_MAP[chr(_c)] = _c - 32

_EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0xA3, 0xA5}


def _normalize_key_name(part: str) -> str:
    cleaned = part.strip().lower()
    return _KEY_ALIASES.get(cleaned, cleaned)


def _resolve_vk(norm_key: str) -> int:
    if norm_key in _VK_MAP:
        return _VK_MAP[norm_key]
    if len(norm_key) == 1:
        vk_res = int(user32.VkKeyScanW(norm_key))
        if vk_res != -1:
            return vk_res & 0xFF
    raise ValueError(f"Unsupported key name: '{norm_key}'")


def _send_vk_event(vk: int, down: bool = True) -> None:
    scan = int(user32.MapVirtualKeyW(vk, 0)) & 0xFF
    flags = KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0
    if not down:
        flags |= KEYEVENTF_KEYUP
    ev = INPUT(type=INPUT_KEYBOARD)
    ev.union.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    arr = (INPUT * 1)(ev)
    user32.SendInput(1, arr, ctypes.sizeof(INPUT))


def _send_key_press(norm_key: str, hold_seconds: float = 0.03) -> None:
    vk = _resolve_vk(norm_key)
    _send_vk_event(vk, down=True)
    if hold_seconds > 0.0:
        time.sleep(hold_seconds)
    _send_vk_event(vk, down=False)


def _send_hotkey(norm_keys: list[str], hold_seconds: float = 0.03) -> None:
    vks = [_resolve_vk(k) for k in norm_keys]
    for vk in vks:
        _send_vk_event(vk, down=True)
        time.sleep(0.01)
    if hold_seconds > 0.0:
        time.sleep(hold_seconds)
    for vk in reversed(vks):
        _send_vk_event(vk, down=False)
        time.sleep(0.01)


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
    _perform_click(int(target_x), int(target_y), button="left", hold_seconds=0.04)
    time.sleep(0.1)
    _check_abort()
    _send_hotkey(["ctrl", "v"], hold_seconds=0.03)
    return {
        "method_used": "clipboard",
        "file_path": str(target_path),
        "target_point": {"x": target_x, "y": target_y},
    }


_OCR_ENGINE_CACHE: Any = None


def _get_ocr_engine():
    """Return a cached Windows.Media.Ocr engine instance."""
    global _OCR_ENGINE_CACHE
    if _OCR_ENGINE_CACHE is not None:
        return _OCR_ENGINE_CACHE
    engine = winrt_ocr.OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        engine = winrt_ocr.OcrEngine.try_create_from_language(winrt_glob.Language("en-US"))
    if engine is None:
        raise RuntimeError(
            "Windows.Media.Ocr could not initialize an OCR engine. "
            "Ensure at least one Windows OCR language pack (e.g. en-US) is installed."
        )
    _OCR_ENGINE_CACHE = engine
    return engine


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

    engine = _get_ocr_engine()

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
    """Interpolated SendInput drag with abort checks on each step and safe mouseUp cleanup."""
    _check_abort()
    _send_mouse_move(int(x1), int(y1))
    time.sleep(0.02)
    _send_mouse_button(int(x1), int(y1), button=button, down=True)
    try:
        steps = max(10, int(max(0.1, duration) * 40))
        step_sleep = max(0.005, duration / steps)
        for i in range(1, steps + 1):
            _check_abort()
            t = i / steps
            nx = int(round(x1 + (x2 - x1) * t))
            ny = int(round(y1 + (y2 - y1) * t))
            _send_mouse_move(nx, ny)
            time.sleep(step_sleep)
    finally:
        _send_mouse_button(int(x2), int(y2), button=button, down=False)


# ---------------------------------------------------------------------------
# MCP Server Definition & Tools
# ---------------------------------------------------------------------------

mcp = MCPServer("weightless-windows-vision")


def _attach_fullscreen_status(
    res: dict[str, Any], target_hwnd: Optional[int] = None
) -> None:
    """Force borderless-fullscreen overlay compatibility if active and record status/warnings."""
    forced = ""
    if (_BUBBLE_OVERLAY.is_visible() or _BUBBLE_OVERLAY.is_session_active()) and _BUBBLE_OVERLAY.force_fullscreen_enabled:
        forced = _BUBBLE_OVERLAY.sync_fullscreen_overlay(timeout=0.6)
        if forced:
            res["fullscreen_forced"] = forced
        if target_hwnd:
            rc = RECT()
            if user32.GetWindowRect(target_hwnd, ctypes.byref(rc)):
                res["bounds"] = {
                    "x": int(rc.left),
                    "y": int(rc.top),
                    "w": int(rc.right - rc.left),
                    "h": int(rc.bottom - rc.top),
                }
    fs_info = detect_exclusive_fullscreen(
        _BUBBLE_OVERLAY.own_hwnds(), hud_hwnd=_BUBBLE_OVERLAY.hud_hwnd
    )
    if fs_info.get("hud_occluded") and not fs_info.get("exclusive_fullscreen"):
        time.sleep(0.15)
        _BUBBLE_OVERLAY.sync_fullscreen_overlay(timeout=0.3)
        fs_info = detect_exclusive_fullscreen(
            _BUBBLE_OVERLAY.own_hwnds(), hud_hwnd=_BUBBLE_OVERLAY.hud_hwnd
        )
    if fs_info.get("warning"):
        res["fullscreen_warning"] = fs_info["warning"]


@mcp.tool()
def show_status_bubble(
    message: str,
    status_type: Literal["intent", "acting", "thinking", "complete", "error", "hide"] = "intent",
    auto_hide_seconds: float = 0.0,
    goal: Optional[str] = None,
    looking_for: Optional[str] = None,
    step_detail: Optional[str] = None,
    mood: Optional[Literal["auto", "nerd", "punk", "goth", "detective", "happy", "none"]] = None,
    force_fullscreen: Optional[bool] = None,
    reset_abort: bool = False,
) -> dict[str, Any]:
    """Update the Fixed HUD card and Woolbur companion overlay on the active monitor.

    The overlay stays DPI-scaled and visible over borderless/F11 fullscreen apps while
    hiding synchronously during screenshots/OCR. Woolbur hops onto the HUD when thinking
    and follows the cursor when acting or assisting.

    Args:
        message: Concise status, action, or milestone text.
        status_type: 'intent' (sets overall Goal), 'acting' (cursor-following step),
            'thinking' (perches Woolbur on HUD with detailed plan), 'complete' (fades after ~4s),
            'error' (alerts with error styling), or 'hide' (dismisses HUD and Woolbur).
        auto_hide_seconds: Seconds before fading out (0 = stay visible for active task;
            defaults to 4.0 for 'complete' if <= 0).
        goal: Optional overarching Goal title displayed in the HUD header.
        looking_for: Optional description of the UI element/target currently sought
            (shown on the HUD and in Woolbur's speech bubble if the user moves the mouse).
        step_detail: Optional multi-line reasoning or plan description shown on the HUD.
        mood: Woolbur's accessory style ('auto', 'nerd', 'punk', 'goth', 'detective', 'happy').
        force_fullscreen: If True (default), gently nudge undecorated borderless fullscreen
            apps by 1px while the overlay is visible so HUD & Woolbur stay visible on top.
        reset_abort: Set True on a new 'intent' after acknowledging an emergency abort.
    """
    _ensure_mouse_hook()
    _ensure_abort_listener()
    if status_type == "hide":
        _BUBBLE_OVERLAY.hide(clear_intent=True)
        return _attach_user_guidance({"visible": False, "status_type": "hide"})
    if reset_abort and status_type == "intent":
        with _ABORT_LOCK:
            _ABORT_EVENT.clear()
    _check_abort()

    effective_hide = float(auto_hide_seconds)
    clear_intent = False
    combine_intent = False
    if status_type == "complete":
        clear_intent = True
        if effective_hide <= 0.0:
            effective_hide = 4.0
    elif status_type in ("acting", "thinking"):
        combine_intent = True

    displayed = _BUBBLE_OVERLAY.show(
        message,
        status_type=status_type,
        auto_hide_seconds=effective_hide,
        clear_intent=clear_intent,
        combine_intent=combine_intent,
        goal=goal,
        looking_for=looking_for,
        step_detail=step_detail,
        mood=mood,
        force_fullscreen=force_fullscreen,
    )
    res: dict[str, Any] = {
        "visible": True,
        "status_type": status_type,
        "displayed_text": displayed,
        "auto_hide_seconds": effective_hide,
    }
    if status_type in ("intent", "thinking"):
        _attach_fullscreen_status(res)
    return _attach_user_guidance(res)


@mcp.tool()
def ask_woolbur(
    question: str,
    options: Optional[list[str]] = None,
    allow_text: bool = True,
    context_hint: Optional[str] = None,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Prompt the user directly over the active app via Woolbur's interactive Q&A card.

    Use this when choosing between methods, clarifying ambiguous UI decisions, or asking
    'Why?' after the user helped or pointed via Assist-mode/Alt+W, without forcing a
    focus switch back to Antigravity chat.

    Args:
        question: Clear question to display beside Woolbur.
        options: Optional list of up to 8 clickable multiple-choice options (1..8 shortcuts).
        allow_text: Whether to include a free-text input box for custom replies.
        context_hint: Optional short context line shown below the question.
        timeout_seconds: Max seconds to wait for a response (default 180s).
    """
    _ensure_abort_listener()
    _ensure_mouse_hook()
    res = _BUBBLE_OVERLAY.ask_user_prompt(
        question=question,
        options=options,
        allow_text=allow_text,
        context_hint=context_hint,
        timeout_seconds=timeout_seconds,
    )
    return _attach_user_guidance(res)


def _begin_observe_action() -> None:
    """Ensure hardware hooks are active for read-only observation without triggering HUD churn."""
    _ensure_abort_listener()
    _ensure_mouse_hook()
    _check_abort()


def _resolve_screenshot_options(
    region: Optional[list[int]],
    scale: Optional[float],
    show_grid: Optional[bool],
    image_format: Optional[Literal["png", "jpeg"]],
) -> tuple[float, bool, Literal["png", "jpeg"]]:
    """Resolve screenshot scale/grid/format with low-res full-screen overview & crisp 1.0x region crops."""
    try:
        cfg = load_config_cached().get("settings", {})
    except Exception:
        cfg = {}
    is_region = region is not None
    if scale is None:
        eff_scale = 1.0 if is_region else float(cfg.get("default_screenshot_scale", 0.5))
    else:
        eff_scale = float(scale)
    eff_scale = max(0.25, min(1.0, eff_scale))
    eff_grid = bool(cfg.get("default_grid", True)) if show_grid is None else bool(show_grid)
    if image_format is None:
        raw_fmt = "png" if is_region else str(cfg.get("default_screenshot_format", "jpeg"))
    else:
        raw_fmt = str(image_format)
    eff_fmt: Literal["png", "jpeg"] = "jpeg" if raw_fmt.lower() in ("jpg", "jpeg") else "png"
    return eff_scale, eff_grid, eff_fmt


def _capture_and_save_screenshot(
    show_grid: Optional[bool] = None,
    grid_interval: int = 100,
    monitor_index: int = 0,
    region: Optional[list[int]] = None,
    image_format: Optional[Literal["png", "jpeg"]] = None,
    scale: Optional[float] = None,
) -> tuple[dict[str, Any], Image.Image]:
    """Capture monitor or region, optionally downscale & composite physical grid, save PNG/JPEG."""
    region_tuple = tuple(region) if region is not None else None
    bounds = _resolve_capture_bounds(monitor_index=monitor_index, region=region_tuple)
    eff_scale, eff_grid, fmt = _resolve_screenshot_options(region, scale, show_grid, image_format)

    rgb_img = _capture_region_rgb(bounds)
    phys_w, phys_h = rgb_img.size
    if eff_scale < 0.999:
        out_w = max(1, int(round(phys_w * eff_scale)))
        out_h = max(1, int(round(phys_h * eff_scale)))
        rgb_img = rgb_img.resize((out_w, out_h), Image.Resampling.BILINEAR)
    if eff_grid:
        rgb_img = _draw_coordinate_grid(
            rgb_img,
            bounds["left"],
            bounds["top"],
            grid_interval,
            physical_width=phys_w,
            physical_height=phys_h,
        ).convert("RGB")

    ext = "jpg" if fmt == "jpeg" else "png"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    mon_label = (
        f"reg_{bounds['left']}_{bounds['top']}"
        if region is not None
        else ("all" if monitor_index == -1 else str(monitor_index))
    )
    out_path = SCREENSHOTS_DIR / f"screenshot_{ts}_mon{mon_label}.{ext}"
    if fmt == "jpeg":
        rgb_img.save(out_path, format="JPEG", quality=88, optimize=False)
    else:
        rgb_img.save(out_path, format="PNG", compress_level=1)

    saved_bytes = int(out_path.stat().st_size)
    storage = _check_screenshot_storage(added_bytes=saved_bytes)
    payload: dict[str, Any] = {
        "file_path": str(out_path),
        "monitor_index": monitor_index,
        "region": list(region_tuple) if region_tuple is not None else None,
        "bounds": bounds,
        "width": int(rgb_img.width),
        "height": int(rgb_img.height),
        "physical_width": int(phys_w),
        "physical_height": int(phys_h),
        "scale": round(eff_scale, 3),
        "coordinate_space": "global_physical",
        "format": fmt,
        "grid_shown": eff_grid,
        "grid_interval": max(20, int(grid_interval)) if eff_grid else None,
        "dpi_mode": DPI_MODE,
        "storage": storage,
    }
    if eff_scale < 0.999:
        payload["detail_hint"] = (
            "Scaled overview capture. Grid labels and click/OCR coordinates are global physical pixels "
            "(0..physical_width). If text or small buttons are hard to read, follow up with "
            "take_screenshot(region=[x, y, w, h]) for a crisp 1.0x detail crop or take_screenshot(scale=1.0)."
        )
    return payload, rgb_img


def _maybe_capture_after_input(
    res: dict[str, Any],
    settle_seconds: float,
    screenshot_after: bool,
    screenshot_region: Optional[list[int]],
    screenshot_grid: Optional[bool],
    screenshot_format: Optional[Literal["png", "jpeg"]],
    screenshot_scale: Optional[float] = None,
) -> dict[str, Any]:
    """Optionally wait for UI settle (abort-aware) and capture a screenshot after an input action."""
    settle = max(0.0, min(5.0, float(settle_seconds)))
    if settle > 0.0:
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline:
            _check_abort()
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    if screenshot_after:
        _check_abort()
        payload, _ = _capture_and_save_screenshot(
            show_grid=screenshot_grid,
            grid_interval=100,
            monitor_index=0,
            region=screenshot_region,
            image_format=screenshot_format,
            scale=screenshot_scale,
        )
        res["screenshot"] = payload
    return res


@mcp.tool()
def get_screen_resolution() -> dict[str, Any]:
    """Return physical pixel dimensions across connected monitors and virtual screen.

    monitor_index=0 is the primary monitor, 1..N are individual monitors, and -1 is
    the full virtual screen across all monitors.
    """
    _begin_observe_action()
    return _attach_user_guidance(_collect_monitor_layout())


@mcp.tool()
def take_screenshot(
    show_grid: Optional[bool] = None,
    grid_interval: int = 100,
    monitor_index: int = 0,
    include_image_content: bool = False,
    region: Optional[list[int]] = None,
    image_format: Optional[Literal["png", "jpeg"]] = None,
    scale: Optional[float] = None,
) -> list[TextContent | ImageContent]:
    """Capture a screenshot (low-res full-screen overview by default, or crisp 1.0x detail crop),
    composite a physical global (x, y) pixel grid, save PNG/JPEG, and check cumulative screenshot storage.

    Args:
        show_grid: Overlay coordinate grid lines and global physical pixel coordinate markers (None uses Woolbur config, default True).
        grid_interval: Physical pixel spacing between major grid lines (minimum 20).
        monitor_index: 0 for primary monitor, 1..N for specific monitors, -1 for all monitors.
        include_image_content: If True, also attach inline base64 ImageContent (default False;
            prefer reading file_path with read_file).
        region: Optional global rectangle [x, y, width, height] for fast crisp crop (defaults to 1.0x PNG).
        image_format: 'png' or 'jpeg' (defaults to config 'jpeg' for full-screen overview, 'png' for region crops).
        scale: Output downscale factor in [0.25, 1.0] (defaults to config 0.5 for full-screen overview, 1.0 for region crops).
    """
    guidance = _cooperate_before_observe()
    _begin_observe_action()
    payload, rgb_img = _capture_and_save_screenshot(
        show_grid=show_grid,
        grid_interval=grid_interval,
        monitor_index=monitor_index,
        region=region,
        image_format=image_format,
        scale=scale,
    )
    _attach_user_guidance(payload, guidance)

    blocks: list[TextContent | ImageContent] = [
        TextContent(type="text", text=json.dumps(payload, indent=2))
    ]
    if include_image_content:
        buf = io.BytesIO()
        pil_fmt = "JPEG" if payload["format"] == "jpeg" else "PNG"
        save_kwargs = {"quality": 88} if pil_fmt == "JPEG" else {"compress_level": 1}
        rgb_img.save(buf, format=pil_fmt, **save_kwargs)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        mime = "image/jpeg" if pil_fmt == "JPEG" else "image/png"
        blocks.append(ImageContent(type="image", data=b64, mimeType=mime))
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
    guidance = _cooperate_before_observe()
    _begin_observe_action()
    region_tuple = tuple(region) if region is not None else None
    bounds = _resolve_capture_bounds(monitor_index=monitor_index, region=region_tuple)
    img = _capture_region_rgba(bounds)
    res = await _run_ocr_on_image(img, bounds["left"], bounds["top"])
    return _attach_user_guidance(res, guidance)


@mcp.tool()
def list_windows(include_minimized: bool = True) -> dict[str, Any]:
    """List visible desktop windows with non-empty titles, hwnd, pid, and global bounds."""
    guidance = _cooperate_before_observe()
    _begin_observe_action()
    wins = _enum_visible_windows(include_minimized=include_minimized)
    return _attach_user_guidance({"count": len(wins), "windows": wins}, guidance)


@mcp.tool()
def focus_window(window_title: str) -> dict[str, Any]:
    """Find a window matching window_title (by title or process name), restore if minimized,
    and bring to foreground."""
    is_antigravity = "antigravity" in window_title.strip().lower()
    if not is_antigravity:
        _check_abort()
    action_label = "Returning to Antigravity..." if is_antigravity else f"Focusing: {window_title[:40]}"
    with _bubble_action_scope(action_label, finish_to_thinking=not is_antigravity):
        wins = _enum_visible_windows(include_minimized=True)
        needle = window_title.strip().lower()
        matches = [
            w
            for w in wins
            if needle in w["title"].lower() or needle in w.get("process_name", "").lower()
        ]
        if not matches:
            try:
                pattern = re.compile(window_title, re.IGNORECASE)
                matches = [
                    w
                    for w in wins
                    if pattern.search(w["title"]) or pattern.search(w.get("process_name", ""))
                ]
            except re.error:
                matches = []

        if not matches:
            return _attach_user_guidance(
                {
                    "success": False,
                    "error": f"No visible window matched '{window_title}'.",
                    "available_windows": [
                        {"title": w["title"], "process_name": w.get("process_name", "")}
                        for w in wins[:25]
                    ],
                }
            )

        def _area(w: dict[str, Any]) -> int:
            b = w["bounds"]
            return max(0, b["w"]) * max(0, b["h"])

        # Prefer exact title match, then exact process name match, then largest non-minimized window
        exact_title = [w for w in matches if w["title"].lower() == needle]
        exact_proc = [
            w
            for w in matches
            if w.get("process_name", "").lower() in (needle, f"{needle}.exe")
        ]
        candidates = exact_title or exact_proc or matches
        candidates.sort(key=lambda w: (not w["is_minimized"], _area(w)), reverse=True)
        target = candidates[0]
        hwnd = target["hwnd"]

        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.03)

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

        for _ in range(5):
            if user32.GetForegroundWindow() == hwnd:
                break
            time.sleep(0.01)

        rc = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        res: dict[str, Any] = {
            "success": True,
            "focused": user32.GetForegroundWindow() == hwnd,
            "hwnd": hwnd,
            "process_name": target.get("process_name", ""),
            "title": target["title"],
            "bounds": {
                "x": int(rc.left),
                "y": int(rc.top),
                "w": int(rc.right - rc.left),
                "h": int(rc.bottom - rc.top),
            },
        }
        if not is_antigravity and _BUBBLE_OVERLAY.hud_hwnd:
            time.sleep(0.04)
        _attach_fullscreen_status(res, target_hwnd=None if is_antigravity else hwnd)
        if not is_antigravity:
            try:
                mem = find_app_memory(
                    process_name=target.get("process_name", ""),
                    title=target.get("title", ""),
                )
                if mem is not None:
                    res["app_memory"] = mem
                elif load_config_cached()["settings"].get("auto_save_app_memory", True):
                    res["app_memory_hint"] = (
                        f"No saved per-app memory yet for '{target.get('process_name') or target['title']}'. "
                        "Use update_app_memory to save reliable coordinates, hotkeys, and quirks you discover."
                    )
            except Exception:
                pass
        return _attach_user_guidance(res)


@mcp.tool()
def get_app_memory(
    app_name: Optional[str] = None,
    process_name: Optional[str] = None,
    window_title: Optional[str] = None,
) -> dict[str, Any]:
    """Read persistent per-app memory (coordinates, hotkeys, notes, timing/scale preferences).

    If all filter arguments are omitted, returns all saved per-app profiles.
    Otherwise finds the matching app profile by key/name, executable process_name, or window_title.
    """
    guidance = _cooperate_before_observe()
    _begin_observe_action()
    if not any(str(v or "").strip() for v in (app_name, process_name, window_title)):
        apps = load_app_memory().get("apps", {})
        return _attach_user_guidance(
            {"count": len(apps), "apps": list(apps.values())},
            guidance,
        )
    mem = find_app_memory(
        app_name=app_name,
        process_name=process_name,
        title=window_title,
    )
    return _attach_user_guidance({"found": mem is not None, "app_memory": mem}, guidance)


@mcp.tool()
def update_app_memory(
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
    """Create, update, or clear persistent per-app memory for an application.

    Stores reusable notes, named UI coordinates (e.g. {'play_hand': {'x': 520, 'y': 980}}),
    hotkeys (e.g. {'new_run': 'r'}), and tester overrides so future sessions auto-load them via
    focus_window and the user can edit them in Woolbur's Antigravity sidebar.
    """
    _begin_observe_action()
    entry = upsert_app_memory(
        app_name=app_name,
        display_name=display_name,
        process_names=process_names,
        title_patterns=title_patterns,
        add_note=add_note,
        remove_note=remove_note,
        coordinates=coordinates,
        remove_coordinates=remove_coordinates,
        hotkeys=hotkeys,
        settings=settings,
        clear=clear,
    )
    return _attach_user_guidance({"updated": True, "app_memory": entry})


def _perform_click(
    x: int,
    y: int,
    button: Literal["left", "right", "middle"] = "left",
    hold_seconds: float = 0.05,
) -> None:
    """Move via SendInput, settle hover across frames, then press-hold-release for SDL2/game reliability."""
    tx, ty = int(x), int(y)
    _send_mouse_move(tx, ty)
    time.sleep(0.025)
    hold = max(0.0, min(1.0, float(hold_seconds)))
    _send_mouse_button(tx, ty, button=button, down=True)
    if hold > 0.0:
        time.sleep(hold)
    _send_mouse_button(tx, ty, button=button, down=False)


@mcp.tool()
def mouse_click(
    x: int,
    y: int,
    button: Literal["left", "right", "middle"] = "left",
    clicks: int = 1,
    hold_seconds: float = 0.05,
    settle_seconds: float = 0.0,
    screenshot_after: bool = False,
    screenshot_region: Optional[list[int]] = None,
    screenshot_grid: Optional[bool] = None,
    screenshot_format: Optional[Literal["png", "jpeg"]] = None,
    screenshot_scale: Optional[float] = None,
) -> dict[str, Any]:
    """Move cursor via Win32 SendInput to global (x, y) and click with the specified button.

    Includes a hover settle and press-hold delay (default 50ms) so SDL2/LÖVE/DirectX
    buttons register reliably. Optionally waits settle_seconds and captures a follow-up
    screenshot in the same MCP round-trip (`screenshot_after=True`).
    """
    paused = _cooperate_before_input()
    if paused is not None:
        return paused
    count = max(1, int(clicks))
    hold = max(0.0, min(1.0, float(hold_seconds)))
    label = f"Clicking ({int(x)}, {int(y)})" if count == 1 else f"Clicking {count}x at ({int(x)}, {int(y)})"
    with _bubble_action_scope(label):
        _begin_input_action()
        with _suppress_physical_mouse():
            for idx in range(count):
                _check_abort()
                _perform_click(int(x), int(y), button=button, hold_seconds=hold)
                if idx + 1 < count:
                    time.sleep(0.03)
        payload: dict[str, Any] = {
            "clicked": True,
            "x": int(x),
            "y": int(y),
            "button": button,
            "clicks": count,
            "hold_seconds": hold,
        }
        _maybe_capture_after_input(
            payload,
            settle_seconds=settle_seconds,
            screenshot_after=screenshot_after,
            screenshot_region=screenshot_region,
            screenshot_grid=screenshot_grid,
            screenshot_format=screenshot_format,
            screenshot_scale=screenshot_scale,
        )
        return _attach_user_guidance(payload)


@mcp.tool()
def batch_clicks(
    points: list[list[int]],
    button: Literal["left", "right", "middle"] = "left",
    hold_seconds: float = 0.05,
    delay_between: float = 0.06,
    label: Optional[str] = None,
    settle_seconds: float = 0.0,
    screenshot_after: bool = False,
    screenshot_region: Optional[list[int]] = None,
    screenshot_grid: Optional[bool] = None,
    screenshot_format: Optional[Literal["png", "jpeg"]] = None,
    screenshot_scale: Optional[float] = None,
) -> dict[str, Any]:
    """Click a sequence of global [x, y] coordinates via Win32 SendInput in one MCP round-trip.

    Ideal for selecting multiple items/cards and clicking an action button (e.g. Play Hand),
    and optionally settling + capturing the resulting screen (`screenshot_after=True`) in one call.
    """
    paused = _cooperate_before_input()
    if paused is not None:
        return paused
    coords: list[tuple[int, int]] = []
    for pt in points:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise ValueError(f"Each point must be [x, y], got {pt!r}")
        coords.append((int(pt[0]), int(pt[1])))
    if not coords:
        return _attach_user_guidance({"clicked_count": 0, "points": []})

    hold = max(0.0, min(1.0, float(hold_seconds)))
    gap = max(0.0, min(2.0, float(delay_between)))
    desc = " ".join(str(label or "").split()).strip() or f"Batch clicking {len(coords)} targets"
    cap = max(8.0, len(coords) * (hold + gap + 0.06) + 4.0)
    clicked_pts: list[dict[str, int]] = []
    with _bubble_action_scope(desc):
        _begin_input_action()
        with _suppress_physical_mouse(duration_cap=cap):
            for idx, (px, py) in enumerate(coords):
                _check_abort()
                _perform_click(px, py, button=button, hold_seconds=hold)
                clicked_pts.append({"x": px, "y": py})
                if idx + 1 < len(coords) and gap > 0:
                    time.sleep(gap)
        payload: dict[str, Any] = {
            "clicked_count": len(clicked_pts),
            "points": clicked_pts,
            "button": button,
            "hold_seconds": hold,
            "delay_between": gap,
        }
        _maybe_capture_after_input(
            payload,
            settle_seconds=settle_seconds,
            screenshot_after=screenshot_after,
            screenshot_region=screenshot_region,
            screenshot_grid=screenshot_grid,
            screenshot_format=screenshot_format,
            screenshot_scale=screenshot_scale,
        )
        return _attach_user_guidance(payload)


@mcp.tool()
def mouse_move(x: int, y: int, duration: float = 0.0) -> dict[str, Any]:
    """Move the cursor via Win32 SendInput to global (x, y) (instant jump by default).

    Physical mousepad/mouse inputs are suppressed during movement to prevent drift.
    Emergency abort hotkeys: Triple-Esc, Pause, or Ctrl+Shift+Esc.
    """
    paused = _cooperate_before_input()
    if paused is not None:
        return paused
    tx, ty = int(x), int(y)
    dur = max(0.0, float(duration))
    with _bubble_action_scope(f"Moving cursor to ({tx}, {ty})"):
        _begin_input_action()
        _check_abort()
        with _suppress_physical_mouse():
            if dur <= 0.0:
                _send_mouse_move(tx, ty)
            else:
                sx, sy = get_cursor_pos()
                steps = max(4, int(dur * 60))
                step_s = dur / steps
                for i in range(1, steps + 1):
                    _check_abort()
                    t = i / steps
                    _send_mouse_move(int(round(sx + (tx - sx) * t)), int(round(sy + (ty - sy) * t)))
                    time.sleep(step_s)
        cx, cy = get_cursor_pos()
        return _attach_user_guidance({"moved": True, "x": cx, "y": cy})


@mcp.tool()
def mouse_drag(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration: float = 0.4,
    button: Literal["left", "right", "middle"] = "left",
) -> dict[str, Any]:
    """Press and drag the mouse from global (x1, y1) to (x2, y2).

    Physical mousepad/mouse inputs are suppressed during drag to prevent drift.
    Emergency abort hotkeys: Triple-Esc, Pause, or Ctrl+Shift+Esc.
    """
    paused = _cooperate_before_input()
    if paused is not None:
        return paused
    with _bubble_action_scope(f"Dragging ({int(x1)}, {int(y1)}) -> ({int(x2)}, {int(y2)})"):
        _begin_input_action()
        with _suppress_physical_mouse(duration_cap=max(5.0, float(duration) + 3.0)):
            _drag_between(int(x1), int(y1), int(x2), int(y2), duration=float(duration), button=button)
        return _attach_user_guidance(
            {
                "dragged": True,
                "from": {"x": int(x1), "y": int(y1)},
                "to": {"x": int(x2), "y": int(y2)},
                "button": button,
            }
        )


@mcp.tool()
def type_text(
    text: str,
    delay_between_keys: float = 0.005,
    fast_batch: bool = True,
) -> dict[str, Any]:
    """Type text into the focused control using Unicode SendInput.

    Uses fast batched SendInput arrays by default when delay_between_keys <= 0.005
    and len(text) > 1. Physical mousepad/mouse inputs are suppressed while typing.
    Emergency abort hotkeys: Triple-Esc, Pause, or Ctrl+Shift+Esc.
    """
    paused = _cooperate_before_input()
    if paused is not None:
        return paused
    preview = text.replace("\n", " ")[:18]
    label = f"Typing '{preview}...' ({len(text)} chars)" if len(text) > 18 else f"Typing '{preview}'"
    with _bubble_action_scope(label, step_summary=f"Typed {len(text)} chars"):
        _begin_input_action()
        delay = max(0.0, float(delay_between_keys))
        typed = 0
        with _suppress_physical_mouse(duration_cap=max(5.0, len(text) * (delay + 0.02) + 2.0)):
            if fast_batch and delay <= 0.005 and len(text) > 1:
                typed = _send_unicode_batch(text)
            else:
                for ch in text:
                    _check_abort()
                    _send_unicode_char(ch)
                    typed += 1
                    if delay > 0:
                        time.sleep(delay)
        return _attach_user_guidance({"typed_chars": typed})


@mcp.tool()
def press_key(key: str, hold_seconds: float = 0.04) -> dict[str, Any]:
    """Press a single key ('enter', 'tab', 'esc') or '+'-separated hotkey ('ctrl+a', 'alt+tab').

    Dispatches native Win32 SendInput with hardware scan codes and a brief hold delay
    (default 40ms) so games/SDL2 apps register key presses reliably. Physical mousepad/mouse
    inputs are suppressed during execution. Emergency abort: Triple-Esc, Pause, Ctrl+Shift+Esc.
    """
    paused = _cooperate_before_input()
    if paused is not None:
        return paused
    raw = key.strip()
    hold = max(0.0, min(1.0, float(hold_seconds)))
    with _bubble_action_scope(f"Pressing {raw}"):
        _begin_input_action()
        _check_abort()
        with _suppress_physical_mouse():
            if "+" in raw:
                parts = [_normalize_key_name(p) for p in raw.split("+") if p.strip()]
                if not parts:
                    raise ValueError(f"Invalid hotkey specification: '{key}'")
                _send_hotkey(parts, hold_seconds=hold)
                return _attach_user_guidance({"pressed_hotkey": parts, "hold_seconds": hold})
            norm = _normalize_key_name(raw)
            _send_key_press(norm, hold_seconds=hold)
            return _attach_user_guidance({"pressed_key": norm, "hold_seconds": hold})


@mcp.tool()
def set_physical_mouse_lock(locked: bool, timeout_seconds: float = 15.0) -> dict[str, Any]:
    """Manually lock or unlock physical mousepad/mouse input for an extended operation.

    When locked, physical mousepad touches and external mouse movements are dropped
    while synthetic agent actions execute uninterrupted. The lock automatically
    releases after timeout_seconds or immediately if an emergency abort hotkey is
    pressed (Triple-Esc, Pause, Ctrl+Shift+Esc).

    Args:
        locked: True to suppress physical mouse/touchpad; False to re-enable.
        timeout_seconds: Automatic safety release timeout in seconds (default 15.0, max 60.0).
    """
    global _PHYSICAL_MOUSE_BLOCKED, _LAST_BLOCK_TIME, _MAX_BLOCK_DURATION
    label = "Locking physical mouse..." if locked else "Unlocking physical mouse..."
    with _bubble_action_scope(label):
        _ensure_mouse_hook()
        _ensure_abort_listener()
        with _MOUSE_BLOCK_LOCK:
            if locked:
                _LAST_BLOCK_TIME = time.time()
                _MAX_BLOCK_DURATION = min(60.0, max(1.0, float(timeout_seconds)))
                _PHYSICAL_MOUSE_BLOCKED = True
            else:
                _PHYSICAL_MOUSE_BLOCKED = False
        return _attach_user_guidance(
            {
                "locked": bool(_PHYSICAL_MOUSE_BLOCKED),
                "timeout_seconds": _MAX_BLOCK_DURATION if locked else 0.0,
                "escape_keys": ["Triple-Esc", "Pause", "Ctrl+Shift+Esc"],
            }
        )


@mcp.tool()
def set_clipboard(text: str) -> dict[str, Any]:
    """Set the Windows system clipboard text."""
    with _bubble_action_scope("Updating clipboard..."):
        pyperclip.copy(text)
        return _attach_user_guidance({"copied": True, "length": len(text)})


@mcp.tool()
def get_clipboard() -> dict[str, Any]:
    """Read the current Windows system clipboard text."""
    with _bubble_action_scope("Reading clipboard..."):
        text = pyperclip.paste()
        return _attach_user_guidance({"text": text, "length": len(text)})


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
    paused = _cooperate_before_input()
    if paused is not None:
        return paused
    resolved = Path(file_path).expanduser().resolve()
    with _bubble_action_scope(f"Dropping {resolved.name} at ({int(target_x)}, {int(target_y)})"):
        _begin_input_action()
        _check_abort()
        if not resolved.exists():
            raise FileNotFoundError(f"File does not exist: {resolved}")

        with _suppress_physical_mouse(duration_cap=15.0):
            if method in ("auto", "wm_dropfiles"):
                return _attach_user_guidance(
                    _drop_via_wm_dropfiles(resolved, int(target_x), int(target_y))
                )
            if method == "clipboard":
                return _attach_user_guidance(
                    _drop_via_clipboard_cf_hdrop(resolved, int(target_x), int(target_y))
                )
            if method == "explorer":
                return _attach_user_guidance(
                    await _drop_via_explorer_drag(resolved, int(target_x), int(target_y))
                )
        raise ValueError(f"Unsupported drag_and_drop_file method: '{method}'")


if __name__ == "__main__":
    _ensure_abort_listener()
    _ensure_mouse_hook()
    _BUBBLE_OVERLAY.register_callbacks(
        focus_antigravity_cb=lambda: focus_window("Antigravity"),
        abort_cb=_trigger_abort,
    )
    logger.info("Starting weightless-windows-vision MCP server (DPI mode: %s)", DPI_MODE)
    if os.environ.get("DESKTOP_TESTER_NO_STARTUP_BUBBLE") != "1":
        _BUBBLE_OVERLAY.show(
            "Woolbur is ready — Alt+W to steer anytime",
            status_type="intent",
            goal="Weightless Tester Ready",
            step_detail="HUD & Woolbur will track active tasks over desktop and borderless windows.",
            auto_hide_seconds=12.0,
            combine_intent=False,
            mark_session_active=False,
        )
    mcp.run(transport="stdio")
