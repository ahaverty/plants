"""The phone notification's picture: every due plant's photo in one labelled grid, in the same
order as the notification's text list, so you can tell which plant is which."""

from __future__ import annotations

import hashlib
import io
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH = 1000          # canvas width; photos are ~250-900px so bigger only adds blur
MAX_TILE = 500
GAP = 6
BG = (18, 22, 30)
PLACEHOLDER = (44, 58, 52)

# Square tiles. Collapsed Android big-picture crops to about 2:1, expanded Samsung shows it all,
# so stay between 1:1 and 3:1 and keep tiles large enough for the labels to read.
LAYOUTS = {1: (1, 1), 2: (2, 1), 3: (3, 1), 4: (2, 2), 5: (3, 2), 6: (3, 2), 7: (4, 2), 8: (4, 2)}

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "C:/Windows/Fonts"]
BOLD = ["DejaVuSans-Bold.ttf", "arialbd.ttf"]
REGULAR = ["DejaVuSans.ttf", "arial.ttf"]


def layout(n: int) -> tuple[int, int]:
    if n in LAYOUTS:
        return LAYOUTS[n]
    return 4, math.ceil(n / 4)


def _font(names: list[str], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for d in FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size=size)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, names: list[str], size: int, max_w: int):
    """Largest font (down to 60% of size) that fits, then ellipsis if it still doesn't."""
    for s in range(size, int(size * 0.6) - 1, -2):
        f = _font(names, s)
        if draw.textlength(text, font=f) <= max_w:
            return text, f
    while len(text) > 1 and draw.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return text.rstrip() + "…", f


def due_items(board: dict) -> list[dict]:
    """Due plants in the notification's order (room route order, then plant order)."""
    out = []
    for r in board["rooms"]:
        for s in r["plants"]:
            if s["id"] in r["due"]:
                out.append({"id": s["id"], "name": s["name"], "room": r["name"],
                            "photo": s.get("photo"), "todo": s.get("todo", [])})
    return out


def version(items: list[dict]) -> str:
    """Cache-buster for the image URL: changes when the plants or their photos change."""
    key = "|".join(f"{i['id']}:{i.get('photo') or ''}" for i in items)
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def _name_lines(draw, text: str, size: int, max_w: int):
    """Name at full size on one line, else wrapped onto two, else shrunk/ellipsised."""
    f = _font(BOLD, size)
    if draw.textlength(text, font=f) <= max_w:
        return [text], f
    words = text.split()
    splits = [(" ".join(words[:c]), " ".join(words[c:])) for c in range(len(words) - 1, 0, -1)]
    sizes = [size, int(size * 0.88), int(size * 0.76)]
    for s in sizes:   # a split where both lines fit, as large as possible
        f = _font(BOLD, s)
        for a, b in splits:
            if draw.textlength(a, font=f) <= max_w and draw.textlength(b, font=f) <= max_w:
                return [a, b], f
    f = _font(BOLD, sizes[-1])   # still too long: longest first line, ellipsise the second
    for a, b in splits:
        if draw.textlength(a, font=f) <= max_w:
            while draw.textlength(b, font=f) > max_w and len(b) > 1:
                b = b[:-2].rstrip() + "…"
            return [a, b], f
    t, f = _fit_text(draw, text, BOLD, size, max_w)
    return [t], f


def _tile(photo: Path | None, t: int) -> Image.Image:
    if photo and photo.is_file():
        try:
            img = ImageOps.exif_transpose(Image.open(photo)).convert("RGB")
            return ImageOps.fit(img, (t, t), Image.LANCZOS)
        except Exception:
            pass
    tile = Image.new("RGB", (t, t), PLACEHOLDER)
    d = ImageDraw.Draw(tile)
    f = _font(BOLD, t // 6)
    d.text((t / 2, t * 0.38), "no photo", font=f, fill=(150, 175, 160), anchor="mm")
    return tile


def render(items: list[dict], photo_dir: Path, fmt: str = "JPEG") -> bytes:
    n = max(len(items), 1)
    cols, rows = layout(n)
    t = min(MAX_TILE, (WIDTH - GAP * (cols - 1)) // cols)
    w, h = cols * t + GAP * (cols - 1), rows * t + GAP * (rows - 1)
    canvas = Image.new("RGB", (w, h), BG)

    name_size = max(18, t // 9)
    room_size = max(14, int(name_size * 0.72))
    pad = max(8, t // 28)
    for i, it in enumerate(items):
        x, y = (i % cols) * (t + GAP), (i // cols) * (t + GAP)
        tile = _tile(photo_dir / it["photo"] if it.get("photo") else None, t).convert("RGBA")
        d = ImageDraw.Draw(tile)
        lines, nf = _name_lines(d, it["name"], name_size, t - 2 * pad)
        lh = int(nf.size * 1.1)
        band_h = lh * len(lines) + room_size + pad * 3
        band = Image.new("RGBA", (t, band_h), (0, 0, 0, 0))
        bd = ImageDraw.Draw(band)
        for yy in range(band_h):   # fade in over the first third, then solid dark
            a = int(210 * min(1.0, yy / (band_h * 0.35)))
            bd.line([(0, yy), (t, yy)], fill=(8, 10, 14, a))
        tile.alpha_composite(band, (0, t - band_h))
        d = ImageDraw.Draw(tile)
        room = it["room"] + (" · feed" if "feed" in it.get("todo", []) else "")
        room, rf = _fit_text(d, room, REGULAR, room_size, t - 2 * pad)
        base = t - pad - room_size - pad // 2
        for k, line in enumerate(lines):
            d.text((pad, base - (len(lines) - 1 - k) * lh), line, font=nf, fill=(255, 255, 255),
                   anchor="ls")
        d.text((pad, t - pad), room, font=rf, fill=(190, 215, 200), anchor="ls")
        # number in the corner, matching the reading order of the text list
        cf = _font(BOLD, max(14, t // 12))
        r = max(14, t // 14)
        d.ellipse([pad, pad, pad + 2 * r, pad + 2 * r], fill=(8, 10, 14, 190))
        d.text((pad + r, pad + r), str(i + 1), font=cf, fill=(255, 255, 255), anchor="mm")
        canvas.paste(tile.convert("RGB"), (x, y))

    buf = io.BytesIO()
    canvas.save(buf, fmt, quality=85) if fmt == "JPEG" else canvas.save(buf, fmt)
    return buf.getvalue()
