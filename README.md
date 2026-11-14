# Desktop Vision & Control Tester (`desktop-tester`)

An Antigravity plugin providing Windows 10/11 OS-level desktop vision, **100% offline Windows native OCR (`Windows.Media.Ocr`)**, multi-window focus management, cursor/keyboard control, clipboard utilities, multi-strategy file drag-and-drop, and a dedicated **`desktop-tester`** automation subagent.

> **Platform Requirement:** Windows 10 / Windows 11 (`x64`) with Python 3.10+ installed on `PATH`.

---

## Features

- **Per-Monitor v2 DPI Awareness:** Coordinates across single and multi-monitor setups match physical screen pixels without scaling drift.
- **Gridded Visual Grounding (`take_screenshot`):** Overlays coordinate grid lines and global `(x, y)` pixel labels directly on saved PNG captures, and tracks cumulative screenshot storage with automatic alerts every `1 GiB`.
- **100% Offline Windows Native OCR (`read_text_from_screen`):** Uses built-in `Windows.Media.Ocr` (via `winrt-Windows.Media.Ocr`) to return line- and word-level text with global bounding boxes `(x, y, w, h)` and pre-computed click centers `(center_x, center_y)`.
- **Reliable Window Control (`list_windows`, `focus_window`):** Enumerates visible windows (with title and executable `process_name`), restores minimized windows, and bypasses Windows foreground focus locks using `AttachThreadInput`.
- **Cursor, Keyboard, Clipboard & File Drop:** Unicode `SendInput` typing, single keys/hotkeys, clipboard get/set, and `drag_and_drop_file` supporting `wm_dropfiles`, `explorer` (`SHOpenFolderAndSelectItems` physical drag), and `clipboard` (`CF_HDROP`) methods.
- **Hardware Failsafe & Emergency Abort:** Disables PyAutoGUI corner failsafe (preventing false triggers from mouse drift) and registers a hardware abort hotkey (**`Pause`**, **`Ctrl+Shift+Esc`**, or **`Ctrl+Alt+Esc`**) to cancel running input sequences immediately.
- **Dedicated `desktop-tester` Subagent:** Enforces an **Observe–Plan–Act–Verify** loop, automatically refocuses the **Antigravity** chat window whenever finishing a task or pausing for user input, and requires explicit chat confirmation before any sensitive system, config, credential, online-account, or destructive file action.

---

## Installation

### Option 1: PowerShell Installer (Recommended)
Clone this repository and run [`install.ps1`](install.ps1):

```powershell
git clone <your-repo-url> desktop-tester
cd desktop-tester
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

This installs the plugin into `%USERPROFILE%\.gemini\config\plugins\desktop-tester`, pre-creates an isolated virtual environment at `%USERPROFILE%\.local_desktop_agent\.venv`, and verifies the MCP stdio handshake.

### Option 2: Portable Direct Plugin Copy
Copy (or clone) this folder directly to `%USERPROFILE%\.gemini\config\plugins\desktop-tester`. On first MCP startup, `scripts/launcher.py` automatically creates `%USERPROFILE%\.local_desktop_agent\.venv` and installs `requirements.txt` (writing bootstrap logs to `stderr` so MCP `stdout` stays clean).

You can override the runtime storage location (venv, screenshots, logs) by setting the `DESKTOP_TESTER_DATA_DIR` environment variable.

---

## Usage Examples

```text
@desktop-tester Focus the Calculator window, take a gridded screenshot of monitor 0, locate buttons via Windows OCR, compute 7 * 6, and verify the result visually.
```

```text
@desktop-tester List all open windows, focus Notepad, paste the test snippet into a new empty tab, and verify with a follow-up screenshot without saving.
```

---

## Repository Structure

```text
desktop-tester/
├── plugin.json                        # Antigravity plugin manifest
├── mcp_config.json                    # Portable stdio MCP entry -> scripts/launcher.py
├── requirements.txt                   # Python dependencies (Windows winrt + UI automation)
├── install.ps1                        # Global plugin installer & environment verifier
├── assets/
│   └── logo.svg                       # Plugin icon
├── agents/
│   └── desktop-tester.md              # Subagent persona, loop, and safety protocol
├── skills/
│   └── desktop-testing/
│       └── SKILL.md                   # Tool workflow reference skill
├── scripts/
│   └── launcher.py                    # Self-bootstrapping portable MCP launcher
└── server/
    └── server.py                      # MCPServer ("desktop-vision") implementation
```

---

## Safety & Emergency Abort

- **Corner Failsafe Disabled:** Resting or bumped cursors at screen corners (`(0, 0)`) will not interrupt tests.
- **Emergency Abort Hotkey:** Press **`Pause`**, **`Ctrl+Shift+Esc`**, or **`Ctrl+Alt+Esc`** at any time to immediately halt an active typing, clicking, or dragging sequence.
- **Approval Gate:** `desktop-tester` is instructed to pause and request explicit confirmation before changing Windows/OS settings, app configs, credentials, online account actions, or deleting/overwriting files.

---

## License

MIT License — see [LICENSE](LICENSE).
