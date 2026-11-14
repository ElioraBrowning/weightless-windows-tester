"""Portable self-bootstrapping launcher for the desktop-vision MCP server.

Creates an isolated virtual environment under DESKTOP_TESTER_DATA_DIR (defaulting
to ~/.local_desktop_agent) on first run, installs requirements, and executes
server/server.py while keeping stdout strictly reserved for MCP JSON-RPC traffic.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = PLUGIN_ROOT / "server" / "server.py"
REQUIREMENTS_TXT = PLUGIN_ROOT / "requirements.txt"
DATA_DIR = Path(
    os.environ.get("DESKTOP_TESTER_DATA_DIR", Path.home() / ".local_desktop_agent")
).expanduser().resolve()
VENV_DIR = DATA_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
DEPS_MARKER = VENV_DIR / ".deps_ok_v1"


def _ensure_environment() -> None:
    if sys.platform != "win32":
        print(
            "Error: desktop-tester MCP server requires Windows 10/11.",
            file=sys.stderr,
        )
        sys.exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not VENV_PYTHON.exists():
        print(
            f"[desktop-tester] Creating isolated Python venv at {VENV_DIR}...",
            file=sys.stderr,
        )
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            check=True,
        )

    if not DEPS_MARKER.exists():
        print(
            "[desktop-tester] Installing required Python dependencies...",
            file=sys.stderr,
        )
        subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            check=False,
        )
        subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "install", "--quiet", "-r", str(REQUIREMENTS_TXT)],
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            check=True,
        )
        DEPS_MARKER.write_text("ok\n", encoding="utf-8")
        print("[desktop-tester] Environment ready.", file=sys.stderr)


def main() -> int:
    _ensure_environment()
    env = os.environ.copy()
    env["DESKTOP_TESTER_DATA_DIR"] = str(DATA_DIR)
    proc = subprocess.run(
        [str(VENV_PYTHON), str(SERVER_PY)],
        env=env,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
