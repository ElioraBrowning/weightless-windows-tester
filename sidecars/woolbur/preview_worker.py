"""Persistent lightweight preview renderer for Woolbur's Antigravity sidebar.

Reads line-delimited JSON render requests from stdin and prints PNG base64
using the exact same server/woolbur_sprites.py renderer as the desktop overlay.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT_DIR / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from woolbur_sprites import render_woolbur_frame, resolve_appearance  # noqa: E402


def main() -> None:
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        req_id = None
        try:
            req = json.loads(raw)
            req_id = req.get("id")
            size = max(48, min(320, int(req.get("size", 180))))
            acc_set = str(req.get("accessory_set", "auto") or "auto").strip().lower()
            mood_in = str(req.get("mood", "normal") or "normal").strip().lower()
            frame = int(req.get("frame", 0)) % 8
            wool_color = str(req.get("wool_color", "#fffbf5") or "#fffbf5").strip().lower()
            horn_color = str(req.get("horn_color", "#fde68a") or "#fde68a").strip().lower()

            expr, resolved_acc = resolve_appearance(
                "intent" if acc_set == "auto" else "acting",
                mood_override=mood_in if mood_in in ("happy", "error") else None,
                accessory_set=acc_set,
            )
            img = render_woolbur_frame(
                size,
                mood=expr,
                anim_frame=frame,
                facing=1,
                wool_color=wool_color,
                horn_color=horn_color,
                accessory_set=resolved_acc,
            )
            buf = io.BytesIO()
            img.save(buf, format="PNG", compress_level=1)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            out = {"id": req_id, "png_b64": b64}
        except Exception as exc:
            out = {"id": req_id, "error": str(exc)}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
