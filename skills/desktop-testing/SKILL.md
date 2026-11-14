---
name: desktop-testing
description: OS-level Windows 10/11 desktop vision, offline Windows.Media.Ocr, multi-window focus control, cursor/keyboard input, clipboard, and file drag-and-drop testing via the desktop-vision MCP server. Use when automating or verifying native Windows desktop applications or multi-window workflows.
---

# Desktop Vision & Cursor Control Workflow

This skill pairs with the **`desktop-vision`** MCP server (`server/server.py`, launched portably via `scripts/launcher.py`) and the **`desktop-tester`** subagent on Windows 10/11.

## Tool Reference (`desktop-vision` MCP)

### 1. Vision & Resolution
- `get_screen_resolution()`: Returns `virtual_screen` and per-monitor bounds (`monitor_index=0` is primary, `1..N` are individual monitors, `-1` is all monitors).
- `take_screenshot(show_grid=True, grid_interval=100, monitor_index=0, include_image_content=False)`:
  - Saves a PNG permanently under `~/.local_desktop_agent/screenshots/` (or `$DESKTOP_TESTER_DATA_DIR/screenshots/`).
  - When `show_grid=True`, overlays pixel grid lines and **global desktop `(x, y)` coordinate labels**.
  - Monitors cumulative screenshot folder size and alerts at every 1 GB increment.
  - Inspect the saved image using `read_file(Path=file_path)`.

### 2. Offline Windows Native OCR
- `read_text_from_screen(region=None, monitor_index=0)`:
  - Runs 100% offline `Windows.Media.Ocr` on the monitor or `[x, y, w, h]` global region.
  - Returns `lines` and `words` with global coordinates `(x, y, w, h)` and pre-computed `(center_x, center_y)` for direct clicking without coordinate guessing.

### 3. Window, Cursor & Keyboard Control
- `list_windows(include_minimized=True)`: Lists visible windows with non-empty titles and global bounds.
- `focus_window(window_title)`: Restores if minimized, brings to foreground using Win32 `AttachThreadInput`, and returns window bounds.
- `mouse_click(x, y, button="left", clicks=1)`
- `mouse_move(x, y, duration=0.1)`
- `mouse_drag(x1, y1, x2, y2, duration=0.4, button="left")`
- `type_text(text, delay_between_keys=0.05)`
- `press_key(key)`: Single keys (`enter`, `tab`, `esc`) or hotkeys (`ctrl+a`, `alt+tab`).

### 4. Clipboard & File Drag-and-Drop
- `set_clipboard(text)` / `get_clipboard()`
- `drag_and_drop_file(file_path, target_x, target_y, method="auto")`: Supports `"auto"`, `"wm_dropfiles"`, `"explorer"`, and `"clipboard"`.

### 5. Hardware Failsafe & Abort
- `pyautogui.FAILSAFE` is disabled so resting/falling mouse cursors in corners never trigger accidental aborts.
- Emergency Abort hotkey: press `Pause`, `Ctrl+Shift+Esc`, or `Ctrl+Alt+Esc` to immediately cancel any running input loop.

## Observe-Plan-Act-Verify Loop
1. **Observe**: Call `list_windows()` / `focus_window(...)`, then `take_screenshot(show_grid=True)` (inspect with `read_file`) and/or `read_text_from_screen()` to obtain exact global `(x, y)` coordinates.
2. **Plan & Safety Check**: Verify target coordinates visually and via OCR; pause for explicit user confirmation before any sensitive or destructive action.
3. **Act**: Execute the smallest precise action (`mouse_click`, `type_text`, `press_key`, etc.).
4. **Verify**: Take a follow-up screenshot or OCR pass to confirm UI state transitioned as expected.
