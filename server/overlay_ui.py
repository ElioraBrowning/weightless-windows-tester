"""Dual-window Dark Glass HUD, Woolbur companion, and interactive Q&A overlay for Windows.

Manages three coordinated Win32 layered windows on a dedicated UI thread:
1. WeightlessHudWnd: DPI-scaled top-center dark glass HUD card on the active monitor.
2. WeightlessWoolburWnd: Animated Sanrio-cute Woolbur companion + speech bubble that
   hops onto the HUD when thinking and glides beside the cursor when acting/assisting.
3. WeightlessPromptWnd: Clickable multiple-choice & text input card for ask_woolbur
   and Alt+W mid-run steering without leaving the target application.
"""

from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
import logging
import math
import queue
import threading
import time
from typing import Any, Callable, Optional

from PIL import Image, ImageDraw, ImageFont
import pyperclip

from woolbur_sprites import render_woolbur_frame, resolve_appearance, resolve_mood
from woolbur_store import load_config_cached, parse_hex_rgba


logger = logging.getLogger("desktop_vision_mcp")

# Use private WinDLL instances so overlay_ui ctypes argtypes never collide with
# server.py, pyautogui, or pynput sharing the global ctypes.windll cache.
user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32")
gdi32 = ctypes.WinDLL("gdi32")
dwmapi = ctypes.WinDLL("dwmapi")
shcore = ctypes.WinDLL("shcore")
shell32 = ctypes.WinDLL("shell32")

_HUD_WINDOW_CLASS = "WeightlessHudWnd"
_WOOLBUR_WINDOW_CLASS = "WeightlessWoolburWnd"
_PROMPT_WINDOW_CLASS = "WeightlessPromptWnd"
ALL_OVERLAY_CLASSES = {_HUD_WINDOW_CLASS, _WOOLBUR_WINDOW_CLASS, _PROMPT_WINDOW_CLASS}

SW_HIDE = 0
SW_SHOW = 5
SW_SHOWNOACTIVATE = 4

WS_POPUP = 0x80000000
WS_THICKFRAME = 0x00040000
WS_CAPTION = 0x00C00000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

GWL_STYLE = -16
GWL_EXSTYLE = -20
GW_HWNDPREV = 3
GW_HWNDNEXT = 2
MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 1
MDT_EFFECTIVE_DPI = 0
DWMWA_TRANSITIONS_FORCEDISABLED = 3
DWMWA_EXCLUDED_FROM_PEEK = 12
DWMWA_CLOAKED = 14
QUNS_RUNNING_D3D_FULL_SCREEN = 3

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SWP_NOSENDCHANGING = 0x0400
SWP_ASYNCWINDOWPOS = 0x4000

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_KEYDOWN = 0x0100
WM_CHAR = 0x0102
WM_KILLFOCUS = 0x0008

VK_BACK = 0x08
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_HOME = 0x24
VK_END = 0x23
VK_DELETE = 0x2E
VK_CONTROL = 0x11
VK_MENU = 0x12


WNDPROCTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROCTYPE),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


def _configure_win32_types() -> None:
    """Declare 64-bit safe Win32 signatures for multi-overlay rendering and Z-order management."""
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND,
        wintypes.HDC,
        ctypes.POINTER(wintypes.POINT),
        ctypes.POINTER(_SIZE),
        wintypes.HDC,
        ctypes.POINTER(wintypes.POINT),
        wintypes.COLORREF,
        ctypes.POINTER(_BLENDFUNCTION),
        wintypes.DWORD,
    ]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetTopWindow.argtypes = [wintypes.HWND]
    user32.GetTopWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFOEXW)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetKeyState.argtypes = [ctypes.c_int]
    user32.GetKeyState.restype = ctypes.c_short
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(_BITMAPINFOHEADER),
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL

    dwmapi.DwmFlush.argtypes = []
    dwmapi.DwmFlush.restype = ctypes.c_long
    dwmapi.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    dwmapi.DwmSetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

    try:
        shcore.GetDpiForMonitor.argtypes = [
            wintypes.HMONITOR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        ]
        shcore.GetDpiForMonitor.restype = ctypes.c_long
    except Exception:
        pass

    try:
        shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
        shell32.SHQueryUserNotificationState.restype = ctypes.c_long
    except Exception:
        pass


_configure_win32_types()


_FONT_CACHE: dict[tuple[tuple[str, ...], int], ImageFont.ImageFont | ImageFont.FreeTypeFont] = {}
_FONT_LOCK = threading.Lock()


def _load_font(
    size: int,
    candidates: tuple[str, ...] = ("segoeui.ttf", "arial.ttf"),
) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    sz = max(10, int(size))
    key = (candidates, sz)
    with _FONT_LOCK:
        cached = _FONT_CACHE.get(key)
        if cached is not None:
            return cached
        loaded: Optional[ImageFont.ImageFont | ImageFont.FreeTypeFont] = None
        for cand in candidates:
            try:
                loaded = ImageFont.truetype(cand, sz)
                break
            except Exception:
                continue
        if loaded is None:
            loaded = ImageFont.load_default()
        _FONT_CACHE[key] = loaded
        return loaded


def _monitor_info_from_hmonitor(hm: Optional[int]) -> tuple[int, int, int, int, float]:
    if hm:
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if user32.GetMonitorInfoW(hm, ctypes.byref(info)):
            rc = info.rcMonitor
            w = max(640, int(rc.right - rc.left))
            h = max(480, int(rc.bottom - rc.top))
            scale = 1.0
            try:
                dx = wintypes.UINT(96)
                dy = wintypes.UINT(96)
                if shcore.GetDpiForMonitor(hm, MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                    scale = max(1.0, float(dx.value) / 96.0)
            except Exception:
                scale = 1.0
            return int(rc.left), int(rc.top), w, h, scale
    w = max(640, int(user32.GetSystemMetrics(0) or 1920))
    h = max(480, int(user32.GetSystemMetrics(1) or 1080))
    return 0, 0, w, h, 1.0


def get_cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        return int(pt.x), int(pt.y)
    return 0, 0


def get_monitor_at_point(x: int, y: int) -> tuple[int, int, int, int, float]:
    hm = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST)
    return _monitor_info_from_hmonitor(hm)


def get_active_monitor_info(own_hwnds: Optional[set[int]] = None) -> tuple[int, int, int, int, float]:
    """Return (left, top, width, height, dpi_scale) for the active foreground window or cursor."""
    fg = user32.GetForegroundWindow()
    if fg and (not own_hwnds or int(fg) not in own_hwnds):
        hm = user32.MonitorFromWindow(fg, MONITOR_DEFAULTTONEAREST)
        if hm:
            return _monitor_info_from_hmonitor(hm)
    cx, cy = get_cursor_pos()
    return get_monitor_at_point(cx, cy)


def _is_hwnd_occluded(hwnd: int, own_hwnds: Optional[set[int]] = None) -> bool:
    """Return True if any visible non-cloaked sibling window overlaps and sits above hwnd."""
    rc_me = _RECT()
    if not hwnd or not user32.IsWindowVisible(hwnd) or not user32.GetWindowRect(hwnd, ctypes.byref(rc_me)):
        return False
    own = own_hwnds or set()
    prev = user32.GetWindow(hwnd, GW_HWNDPREV)
    steps = 0
    while prev and steps < 250:
        steps += 1
        p_int = int(prev)
        if p_int not in own and user32.IsWindowVisible(prev):
            cloaked = ctypes.c_int(0)
            dwmapi.DwmGetWindowAttribute(prev, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            if cloaked.value == 0:
                rc = _RECT()
                if user32.GetWindowRect(prev, ctypes.byref(rc)) and rc.right > rc.left and rc.bottom > rc.top:
                    if not (
                        rc.right <= rc_me.left
                        or rc.left >= rc_me.right
                        or rc.bottom <= rc_me.top
                        or rc.top >= rc_me.bottom
                    ):
                        return True
        prev = user32.GetWindow(prev, GW_HWNDPREV)
    return False


def get_user_notification_state() -> int:
    """Return Windows SHQueryUserNotificationState value (0 if unavailable)."""
    try:
        state = ctypes.c_int(0)
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            return int(state.value)
    except Exception:
        pass
    return 0


def _get_window_class_name(hwnd: int) -> str:
    """Return Win32 class name for an HWND cleanly."""
    buf = ctypes.create_unicode_buffer(128)
    if user32.GetClassNameW(hwnd, buf, len(buf)):
        return buf.value
    return ""


def detect_exclusive_fullscreen(
    own_hwnds: Optional[set[int]] = None,
    hud_hwnd: Optional[int] = None,
) -> dict[str, Any]:
    """Detect whether Windows reports D3D Exclusive Fullscreen or actual overlay occlusion."""
    quns_val = get_user_notification_state()
    quns_fullscreen = quns_val == QUNS_RUNNING_D3D_FULL_SCREEN

    fg = user32.GetForegroundWindow()
    fg_covers_monitor = False
    fg_style = 0
    fg_exstyle = 0
    if fg and (not own_hwnds or int(fg) not in own_hwnds):
        fg_style = int(user32.GetWindowLongW(fg, GWL_STYLE))
        fg_exstyle = int(user32.GetWindowLongW(fg, GWL_EXSTYLE))
        rc = _RECT()
        if user32.GetWindowRect(fg, ctypes.byref(rc)):
            ml, mt, mw, mh, _ = _monitor_info_from_hmonitor(
                user32.MonitorFromWindow(fg, MONITOR_DEFAULTTONEAREST)
            )
            fg_covers_monitor = (
                rc.left <= ml and rc.top <= mt and rc.right >= ml + mw and rc.bottom >= mt + mh
            )

    occluded = _is_hwnd_occluded(hud_hwnd, own_hwnds) if hud_hwnd else False
    hud_exstyle = int(user32.GetWindowLongW(hud_hwnd, GWL_EXSTYLE)) if hud_hwnd else 0
    logger.info(
        "Fullscreen check: quns=%d fg=%s fg_covers=%s fg_style=0x%x fg_ex=0x%x hud_ex=0x%x occluded=%s",
        quns_val,
        int(fg) if fg else 0,
        fg_covers_monitor,
        fg_style & 0xFFFFFFFF,
        fg_exstyle & 0xFFFFFFFF,
        hud_exstyle & 0xFFFFFFFF,
        occluded,
    )
    warning: Optional[str] = None
    if occluded:
        warning = (
            "Active app is occluding the desktop overlay (likely Exclusive Fullscreen or elevated). "
            "Switch the app to Windowed mode so the HUD and Woolbur remain visible."
        )
    elif quns_fullscreen and fg_covers_monitor:
        warning = (
            "Active window covers the full monitor and triggers Windows fullscreen presentation "
            "(QUNS_RUNNING_D3D_FULL_SCREEN=3), which hides unprivileged ZBID_DESKTOP overlays even "
            "in Borderless mode. Switch the app to Windowed mode for HUD & Woolbur visibility."
        )
    return {
        "quns_state": quns_val,
        "exclusive_fullscreen": quns_fullscreen,
        "foreground_covers_monitor": fg_covers_monitor,
        "hud_occluded": occluded,
        "fg_style": hex(fg_style & 0xFFFFFFFF),
        "fg_exstyle": hex(fg_exstyle & 0xFFFFFFFF),
        "hud_exstyle": hex(hud_exstyle & 0xFFFFFFFF),
        "warning": warning,
    }


def _pil_to_bgra(img: Image.Image) -> tuple[int, int, bytes]:
    premult = img.convert("RGBa")
    r, g, b, a = premult.split()
    return img.width, img.height, Image.merge("RGBA", (b, g, r, a)).tobytes()


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: Any,
    max_width_px: int,
    max_lines: int,
) -> list[str]:
    cleaned = " ".join(str(text).split()).strip()
    if not cleaned:
        return []
    words = cleaned.split(" ")
    lines: list[str] = []
    current = ""

    def _text_w(s: str) -> int:
        bb = draw.textbbox((0, 0), s, font=font)
        return bb[2] - bb[0]

    for word in words:
        cand = f"{current} {word}".strip() if current else word
        if _text_w(cand) <= max_width_px:
            current = cand
        else:
            if current:
                lines.append(current)
                current = word
            else:
                # Break oversized token cleanly
                chunk = ""
                for ch in word:
                    if _text_w(chunk + ch) <= max_width_px:
                        chunk += ch
                    else:
                        if chunk:
                            lines.append(chunk)
                        chunk = ch
                current = chunk
            if len(lines) >= max_lines:
                break

    if current and len(lines) < max_lines:
        lines.append(current)
    elif current and len(lines) == max_lines:
        # Ellipsize last line if overflowed
        last = lines[-1]
        while last and _text_w(last + "...") > max_width_px:
            last = last[:-1].rstrip()
        lines[-1] = (last + "...") if last else "..."
    return lines


class _LayeredSurface:
    """Manages a single Win32 WS_EX_LAYERED popup HWND with DIB caching and click-through toggling."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.visible = False
        self.temp_hidden = False
        self.click_through = True
        self.x = 0
        self.y = 0
        self.w = 0
        self.h = 0
        self._dc: Optional[int] = None
        self._bmp: Optional[int] = None
        self._old_bmp: Optional[int] = None
        self._alpha = 255
        self._render_key: Any = None

    def free_bitmap(self) -> None:
        if self._dc:
            if self._old_bmp:
                gdi32.SelectObject(self._dc, self._old_bmp)
                self._old_bmp = None
            if self._bmp:
                gdi32.DeleteObject(self._bmp)
                self._bmp = None
            gdi32.DeleteDC(self._dc)
            self._dc = None
        self.w = 0
        self.h = 0
        self._render_key = None

    def set_click_through(self, click_through: bool) -> None:
        if self.click_through == click_through:
            return
        self.click_through = click_through
        ex = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        if click_through:
            ex |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        else:
            ex &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex)
        user32.SetWindowPos(
            self.hwnd,
            wintypes.HWND(HWND_TOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE | SWP_FRAMECHANGED,
        )

    def upload_bgra(self, w: int, h: int, bgra: bytes, render_key: Any = None) -> None:
        self.free_bitmap()
        screen_dc = user32.GetDC(None)
        try:
            self._dc = gdi32.CreateCompatibleDC(screen_dc)
            bmi = _BITMAPINFOHEADER(
                biSize=ctypes.sizeof(_BITMAPINFOHEADER),
                biWidth=w,
                biHeight=-h,
                biPlanes=1,
                biBitCount=32,
                biCompression=0,
                biSizeImage=w * h * 4,
            )
            ppv_bits = ctypes.c_void_p()
            self._bmp = gdi32.CreateDIBSection(
                self._dc,
                ctypes.byref(bmi),
                0,
                ctypes.byref(ppv_bits),
                None,
                0,
            )
            self._old_bmp = gdi32.SelectObject(self._dc, self._bmp)
            ctypes.memmove(ppv_bits, bgra, len(bgra))
            self.w = w
            self.h = h
            self._render_key = render_key
        finally:
            user32.ReleaseDC(None, screen_dc)

    def present(self, x: int, y: int, alpha: int = 255, activate: bool = False) -> None:
        self.x = int(x)
        self.y = int(y)
        self._alpha = max(0, min(255, int(alpha)))
        if not self._dc or self.w <= 0 or self.h <= 0:
            return
        screen_dc = user32.GetDC(None)
        try:
            pt_dst = wintypes.POINT(self.x, self.y)
            pt_src = wintypes.POINT(0, 0)
            sz = _SIZE(self.w, self.h)
            blend = _BLENDFUNCTION(AC_SRC_OVER, 0, self._alpha, AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(
                self.hwnd,
                screen_dc,
                ctypes.byref(pt_dst),
                ctypes.byref(sz),
                self._dc,
                ctypes.byref(pt_src),
                0,
                ctypes.byref(blend),
                ULW_ALPHA,
            )
        finally:
            user32.ReleaseDC(None, screen_dc)
        if not self.visible and not self.temp_hidden:
            user32.ShowWindow(self.hwnd, SW_SHOW if activate else SW_SHOWNOACTIVATE)
            self.visible = True

    def hide(self) -> None:
        if self.visible or self.temp_hidden:
            user32.ShowWindow(self.hwnd, SW_HIDE)
        self.visible = False
        self.temp_hidden = False

    def contains_point(self, px: int, py: int) -> bool:
        if not self.visible or self.temp_hidden or self.w <= 0 or self.h <= 0:
            return False
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h


class _WoolburOverlayManager:
    """Coordinates Fixed HUD, animated Woolbur companion + speech bubble, and interactive Q&A."""

    STATUS_COLORS: dict[str, tuple[int, int, int]] = {
        "acting": (52, 211, 153),      # Mint green
        "thinking": (251, 191, 36),    # Amber
        "intent": (244, 114, 182),     # Pink
        "assist": (56, 189, 248),      # Sky blue
        "question": (192, 132, 252),   # Soft purple
        "error": (248, 113, 113),      # Coral red
        "complete": (167, 139, 250),   # Lavender
    }

    STATUS_LABELS: dict[str, str] = {
        "acting": "ACTING",
        "thinking": "THINKING",
        "intent": "GOAL",
        "assist": "ASSIST",
        "question": "ASKING",
        "error": "ERROR",
        "complete": "DONE",
    }

    def __init__(self) -> None:
        self.hwnd: Optional[int] = None  # HUD hwnd (legacy alias)
        self.hud_hwnd: Optional[int] = None
        self.woolbur_hwnd: Optional[int] = None
        self.prompt_hwnd: Optional[int] = None

        self._thread_started = False
        self._start_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._wake_event = threading.Event()
        self._assist_done_event = threading.Event()
        self._assist_done_event.set()
        self._prompt_done_event = threading.Event()
        self._prompt_done_event.set()

        self._wnd_procs: list[Any] = []
        self._measure_img = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
        self._measure_draw = ImageDraw.Draw(self._measure_img)

        # Agent/HUD state
        self._session_active = False
        self._current_intent = ""
        self._goal = ""
        self._looking_for = ""
        self._step_detail = ""
        self._live_action = ""
        self._last_message = ""
        self._last_status_type = "intent"
        self._mood_override = "auto"
        self._recent_steps: list[str] = []

        # User guidance & co-pilot state
        self._pending_user_events: list[dict[str, Any]] = []
        self._assist_until = 0.0
        self._assist_ack_until = 0.0
        self._assist_ack_text = ""
        self._prompt_open = False
        self._prompt_result: Optional[dict[str, Any]] = None
        self._prompt_mode = "ask"  # "ask" | "steer"
        self._prompt_question = ""
        self._prompt_context = ""
        self._prompt_options: list[str] = []
        self._prompt_allow_text = True
        self._prompt_pointer: Optional[tuple[int, int]] = None
        self._prompt_include_pointer = False
        self._prompt_text = ""
        self._prompt_cursor_idx = 0
        self._prompt_hover_id: Optional[str] = None
        self._prompt_hit_rects: list[tuple[str, tuple[int, int, int, int]]] = []
        self._prompt_prev_fg: Optional[int] = None
        self._focus_antigravity_cb: Optional[Callable[[], Any]] = None
        self._abort_cb: Optional[Callable[[str], None]] = None

        # Cached surface references for hit-testing from hooks
        self._hud_rect = (0, 0, 0, 0)
        self._woolbur_rect = (0, 0, 0, 0)
        self._prompt_rect = (0, 0, 0, 0)
        self._hud_visible = False
        self._woolbur_visible = False

        # Reversible borderless-fullscreen overlay forcing state
        try:
            init_cfg = load_config_cached()
            init_fs = bool(init_cfg.get("settings", {}).get("force_fullscreen", True))
        except Exception:
            init_fs = True
        self._force_fullscreen: bool = init_fs
        self._demoted_windows: dict[int, tuple[int, int, int, int]] = {}
        self._demote_failed_until: dict[int, float] = {}
        self._demote_last_ts: dict[int, float] = {}
        self._last_forced_info: str = ""

    @staticmethod
    def _read_live_cfg() -> dict[str, Any]:
        """Return live appearance + fullscreen config without crashing on transient disk state."""
        try:
            cfg = load_config_cached()
            app = cfg.get("appearance", {})
            settings = cfg.get("settings", {})
            return {
                "accessory_set": str(app.get("accessory_set", app.get("mood", "auto")) or "auto").strip().lower(),
                "mood": str(app.get("mood", "auto") or "auto").strip().lower(),
                "scale": float(app.get("scale", 1.0)),
                "opacity": float(app.get("opacity", 0.95)),
                "show_hud": bool(app.get("show_hud", True)),
                "show_woolbur": bool(app.get("show_woolbur", True)),
                "follow_cursor": bool(app.get("follow_cursor", True)),
                "accent_color": str(app.get("accent_color", "#38bdf8") or "#38bdf8").strip().lower(),
                "wool_color": str(app.get("wool_color", "#fffbf5") or "#fffbf5").strip().lower(),
                "horn_color": str(app.get("horn_color", "#fde68a") or "#fde68a").strip().lower(),
                "force_fullscreen": bool(settings.get("force_fullscreen", True)),
            }
        except Exception:
            return {
                "accessory_set": "auto",
                "mood": "auto",
                "scale": 1.0,
                "opacity": 0.95,
                "show_hud": True,
                "show_woolbur": True,
                "follow_cursor": True,
                "accent_color": "#38bdf8",
                "wool_color": "#fffbf5",
                "horn_color": "#fde68a",
                "force_fullscreen": True,
            }

    def _resolve_accent_rgb(self, eff_type: str, custom_accent_hex: str) -> tuple[int, int, int]:
        base = self.STATUS_COLORS.get(eff_type, self.STATUS_COLORS["intent"])
        norm = str(custom_accent_hex or "").strip().lower()
        if not norm or norm == "#38bdf8" or eff_type == "error":
            return base
        return parse_hex_rgba(norm, base, 255)[:3]

    _SKIP_FULLSCREEN_CLASSES = frozenset(
        {"Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow"}
    )

    def register_callbacks(
        self,
        focus_antigravity_cb: Optional[Callable[[], Any]] = None,
        abort_cb: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._focus_antigravity_cb = focus_antigravity_cb
        self._abort_cb = abort_cb

    def own_hwnds(self) -> set[int]:
        return {h for h in (self.hud_hwnd, self.woolbur_hwnd, self.prompt_hwnd) if h}

    def is_visible(self) -> bool:
        with self._state_lock:
            return bool(self._hud_visible or self._woolbur_visible or self._prompt_open)

    @property
    def force_fullscreen_enabled(self) -> bool:
        with self._state_lock:
            return self._force_fullscreen

    @property
    def last_forced_info(self) -> str:
        with self._state_lock:
            return self._last_forced_info

    def set_force_fullscreen(self, enabled: bool) -> None:
        with self._state_lock:
            self._force_fullscreen = bool(enabled)
        if not enabled and self._thread_started:
            self._queue.put({"op": "restore_demoted"})
            self._wake_event.set()

    def sync_fullscreen_overlay(self, timeout: float = 0.6) -> str:
        """Synchronously force borderless fullscreen compatibility & Z-order if needed."""
        if not self._thread_started or not self.force_fullscreen_enabled:
            return self.last_forced_info
        if get_user_notification_state() != QUNS_RUNNING_D3D_FULL_SCREEN:
            return self.last_forced_info
        ev = threading.Event()
        self._queue.put({"op": "force_fs", "done_ev": ev})
        self._wake_event.set()
        ev.wait(max(0.05, float(timeout)))
        return self.last_forced_info

    def is_session_active(self) -> bool:
        with self._state_lock:
            return self._session_active

    def is_prompt_open(self) -> bool:
        with self._state_lock:
            return self._prompt_open

    def is_assisting(self) -> bool:
        with self._state_lock:
            return time.monotonic() < self._assist_until

    def point_in_interactive_ui(self, px: int, py: int) -> bool:
        with self._state_lock:
            if self._prompt_open:
                l, t, r, b = self._prompt_rect
                if l <= px <= r and t <= py <= b:
                    return True
            if self._hud_visible and self._last_status_type in ("thinking", "complete", "intent", "question"):
                hl, ht, hr, hb = self._hud_rect
                wl, wt, wr, wb = self._woolbur_rect
                if (hl <= px <= hr and ht <= py <= hb) or (
                    self._woolbur_visible and wl <= px <= wr and wt <= py <= wb
                ):
                    return True
            return False

    def _ensure_started(self) -> None:
        if self._thread_started and self.hud_hwnd:
            return
        with self._start_lock:
            if self._thread_started:
                return
            self._thread_started = True
            ready_ev = threading.Event()
            th = threading.Thread(target=self._worker_loop, args=(ready_ev,), daemon=True)
            th.start()
            ready_ev.wait(2.5)

    def show(
        self,
        message: str,
        status_type: str = "intent",
        auto_hide_seconds: float = 0.0,
        clear_intent: bool = False,
        combine_intent: bool = True,
        goal: Optional[str] = None,
        looking_for: Optional[str] = None,
        step_detail: Optional[str] = None,
        mood: Optional[str] = None,
        completed_step: Optional[str] = None,
        mark_session_active: bool = True,
        force_fullscreen: Optional[bool] = None,
    ) -> str:
        self._ensure_started()
        if force_fullscreen is not None:
            self.set_force_fullscreen(force_fullscreen)
        cleaned = " ".join(str(message).split()).strip() or "Working..."
        stype = status_type if status_type in self.STATUS_COLORS else "intent"

        with self._state_lock:
            if clear_intent:
                self._current_intent = ""
                self._looking_for = ""
                self._step_detail = ""
                self._recent_steps = []
            if goal is not None:
                self._goal = " ".join(str(goal).split()).strip()
            if looking_for is not None:
                self._looking_for = " ".join(str(looking_for).split()).strip()
            if step_detail is not None:
                self._step_detail = " ".join(str(step_detail).split()).strip()
            if mood is not None:
                self._mood_override = mood

            if completed_step:
                c_step = " ".join(str(completed_step).split()).strip()
                if c_step and (not self._recent_steps or self._recent_steps[-1] != c_step):
                    self._recent_steps.append(c_step)
                    self._recent_steps = self._recent_steps[-3:]

            if stype == "intent":
                self._current_intent = cleaned
                if not self._goal:
                    self._goal = cleaned
                self._live_action = ""
                display_text = cleaned
            elif stype == "acting":
                self._live_action = cleaned
                display_text = f"{self._goal or self._current_intent}  •  {cleaned}" if (self._goal or self._current_intent) else cleaned
            elif stype == "thinking":
                self._live_action = ""
                if not self._step_detail and cleaned and cleaned != "Thinking / planning next action...":
                    self._step_detail = cleaned
                display_text = self._step_detail or self._looking_for or cleaned
            elif stype == "complete":
                self._live_action = cleaned
                self._session_active = False
                self._assist_until = 0.0
                self._assist_done_event.set()
                display_text = cleaned
            elif stype == "error":
                self._live_action = cleaned
                self._session_active = False
                self._assist_until = 0.0
                self._assist_done_event.set()
                display_text = cleaned
            else:
                display_text = cleaned

            if mark_session_active and stype in ("intent", "acting", "thinking", "assist", "question"):
                self._session_active = True

            self._last_message = display_text
            self._last_status_type = stype

        self._queue.put(
            {
                "op": "show",
                "status_type": stype,
                "auto_hide_seconds": max(0.0, float(auto_hide_seconds)),
            }
        )
        self._wake_event.set()
        return display_text

    def hide(self, clear_intent: bool = True) -> None:
        with self._state_lock:
            self._session_active = False
            self._assist_until = 0.0
            self._assist_done_event.set()
            if clear_intent:
                self._current_intent = ""
                self._goal = ""
                self._looking_for = ""
                self._step_detail = ""
                self._recent_steps = []
            self._live_action = ""
            self._last_message = ""
        if not self._thread_started:
            return
        self._queue.put({"op": "hide"})
        self._wake_event.set()

    def on_physical_assist_move(self, x: int, y: int) -> None:
        """Triggered from WH_MOUSE_LL when user deliberately moves physical mouse."""
        with self._state_lock:
            if not self._session_active or self._prompt_open:
                return
            self._assist_until = time.monotonic() + 2.8
            self._assist_done_event.clear()
        self._queue.put({"op": "assist_ping", "x": int(x), "y": int(y)})
        self._wake_event.set()

    def on_physical_assist_click(self, x: int, y: int, button: str = "left") -> None:
        """Triggered from WH_MOUSE_LL when user clicks to help the agent."""
        with self._state_lock:
            if not self._session_active or self._prompt_open:
                return
            self._assist_until = 0.0
            self._assist_done_event.set()
            self._assist_ack_text = f"Got your {button} click at ({x}, {y})!"
            self._assist_ack_until = time.monotonic() + 2.5
            self._pending_user_events.append(
                {
                    "type": "assist_click",
                    "x": int(x),
                    "y": int(y),
                    "button": button,
                    "timestamp": time.time(),
                    "looking_for": self._looking_for or self._goal or self._current_intent,
                }
            )
        self._queue.put({"op": "refresh"})
        self._wake_event.set()

    def open_steer_prompt(self, pointer: Optional[tuple[int, int]] = None) -> None:
        """Open (or toggle closed) Woolbur's mid-run steering card via Alt+W or clicking HUD."""
        self._ensure_started()
        with self._state_lock:
            if self._prompt_open:
                if self._prompt_mode == "steer":
                    should_close = True
                else:
                    return
            else:
                should_close = False
        if should_close:
            self._complete_prompt({"answered": False, "dismissed": True})
            return
        with self._state_lock:
            if self._prompt_open:
                return
            self._assist_until = 0.0
            self._assist_done_event.set()
            self._prompt_open = True
            self._prompt_done_event.clear()
            self._prompt_mode = "steer"
            self._prompt_question = "Tell Woolbur what to do or why:"
            self._prompt_context = (
                f"Looking for: {self._looking_for}"
                if self._looking_for
                else (self._step_detail or self._goal or "Active desktop task")
            )
            self._prompt_options = ["Continue as planned", "Skip current step", "Abort task"]
            self._prompt_allow_text = True
            self._prompt_pointer = pointer
            self._prompt_include_pointer = pointer is not None
            self._prompt_text = ""
            self._prompt_cursor_idx = 0
            self._prompt_hover_id = None
            self._prompt_result = None
        self._queue.put({"op": "open_prompt"})
        self._wake_event.set()

    def ask_user_prompt(
        self,
        question: str,
        options: Optional[list[str]] = None,
        allow_text: bool = True,
        context_hint: Optional[str] = None,
        timeout_seconds: float = 180.0,
    ) -> dict[str, Any]:
        """Synchronously ask the user via Woolbur's multiple-choice/text popup card."""
        self._ensure_started()
        clean_opts = [str(o).strip() for o in (options or []) if str(o).strip()][:8]
        with self._state_lock:
            self._session_active = True
            self._assist_until = 0.0
            self._assist_done_event.set()
            self._prompt_open = True
            self._prompt_done_event.clear()
            self._prompt_mode = "ask"
            self._prompt_question = " ".join(str(question).split()).strip() or "How should Woolbur proceed?"
            self._prompt_context = " ".join(str(context_hint or "").split()).strip()
            self._prompt_options = clean_opts
            self._prompt_allow_text = bool(allow_text)
            self._prompt_pointer = None
            self._prompt_include_pointer = False
            self._prompt_text = ""
            self._prompt_cursor_idx = 0
            self._prompt_hover_id = None
            self._prompt_result = None
            self._last_status_type = "question"

        self._queue.put({"op": "open_prompt"})
        self._wake_event.set()

        wait_cap = max(5.0, float(timeout_seconds))
        finished = self._prompt_done_event.wait(wait_cap)
        with self._state_lock:
            if not finished and self._prompt_open:
                self._prompt_open = False
                self._prompt_done_event.set()
                self._queue.put({"op": "close_prompt"})
                self._wake_event.set()
                return {
                    "answered": False,
                    "timed_out": True,
                    "question": self._prompt_question,
                }
            res = dict(self._prompt_result or {"answered": False, "dismissed": True})
            self._last_status_type = "thinking"
        self._queue.put({"op": "refresh"})
        self._wake_event.set()
        return res

    def wait_for_cooperation(self, abort_check: Optional[Callable[[], None]] = None) -> list[dict[str, Any]]:
        """Pause before an automated step while user is actively steering or moving mouse to assist."""
        while True:
            if abort_check is not None:
                abort_check()
            with self._state_lock:
                prompt_open = self._prompt_open
                assist_rem = self._assist_until - time.monotonic()
                session_active = self._session_active
            if not session_active:
                break
            if prompt_open:
                self._prompt_done_event.wait(0.05)
                continue
            if assist_rem > 0:
                self._assist_done_event.wait(min(0.08, assist_rem))
                continue
            break
        return self.pop_pending_user_events()

    def pop_pending_user_events(self) -> list[dict[str, Any]]:
        with self._state_lock:
            if not self._pending_user_events:
                return []
            events = list(self._pending_user_events)
            self._pending_user_events.clear()
            return events

    @contextlib.contextmanager
    def suspend_for_capture(self):
        """Synchronously hide all overlay windows during screen capture and restore immediately after."""
        if not self._thread_started or not self.hud_hwnd:
            yield
            return
        hide_ev = threading.Event()
        self._queue.put({"op": "temp_hide", "done_ev": hide_ev})
        self._wake_event.set()
        hide_ev.wait(0.18)
        try:
            yield
        finally:
            restore_ev = threading.Event()
            self._queue.put({"op": "temp_restore", "done_ev": restore_ev})
            self._wake_event.set()
            restore_ev.wait(0.18)

    def _render_hud_bgra(
        self,
        scale: float,
        mon_w: int,
        custom_accent_hex: str = "#38bdf8",
    ) -> tuple[int, int, bytes]:
        s = 2
        sc = s * scale
        with self._state_lock:
            stype = self._last_status_type
            goal = self._goal or self._current_intent or "Weightless Windows Tester"
            looking_for = self._looking_for
            step_detail = self._step_detail
            raw_msg = self._last_message
            recent_steps = list(self._recent_steps)
            assisting = time.monotonic() < self._assist_until

        if assisting and stype in ("thinking", "intent", "acting"):
            eff_type = "assist"
        else:
            eff_type = stype
        accent = self._resolve_accent_rgb(eff_type, custom_accent_hex)
        badge_text = self.STATUS_LABELS.get(eff_type, "STATUS")

        target_w_px = min(int(round(620 * scale)), max(360, mon_w - int(round(40 * scale))))
        w_hi = target_w_px * s

        badge_font = _load_font(int(round(10.5 * sc)), ("seguisb.ttf", "segoeui.ttf", "arial.ttf"))
        goal_font = _load_font(int(round(14.5 * sc)), ("seguisb.ttf", "segoeui.ttf", "arial.ttf"))
        body_font = _load_font(int(round(13.0 * sc)), ("segoeui.ttf", "arial.ttf"))
        hint_font = _load_font(int(round(10.5 * sc)), ("segoeui.ttf", "arial.ttf"))
        trail_font = _load_font(int(round(11.5 * sc)), ("segoeui.ttf", "arial.ttf"))

        # Reserve left perch margin so Woolbur hopping on/off doesn't shift HUD text
        left_pad = int(round(76 * sc))
        right_pad = int(round(18 * sc))
        top_pad = int(round(14 * sc))
        bottom_pad = int(round(14 * sc))

        hints_text = "Alt+W Steer  •  3x Esc Abort"
        hints_bb = self._measure_draw.textbbox((0, 0), hints_text, font=hint_font)
        hints_w = hints_bb[2] - hints_bb[0]

        badge_bb = self._measure_draw.textbbox((0, 0), badge_text, font=badge_font)
        badge_w = (badge_bb[2] - badge_bb[0]) + int(round(16 * sc))
        badge_h = int(round(20 * sc))

        goal_x = left_pad + badge_w + int(round(10 * sc))
        goal_max_w = max(int(round(120 * sc)), w_hi - goal_x - hints_w - right_pad - int(round(12 * sc)))
        goal_lines = _wrap_text(self._measure_draw, goal, goal_font, goal_max_w, 2) or ["Active Task"]

        body_max_w = max(int(round(160 * sc)), w_hi - left_pad - right_pad)
        body_lines: list[tuple[str, tuple[int, int, int, int]]] = []
        if assisting:
            lf = looking_for or goal
            for idx, ln in enumerate(_wrap_text(self._measure_draw, f"Looking for: {lf}", body_font, body_max_w, 2)):
                body_lines.append((ln, (125, 211, 252, 255) if idx == 0 else (224, 242, 254, 255)))
            body_lines.append(("Assist paused — click to help or Alt+W to point & explain", (190, 210, 232, 220)))
        else:
            if looking_for:
                for idx, ln in enumerate(_wrap_text(self._measure_draw, f"Looking for: {looking_for}", body_font, body_max_w, 2)):
                    body_lines.append((ln, (251, 207, 232, 255) if idx == 0 else (238, 228, 246, 245)))
            detail_src = step_detail or (
                raw_msg if (raw_msg and raw_msg != goal and eff_type in ("thinking", "complete", "error", "question")) else ""
            )
            if detail_src:
                for ln in _wrap_text(self._measure_draw, detail_src, body_font, body_max_w, 3):
                    body_lines.append((ln, (226, 222, 240, 245)))
            if not body_lines:
                fallback = (
                    "Preparing next action..."
                    if eff_type == "thinking"
                    else (raw_msg or "Ready")
                )
                for ln in _wrap_text(self._measure_draw, fallback, body_font, body_max_w, 2):
                    body_lines.append((ln, (226, 222, 240, 245)))

        trail_lines: list[str] = []
        trail_text_pad = int(round(16 * sc))
        for st in recent_steps[-3:]:
            wrapped = _wrap_text(self._measure_draw, st, trail_font, max(60, body_max_w - trail_text_pad), 1)
            if wrapped:
                trail_lines.append(wrapped[0])

        goal_lh = int(round(19 * sc))
        body_lh = int(round(18 * sc))
        trail_lh = int(round(16 * sc))

        header_h = max(badge_h, len(goal_lines) * goal_lh)
        body_h = len(body_lines) * body_lh
        trail_block_h = (int(round(10 * sc)) + len(trail_lines) * trail_lh) if trail_lines else 0
        h_hi = top_pad + header_h + int(round(8 * sc)) + body_h + trail_block_h + bottom_pad
        h_logical = max(int(round(78 * scale)), int(math.ceil(h_hi / s)))
        h_hi = h_logical * s

        img = Image.new("RGBA", (w_hi, h_hi), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        rad = int(round(18 * sc))
        # Outer aura + dark glass card
        draw.rounded_rectangle(
            [1 * s, 1 * s, w_hi - 1 * s - 1, h_hi - 1 * s - 1],
            radius=rad + 1 * s,
            outline=(*accent, 80),
            width=2 * s,
        )
        draw.rounded_rectangle(
            [2 * s, 2 * s, w_hi - 2 * s - 1, h_hi - 2 * s - 1],
            radius=rad,
            fill=(14, 11, 22, 242),
            outline=(*accent, 225),
            width=2 * s,
        )
        draw.line([(rad, 4 * s), (w_hi - rad, 4 * s)], fill=(255, 225, 245, 36), width=s)

        # Status badge pill
        by = top_pad + (goal_lh - badge_h) // 2
        draw.rounded_rectangle(
            [left_pad, by, left_pad + badge_w, by + badge_h],
            radius=badge_h // 2,
            fill=(*accent, 44),
            outline=(*accent, 200),
            width=max(1, s),
        )
        btx = left_pad + int(round(8 * sc))
        bty = by + (badge_h - (badge_bb[3] - badge_bb[1])) // 2 - badge_bb[1]
        draw.text((btx, bty), badge_text, font=badge_font, fill=(*accent, 255))

        # Goal lines
        gy = top_pad
        for ln in goal_lines:
            draw.text((goal_x, gy), ln, font=goal_font, fill=(250, 248, 255, 255))
            gy += goal_lh

        # Right-side hotkey hint
        hx = w_hi - right_pad - hints_w
        hy = top_pad + int(round(2 * sc))
        draw.text((hx, hy), hints_text, font=hint_font, fill=(195, 175, 208, 195))

        # Body wrapped lines
        cy = top_pad + header_h + int(round(8 * sc))
        for ln, col in body_lines:
            draw.text((left_pad, cy), ln, font=body_font, fill=col)
            cy += body_lh

        # Recent steps divider + trail
        if trail_lines:
            cy += int(round(4 * sc))
            draw.line([(left_pad, cy), (w_hi - right_pad, cy)], fill=(*accent, 60), width=max(1, s))
            cy += int(round(6 * sc))
            for tln in trail_lines:
                # Crisp vector checkmark (avoids Segoe UI missing-glyph tofu boxes)
                ck_y = cy + int(round(8 * sc))
                draw.line(
                    [
                        (left_pad + int(round(1 * sc)), ck_y),
                        (left_pad + int(round(4.5 * sc)), ck_y + int(round(3.5 * sc))),
                        (left_pad + int(round(10 * sc)), ck_y - int(round(3.5 * sc))),
                    ],
                    fill=(52, 211, 153, 235),
                    width=max(2, int(round(1.8 * sc))),
                    joint="curve",
                )
                draw.text((left_pad + trail_text_pad, cy), tln, font=trail_font, fill=(180, 168, 204, 215))
                cy += trail_lh

        final_img = img.resize((target_w_px, h_logical), Image.Resampling.LANCZOS)
        return _pil_to_bgra(final_img)

    def _render_woolbur_bubble_bgra(
        self,
        scale: float,
        mood: str,
        anim_frame: int,
        facing: int,
        speech_title: str,
        speech_sub: str,
        accent: tuple[int, int, int],
        bubble_on_right: bool,
        wool_color: str = "#fffbf5",
        horn_color: str = "#fde68a",
        accessory_set: Optional[str] = None,
    ) -> tuple[int, int, int, int, bytes]:
        """Render Woolbur sprite + optional speech bubble; returns (w, h, anchor_x, anchor_y, bgra)."""
        sprite_sz = int(round(92 * scale))
        sprite = render_woolbur_frame(
            sprite_sz,
            mood=mood,
            anim_frame=anim_frame,
            facing=facing,
            wool_color=wool_color,
            horn_color=horn_color,
            accessory_set=accessory_set,
        )

        if not speech_title:
            w, h, bgra = _pil_to_bgra(sprite)
            return w, h, w // 2, h // 2, bgra

        s = 2
        sc = s * scale
        title_font = _load_font(int(round(12.5 * sc)), ("seguisb.ttf", "segoeui.ttf", "arial.ttf"))
        sub_font = _load_font(int(round(10.5 * sc)), ("segoeui.ttf", "arial.ttf"))

        max_bub_w_hi = int(round(290 * sc))
        pad_x = int(round(14 * sc))
        pad_y = int(round(10 * sc))
        text_max_w = max_bub_w_hi - 2 * pad_x

        t_lines = _wrap_text(self._measure_draw, speech_title, title_font, text_max_w, 2)
        s_lines = _wrap_text(self._measure_draw, speech_sub, sub_font, text_max_w, 2) if speech_sub else []

        t_lh = int(round(16 * sc))
        s_lh = int(round(14 * sc))
        max_line_w = 0
        for ln in t_lines:
            bb = self._measure_draw.textbbox((0, 0), ln, font=title_font)
            max_line_w = max(max_line_w, bb[2] - bb[0])
        for ln in s_lines:
            bb = self._measure_draw.textbbox((0, 0), ln, font=sub_font)
            max_line_w = max(max_line_w, bb[2] - bb[0])

        bub_w_hi = max(int(round(120 * sc)), min(max_bub_w_hi, max_line_w + 2 * pad_x))
        bub_h_hi = pad_y * 2 + len(t_lines) * t_lh + (int(round(3 * sc)) + len(s_lines) * s_lh if s_lines else 0)
        bub_w = int(math.ceil(bub_w_hi / s))
        bub_h = int(math.ceil(bub_h_hi / s))
        bub_w_hi = bub_w * s
        bub_h_hi = bub_h * s

        tail_gap = int(round(10 * scale))
        bub_img = Image.new("RGBA", (bub_w_hi + tail_gap * s, bub_h_hi), (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(bub_img)
        bx0 = tail_gap * s if bubble_on_right else 0
        bx1 = bx0 + bub_w_hi - 1
        rad = int(round(14 * sc))
        bdraw.rounded_rectangle(
            [bx0 + s, s, bx1 - s, bub_h_hi - s - 1],
            radius=rad,
            fill=(15, 12, 24, 244),
            outline=(*accent, 235),
            width=2 * s,
        )
        # Speech tail pointing to Woolbur
        cy_tail = bub_h_hi // 2
        if bubble_on_right:
            tail_pts = [(bx0 + 2 * s, cy_tail - 6 * s), (2 * s, cy_tail), (bx0 + 2 * s, cy_tail + 6 * s)]
            bdraw.polygon(tail_pts, fill=(15, 12, 24, 244))
            bdraw.line([tail_pts[0], tail_pts[1], tail_pts[2]], fill=(*accent, 235), width=2 * s)
        else:
            tail_pts = [(bx1 - 2 * s, cy_tail - 6 * s), (bub_w_hi + tail_gap * s - 2 * s, cy_tail), (bx1 - 2 * s, cy_tail + 6 * s)]
            bdraw.polygon(tail_pts, fill=(15, 12, 24, 244))
            bdraw.line([tail_pts[0], tail_pts[1], tail_pts[2]], fill=(*accent, 235), width=2 * s)

        ty = pad_y
        for ln in t_lines:
            bdraw.text((bx0 + pad_x, ty), ln, font=title_font, fill=(249, 247, 255, 255))
            ty += t_lh
        if s_lines:
            ty += int(round(3 * sc))
            for ln in s_lines:
                bdraw.text((bx0 + pad_x, ty), ln, font=sub_font, fill=(205, 185, 218, 220))
                ty += s_lh

        bub_small = bub_img.resize((bub_w + tail_gap, bub_h), Image.Resampling.LANCZOS)
        total_w = sprite_sz + bub_small.width
        total_h = max(sprite_sz, bub_small.height)
        canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
        sp_y = (total_h - sprite_sz) // 2
        bub_y = (total_h - bub_small.height) // 2
        if bubble_on_right:
            canvas.alpha_composite(sprite, (0, sp_y))
            canvas.alpha_composite(bub_small, (sprite_sz - int(round(6 * scale)), bub_y))
            anchor_x = sprite_sz // 2
        else:
            canvas.alpha_composite(bub_small, (0, bub_y))
            canvas.alpha_composite(sprite, (bub_small.width - int(round(6 * scale)), sp_y))
            anchor_x = bub_small.width - int(round(6 * scale)) + sprite_sz // 2
        w, h, bgra = _pil_to_bgra(canvas)
        return w, h, anchor_x, total_h // 2, bgra

    def _render_prompt_bgra(
        self,
        scale: float,
        mon_w: int,
        custom_accent_hex: str = "#38bdf8",
        wool_color: str = "#fffbf5",
        horn_color: str = "#fde68a",
        accessory_set: str = "auto",
    ) -> tuple[int, int, bytes]:
        s = 2
        sc = s * scale
        with self._state_lock:
            mode = self._prompt_mode
            question = self._prompt_question
            context = self._prompt_context
            options = list(self._prompt_options)
            allow_text = self._prompt_allow_text
            pointer = self._prompt_pointer
            include_ptr = self._prompt_include_pointer
            text_val = self._prompt_text
            hover_id = self._prompt_hover_id

        card_w = min(int(round(520 * scale)), max(340, mon_w - int(round(40 * scale))))
        w_hi = card_w * s

        badge_font = _load_font(int(round(10.5 * sc)), ("seguisb.ttf", "segoeui.ttf", "arial.ttf"))
        q_font = _load_font(int(round(14.0 * sc)), ("seguisb.ttf", "segoeui.ttf", "arial.ttf"))
        ctx_font = _load_font(int(round(11.5 * sc)), ("segoeui.ttf", "arial.ttf"))
        opt_font = _load_font(int(round(12.5 * sc)), ("seguisb.ttf", "segoeui.ttf", "arial.ttf"))

        pad = int(round(18 * sc))
        inner_w = w_hi - 2 * pad
        accent = self._resolve_accent_rgb("question" if mode == "ask" else "assist", custom_accent_hex)

        q_lines = _wrap_text(self._measure_draw, question, q_font, inner_w - int(round(62 * sc)), 3)
        ctx_lines = _wrap_text(self._measure_draw, context, ctx_font, inner_w, 2) if context else []

        portrait_sz = int(round(52 * sc))
        q_lh = int(round(19 * sc))
        ctx_lh = int(round(16 * sc))
        opt_h = int(round(34 * sc))
        opt_gap = int(round(8 * sc))
        input_h = int(round(38 * sc))
        btn_h = int(round(32 * sc))

        header_h = max(portrait_sz, int(round(22 * sc)) + len(q_lines) * q_lh)
        ctx_h = (int(round(6 * sc)) + len(ctx_lines) * ctx_lh) if ctx_lines else 0
        ptr_h = int(round(32 * sc)) if pointer is not None else 0
        opts_h = (int(round(8 * sc)) + len(options) * (opt_h + opt_gap)) if options else 0
        input_block_h = (int(round(6 * sc)) + input_h) if allow_text else 0
        actions_h = int(round(12 * sc)) + btn_h

        h_hi = pad + header_h + ctx_h + ptr_h + opts_h + input_block_h + actions_h + pad
        h_logical = max(140, int(math.ceil(h_hi / s)))
        h_hi = h_logical * s

        img = Image.new("RGBA", (w_hi, h_hi), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        rad = int(round(18 * sc))
        draw.rounded_rectangle(
            [1 * s, 1 * s, w_hi - 1 * s - 1, h_hi - 1 * s - 1],
            radius=rad + s,
            outline=(*accent, 95),
            width=2 * s,
        )
        draw.rounded_rectangle(
            [2 * s, 2 * s, w_hi - 2 * s - 1, h_hi - 2 * s - 1],
            radius=rad,
            fill=(16, 12, 26, 248),
            outline=(*accent, 235),
            width=2 * s,
        )

        # Mini Woolbur portrait
        _, prompt_acc = resolve_appearance(
            "question" if mode == "ask" else "thinking",
            accessory_set=accessory_set,
        )
        mini_sp = render_woolbur_frame(
            portrait_sz,
            mood="normal",
            anim_frame=0,
            wool_color=wool_color,
            horn_color=horn_color,
            accessory_set=prompt_acc,
        )
        img.alpha_composite(mini_sp, (pad, pad))

        tx = pad + portrait_sz + int(round(10 * sc))
        badge_lbl = "WOOLBUR ASKS" if mode == "ask" else "STEER WOOLBUR"
        draw.text((tx, pad), badge_lbl, font=badge_font, fill=(*accent, 255))
        qy = pad + int(round(18 * sc))
        for ln in q_lines:
            draw.text((tx, qy), ln, font=q_font, fill=(250, 248, 255, 255))
            qy += q_lh

        cy = pad + header_h
        if ctx_lines:
            cy += int(round(6 * sc))
            for ln in ctx_lines:
                draw.text((pad, cy), ln, font=ctx_font, fill=(195, 182, 214, 225))
                cy += ctx_lh

        hit_rects: list[tuple[str, tuple[int, int, int, int]]] = []

        # Pointer inclusion toggle when Alt+W was pressed
        if pointer is not None:
            cy += int(round(6 * sc))
            px0, py0, px1, py1 = pad, cy, w_hi - pad, cy + int(round(24 * sc))
            pfill = (56, 189, 248, 45) if include_ptr else (40, 32, 58, 120)
            pout = (56, 189, 248, 200) if include_ptr else (120, 105, 145, 140)
            draw.rounded_rectangle([px0, py0, px1, py1], radius=int(round(8 * sc)), fill=pfill, outline=pout, width=s)
            bx_cb = px0 + int(round(10 * sc))
            by_cb = py0 + int(round(5 * sc))
            cb_sz = int(round(13 * sc))
            draw.rounded_rectangle(
                [bx_cb, by_cb, bx_cb + cb_sz, by_cb + cb_sz],
                radius=int(round(3 * sc)),
                fill=(56, 189, 248, 200) if include_ptr else (20, 16, 32, 220),
                outline=(186, 230, 253, 240) if include_ptr else (160, 145, 185, 180),
                width=max(1, s),
            )
            if include_ptr:
                draw.line(
                    [
                        (bx_cb + int(round(3 * sc)), by_cb + int(round(6.5 * sc))),
                        (bx_cb + int(round(5.5 * sc)), by_cb + int(round(9.5 * sc))),
                        (bx_cb + int(round(10.5 * sc)), by_cb + int(round(3.5 * sc))),
                    ],
                    fill=(14, 11, 22, 255),
                    width=max(2, int(round(2 * sc))),
                    joint="curve",
                )
            draw.text(
                (bx_cb + cb_sz + int(round(8 * sc)), py0 + int(round(4 * sc))),
                f"Include pointed coordinate ({pointer[0]}, {pointer[1]})",
                font=ctx_font,
                fill=(210, 238, 255, 245),
            )
            hit_rects.append(("toggle_pointer", (px0 // s, py0 // s, px1 // s, py1 // s)))
            cy += int(round(26 * sc))

        # Multiple-choice option buttons
        if options:
            cy += int(round(8 * sc))
        for idx, opt in enumerate(options):
            oid = f"opt:{idx}"
            is_hov = hover_id == oid
            ox0, oy0, ox1, oy1 = pad, cy, w_hi - pad, cy + opt_h
            ofill = (*accent, 75) if is_hov else (32, 25, 48, 225)
            oout = (*accent, 245) if is_hov else (140, 118, 175, 150)
            draw.rounded_rectangle([ox0, oy0, ox1, oy1], radius=int(round(10 * sc)), fill=ofill, outline=oout, width=max(1, s))
            lbl = _wrap_text(self._measure_draw, f"[{idx + 1}]  {opt}", opt_font, inner_w - int(round(20 * sc)), 1)
            if lbl:
                bb = draw.textbbox((0, 0), lbl[0], font=opt_font)
                ty_opt = oy0 + (opt_h - (bb[3] - bb[1])) // 2 - bb[1]
                draw.text((ox0 + int(round(12 * sc)), ty_opt), lbl[0], font=opt_font, fill=(248, 246, 255, 255))
            hit_rects.append((oid, (ox0 // s, oy0 // s, ox1 // s, oy1 // s)))
            cy += opt_h + opt_gap

        # Text input box
        if allow_text:
            cy += int(round(6 * sc))
            ix0, iy0, ix1, iy1 = pad, cy, w_hi - pad, cy + input_h
            draw.rounded_rectangle(
                [ix0, iy0, ix1, iy1],
                radius=int(round(10 * sc)),
                fill=(10, 8, 18, 245),
                outline=(244, 114, 182, 220),
                width=s,
            )
            show_txt = text_val if text_val else "Type your reply and press Enter..."
            txt_col = (250, 248, 255, 255) if text_val else (150, 138, 172, 185)
            # Keep tail visible if text exceeds box width
            disp = show_txt
            max_tw = inner_w - int(round(24 * sc))
            if text_val:
                while len(disp) > 1 and (draw.textbbox((0, 0), disp, font=opt_font)[2] - draw.textbbox((0, 0), disp, font=opt_font)[0]) > max_tw:
                    disp = disp[1:]
            bb = draw.textbbox((0, 0), disp, font=opt_font)
            ty_in = iy0 + (input_h - (bb[3] - bb[1])) // 2 - bb[1]
            draw.text((ix0 + int(round(12 * sc)), ty_in), disp, font=opt_font, fill=txt_col)
            # Blinking caret
            if int(time.monotonic() * 2) % 2 == 0:
                tw_cur = (draw.textbbox((0, 0), disp, font=opt_font)[2] - draw.textbbox((0, 0), disp, font=opt_font)[0]) if text_val else 0
                cx_cur = ix0 + int(round(12 * sc)) + tw_cur + int(round(2 * sc))
                draw.line([(cx_cur, iy0 + int(round(8 * sc))), (cx_cur, iy1 - int(round(8 * sc)))], fill=(244, 114, 182, 255), width=max(1, s))
            hit_rects.append(("input_box", (ix0 // s, iy0 // s, ix1 // s, iy1 // s)))
            cy += input_h

        # Bottom action buttons
        cy += int(round(12 * sc))
        btn_y0, btn_y1 = cy, cy + btn_h
        btns: list[tuple[str, str, tuple[int, int, int]]] = [("send", "Send (Enter)", (244, 114, 182))]
        if mode == "ask":
            btns.append(("chat", "Answer in Chat", (167, 139, 250)))
        btns.append(("cancel", "Cancel (Esc)", (148, 163, 184)))

        gap_btn = int(round(10 * sc))
        each_w = (inner_w - gap_btn * (len(btns) - 1)) // len(btns)
        bx = pad
        for bid, blbl, bcol in btns:
            is_hov = hover_id == bid
            bfill = (*bcol, 85 if is_hov else 42)
            bout = (*bcol, 245 if is_hov else 190)
            draw.rounded_rectangle([bx, btn_y0, bx + each_w, btn_y1], radius=int(round(9 * sc)), fill=bfill, outline=bout, width=max(1, s))
            bb = draw.textbbox((0, 0), blbl, font=ctx_font)
            btx = bx + (each_w - (bb[2] - bb[0])) // 2 - bb[0]
            bty = btn_y0 + (btn_h - (bb[3] - bb[1])) // 2 - bb[1]
            draw.text((btx, bty), blbl, font=ctx_font, fill=(248, 246, 255, 255))
            hit_rects.append((bid, (bx // s, btn_y0 // s, (bx + each_w) // s, btn_y1 // s)))
            bx += each_w + gap_btn

        with self._state_lock:
            self._prompt_hit_rects = hit_rects

        final_img = img.resize((card_w, h_logical), Image.Resampling.LANCZOS)
        w, h, bgra = _pil_to_bgra(final_img)
        return w, h, bgra

    def _complete_prompt(self, result: dict[str, Any]) -> None:
        trigger_abort_reason: Optional[str] = None
        with self._state_lock:
            mode = self._prompt_mode
            self._prompt_open = False
            self._prompt_result = result
            prev_fg = self._prompt_prev_fg
            self._prompt_prev_fg = None
            if mode == "steer" and result.get("answered"):
                txt = str(result.get("text_response") or result.get("selected_option") or "").strip()
                if txt == "Abort task":
                    self._pending_user_events.append({"type": "abort_requested", "message": "User requested abort via Woolbur"})
                    trigger_abort_reason = "User clicked Abort in Woolbur steering box"
                elif txt and txt != "Continue as planned":
                    ev: dict[str, Any] = {
                        "type": "user_steering",
                        "message": txt,
                        "timestamp": time.time(),
                    }
                    if result.get("pointer"):
                        ev["pointer"] = result["pointer"]
                    self._pending_user_events.append(ev)
                    self._assist_ack_text = "Got your note! Adjusting..."
                    self._assist_ack_until = time.monotonic() + 2.5
            self._prompt_done_event.set()
        self._queue.put({"op": "close_prompt", "restore_fg": prev_fg})
        self._wake_event.set()
        if trigger_abort_reason is not None and self._abort_cb is not None:
            self._abort_cb(trigger_abort_reason)

    def _handle_prompt_click(self, lx: int, ly: int) -> None:
        with self._state_lock:
            hits = list(self._prompt_hit_rects)
            options = list(self._prompt_options)
            text_val = self._prompt_text
            include_ptr = self._prompt_include_pointer
            ptr = self._prompt_pointer

        clicked_id: Optional[str] = None
        for hid, (x0, y0, x1, y1) in hits:
            if x0 <= lx <= x1 and y0 <= ly <= y1:
                clicked_id = hid
                break
        if not clicked_id:
            return

        if clicked_id == "toggle_pointer":
            with self._state_lock:
                self._prompt_include_pointer = not self._prompt_include_pointer
            self._queue.put({"op": "redraw_prompt"})
            self._wake_event.set()
            return
        if clicked_id == "input_box":
            return
        if clicked_id == "cancel":
            self._complete_prompt({"answered": False, "dismissed": True})
            return
        if clicked_id == "chat":
            self._complete_prompt({"answered": False, "redirect_to_chat": True})
            if self._focus_antigravity_cb is not None:
                threading.Thread(target=self._focus_antigravity_cb, daemon=True).start()
            return
        if clicked_id == "send":
            self._complete_prompt(
                {
                    "answered": True,
                    "selected_option": None,
                    "text_response": text_val.strip(),
                    "pointer": list(ptr) if (include_ptr and ptr) else None,
                }
            )
            return
        if clicked_id.startswith("opt:"):
            idx = int(clicked_id.split(":", 1)[1])
            chosen = options[idx] if 0 <= idx < len(options) else ""
            self._complete_prompt(
                {
                    "answered": True,
                    "selected_index": idx,
                    "selected_option": chosen,
                    "text_response": text_val.strip() or None,
                    "pointer": list(ptr) if (include_ptr and ptr) else None,
                }
            )

    def _has_visible_sibling_above(self, hwnd: int, own_hwnds: set[int]) -> bool:
        """Check if any visible non-cloaked sibling window overlaps or sits above hwnd."""
        return _is_hwnd_occluded(hwnd, own_hwnds)

    def _reassert_topmost_stack(self, surfaces: list[_LayeredSurface], force: bool = False) -> None:
        own = self.own_hwnds()
        visible_surfs = [s for s in surfaces if s.visible and not s.temp_hidden]
        if not visible_surfs:
            return
        if not force and not any(self._has_visible_sibling_above(s.hwnd, own) for s in visible_surfs):
            return
        for s in visible_surfs:
            user32.SetWindowPos(
                s.hwnd,
                wintypes.HWND(HWND_NOTOPMOST),
                0,
                0,
                0,
                0,
                SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE,
            )
            user32.SetWindowPos(
                s.hwnd,
                wintypes.HWND(HWND_TOPMOST),
                0,
                0,
                0,
                0,
                SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
            )

    def _restore_demoted_windows(self) -> None:
        """Restore any borderless fullscreen windows back to their original bounds."""
        if not self._demoted_windows:
            return
        flags = SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS
        for hwnd_int, (ox, oy, ow, oh) in list(self._demoted_windows.items()):
            hw = wintypes.HWND(hwnd_int)
            if user32.IsWindow(hw):
                try:
                    user32.SetWindowPos(hw, 0, ox, oy, ow, oh, flags)
                except Exception:
                    pass
        self._demoted_windows.clear()
        self._demote_last_ts.clear()
        with self._state_lock:
            self._last_forced_info = ""

    def _maybe_demote_fullscreen(self, surfaces: list[_LayeredSurface], require_visible: bool = True) -> None:
        """Nudge an undecorated borderless fullscreen window by 1px to exit DirectFlip/exclusive scanout."""
        if not self.force_fullscreen_enabled:
            return
        if require_visible and not any(s.visible and not s.temp_hidden for s in surfaces):
            return
        fg = user32.GetForegroundWindow()
        if not fg:
            return
        fg_int = int(fg)
        if fg_int in self.own_hwnds():
            return
        quns_before = get_user_notification_state()
        if quns_before != QUNS_RUNNING_D3D_FULL_SCREEN:
            return

        now = time.monotonic()
        if now < self._demote_failed_until.get(fg_int, 0.0):
            return
        if not user32.IsWindowVisible(fg) or user32.IsIconic(fg):
            return
        if _get_window_class_name(fg_int) in self._SKIP_FULLSCREEN_CLASSES:
            return

        style = int(user32.GetWindowLongW(fg, GWL_STYLE))
        if style & (WS_CAPTION | WS_THICKFRAME):
            return

        rc = _RECT()
        if not user32.GetWindowRect(fg, ctypes.byref(rc)):
            return
        ml, mt, mw, mh, _ = _monitor_info_from_hmonitor(
            user32.MonitorFromWindow(fg, MONITOR_DEFAULTTONEAREST)
        )
        if not (rc.left <= ml and rc.top <= mt and rc.right >= ml + mw and rc.bottom >= mt + mh):
            return

        # Guard against thrashing if an app actively snaps back within 1.5s
        if fg_int in self._demoted_windows and (now - self._demote_last_ts.get(fg_int, 0.0)) < 1.5:
            self._demote_failed_until[fg_int] = now + 5.0
            with self._state_lock:
                self._last_forced_info = ""
            logger.warning(
                "Borderless fullscreen hwnd=%d snapped back to quns=3; pausing nudge retries for 5s.",
                fg_int,
            )
            return

        orig = self._demoted_windows.setdefault(
            fg_int,
            (int(rc.left), int(rc.top), int(rc.right - rc.left), int(rc.bottom - rc.top)),
        )
        flags = SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS
        msg = wintypes.MSG()
        nudged_dims: Optional[tuple[int, int, int]] = None
        for cw, ch in ((mw - 1, mh), (mw, mh - 1)):
            if cw <= 100 or ch <= 100:
                continue
            if not user32.SetWindowPos(fg, 0, ml, mt, cw, ch, flags):
                continue
            deadline = time.monotonic() + 0.08
            while time.monotonic() < deadline:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.015)
            rc_now = _RECT()
            user32.GetWindowRect(fg, ctypes.byref(rc_now))
            quns_now = get_user_notification_state()
            if quns_now != QUNS_RUNNING_D3D_FULL_SCREEN:
                nudged_dims = (
                    int(rc_now.right - rc_now.left),
                    int(rc_now.bottom - rc_now.top),
                    quns_now,
                )
                break

        if nudged_dims is not None:
            nw, nh, quns_now = nudged_dims
            self._demote_last_ts[fg_int] = time.monotonic()
            info = (
                f"Nudged borderless fullscreen window (hwnd={fg_int}) to {nw}x{nh} "
                f"(quns {quns_before}->{quns_now})"
            )
            with self._state_lock:
                self._last_forced_info = info
            logger.info(info)
            self._reassert_topmost_stack(surfaces, force=True)
            return

        # Failed (e.g., true exclusive fullscreen); restore cleanly and enter cooldown
        try:
            user32.SetWindowPos(fg, 0, orig[0], orig[1], orig[2], orig[3], flags)
        except Exception:
            pass
        self._demoted_windows.pop(fg_int, None)
        self._demote_failed_until[fg_int] = time.monotonic() + 5.0
        with self._state_lock:
            self._last_forced_info = ""
        logger.warning(
            "Fullscreen hwnd=%d stayed quns=3 after borderless 1px nudge (likely Exclusive Fullscreen).",
            fg_int,
        )

    def _worker_loop(self, ready_ev: threading.Event) -> None:
        hinst = kernel32.GetModuleHandleW(None)

        def _hud_proc(hwnd, msg, wparam, lparam):
            if msg == WM_LBUTTONUP:
                with self._state_lock:
                    can_click = not self._prompt_open and self._last_status_type in (
                        "thinking",
                        "complete",
                        "intent",
                        "question",
                    )
                if can_click:
                    self.open_steer_prompt(pointer=None)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        def _prompt_proc(hwnd, msg, wparam, lparam):
            if msg == WM_MOUSEMOVE:
                lx = ctypes.c_short(lparam & 0xFFFF).value
                ly = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                with self._state_lock:
                    hits = list(self._prompt_hit_rects)
                    old_hov = self._prompt_hover_id
                new_hov = None
                for hid, (x0, y0, x1, y1) in hits:
                    if x0 <= lx <= x1 and y0 <= ly <= y1:
                        new_hov = hid
                        break
                if new_hov != old_hov:
                    with self._state_lock:
                        self._prompt_hover_id = new_hov
                    self._queue.put({"op": "redraw_prompt"})
                    self._wake_event.set()
                return 0
            if msg == WM_LBUTTONUP:
                lx = ctypes.c_short(lparam & 0xFFFF).value
                ly = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                self._handle_prompt_click(lx, ly)
                return 0
            if msg == WM_CHAR:
                ch_code = int(wparam)
                ctrl = (user32.GetKeyState(VK_CONTROL) & 0x8000) != 0
                alt = (user32.GetKeyState(VK_MENU) & 0x8000) != 0
                if ch_code >= 32 and ch_code != 127 and not ctrl and not alt:
                    with self._state_lock:
                        if self._prompt_allow_text and len(self._prompt_text) < 500:
                            self._prompt_text += chr(ch_code)
                    self._queue.put({"op": "redraw_prompt"})
                    self._wake_event.set()
                return 0
            if msg == WM_KEYDOWN:
                vk = int(wparam)
                ctrl = (user32.GetKeyState(VK_CONTROL) & 0x8000) != 0
                if vk == VK_ESCAPE:
                    self._complete_prompt({"answered": False, "dismissed": True})
                    return 0
                if vk == VK_RETURN:
                    with self._state_lock:
                        txt = self._prompt_text.strip()
                        opts = list(self._prompt_options)
                        ptr = self._prompt_pointer
                        inc_ptr = self._prompt_include_pointer
                    if txt or not opts:
                        self._complete_prompt(
                            {
                                "answered": True,
                                "selected_option": None,
                                "text_response": txt,
                                "pointer": list(ptr) if (inc_ptr and ptr) else None,
                            }
                        )
                    return 0
                if vk == VK_BACK:
                    with self._state_lock:
                        if ctrl:
                            self._prompt_text = ""
                        elif self._prompt_text:
                            self._prompt_text = self._prompt_text[:-1]
                    self._queue.put({"op": "redraw_prompt"})
                    self._wake_event.set()
                    return 0
                if ctrl and vk == ord("V"):
                    try:
                        pasted = pyperclip.paste() or ""
                    except Exception:
                        pasted = ""
                    if pasted:
                        clean_paste = " ".join(str(pasted).splitlines()).strip()
                        with self._state_lock:
                            if self._prompt_allow_text:
                                self._prompt_text = (self._prompt_text + clean_paste)[:500]
                        self._queue.put({"op": "redraw_prompt"})
                        self._wake_event.set()
                    return 0
                # Number shortcuts 1-8 when text box is empty
                if 0x31 <= vk <= 0x38:
                    with self._state_lock:
                        empty_txt = not self._prompt_text
                        opts = list(self._prompt_options)
                        ptr = self._prompt_pointer
                        inc_ptr = self._prompt_include_pointer
                    idx = vk - 0x31
                    if empty_txt and 0 <= idx < len(opts):
                        self._complete_prompt(
                            {
                                "answered": True,
                                "selected_index": idx,
                                "selected_option": opts[idx],
                                "text_response": None,
                                "pointer": list(ptr) if (inc_ptr and ptr) else None,
                            }
                        )
                        return 0
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        hud_proc_cb = WNDPROCTYPE(_hud_proc)
        prompt_proc_cb = WNDPROCTYPE(_prompt_proc)
        self._wnd_procs = [hud_proc_cb, prompt_proc_cb]

        for cls_name, proc in (
            (_HUD_WINDOW_CLASS, hud_proc_cb),
            (_WOOLBUR_WINDOW_CLASS, hud_proc_cb),
            (_PROMPT_WINDOW_CLASS, prompt_proc_cb),
        ):
            wc = _WNDCLASSEXW(
                cbSize=ctypes.sizeof(_WNDCLASSEXW),
                lpfnWndProc=proc,
                hInstance=hinst,
                lpszClassName=cls_name,
            )
            user32.RegisterClassExW(ctypes.byref(wc))

        base_ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        hud_hwnd = user32.CreateWindowExW(base_ex, _HUD_WINDOW_CLASS, "", WS_POPUP, 0, 0, 600, 110, None, None, hinst, None)
        wool_hwnd = user32.CreateWindowExW(base_ex, _WOOLBUR_WINDOW_CLASS, "", WS_POPUP, 0, 0, 120, 120, None, None, hinst, None)
        prompt_ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
        prompt_hwnd = user32.CreateWindowExW(prompt_ex, _PROMPT_WINDOW_CLASS, "", WS_POPUP, 0, 0, 480, 240, None, None, hinst, None)

        if not hud_hwnd or not wool_hwnd or not prompt_hwnd:
            logger.warning("Failed to initialize Woolbur overlay windows (Error %d).", ctypes.GetLastError())
            ready_ev.set()
            return

        for h in (hud_hwnd, wool_hwnd, prompt_hwnd):
            excl = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(h, DWMWA_EXCLUDED_FROM_PEEK, ctypes.byref(excl), ctypes.sizeof(excl))
            dwmapi.DwmSetWindowAttribute(h, DWMWA_TRANSITIONS_FORCEDISABLED, ctypes.byref(excl), ctypes.sizeof(excl))

        self.hud_hwnd = int(hud_hwnd)
        self.hwnd = self.hud_hwnd
        self.woolbur_hwnd = int(wool_hwnd)
        self.prompt_hwnd = int(prompt_hwnd)

        hud_surf = _LayeredSurface(self.hud_hwnd)
        wool_surf = _LayeredSurface(self.woolbur_hwnd)
        prompt_surf = _LayeredSurface(self.prompt_hwnd)
        all_surfs = [hud_surf, wool_surf, prompt_surf]
        logger.info(
            "Woolbur dual-overlay initialized (hud=%d, woolbur=%d, prompt=%d).",
            self.hud_hwnd,
            self.woolbur_hwnd,
            self.prompt_hwnd,
        )
        ready_ev.set()

        fading = False
        overlay_active = False
        fade_start = 0.0
        fade_duration = 0.35
        hide_deadline: Optional[float] = None
        last_z_check = 0.0
        last_fs_check = 0.0
        last_cfg_check = 0.0
        live_cfg = self._read_live_cfg()
        prev_cfg = dict(live_cfg)
        last_fg_hwnd = 0
        last_anim_tick = 0.0
        last_prompt_blink = 0

        # Smooth Woolbur spring/hop state
        wool_cx = 0.0
        wool_cy = 0.0
        wool_pos_init = False
        hop_active = False
        hop_start_time = 0.0
        hop_duration = 0.28
        hop_from = (0.0, 0.0)
        prev_target_mode = "hud"
        anim_frame = 0
        facing = 1

        msg = wintypes.MSG()
        try:
            while True:
                self._wake_event.clear()
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))

                cmds: list[dict[str, Any]] = []
                while True:
                    try:
                        cmds.append(self._queue.get_nowait())
                    except queue.Empty:
                        break

                need_hud_redraw = False
                need_wool_redraw = False
                need_prompt_redraw = False
                open_prompt_now = False
                close_prompt_now = False
                restore_fg_hwnd: Optional[int] = None
                force_topmost = False
                run_force_fs = False
                force_fs_events: list[Optional[threading.Event]] = []

                for cmd in cmds:
                    op = cmd.get("op")
                    if op == "temp_hide":
                        for s in all_surfs:
                            if s.visible and not s.temp_hidden:
                                user32.ShowWindow(s.hwnd, SW_HIDE)
                                s.temp_hidden = True
                        try:
                            dwmapi.DwmFlush()
                        except Exception:
                            pass
                        done_ev = cmd.get("done_ev")
                        if done_ev is not None:
                            done_ev.set()
                    elif op == "temp_restore":
                        for s in all_surfs:
                            if s.visible and s.temp_hidden:
                                user32.ShowWindow(s.hwnd, SW_SHOWNOACTIVATE)
                                s.temp_hidden = False
                        force_topmost = True
                        run_force_fs = True
                        self._reassert_topmost_stack(all_surfs, force=True)
                        done_ev = cmd.get("done_ev")
                        if done_ev is not None:
                            done_ev.set()
                    elif op == "hide":
                        fading = False
                        overlay_active = False
                        hide_deadline = None
                        for s in all_surfs:
                            s.hide()
                            s.free_bitmap()
                        self._restore_demoted_windows()
                        with self._state_lock:
                            self._hud_visible = False
                            self._woolbur_visible = False
                    elif op == "force_fs":
                        force_topmost = True
                        run_force_fs = True
                        force_fs_events.append(cmd.get("done_ev"))
                    elif op == "restore_demoted":
                        self._restore_demoted_windows()
                        done_ev = cmd.get("done_ev")
                        if done_ev is not None:
                            done_ev.set()
                    elif op == "show":
                        fading = False
                        overlay_active = True
                        last_cfg_check = 0.0
                        need_hud_redraw = True
                        need_wool_redraw = True
                        force_topmost = True
                        run_force_fs = True
                        auto_s = float(cmd.get("auto_hide_seconds", 0.0))
                        hide_deadline = (time.monotonic() + auto_s) if auto_s > 0.0 else None
                    elif op in ("refresh", "assist_ping"):
                        need_hud_redraw = True
                        need_wool_redraw = True
                        force_topmost = True
                    elif op == "open_prompt":
                        open_prompt_now = True
                        need_prompt_redraw = True
                        need_hud_redraw = True
                        need_wool_redraw = True
                        force_topmost = True
                        run_force_fs = True
                    elif op == "redraw_prompt":
                        need_prompt_redraw = True
                    elif op == "close_prompt":
                        close_prompt_now = True
                        restore_fg_hwnd = cmd.get("restore_fg")
                        need_hud_redraw = True
                        need_wool_redraw = True

                now = time.monotonic()
                own = self.own_hwnds()
                mon_l, mon_t, mon_w, mon_h, dpi_scale = get_active_monitor_info(own)

                if now - last_cfg_check >= 0.25:
                    last_cfg_check = now
                    live_cfg = self._read_live_cfg()
                    if live_cfg != prev_cfg:
                        if live_cfg["force_fullscreen"] != prev_cfg.get("force_fullscreen"):
                            self.set_force_fullscreen(live_cfg["force_fullscreen"])
                        if overlay_active:
                            need_hud_redraw = True
                            need_wool_redraw = True
                        if prompt_open or prompt_surf.visible:
                            need_prompt_redraw = True
                        force_topmost = True
                        run_force_fs = True
                        prev_cfg = dict(live_cfg)

                cfg_scale = float(live_cfg["scale"])
                scale = dpi_scale * cfg_scale
                base_alpha = max(65, min(255, int(round(255.0 * float(live_cfg["opacity"])))))
                show_hud = bool(live_cfg["show_hud"])
                show_woolbur = bool(live_cfg["show_woolbur"])
                follow_cursor = bool(live_cfg["follow_cursor"])
                custom_accent = str(live_cfg["accent_color"])
                wool_color = str(live_cfg["wool_color"])
                horn_color = str(live_cfg["horn_color"])
                cfg_acc_set = str(live_cfg["accessory_set"])
                cfg_mood = str(live_cfg["mood"])

                with self._state_lock:
                    stype = self._last_status_type
                    assisting = now < self._assist_until
                    ack_active = now < self._assist_ack_until
                    ack_text = self._assist_ack_text if ack_active else ""
                    prompt_open = self._prompt_open
                    goal = self._goal or self._current_intent
                    looking_for = self._looking_for
                    live_action = self._live_action
                    mood_override = cfg_mood if cfg_mood != "auto" else self._mood_override

                # Auto-clear expired assist state cleanly
                if not assisting and not self._assist_done_event.is_set():
                    self._assist_done_event.set()
                    need_hud_redraw = True
                    need_wool_redraw = True

                # Animation frame cadence
                is_moving_target = (stype == "acting") or assisting
                frame_period = 0.075 if (is_moving_target or hop_active) else 0.22
                if (overlay_active or need_hud_redraw) and (now - last_anim_tick >= frame_period):
                    last_anim_tick = now
                    anim_frame = (anim_frame + 1) % 8
                    need_wool_redraw = True

                # Re-render or toggle Fixed HUD cleanly
                if overlay_active and show_hud and not fading:
                    if (need_hud_redraw or not hud_surf.visible) and not hud_surf.temp_hidden:
                        hud_w, hud_h, hud_bgra = self._render_hud_bgra(
                            scale,
                            mon_w,
                            custom_accent_hex=custom_accent,
                        )
                        hud_x = mon_l + (mon_w - hud_w) // 2
                        hud_y = mon_t + int(round(18 * scale))
                        hud_surf.upload_bgra(hud_w, hud_h, hud_bgra)
                        hud_surf.present(hud_x, hud_y, alpha=base_alpha)
                        # Clickable only when thinking/complete/intent, never when acting!
                        hud_surf.set_click_through(stype == "acting" or assisting or prompt_open)
                        with self._state_lock:
                            self._hud_rect = (hud_x, hud_y, hud_x + hud_w, hud_y + hud_h)
                            self._hud_visible = True
                elif hud_surf.visible and not fading:
                    hud_surf.hide()
                    hud_surf.free_bitmap()
                    with self._state_lock:
                        self._hud_rect = (0, 0, 0, 0)
                        self._hud_visible = False

                # Compute Woolbur target mode (cursor vs HUD perch)
                target_mode = "cursor" if (follow_cursor and (stype == "acting" or assisting) and not prompt_open) else "hud"
                if target_mode != prev_target_mode:
                    hop_active = True
                    hop_start_time = now
                    hop_from = (wool_cx, wool_cy) if wool_pos_init else (float(hud_surf.x), float(hud_surf.y))
                    prev_target_mode = target_mode

                # Determine Woolbur speech bubble text
                speech_title = ""
                speech_sub = ""
                if assisting:
                    speech_title = f"Looking for: {looking_for or goal or 'target'}"
                    speech_sub = "Click to help • Alt+W to point & say why"
                    eff_stype = "assist"
                elif ack_text:
                    speech_title = ack_text
                    eff_stype = stype
                elif stype == "acting" and live_action:
                    speech_title = live_action
                    eff_stype = "acting"
                elif stype == "error" and live_action:
                    speech_title = live_action
                    eff_stype = "error"
                else:
                    eff_stype = stype

                mood, eff_acc_set = resolve_appearance(
                    eff_stype,
                    mood_override=mood_override,
                    accessory_set=cfg_acc_set,
                    action_hint=live_action,
                )
                accent = self._resolve_accent_rgb(eff_stype, custom_accent)

                # Compute desired anchor position for Woolbur sprite center
                cur_x, cur_y = get_cursor_pos()
                c_mon_l, c_mon_t, c_mon_w, c_mon_h, c_dpi_scale = get_monitor_at_point(cur_x, cur_y)
                c_scale = c_dpi_scale * cfg_scale
                if target_mode == "cursor":
                    # Offset cleanly away from cursor point, flipping near edges
                    off_x = int(round(62 * c_scale))
                    off_y = int(round(58 * c_scale))
                    des_x = float(cur_x + off_x)
                    des_y = float(cur_y + off_y)
                    if des_x + int(round(260 * c_scale)) > c_mon_l + c_mon_w:
                        des_x = float(cur_x - off_x)
                        facing = -1
                    else:
                        facing = 1
                    if des_y + int(round(65 * c_scale)) > c_mon_t + c_mon_h:
                        des_y = float(cur_y - off_y)
                    bubble_on_right = (des_x + int(round(340 * c_scale))) <= (c_mon_l + c_mon_w)
                else:
                    # Dock cleanly on upper-left rim of Fixed HUD card (or top-center if HUD is hidden)
                    if hud_surf.visible:
                        des_x = float(hud_surf.x + int(round(22 * scale)))
                        des_y = float(hud_surf.y + int(round(36 * scale)))
                    else:
                        des_x = float(mon_l + mon_w // 2)
                        des_y = float(mon_t + int(round(44 * scale)))
                    facing = 1
                    bubble_on_right = True

                if not wool_pos_init:
                    wool_cx, wool_cy = des_x, des_y
                    wool_pos_init = True

                # Smooth interpolation / parabolic hop
                if hop_active:
                    t = min(1.0, (now - hop_start_time) / hop_duration)
                    ease = 1.0 - (1.0 - t) ** 3
                    arc = -math.sin(t * math.pi) * (58.0 * scale)
                    wool_cx = hop_from[0] + (des_x - hop_from[0]) * ease
                    wool_cy = hop_from[1] + (des_y - hop_from[1]) * ease + arc
                    if t >= 1.0:
                        hop_active = False
                        wool_cx, wool_cy = des_x, des_y
                elif target_mode == "cursor":
                    wool_cx += (des_x - wool_cx) * 0.42
                    wool_cy += (des_y - wool_cy) * 0.42
                else:
                    wool_cx, wool_cy = des_x, des_y

                if overlay_active and show_woolbur and not wool_surf.temp_hidden and not fading:
                    w_key = (
                        round(scale, 2),
                        mood,
                        eff_acc_set,
                        anim_frame,
                        facing,
                        speech_title,
                        speech_sub,
                        eff_stype,
                        bubble_on_right,
                        custom_accent,
                        wool_color,
                        horn_color,
                        base_alpha,
                    )
                    if wool_surf._render_key != w_key or need_wool_redraw or not wool_surf.visible:
                        ww, wh, ax, ay, w_bgra = self._render_woolbur_bubble_bgra(
                            scale,
                            mood,
                            anim_frame,
                            facing,
                            speech_title,
                            speech_sub,
                            accent,
                            bubble_on_right,
                            wool_color=wool_color,
                            horn_color=horn_color,
                            accessory_set=eff_acc_set,
                        )
                        wool_surf.upload_bgra(ww, wh, w_bgra, render_key=w_key)
                        wool_surf._anchor_ox = ax  # type: ignore[attr-defined]
                        wool_surf._anchor_oy = ay  # type: ignore[attr-defined]
                    ax_off = getattr(wool_surf, "_anchor_ox", wool_surf.w // 2)
                    ay_off = getattr(wool_surf, "_anchor_oy", wool_surf.h // 2)
                    wx = int(round(wool_cx - ax_off))
                    wy = int(round(wool_cy - ay_off))
                    # Clamp inside active monitor bounds
                    m_l, m_t, m_w, m_h = (c_mon_l, c_mon_t, c_mon_w, c_mon_h) if target_mode == "cursor" else (mon_l, mon_t, mon_w, mon_h)
                    wx = max(m_l + 4, min(m_l + m_w - wool_surf.w - 4, wx))
                    wy = max(m_t + 4, min(m_t + m_h - wool_surf.h - 4, wy))
                    wool_surf.present(wx, wy, alpha=base_alpha)
                    wool_surf.set_click_through(target_mode == "cursor" or assisting or prompt_open)
                    with self._state_lock:
                        self._woolbur_rect = (wx, wy, wx + wool_surf.w, wy + wool_surf.h)
                        self._woolbur_visible = True
                elif not show_woolbur and wool_surf.visible and not fading:
                    wool_surf.hide()
                    wool_surf.free_bitmap()
                    with self._state_lock:
                        self._woolbur_rect = (0, 0, 0, 0)
                        self._woolbur_visible = False

                # Interactive Prompt Window (ask_woolbur or Alt+W steering)
                if close_prompt_now:
                    prompt_surf.hide()
                    prompt_surf.free_bitmap()
                    with self._state_lock:
                        self._prompt_rect = (0, 0, 0, 0)
                    if restore_fg_hwnd:
                        try:
                            fg = user32.GetForegroundWindow()
                            if fg == self.prompt_hwnd:
                                curr_tid = kernel32.GetCurrentThreadId()
                                target_tid = user32.GetWindowThreadProcessId(restore_fg_hwnd, None)
                                attached = False
                                if target_tid and target_tid != curr_tid:
                                    attached = bool(user32.AttachThreadInput(curr_tid, target_tid, True))
                                user32.SetForegroundWindow(restore_fg_hwnd)
                                if attached:
                                    user32.AttachThreadInput(curr_tid, target_tid, False)
                        except Exception:
                            pass

                blink_tick = int(now * 2)
                if prompt_open and (need_prompt_redraw or blink_tick != last_prompt_blink) and not prompt_surf.temp_hidden:
                    last_prompt_blink = blink_tick
                    if open_prompt_now:
                        self._maybe_demote_fullscreen(all_surfs, require_visible=False)
                        fg_now = user32.GetForegroundWindow()
                        if fg_now and int(fg_now) not in own:
                            with self._state_lock:
                                self._prompt_prev_fg = int(fg_now)
                    pw, ph, p_bgra = self._render_prompt_bgra(
                        scale,
                        mon_w,
                        custom_accent_hex=custom_accent,
                        wool_color=wool_color,
                        horn_color=horn_color,
                        accessory_set=cfg_acc_set,
                    )
                    with self._state_lock:
                        ptr = self._prompt_pointer
                    if ptr is not None:
                        px = max(mon_l + 12, min(mon_l + mon_w - pw - 12, ptr[0] + int(round(20 * scale))))
                        py = max(mon_t + 12, min(mon_t + mon_h - ph - 12, ptr[1] + int(round(20 * scale))))
                    else:
                        px = mon_l + (mon_w - pw) // 2
                        py = (hud_surf.y + hud_surf.h + int(round(12 * scale))) if hud_surf.visible else (mon_t + int(round(60 * scale)))
                    prompt_surf.upload_bgra(pw, ph, p_bgra)
                    prompt_surf.present(px, py, alpha=255, activate=open_prompt_now)
                    with self._state_lock:
                        self._prompt_rect = (px, py, px + pw, py + ph)
                    if open_prompt_now:
                        curr_fg = user32.GetForegroundWindow()
                        curr_tid = kernel32.GetCurrentThreadId()
                        fg_tid = user32.GetWindowThreadProcessId(curr_fg, None) if curr_fg else 0
                        attached = False
                        try:
                            if fg_tid and fg_tid != curr_tid:
                                attached = bool(user32.AttachThreadInput(curr_tid, fg_tid, True))
                            user32.BringWindowToTop(self.prompt_hwnd)
                            user32.SetForegroundWindow(self.prompt_hwnd)
                        finally:
                            if attached and fg_tid:
                                user32.AttachThreadInput(curr_tid, fg_tid, False)

                # Z-order check & re-promotion (handles borderless fullscreen apps cleanly!)
                fg_curr = user32.GetForegroundWindow()
                fg_curr_int = int(fg_curr) if fg_curr else 0
                if fg_curr_int and fg_curr_int != last_fg_hwnd:
                    last_fg_hwnd = fg_curr_int
                    if fg_curr_int not in own:
                        force_topmost = True
                        run_force_fs = True
                if any(s.visible and not s.temp_hidden for s in all_surfs) and not fading:
                    if run_force_fs or (now - last_fs_check >= 0.35):
                        last_fs_check = now
                        self._maybe_demote_fullscreen(all_surfs)
                    if force_topmost or (now - last_z_check >= 0.12):
                        last_z_check = now
                        self._reassert_topmost_stack(all_surfs, force=force_topmost)
                for ev in force_fs_events:
                    if ev is not None:
                        ev.set()

                # Auto-hide fade on completion/error
                if (
                    overlay_active
                    and (hud_surf.visible or wool_surf.visible)
                    and not hud_surf.temp_hidden
                    and not fading
                    and not prompt_open
                    and hide_deadline is not None
                    and now >= hide_deadline
                ):
                    fading = True
                    fade_start = now
                    hide_deadline = None

                if fading and (hud_surf.visible or wool_surf.visible):
                    progress = (now - fade_start) / fade_duration
                    if progress >= 1.0:
                        for s in (hud_surf, wool_surf):
                            s.hide()
                            s.free_bitmap()
                        self._restore_demoted_windows()
                        fading = False
                        overlay_active = False
                        with self._state_lock:
                            self._hud_visible = False
                            self._woolbur_visible = False
                    else:
                        alpha = int(round(base_alpha * (1.0 - progress)))
                        if hud_surf.visible and not hud_surf.temp_hidden:
                            hud_surf.present(hud_surf.x, hud_surf.y, alpha=alpha)
                        if wool_surf.visible and not wool_surf.temp_hidden:
                            wool_surf.present(wool_surf.x, wool_surf.y, alpha=alpha)

                wait_s = 0.016 if (fading or hop_active or target_mode == "cursor") else 0.035
                self._wake_event.wait(wait_s)
        finally:
            self._restore_demoted_windows()
            while True:
                try:
                    rem = self._queue.get_nowait()
                    done_ev = rem.get("done_ev")
                    if done_ev is not None:
                        done_ev.set()
                except queue.Empty:
                    break
            for s in all_surfs:
                s.free_bitmap()
                if s.hwnd:
                    user32.DestroyWindow(s.hwnd)
            self.hud_hwnd = None
            self.hwnd = None
            self.woolbur_hwnd = None
            self.prompt_hwnd = None
