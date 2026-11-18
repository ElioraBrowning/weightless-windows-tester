# Weightless Windows Tester (`weightless-windows-tester`)

An Antigravity plugin providing Windows 10/11 OS-level desktop vision, **100% offline Windows native OCR (`Windows.Media.Ocr`)**, multi-window focus management, cursor/keyboard control with **physical mousepad suppression**, clipboard utilities, multi-strategy file drag-and-drop, and the dedicated **`@weightless-windows-tester`** automation subagent.

> **Platform Requirement:** Windows 10 / Windows 11 (`x64`) with Python 3.10+ (and Node.js for the Antigravity Woolbur sidebar).

---

## Features

- **Per-Monitor v2 DPI Awareness:** Coordinates across single and multi-monitor setups match physical screen pixels without scaling drift.
- **Adaptive Gridded Visual Grounding (`take_screenshot`):** Defaults to a compact `0.5x` JPEG overview with sharp physical global `(x, y)` grid labels (`0..physical_width`), auto-escalates to `1.0x` lossless PNG for zoomed `region` follow-ups, and tracks cumulative screenshot storage with automatic alerts every `1 GiB`.
- **100% Offline Windows Native OCR (`read_text_from_screen`):** Uses built-in `Windows.Media.Ocr` (via `winrt-Windows.Media.Ocr`) to return line- and word-level text with global bounding boxes `(x, y, w, h)` and pre-computed click centers `(center_x, center_y)`.
- **Reliable Window Control & Per-App Memory (`list_windows`, `focus_window`, `get_app_memory`, `update_app_memory`):** Enumerates visible windows (with title and executable `process_name`), restores minimized windows via `AttachThreadInput`, automatically attaches saved per-app coordinates/hotkeys/notes when focusing known apps, and persists updates across runs.
- **Cursor, Keyboard, Clipboard & File Drop:** Fast batched Unicode `SendInput` typing (`delay_between_keys=0.005`, `fast_batch=True`), multi-click sequences (`batch_clicks`), instant cursor jumps (`duration=0.0`), single keys/hotkeys, clipboard get/set, and `drag_and_drop_file` (`wm_dropfiles`, `explorer`, and `clipboard`).
- **Fixed Dark Glass HUD & Animated Woolbur Companion (`show_status_bubble`, `ask_woolbur`):** Renders a DPI-scaled top-center HUD card (Goal, Looking-for/Plan, and Recent Steps) paired with **Woolbur**—an animated flat cloud-sheep companion with customizable wool color, horn color, and selectable accessory sets (`auto`, `nerd`, `detective`, `punk`, `goth`, `happy`, `none`). Re-asserts topmost Z-order over borderless fullscreen apps cleanly, hides synchronously during screenshots (`suspend_for_capture`), and supports **`Alt+W`** mid-run steering, Assist-mode, and clickable **`ask_woolbur`** cards.
- **Antigravity Woolbur Studio Sidebar (`sidecars/woolbur/`):** Auxiliary-pane UI to live-customize Woolbur's appearance, adaptive screenshot/safety settings, and persistent per-app memories with real-time SSE sync.
- **Hardware Input Suppression & Emergency Abort:** Automatically suppresses physical touchpad touches and external mouse movements during agent actions (`WH_MOUSE_LL`). Registers emergency hardware abort sequences (**Triple-`Esc`** within `1.8s` configurable, **`Pause`**, or **`Ctrl+Shift+Esc`**) to cancel running actions, latch abort state, and unblock the mouse immediately.
- **Dedicated `@weightless-windows-tester` Subagent:** Uses a **Fast Observe–Plan–Batch Act–Verify** loop with Smart Action Batching, updates the status bubble in parallel with actions, refocuses **Antigravity** upon completion or prompts, and requires explicit chat confirmation before any sensitive system, config, credential, online-account, or destructive file action.

---

## Installation

### Option 1: PowerShell Installer (Recommended)
Clone this repository and run [`install.ps1`](install.ps1):

```powershell
git clone https://github.com/ElioraBrowning/weightless-windows-tester.git weightless-windows-tester
cd weightless-windows-tester
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

This installs the plugin into `%USERPROFILE%\.gemini\config\plugins\weightless-windows-tester`, pre-creates an isolated virtual environment at `%USERPROFILE%\.local_desktop_agent\.venv`, and verifies the MCP stdio handshake.

### Option 2: Portable Direct Plugin Copy
Copy (or clone) this folder directly to `%USERPROFILE%\.gemini\config\plugins\weightless-windows-tester`. On first MCP startup, `scripts/launcher.py` automatically creates `%USERPROFILE%\.local_desktop_agent\.venv` and installs `requirements.txt` (writing bootstrap logs to `stderr` so MCP `stdout` stays clean).

You can override the runtime storage location (venv, screenshots, logs) by setting the `DESKTOP_TESTER_DATA_DIR` environment variable.

---

## Usage Examples

```text
@weightless-windows-tester Focus the Calculator window, take a gridded screenshot of monitor 0, locate buttons via Windows OCR, compute 7 * 6, and verify the result visually.
```

```text
@weightless-windows-tester List all open windows, focus Notepad, paste the test snippet into a new empty tab, and verify with a follow-up screenshot without saving.
```

---

## Repository Structure

```text
weightless-windows-tester/
├── plugin.json                        # Antigravity plugin manifest
├── mcp_config.json                    # Portable stdio MCP entry -> scripts/launcher.py
├── requirements.txt                   # Python dependencies (Windows winrt + UI automation)
├── install.ps1                        # Global plugin installer & environment verifier
├── assets/
│   └── logo.svg                       # Plugin icon
├── agents/
│   └── weightless-windows-tester.md   # Subagent persona, loop, and safety protocol
├── skills/
│   └── weightless-windows-testing/
│       └── SKILL.md                   # Tool workflow reference skill
├── scripts/
│   └── launcher.py                    # Self-bootstrapping portable MCP launcher
├── sidecars/
│   └── woolbur/
│       ├── sidecar.json               # Antigravity auxiliary-pane sidecar manifest
│       ├── server.mjs                 # Local HTTP + SSE server for config/app memory/preview
│       ├── preview_worker.py          # Pixel-matched preview bridge to woolbur_sprites.py
│       └── ui.html                    # Woolbur Studio sidebar UI
└── server/
    ├── server.py                      # MCPServer ("weightless-windows-vision") implementation
    ├── overlay_ui.py                  # Dual-overlay Fixed HUD, Woolbur, and interactive prompt UI
    ├── woolbur_sprites.py             # Flat cloud-sheep Woolbur renderer & accessory kits
    └── woolbur_store.py               # Atomic persistent config & per-app memory store
```

---

## Safety, Mousepad Suppression & Emergency Abort

- **Physical Mouse/Touchpad Suppression (`WH_MOUSE_LL`):** During active clicking, moving, typing, and dragging, physical mousepad contact and external mouse movements are automatically dropped so resting hands or accidental nudges cannot derail coordinate targeting. Synthetic agent actions execute cleanly without requiring administrator privileges.
- **Safety Watchdog:** If an action completes or encounters an error, mousepad control is restored immediately. An automatic 15-second watchdog timer prevents the mouse from ever staying locked.
- **Emergency Abort Sequences:** Press **`Esc` three times quickly** (`Triple-Esc`), **`Pause / Break`**, or **`Ctrl+Shift+Esc`** / **`Ctrl+Alt+Esc`** to immediately cancel running input actions, restore physical mouse control, and stop the agent.
- **Approval Gate:** `weightless-windows-tester` is instructed to pause, refocus Antigravity chat, and request explicit confirmation before changing Windows/OS settings, app configs, credentials, online account actions, or deleting/overwriting files.

---

## License

MIT License — see [LICENSE](LICENSE).
