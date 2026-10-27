"""Plant photos: shrunk to a phone-friendly JPEG and stored under data/photos.

A new photo never replaces the old file, so every photo a plant has had is kept in the
`photos` table as its progress history. New photos are queued for the AI look-over
(worker/photo_worker.py on the PC)."""

from __future__ import annotations

import io
import re
import time
from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageOps

MAX_PX = 900
NUDGE_DAYS = 180     # the plant page suggests a fresh photo once the newest is this old


def save(photo_dir: Path, plant_id: int, raw: bytes) -> str:
    photo_dir.mkdir(parents=True, exist_ok=True)
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    img.thumbnail((MAX_PX, MAX_PX))
    name = f"plant-{plant_id}-{int(time.time())}.jpg"
    img.save(photo_dir / name, "JPEG", quality=82)
    return name


def add(store, photo_dir: Path, plant_id: int, raw: bytes) -> str:
    """Save a new photo, make it the plant's main one and queue it for the AI look-over."""
    name = save(photo_dir, plant_id, raw)
    store.update_plant(plant_id, photo=name)
    store.add_photo(plant_id, name, date.today())
    return name


def backfill(store, photo_dir: Path) -> int:
    """One-off (23 Oct 2026): build the history from the files already on disk. Uploads were
    always kept as plant-<id>-<unix time>.jpg; the seeded photos only exist as a plant's
    current photo."""
    if store.get("photos_backfilled"):
        return 0
    n = 0
    plants = {p["id"]: p for p in store.plants(include_archived=True)}
    for f in sorted(photo_dir.glob("plant-*-*.jpg")):
        m = re.fullmatch(r"plant-(\d+)-(\d+)\.jpg", f.name)
        if m and int(m.group(1)) in plants:
            n += bool(store.add_photo(int(m.group(1)), f.name,
                                      datetime.fromtimestamp(int(m.group(2))).date(), ask_ai=False))
    for p in plants.values():
        f = photo_dir / p["photo"] if p["photo"] else None
        if f and f.is_file():
            n += bool(store.add_photo(p["id"], f.name,
                                      datetime.fromtimestamp(f.stat().st_mtime).date(), ask_ai=False))
    store.set("photos_backfilled", date.today().isoformat())
    return n
