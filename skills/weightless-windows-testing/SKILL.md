---
name: weightless-windows-testing
description: OS-level Windows 10/11 desktop vision, offline Windows.Media.Ocr, multi-window focus control, cursor/keyboard input with physical mousepad suppression, clipboard, and file drag-and-drop testing via the weightless-windows-vision MCP server. Use when automating or verifying native Windows desktop applications or multi-window workflows.
---

# Desktop Vision & Cursor Control Workflow

This skill pairs with the **`weightless-windows-vision`** MCP server (`server/server.py`, launched portably via `scripts/launcher.py`) and the **`weightless-windows-tester`** subagent on Windows 10/11.

## Tool Reference (`weightless-windows-vision` MCP)

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
- `list_windows(include_minimized=True)`: Lists visible windows with non-empty titles, `process_name`, and global bounds.
- `focus_window(window_title)`: Matches by window title or executable process name (e.g., `"Antigravity"`), restores if minimized, brings to foreground using Win32 `AttachThreadInput`, and returns window bounds.
- `mouse_click(x, y, button="left", clicks=1)`: Clicks target coordinate while suppressing physical mousepad touches.
- `mouse_move(x, y, duration=0.1)`: Moves cursor smoothly while suppressing physical mousepad touches.
- `mouse_drag(x1, y1, x2, y2, duration=0.4, button="left")`: Drags between coordinates while suppressing physical mousepad touches.
- `type_text(text, delay_between_keys=0.05)`: Unicode typing into focused control.
- `press_key(key)`: Single keys (`enter`, `tab`, `esc`) or hotkeys (`ctrl+a`, `alt+tab`).
- `set_physical_mouse_lock(locked=True/False, timeout_seconds=15)`: Manually lock or unlock physical mousepad/mouse input for multi-step sequences with automatic safety timeout.

### 4. Clipboard & File Drag-and-Drop
- `set_clipboard(text)` / `get_clipboard()`
- `drag_and_drop_file(file_path, target_x, target_y, method="auto")`: Supports `"auto"`, `"wm_dropfiles"`, `"explorer"`, and `"clipboard"`.

### 5. Hardware Input Suppression & Emergency Abort
- **Physical Mouse/Touchpad Suppression (`WH_MOUSE_LL`)**:
  - Automatically drops physical touchpad touches and external mouse movements during agent actions, preventing accidental hand brushing from throwing off coordinate clicks.
  - Software/synthetic clicks (`LLMHF_INJECTED`) pass through seamlessly without requiring administrator privileges.
  - Automatic safety watchdog ensures physical mouse is restored even in case of unexpected errors or timeout (15s).
- **Emergency Hardware Abort Sequence**:
  - **Triple-`Esc`** (press `Esc` 3 times quickly within 1 second)
  - **`Pause / Break`** key
  - **`Ctrl+Shift+Esc`** or **`Ctrl+Alt+Esc`**
  - Pressing any abort sequence immediately kills physical mouse suppression, halts the running action, and raises an emergency stop.

## Observe-Plan-Act-Verify Loop
1. **Observe**: Call `list_windows()` / `focus_window(...)`, then `take_screenshot(show_grid=True)` (inspect with `read_file`) and/or `read_text_from_screen()` to obtain exact global `(x, y)` coordinates.
2. **Plan & Safety Check**: Verify target coordinates visually and via OCR; pause and call `focus_window("Antigravity")` before asking for explicit user confirmation on any sensitive or destructive action.
3. **Act**: Execute the smallest precise action (`mouse_click`, `type_text`, `press_key`, etc.).
4. **Verify**: Take a follow-up screenshot or OCR pass to confirm UI state transitioned as expected.
5. **Return to Antigravity**: Call `focus_window(window_title="Antigravity")` upon completing a task or pausing for user input so the chat window returns to the foreground.
