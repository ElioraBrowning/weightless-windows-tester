"""Flat cel-shaded cloud-loaf Woolbur sprite renderer.

Draws a squishy wide cloud-mound sheep with layered depth:
1. Back hooves & wide flattened cloud-mound body (cel-shaded warm outlines)
2. Ridged, curled ram horns (in front of the cloud body, behind the head)
3. Soft-bordered head-wool fringe, forehead bangs, and wide squishy mochi face
4. Selected accessory set fitted seamlessly to the head/neck contour
"""

from __future__ import annotations

import math
import threading
from typing import Optional

from PIL import Image, ImageDraw

from woolbur_store import parse_hex_rgba


# Warm cocoa-brown flat-vector ink & default palettes
INK = (72, 48, 42, 255)
WOOL_BASE = (255, 251, 245, 255)
WOOL_SHADE = (242, 228, 222, 255)
WOOL_HIGHLIGHT = (255, 255, 255, 255)
WOOL_CURL = (224, 206, 200, 200)
HORN_BASE = (253, 230, 138, 255)

FACE_BASE = (255, 245, 238, 255)
FACE_SHADE = (248, 229, 218, 255)
EYE_DARK = (60, 40, 36, 255)
HOOF_BASE = (122, 86, 78, 255)

CHEEK_PINK = (252, 184, 184, 195)
BLUSH_TICK = (238, 136, 142, 195)
PINK_ACCENT = (244, 114, 182, 255)
HOT_PINK = (236, 72, 153, 255)
LAVENDER_ACCENT = (167, 139, 250, 255)
AMBER_ACCENT = (251, 191, 36, 255)
MINT_ACCENT = (52, 211, 153, 255)
SKY_ACCENT = (56, 189, 248, 255)
SILVER = (232, 236, 248, 255)
LEATHER_DARK = (46, 32, 46, 255)
GOLD_PENCIL = (251, 204, 88, 255)
WOOD_PENCIL = (244, 208, 168, 255)
CAP_CARAMEL = (186, 134, 88, 255)
CAP_SHADE = (138, 94, 58, 255)
TORTOISE_RIM = (214, 144, 78, 255)

VALID_ACCESSORY_SETS = frozenset({"nerd", "detective", "punk", "goth", "happy", "none"})

MOOD_MAP = {
    "thinking": "nerd",
    "intent": "nerd",
    "detective": "detective",
    "acting": "punk",
    "assist": "goth",
    "question": "goth",
    "error": "goth",
    "complete": "happy",
}

_SPRITE_CACHE: dict[tuple[str, str, int, int, bool, str, str], Image.Image] = {}
_SPRITE_CACHE_LOCK = threading.Lock()


def _mix_rgba(
    c1: tuple[int, int, int, int],
    c2: tuple[int, int, int, int],
    t: float,
    alpha: Optional[int] = None,
) -> tuple[int, int, int, int]:
    r = int(round(c1[0] * (1.0 - t) + c2[0] * t))
    g = int(round(c1[1] * (1.0 - t) + c2[1] * t))
    b = int(round(c1[2] * (1.0 - t) + c2[2] * t))
    a = c1[3] if alpha is None else int(alpha)
    return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)), max(0, min(255, a)))


def _resolve_wool_palette(
    wool_color: Optional[str],
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int], tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Return (base, shade, highlight, curl) RGBA colors for Woolbur's cloud wool."""
    raw = str(wool_color or "").strip().lower()
    if not raw or raw == "#fffbf5":
        return WOOL_BASE, WOOL_SHADE, WOOL_HIGHLIGHT, WOOL_CURL
    base = parse_hex_rgba(raw, WOOL_BASE[:3], 255)
    shade = _mix_rgba(base, (150, 122, 134, 255), 0.18, 255)
    highlight = _mix_rgba(base, (255, 255, 255, 255), 0.55, 255)
    curl = _mix_rgba(base, (124, 96, 106, 255), 0.28, 210)
    return base, shade, highlight, curl


def _resolve_horn_palette(
    horn_color: Optional[str],
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Return (base, shade, highlight) RGBA colors for Woolbur's curled ram horns."""
    raw = str(horn_color or "").strip().lower()
    base = HORN_BASE if (not raw or raw == "#fde68a") else parse_hex_rgba(raw, HORN_BASE[:3], 255)
    shade = _mix_rgba(base, (124, 82, 62, 255), 0.24, 255)
    highlight = _mix_rgba(base, (255, 255, 255, 255), 0.50, 255)
    return base, shade, highlight


def resolve_appearance(
    status_type: str,
    mood_override: Optional[str] = None,
    accessory_set: Optional[str] = None,
    action_hint: str = "",
) -> tuple[str, str]:
    """Return (expression, accessories) from live task state and user sidebar settings."""
    if status_type == "error":
        expr = "error"
    elif status_type == "complete":
        expr = "happy"
    else:
        expr = "normal"

    acc_in = str(accessory_set or "").strip().lower()
    if acc_in in VALID_ACCESSORY_SETS:
        return expr, acc_in

    mood_in = str(mood_override or "").strip().lower()
    if mood_in in VALID_ACCESSORY_SETS:
        if expr == "normal" and mood_in == "happy":
            expr = "happy"
        return expr, mood_in
    if mood_in == "error":
        return "error", "goth"

    hint = action_hint.lower()
    if status_type == "acting" and any(
        kw in hint for kw in ("screenshot", "ocr", "reading text", "listing", "checking screen", "inspect")
    ):
        return expr, "detective"
    return expr, MOOD_MAP.get(status_type, "nerd")


def resolve_mood(status_type: str, mood_override: Optional[str] = None, action_hint: str = "") -> str:
    """Backwards-compatible single-string mood resolver."""
    expr, acc = resolve_appearance(status_type, mood_override=mood_override, action_hint=action_hint)
    if expr == "error":
        return "error"
    return acc if acc != "none" else "happy"


def _circle(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    r: float,
    fill: tuple[int, int, int, int],
    outline: Optional[tuple[int, int, int, int]] = None,
    width: int = 1,
) -> None:
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill, outline=outline, width=width)


def _oval(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    fill: tuple[int, int, int, int],
    outline: Optional[tuple[int, int, int, int]] = None,
    width: int = 1,
) -> None:
    draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=fill, outline=outline, width=width)


def _rotated_oval_poly(
    cx: float, cy: float, rx: float, ry: float, angle_deg: float, steps: int = 28
) -> list[tuple[float, float]]:
    ang = math.radians(angle_deg)
    ca, sa = math.cos(ang), math.sin(ang)
    pts: list[tuple[float, float]] = []
    for i in range(steps):
        t = (2.0 * math.pi * i) / steps
        lx = rx * math.cos(t)
        ly = ry * math.sin(t)
        pts.append((cx + lx * ca - ly * sa, cy + lx * sa + ly * ca))
    return pts


def _draw_rotated_oval(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    angle_deg: float,
    fill: tuple[int, int, int, int],
    outline: Optional[tuple[int, int, int, int]] = None,
    width: int = 1,
) -> None:
    pts = _rotated_oval_poly(cx, cy, rx, ry, angle_deg)
    draw.polygon(pts, fill=fill)
    if outline is not None and width > 0:
        draw.line(pts + [pts[0]], fill=outline, width=width, joint="curve")


def _draw_star(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    outer_r: float,
    inner_r: float,
    points: int,
    fill: tuple[int, int, int, int],
    outline: Optional[tuple[int, int, int, int]] = None,
    width: int = 4,
    rot_deg: float = -90.0,
) -> None:
    verts: list[tuple[float, float]] = []
    for i in range(points * 2):
        r = outer_r if i % 2 == 0 else inner_r
        ang = math.radians(rot_deg + (180.0 * i) / points)
        verts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    draw.polygon(verts, fill=fill)
    if outline is not None and width > 0:
        draw.line(verts + [verts[0]], fill=outline, width=width, joint="curve")


def _draw_heart(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    scale: float,
    fill: tuple[int, int, int, int],
    outline: Optional[tuple[int, int, int, int]] = None,
    width: int = 4,
) -> None:
    pts: list[tuple[float, float]] = []
    for i in range(28):
        t = (2.0 * math.pi * i) / 28.0
        hx = 16.0 * (math.sin(t) ** 3)
        hy = -(13.0 * math.cos(t) - 5.0 * math.cos(2 * t) - 2.0 * math.cos(3 * t) - math.cos(4 * t))
        pts.append((cx + hx * scale, cy + hy * scale))
    draw.polygon(pts, fill=fill)
    if outline is not None and width > 0:
        draw.line(pts + [pts[0]], fill=outline, width=width, joint="curve")


def _draw_segmented_horns(
    draw: ImageDraw.ImageDraw,
    head_cx: float,
    head_cy: float,
    horn_base: tuple[int, int, int, int],
    horn_shade: tuple[int, int, int, int],
    horn_hi: tuple[int, int, int, int],
) -> None:
    """Draw tight spiral segmented ram horns IN FRONT of the flat cloud body & BEHIND the head."""
    n_pts = 28
    # Slightly asymmetric cozy 3/4 curl framing head_cx
    configs = (
        (-1.0, head_cx - 64.0, head_cy - 12.0, 29.0, 17.0),
        (1.0, head_cx + 68.0, head_cy - 10.0, 32.0, 18.5),
    )
    for sign, scx, scy, spiral_base_r, start_hw in configs:
        outer_pts: list[tuple[float, float]] = []
        inner_pts: list[tuple[float, float]] = []
        mid_pts: list[tuple[float, float, float, float]] = []

        for i in range(n_pts):
            t = i / (n_pts - 1)
            theta = math.radians(-122.0 + 305.0 * t)
            spiral_r = spiral_base_r - 9.5 * (t ** 1.15)
            half_w = start_hw * (1.0 - 0.60 * (t ** 0.9))

            px = scx + sign * (spiral_r * math.cos(theta))
            py = scy + (spiral_r * 0.86 * math.sin(theta))
            nx = sign * math.cos(theta)
            ny = math.sin(theta)

            outer_pts.append((px + nx * half_w, py + ny * half_w))
            inner_pts.append((px - nx * half_w, py - ny * half_w))
            mid_pts.append((px, py, nx, half_w))

        poly = outer_pts + list(reversed(inner_pts))
        draw.polygon(poly, fill=horn_base)

        shade_inner: list[tuple[float, float]] = []
        for i in range(n_pts):
            t = i / (n_pts - 1)
            px, py, nx, hw = mid_pts[i]
            theta = math.radians(-122.0 + 305.0 * t)
            ny = math.sin(theta)
            shade_inner.append((px + nx * hw * 0.10, py + ny * hw * 0.10 + 1.5))
        draw.polygon(shade_inner + list(reversed(inner_pts)), fill=horn_shade)

        for i in range(4, 12):
            px, py, nx, hw = mid_pts[i]
            t = i / (n_pts - 1)
            theta = math.radians(-122.0 + 305.0 * t)
            ny = math.sin(theta)
            _circle(draw, px + nx * hw * 0.40, py + ny * hw * 0.40, max(2.2, hw * 0.21), horn_hi)

        for seg_idx in (6, 11, 16, 21):
            t = seg_idx / (n_pts - 1)
            theta = math.radians(-122.0 + 305.0 * t)
            px, py, nx, hw = mid_pts[seg_idx]
            ny = math.sin(theta)
            tx, ty = -ny, nx
            p1 = outer_pts[seg_idx]
            p2 = inner_pts[seg_idx]
            mid_c = (px + tx * hw * 0.24, py + ty * hw * 0.24)
            draw.line([p1, mid_c, p2], fill=INK, width=4, joint="curve")

        draw.line(poly + [poly[0]], fill=INK, width=6, joint="curve")
        end_px, end_py, _, end_hw = mid_pts[-1]
        _circle(draw, end_px, end_py, max(3.5, end_hw), horn_base, outline=INK, width=5)


def _draw_bowtie(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    wing_col: tuple[int, int, int, int],
    knot_col: tuple[int, int, int, int],
    scale: float = 1.0,
) -> None:
    """Draw a flat kawaii ribbon bowtie centered on the chest cloud."""
    for sign in (-1.0, 1.0):
        pts = [
            (cx, cy),
            (cx + sign * 22 * scale, cy - 13 * scale),
            (cx + sign * 26 * scale, cy - 3 * scale),
            (cx + sign * 22 * scale, cy + 4 * scale),
            (cx + sign * 25 * scale, cy + 11 * scale),
            (cx + sign * 18 * scale, cy + 14 * scale),
        ]
        draw.polygon(pts, fill=wing_col)
        draw.line(pts + [pts[0]], fill=INK, width=int(5.5 * scale), joint="curve")
    _oval(draw, cx, cy + 1, 8.5 * scale, 9 * scale, knot_col, outline=INK, width=int(5.5 * scale))


def _draw_bat_wing_clip(draw: ImageDraw.ImageDraw, cx: float, cy: float, s: float = 1.0) -> None:
    """Draw a cute pastel-goth bat-wing bow hairclip."""
    for sign in (-1.0, 1.0):
        wing = [
            (cx, cy),
            (cx + sign * 22 * s, cy - 16 * s),
            (cx + sign * 36 * s, cy - 8 * s),
            (cx + sign * 28 * s, cy + 5 * s),
            (cx + sign * 17 * s, cy + 12 * s),
            (cx + sign * 9 * s, cy + 7 * s),
        ]
        draw.polygon(wing, fill=LEATHER_DARK)
        draw.line(wing + [wing[0]], fill=INK, width=int(5 * s), joint="curve")
        inner = [
            (cx + sign * 6 * s, cy + 1 * s),
            (cx + sign * 20 * s, cy - 10 * s),
            (cx + sign * 27 * s, cy - 4 * s),
            (cx + sign * 17 * s, cy + 4 * s),
        ]
        draw.polygon(inner, fill=(139, 92, 246, 225))
    _oval(draw, cx, cy + 2 * s, 8.5 * s, 8 * s, PINK_ACCENT, outline=INK, width=int(5 * s))


def _draw_nerd_glasses(draw: ImageDraw.ImageDraw, left_ex: float, right_ex: float, eye_y: float) -> None:
    """Draw warm round tortoise glasses centered on wide-set horizontal kawaii eyes."""
    r = 18.5
    draw.line([(left_ex + r - 3, eye_y - 1), (right_ex - r + 3, eye_y - 1)], fill=INK, width=9)
    draw.line([(left_ex + r - 3, eye_y - 1), (right_ex - r + 3, eye_y - 1)], fill=TORTOISE_RIM, width=4)
    draw.line([(left_ex - r - 9, eye_y - 3), (left_ex - r + 2, eye_y - 1)], fill=INK, width=7)
    draw.line([(right_ex + r - 2, eye_y - 1), (right_ex + r + 9, eye_y - 3)], fill=INK, width=7)
    for ex in (left_ex, right_ex):
        draw.ellipse([ex - r - 2, eye_y - r - 2, ex + r + 2, eye_y + r + 2], outline=INK, width=8)
        draw.ellipse([ex - r, eye_y - r, ex + r, eye_y + r], outline=TORTOISE_RIM, width=4)


def _draw_detective_cap(draw: ImageDraw.ImageDraw, cx: float, top_y: float) -> None:
    """Draw a cute tilted caramel deerstalker cap perched atop the head fringe."""
    _draw_rotated_oval(draw, cx - 2, top_y, 38, 19, -7, CAP_CARAMEL, outline=INK, width=7)
    _draw_rotated_oval(draw, cx - 1, top_y + 10, 40, 8, -7, CAP_SHADE, outline=INK, width=6)
    brim = [
        (cx - 40, top_y + 15),
        (cx - 2, top_y + 3),
        (cx + 36, top_y + 7),
        (cx + 10, top_y + 20),
    ]
    draw.polygon(brim, fill=CAP_SHADE)
    draw.line(brim + [brim[0]], fill=INK, width=6, joint="curve")
    _circle(draw, cx - 4, top_y - 18, 7, CAP_SHADE, outline=INK, width=5)


def _draw_punk_headphones(draw: ImageDraw.ImageDraw, head_cx: float, head_cy: float) -> None:
    """Draw pastel studio headphones arching neatly over the head cloud."""
    draw.arc([head_cx - 72, head_cy - 84, head_cx + 72, head_cy + 22], 190, 350, fill=INK, width=16)
    draw.arc([head_cx - 70, head_cy - 82, head_cx + 70, head_cy + 20], 192, 348, fill=SILVER, width=7)
    for sign in (-1.0, 1.0):
        ex = head_cx + sign * 70
        ey = head_cy + 2
        _draw_rotated_oval(draw, ex, ey, 12, 20, sign * -8, HOT_PINK, outline=INK, width=7)
        _draw_rotated_oval(draw, ex + sign * 2, ey, 6, 11, sign * -8, LEATHER_DARK, outline=INK, width=3)


def _draw_punk_collar(
    draw: ImageDraw.ImageDraw,
    head_cx: float,
    neck_y: float,
    collar_rx: float,
    accent: tuple[int, int, int, int],
) -> None:
    """Draw a sleek fitted choker wrapping cleanly across the lower head/cheek collar."""
    pts = []
    # Gentle wide neck curve spanning full cheek-to-cheek width (~2*collar_rx*0.92)
    for i in range(15):
        t = math.pi * (0.12 + 0.76 * (i / 14.0))
        pts.append((head_cx + collar_rx * math.cos(t), neck_y + 13.0 * math.sin(t)))
    draw.line(pts, fill=INK, width=12, joint="curve")
    draw.line(pts, fill=LEATHER_DARK, width=7, joint="curve")

    # Dainty pyramid studs spaced neatly across the full wrap
    for idx in (2, 5, 7, 9, 12):
        sx, sy = pts[idx]
        if idx == 7:
            # Cute central gem/ring pendant
            _circle(draw, sx, sy + 6, 6.5, accent, outline=INK, width=4)
        else:
            stud = [(sx, sy + 5.5), (sx - 4.5, sy - 3.5), (sx + 4.5, sy - 3.5)]
            draw.polygon(stud, fill=SILVER)
            draw.line(stud + [stud[0]], fill=INK, width=3, joint="curve")


def _draw_magnifying_glass(draw: ImageDraw.ImageDraw, mx: float, my: float) -> None:
    """Draw a sky-blue & pink detective magnifying glass beside Woolbur."""
    draw.line([(mx + 16, my + 16), (mx + 42, my + 42)], fill=INK, width=14)
    draw.line([(mx + 16, my + 16), (mx + 42, my + 42)], fill=HOT_PINK, width=7)
    _circle(draw, mx, my, 24, (186, 230, 253, 160))
    draw.ellipse([mx - 24, my - 24, mx + 24, my + 24], outline=INK, width=8)
    draw.ellipse([mx - 20, my - 20, mx + 20, my + 20], outline=PINK_ACCENT, width=4)
    draw.line([(mx - 10, my - 8), (mx - 2, my - 16)], fill=(255, 255, 255, 225), width=4)


def _draw_pencil_behind_ear(draw: ImageDraw.ImageDraw, px: float, py: float) -> None:
    """Draw a tiny retro yellow pencil tucked behind Woolbur's head fluff."""
    ang = math.radians(-30)
    dx, dy = math.cos(ang), math.sin(ang)
    x1, y1 = px - 20 * dx, py - 20 * dy
    x2, y2 = px + 28 * dx, py + 28 * dy
    draw.line([(x1, y1), (x2, y2)], fill=INK, width=14)
    draw.line([(x1, y1), (x2, y2)], fill=GOLD_PENCIL, width=8)
    ex1, ey1 = px + 18 * dx, py + 18 * dy
    draw.line([(ex1, ey1), (x2, y2)], fill=PINK_ACCENT, width=8)
    tip = [(x1 - 9 * dx, y1 - 9 * dy), (x1 - 4 * dy, y1 + 4 * dx), (x1 + 4 * dy, y1 - 4 * dx)]
    draw.polygon(tip, fill=WOOD_PENCIL)


def _draw_flower_crown(draw: ImageDraw.ImageDraw, head_cx: float, cy: float) -> None:
    """Draw a dainty 3-blossom daisy crown nestled on Woolbur's head wool."""
    blossoms = [
        (head_cx - 30, cy + 3, AMBER_ACCENT, WOOL_HIGHLIGHT),
        (head_cx, cy - 3, PINK_ACCENT, AMBER_ACCENT),
        (head_cx + 30, cy + 3, SKY_ACCENT, WOOL_HIGHLIGHT),
    ]
    for fx, fy, petal_col, center_col in blossoms:
        for i in range(5):
            ang = math.radians(i * 72 - 90)
            px = fx + math.cos(ang) * 8.5
            py = fy + math.sin(ang) * 8.5
            _circle(draw, px, py, 7, INK)
        for i in range(5):
            ang = math.radians(i * 72 - 90)
            px = fx + math.cos(ang) * 8.5
            py = fy + math.sin(ang) * 8.5
            _circle(draw, px, py, 5, petal_col)
        _circle(draw, fx, fy, 5, center_col, outline=INK, width=3)


def render_woolbur_frame(
    size_px: int,
    mood: str = "nerd",
    anim_frame: int = 0,
    facing: int = 1,
    wool_color: Optional[str] = None,
    horn_color: Optional[str] = None,
    accessory_set: Optional[str] = None,
) -> Image.Image:
    """Render a wide, flat cel-shaded cloud-loaf Woolbur sprite of target square size_px.

    Layering order:
    1. Tucked hooves & wide flattened cloud-mound body
    2. Curled segmented ram horns (IN FRONT OF the cloud body, BEHIND the head)
    3. Soft-edged head-wool ring, scalloped bangs & wide squished kawaii face
    4. Active accessory set
    """
    bucket = max(48, int(round(size_px / 4.0) * 4))
    phase = int(anim_frame) % 8
    flip = facing < 0
    wool_key = str(wool_color or "").strip().lower() or "#fffbf5"
    horn_key = str(horn_color or "").strip().lower() or "#fde68a"

    acc_norm = str(accessory_set or "").strip().lower()
    mood_norm = str(mood or "nerd").strip().lower()
    if acc_norm in VALID_ACCESSORY_SETS:
        eff_acc = acc_norm
    elif mood_norm in VALID_ACCESSORY_SETS:
        eff_acc = mood_norm
    elif mood_norm == "error":
        eff_acc = "goth"
    else:
        eff_acc = "nerd"

    eff_expr = "error" if mood_norm == "error" else ("happy" if mood_norm == "happy" else "normal")

    wool_base, wool_shade, wool_hi, wool_curl = _resolve_wool_palette(wool_key)
    horn_base, horn_shade, horn_hi = _resolve_horn_palette(horn_key)
    # Soft warm-rose/taupe edge used on interior face & head-wool lines so face melts into cloud
    soft_line = _mix_rgba(INK, wool_base, 0.45, 255)

    cache_key = (eff_expr, eff_acc, bucket, phase, flip, wool_key, horn_key)
    with _SPRITE_CACHE_LOCK:
        cached = _SPRITE_CACHE.get(cache_key)
        if cached is not None:
            return cached

    W = 480
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bob_y = -6.0 * abs(math.sin((phase / 8.0) * math.pi * 2.0))
    trot = math.sin((phase / 8.0) * math.pi * 2.0)
    blink = phase == 7 and eff_expr not in ("error", "happy")

    cx = W * 0.50
    cy = 278.0 + bob_y
    # Cozy slightly-left head offset so right back wool billows out like reference image
    head_cx = cx - 22.0
    head_cy = cy + 16.0

    # Soft wide ground shadow under flattened mound
    shadow_rx = 166 - abs(bob_y) * 1.4
    _oval(draw, cx, 366.0, shadow_rx, 13.0, (54, 40, 62, 48))

    # =========================================================================
    # LAYER 1: Wide Flattened Cloud-Mound Body & Tucked Resting Hooves
    # =========================================================================
    cloud_lobes = [
        # Broad smooth top mound arch (smooth gentle dome, no spiky small bumps)
        (cx - 10, cy - 26, 92),
        (cx - 78, cy - 16, 74),
        (cx + 68, cy - 14, 78),
        # Flared wide lower puddle hips
        (cx - 128, cy + 18, 54),
        (cx - 152, cy + 34, 36),
        (cx + 124, cy + 18, 56),
        (cx + 154, cy + 32, 38),
        # Flat wavy resting bottom contour (y ≈ 318..356)
        (cx - 108, cy + 40, 32),
        (cx - 56, cy + 44, 32),
        (cx - 4, cy + 46, 32),
        (cx + 48, cy + 44, 32),
        (cx + 104, cy + 40, 32),
    ]

    # Tucked curved hooves resting right at the bottom puddle corners
    _draw_rotated_oval(draw, cx - 140, cy + 68 + trot * 3, 21, 13, -24, HOOF_BASE, outline=INK, width=7)
    _draw_rotated_oval(draw, head_cx + 76, cy + 76 - trot * 3, 24, 14, 14, HOOF_BASE, outline=INK, width=7)

    # Pass A: crisp dark cocoa outline
    for px, py, pr in cloud_lobes:
        _circle(draw, px, py, pr + 8.0, INK)
    _oval(draw, cx, cy + 2, 162, 68, INK)

    # Pass B: full silhouette cel-shade base
    for px, py, pr in cloud_lobes:
        _circle(draw, px, py, pr, wool_shade)
    _oval(draw, cx, cy + 2, 154, 62, wool_shade)

    # Pass C: slightly shifted up-left wool_base for crisp flat bottom shadow band
    for px, py, pr in cloud_lobes:
        _circle(draw, px - 5, py - 10, max(10.0, pr - 4.0), wool_base)
    _oval(draw, cx - 4, cy - 8, 150, 58, wool_base)

    # Soft cloud top sheen + delicate subtle wool-curl accents on right back fluff
    _oval(draw, cx - 18, cy - 92, 52, 16, wool_hi)
    draw.arc([cx + 70, cy - 58, cx + 108, cy - 26], 205, 335, fill=wool_curl, width=4)
    draw.arc([cx + 114, cy + 16, cx + 136, cy + 36], -45, 160, fill=wool_curl, width=4)

    # =========================================================================
    # LAYER 2: Curled Segmented Horns (IN FRONT OF cloud body, BEHIND head!)
    # =========================================================================
    _draw_segmented_horns(draw, head_cx, head_cy, horn_base, horn_shade, horn_hi)

    # =========================================================================
    # LAYER 3: Soft-Bordered Head Wool Frame, Bangs & Wide Squished Face
    # =========================================================================
    head_wool = [
        (head_cx - 52, head_cy - 22, 19),
        (head_cx + 52, head_cy - 22, 19),
        (head_cx - 64, head_cy + 2, 19),
        (head_cx + 64, head_cy + 2, 19),
        (head_cx - 56, head_cy + 24, 18),
        (head_cx + 56, head_cy + 24, 18),
        (head_cx - 28, head_cy + 34, 18),
        (head_cx, head_cy + 36, 20),
        (head_cx + 28, head_cy + 34, 18),
        (head_cx - 26, head_cy - 36, 20),
        (head_cx + 26, head_cy - 36, 20),
        (head_cx, head_cy - 40, 22),
    ]
    # Soft pastel outline around the inner face-wool collar so head blends into the cloud puff
    for hx, hy, hr in head_wool:
        _circle(draw, hx, hy, hr + 5.0, soft_line)
    for hx, hy, hr in head_wool:
        _circle(draw, hx, hy, hr, wool_base)

    # Pencil behind head-fluff (Nerd accessory, tucked behind face outline)
    if eff_acc == "nerd":
        _draw_pencil_behind_ear(draw, head_cx + 54, head_cy - 40)

    # Wide squished kawaii mochi face nestled low inside the head wool ring
    _oval(draw, head_cx, head_cy + 7, 54, 29, FACE_BASE, outline=soft_line, width=5)
    draw.arc([head_cx - 48, head_cy - 12, head_cx + 48, head_cy + 34], 18, 162, fill=FACE_SHADE, width=5)

    # Scalloped bangs across top edge of forehead
    bang_puffs = [
        (head_cx - 36, head_cy - 21, 15),
        (head_cx - 12, head_cy - 24, 16),
        (head_cx + 12, head_cy - 24, 16),
        (head_cx + 36, head_cy - 21, 15),
    ]
    for bx, by, br in bang_puffs:
        _circle(draw, bx, by, br + 4.5, soft_line)
    for bx, by, br in bang_puffs:
        _circle(draw, bx, by, br, wool_base)
    _oval(draw, head_cx, head_cy - 33, 50, 17, wool_base)

    # --- 3b. Wide-Set Squishy Horizontal Eyes, Tiny Nose, Dainty ':3' Smile ---
    left_ex = head_cx - 31
    right_ex = head_cx + 31
    eye_y = head_cy + 7
    cheek_y = head_cy + 16

    # Soft subtle coral blush cheeks
    for chx in (head_cx - 40, head_cx + 40):
        _oval(draw, chx, cheek_y, 10, 5.5, CHEEK_PINK)

    if eff_expr == "error":
        for ex, sgn in ((left_ex, 1), (right_ex, -1)):
            pts = [(ex - 7 * sgn, eye_y - 6), (ex + 5 * sgn, eye_y), (ex - 7 * sgn, eye_y + 6)]
            draw.line(pts, fill=EYE_DARK, width=5, joint="curve")
        draw.arc([head_cx - 9, head_cy + 12, head_cx, head_cy + 20], 180, 360, fill=EYE_DARK, width=4)
        draw.arc([head_cx, head_cy + 12, head_cx + 9, head_cy + 20], 0, 180, fill=EYE_DARK, width=4)
        drop = [(head_cx + 45, head_cy - 16), (head_cx + 39, head_cy - 2), (head_cx + 51, head_cy - 2)]
        draw.polygon(drop, fill=SKY_ACCENT)
        _circle(draw, head_cx + 45, head_cy - 2, 6, SKY_ACCENT)
        draw.line([(head_cx + 45, head_cy - 16), (head_cx + 39, head_cy - 2)], fill=INK, width=3)
    elif eff_expr == "happy":
        for ex in (left_ex, right_ex):
            draw.arc([ex - 9, eye_y - 6, ex + 9, eye_y + 7], 195, 345, fill=EYE_DARK, width=5)
        draw.arc([head_cx - 9, head_cy + 9, head_cx, head_cy + 18], 0, 180, fill=EYE_DARK, width=4)
        draw.arc([head_cx, head_cy + 9, head_cx + 9, head_cy + 18], 0, 180, fill=EYE_DARK, width=4)
    elif blink:
        for ex in (left_ex, right_ex):
            draw.arc([ex - 8, eye_y - 2, ex + 8, eye_y + 6], 10, 170, fill=EYE_DARK, width=5)
        draw.arc([head_cx - 8, head_cy + 10, head_cx, head_cy + 18], 0, 180, fill=EYE_DARK, width=4)
        draw.arc([head_cx, head_cy + 10, head_cx + 8, head_cy + 18], 0, 180, fill=EYE_DARK, width=4)
    else:
        for ex in (left_ex, right_ex):
            _oval(draw, ex, eye_y, 8.5, 6.2, EYE_DARK)
            _circle(draw, ex - 2.5, eye_y - 1.8, 2.3, WOOL_HIGHLIGHT)
        if eff_acc == "goth":
            _oval(draw, head_cx, head_cy + 14, 4.5, 5.2, PINK_ACCENT, outline=EYE_DARK, width=3)
        else:
            _oval(draw, head_cx, head_cy + 7.5, 2.8, 2.0, EYE_DARK)
            draw.arc([head_cx - 8.5, head_cy + 9, head_cx, head_cy + 17], 0, 180, fill=EYE_DARK, width=4)
            draw.arc([head_cx, head_cy + 9, head_cx + 8.5, head_cy + 17], 0, 180, fill=EYE_DARK, width=4)

    # =========================================================================
    # LAYER 4: Selected Accessory Set (100% controlled by user's choice!)
    # =========================================================================
    if eff_acc == "nerd":
        _draw_nerd_glasses(draw, left_ex, right_ex, eye_y)
        _draw_bowtie(draw, head_cx, head_cy + 46, SKY_ACCENT, AMBER_ACCENT, scale=0.84)
    elif eff_acc == "detective":
        _draw_detective_cap(draw, head_cx, head_cy - 62)
        _draw_nerd_glasses(draw, left_ex, right_ex, eye_y)
        _draw_bowtie(draw, head_cx, head_cy + 46, CAP_CARAMEL, MINT_ACCENT, scale=0.84)
        _draw_magnifying_glass(draw, head_cx + 126, cy + 28)
    elif eff_acc == "punk":
        _draw_punk_headphones(draw, head_cx, head_cy)
        _draw_punk_collar(draw, head_cx, head_cy + 31, 68, HOT_PINK)
        _draw_star(draw, head_cx - 24, head_cy - 40, 14, 6.5, 5, HOT_PINK, outline=INK, width=5, rot_deg=-18)
        _circle(draw, head_cx - 24, head_cy - 40, 2.6, WOOL_HIGHLIGHT)
    elif eff_acc == "goth":
        _draw_punk_collar(draw, head_cx, head_cy + 31, 68, LAVENDER_ACCENT)
        _draw_bat_wing_clip(draw, head_cx - 20, head_cy - 46, s=0.86)
    elif eff_acc == "happy":
        _draw_flower_crown(draw, head_cx, head_cy - 48)
        _draw_bowtie(draw, head_cx, head_cy + 46, PINK_ACCENT, AMBER_ACCENT, scale=0.88)
        _draw_star(draw, cx - 150, cy - 70, 14, 5.0, 4, AMBER_ACCENT, outline=INK, width=4, rot_deg=0)
        _draw_star(draw, cx + 148, cy - 68, 13, 4.5, 4, AMBER_ACCENT, outline=INK, width=4, rot_deg=0)
        _draw_heart(draw, cx - 166, cy - 16, 0.78, (253, 186, 168, 255), outline=INK, width=4)
        _draw_heart(draw, cx + 162, cy + 44, 0.75, PINK_ACCENT, outline=INK, width=4)

    if flip:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

    resized = img.resize((bucket, bucket), Image.Resampling.LANCZOS)
    with _SPRITE_CACHE_LOCK:
        if len(_SPRITE_CACHE) > 320:
            _SPRITE_CACHE.clear()
        _SPRITE_CACHE[cache_key] = resized
    return resized
