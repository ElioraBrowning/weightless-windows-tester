---
name: weightless-windows-testing
description: OS-level Windows 10/11 desktop vision, offline Windows.Media.Ocr, multi-window focus control, cursor/keyboard input with physical mousepad suppression, clipboard, and file drag-and-drop testing via the weightless-windows-vision MCP server. Use when automating or verifying native Windows desktop applications or multi-window workflows.
---

# Desktop Vision & Cursor Control Workflow

This skill pairs with the **`weightless-windows-vision`** MCP server (`server/server.py`, launched portably via `scripts/launcher.py`) and the **`weightless-windows-tester`** subagent on Windows 10/11.

## Tool Reference (`weightless-windows-vision` MCP)

### 1. Vision & Resolution
- `get_screen_resolution()`: Returns `virtual_screen` and per-monitor bounds (`monitor_index=0` is primary, `1..N` are individual monitors, `-1` is all monitors).
- `take_screenshot(show_grid=None, grid_interval=100, monitor_index=0, include_image_content=False, region=None, image_format=None, scale=None)`:
  - Full-screen overview captures default to **`0.5x` JPEG** (configurable in the **Woolbur** Antigravity sidebar) for small payloads and faster vision, while drawing the coordinate grid AFTER downscaling so labels remain sharp in **exact physical screen coordinates (`0..physical_width`)**.
  - Whenever text, numbers, cards, or fine controls look unreadable in a scaled overview, **follow up immediately** with a cropped `take_screenshot(region=[x, y, w, h])` (which automatically captures at **`1.0x` lossless PNG**) or `take_screenshot(scale=1.0)`.
  - Saves permanently under `~/.local_desktop_agent/screenshots/` (or `$DESKTOP_TESTER_DATA_DIR/screenshots/`), with `scale` / `screenshot_scale` also supported on `mouse_click` and `batch_clicks`.
  - Keep `include_image_content=False` (inspect `file_path` via `read_file`).

### 2. Offline Windows Native OCR
- `read_text_from_screen(region=None, monitor_index=0)`:
  - Runs 100% offline `Windows.Media.Ocr` on the monitor or `[x, y, w, h]` global region.
  - Returns `lines` and `words` with global coordinates `(x, y, w, h)` and pre-computed `(center_x, center_y)` for direct clicking without PNG disk I/O or `read_file` round-trips.

### 3. Window, Cursor, Per-App Memory & Keyboard Control
- `list_windows(include_minimized=True)`: Lists visible windows with non-empty titles, `process_name`, and global bounds.
- `focus_window(window_title)`: Matches by window title or executable process name (e.g., `"Antigravity"`), restores if minimized, brings to foreground using Win32 `AttachThreadInput`, returns window bounds, and **automatically attaches matching `app_memory`** (or `app_memory_hint` when no profile exists yet).
- `get_app_memory(app_name=None, process_name=None, window_title=None)`: Reads persistent per-app memory (`~/.local_desktop_agent/app_memory.json`) for a specific app or lists all saved profiles.
- `update_app_memory(app_name, display_name=None, process_names=None, title_patterns=None, add_note=None, remove_note=None, coordinates=None, remove_coordinates=None, hotkeys=None, settings=None, clear=False)`: Persists verified coordinates, hotkeys, notes/quirks, and custom settings per app. Editable live in the **Woolbur** Antigravity sidebar (`[Woolbur](sidecar://weightless-windows-tester/woolbur/)`).
- `mouse_click(x, y, button="left", clicks=1, hold_seconds=0.05, settle_seconds=0.0, screenshot_after=False, screenshot_region=None, screenshot_grid=None, screenshot_format=None, screenshot_scale=None)`: Clicks via Win32 `SendInput` (`MOUSEEVENTF_ABSOLUTE` + press-hold delay, default 50ms for SDL2/LÖVE/DirectX reliability) while suppressing physical mousepad touches. Pass `settle_seconds` + `screenshot_after=True` to click, wait for UI animation, and capture in one MCP round-trip.
- `batch_clicks(points, button="left", hold_seconds=0.05, delay_between=0.06, label=None, settle_seconds=0.0, screenshot_after=False, screenshot_region=None, screenshot_grid=None, screenshot_format=None, screenshot_scale=None)`: Clicks a list of `[x, y]` coordinates via `SendInput` in a single MCP round-trip (and optionally settles + screenshots after).
- `mouse_move(x, y, duration=0.0)`: Moves cursor to `(x, y)` (instant jump by default) while suppressing physical mousepad touches.
- `mouse_drag(x1, y1, x2, y2, duration=0.4, button="left")`: Drags between coordinates while suppressing physical mousepad touches.
- `type_text(text, delay_between_keys=0.005, fast_batch=True)`: Fast Unicode typing into focused control using batched `SendInput` arrays by default (`delay_between_keys <= 0.005`).
- `press_key(key, hold_seconds=0.03)`: Single keys (`enter`, `tab`, `esc`) or hotkeys (`ctrl+a`, `alt+tab`) with brief hold delay for games.
- `set_physical_mouse_lock(locked=True/False, timeout_seconds=15)`: Manually lock or unlock physical mousepad/mouse input for multi-step sequences with automatic safety timeout.

### 4. Clipboard, File Drop, Fixed HUD & Woolbur Interactive Companion
- `set_clipboard(text)` / `get_clipboard()`
- `drag_and_drop_file(file_path, target_x, target_y, method="auto")`: Supports `"auto"`, `"wm_dropfiles"`, `"explorer"`, and `"clipboard"`.
- `show_status_bubble(message, status_type="intent", auto_hide_seconds=0.0, goal=None, looking_for=None, step_detail=None, mood="auto", force_fullscreen=True, reset_abort=False)`: Controls the DPI-scaled top-center Dark Glass HUD card and **Woolbur** companion (who hops to the HUD when thinking and glides beside the cursor during actions, wearing dynamic nerd/detective/punk/goth accessories). When `force_fullscreen=True`, reversibly nudges undecorated borderless fullscreen apps by 1px while visible so the overlay renders on top, restoring original bounds on `hide` / `complete`. Uses `suspend_for_capture` during screenshots/OCR without triggering HUD churn on read-only observation calls.
- `ask_woolbur(question, options=None, allow_text=True, context_hint=None, timeout_seconds=180.0)`: Displays a clickable multiple-choice and text-input card directly over the active app when choosing between methods or following up on user help.

### 5. Hardware Input Suppression, Co-Pilot Assist & Emergency Abort
- **Physical Mouse/Touchpad Suppression (`WH_MOUSE_LL`)**:
  - Automatically drops physical touchpad touches and external mouse movements during active input execution, preventing accidental hand brushing from throwing off coordinate clicks.
  - Synthetic clicks (`LLMHF_INJECTED`) pass through seamlessly without requiring administrator privileges.
  - Automatic safety watchdog ensures physical mouse is restored even in case of unexpected errors or timeout (15s).
- **Assist-Mode & `Alt+W` Mid-Run Steering**:
  - Between actions while a task is active, deliberate physical mouse movement (>45px, ignoring drift) wakes Woolbur beside the cursor to show what he is looking for (`looking_for`), lets real clicks pass through to help (recording `(x, y)` in `user_guidance`), or lets **`Alt+W`** (or clicking the HUD/Woolbur while thinking) open a quick steering/point card without ending the session.
- **Emergency Hardware Abort Sequence (`WH_KEYBOARD_LL`)**:
  - **Triple-`Esc`** (press physical `Esc` 3 times within **1.8 seconds**; ignores held key auto-repeat and synthetic `SendInput` Esc)
  - **`Pause / Break`** key
  - **`Ctrl+Shift+Esc`** or **`Ctrl+Alt+Esc`**
  - Pressing any abort sequence immediately restores physical mouse control, halts running loops, and **latches** abort state across all subsequent tools until `show_status_bubble(..., status_type="intent", reset_abort=True)` is called on a new user turn.

## Fast Observe-Plan-Batch Act-Verify Loop
1. **Minimize LLM Round-Trips (Primary Speed Lever)**:
   - Reuse coordinates, hotkeys, and quirks returned automatically in `focus_window`'s `app_memory` before re-exploring.
   - Always keep `include_image_content=False` on `take_screenshot`; capture a `0.5x` overview first (`take_screenshot()`), and follow up with `take_screenshot(region=[x, y, w, h])` (`1.0x` PNG) or `read_text_from_screen(region=...)` if any text or fine controls are unreadable.
   - Run `show_status_bubble` and `batch_clicks` / `mouse_click` in parallel rather than spending an extra sequential `"thinking"` turn.
   - Skip redundant `list_windows()` and `focus_window()` calls once the target app is already focused.
2. **Cache Static Coordinates & Chain Click + Capture**:
   - After 1 gridded screenshot per layout/resolution, persist verified layout coordinates, hotkeys, and quirks via `update_app_memory` and reuse them (`show_grid=False` or `image_format="jpeg"` / cropped `region=...` for fast verification).
   - Use `batch_clicks(..., settle_seconds=..., screenshot_after=True)` (or `mouse_click(..., settle_seconds=..., screenshot_after=True)`) for deterministic multi-click + wait + capture transitions in a single MCP round-trip (e.g., selecting multiple cards + Play/Discard, or clicking a card + its known `BUY`/`OPEN`/`SELECT` offset).
3. **Hotkeys & In-App Speed Settings First**:
   - Prefer keyboard shortcuts (`press_key`) and maximum in-app animation speed (`Game Speed = 4`, quick-restart hotkeys like `R`) before pixel clicking.
4. **Return to Antigravity**: Call `show_status_bubble(message="Done: ...", status_type="complete", auto_hide_seconds=4.0)` and `focus_window(window_title="Antigravity")` upon completing a task or pausing for user confirmation (and before sensitive/destructive steps).
