"""SQLite storage: rooms, plants, care events (water / feed / rain / skip) and settings."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id      TEXT PRIMARY KEY,          -- 'kitchen'
    name    TEXT NOT NULL,             -- 'Kitchen'
    sort    INTEGER NOT NULL DEFAULT 0,-- watering route order (kitchen first, upstairs last)
    floor   TEXT NOT NULL DEFAULT '',  -- 'downstairs' / 'upstairs' / 'outside'
    outdoor INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plants (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    species       TEXT NOT NULL DEFAULT '',
    room_id       TEXT NOT NULL REFERENCES rooms(id),
    photo         TEXT,                 -- file name under data/photos
    exposure      TEXT NOT NULL DEFAULT 'open',  -- outdoor only: 'open' (gets rain) / 'covered'
    in_ground     INTEGER NOT NULL DEFAULT 0,
    water_summer  INTEGER NOT NULL DEFAULT 7,    -- days between waterings in June-August
    water_winter  INTEGER,                       -- days in Dec-Feb; NULL = rain covers winter
    feed_days     INTEGER,                       -- days between feeds in season; NULL = never
    feed_from     INTEGER NOT NULL DEFAULT 3,    -- first feeding month (1-12)
    feed_to       INTEGER NOT NULL DEFAULT 9,    -- last feeding month
    pot           TEXT,                 -- optional: small / medium / large
    light         TEXT,                 -- optional: low / medium / bright
    kind          TEXT NOT NULL DEFAULT 'normal',  -- normal / establishing / cutting
    care          TEXT NOT NULL DEFAULT '',  -- care notes (why the schedule is what it is)
    notes         TEXT NOT NULL DEFAULT '',  -- your own notes
    snoozed_until TEXT,                 -- ISO date: hidden from the due list until after it
    archived      INTEGER NOT NULL DEFAULT 0,  -- 1 = in the graveyard (died); restorable
    archived_on   TEXT,                 -- ISO date it went to the graveyard
    task          TEXT,                 -- a one-off job, e.g. 'Repot' (from photo advice)
    task_on       TEXT,                 -- ISO date the job comes up on the page and phone
    created       TEXT NOT NULL DEFAULT (date('now'))
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    plant_id INTEGER NOT NULL REFERENCES plants(id),
    kind     TEXT NOT NULL,   -- water / feed / rain / skip / change (setting applied) / job (done)
    day      TEXT NOT NULL,   -- ISO date it happened
    note     TEXT NOT NULL DEFAULT '',
    source   TEXT NOT NULL DEFAULT '',  -- web / ha / mcp / weather / seed
    created  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS events_plant ON events(plant_id, kind, day);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS photos (
    id        INTEGER PRIMARY KEY,
    plant_id  INTEGER NOT NULL REFERENCES plants(id),
    file      TEXT NOT NULL UNIQUE,     -- file name under data/photos (never deleted on replace)
    day       TEXT NOT NULL,            -- ISO date the photo was added
    ai_state  TEXT,                     -- NULL (not asked) / pending / working / done / error
    ai_since  TEXT,                     -- when the worker took it (re-offered if it goes quiet)
    analysis  TEXT,                     -- JSON from the photo worker (worker/photo_worker.py)
    created   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS photos_plant ON photos(plant_id, day);
"""

PLANT_FIELDS = ("name", "species", "room_id", "photo", "exposure", "in_ground", "water_summer",
                "water_winter", "feed_days", "feed_from", "feed_to", "pot", "light", "kind",
                "care", "notes", "snoozed_until", "archived", "archived_on", "task", "task_on")
EVENT_KINDS = ("water", "feed", "rain", "skip", "change", "job")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(plants)")}
            for col in ("archived_on", "task", "task_on"):
                if col not in cols:
                    self._db.execute(f"ALTER TABLE plants ADD COLUMN {col} TEXT")
            self._db.commit()

    def _q(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid

    # rooms
    def rooms(self) -> list[dict]:
        return self._q("SELECT * FROM rooms ORDER BY sort, name")

    def room(self, room_id: str) -> dict | None:
        r = self._q("SELECT * FROM rooms WHERE id=?", (room_id,))
        return r[0] if r else None

    def find_room(self, name_or_id: str) -> dict | None:
        key = name_or_id.strip().lower()
        for r in self.rooms():
            if key in (r["id"], r["name"].lower()):
                return r
        return None

    def upsert_room(self, room_id: str, name: str, sort: int, floor: str, outdoor: bool) -> None:
        self._x("INSERT INTO rooms(id,name,sort,floor,outdoor) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, sort=excluded.sort, "
                "floor=excluded.floor, outdoor=excluded.outdoor",
                (room_id, name, sort, floor, int(outdoor)))

    # plants
    def plants(self, include_archived: bool = False) -> list[dict]:
        where = "" if include_archived else "WHERE p.archived=0"
        return self._q(f"SELECT p.*, r.name AS room, r.sort AS room_sort, r.floor, r.outdoor "
                       f"FROM plants p JOIN rooms r ON r.id=p.room_id {where} "
                       f"ORDER BY r.sort, p.name")

    def graveyard(self) -> list[dict]:
        return self._q("SELECT p.id, p.name, p.species, p.photo, p.archived_on, r.name AS room "
                       "FROM plants p JOIN rooms r ON r.id=p.room_id WHERE p.archived=1 "
                       "ORDER BY p.archived_on DESC, p.name")

    def plant(self, plant_id: int) -> dict | None:
        r = self._q("SELECT p.*, r.name AS room, r.sort AS room_sort, r.floor, r.outdoor "
                    "FROM plants p JOIN rooms r ON r.id=p.room_id WHERE p.id=?", (plant_id,))
        return r[0] if r else None

    def add_plant(self, **fields: Any) -> int:
        cols = [k for k in fields if k in PLANT_FIELDS]
        return self._x(f"INSERT INTO plants({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                       tuple(fields[c] for c in cols))

    def update_plant(self, plant_id: int, **fields: Any) -> None:
        if "archived" in fields and "archived_on" not in fields:
            fields["archived_on"] = date.today().isoformat() if fields["archived"] else None
        cols = [k for k in fields if k in PLANT_FIELDS]
        if cols:
            self._x(f"UPDATE plants SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                    tuple(fields[c] for c in cols) + (plant_id,))

    # events
    def add_event(self, plant_id: int, kind: str, day: date, note: str = "",
                  source: str = "") -> int:
        if kind not in EVENT_KINDS:
            raise ValueError(f"kind must be one of {EVENT_KINDS}")
        return self._x("INSERT INTO events(plant_id,kind,day,note,source) VALUES(?,?,?,?,?)",
                       (plant_id, kind, day.isoformat(), note, source))

    def has_event(self, plant_id: int, kind: str, day: date) -> bool:
        return bool(self._q("SELECT 1 FROM events WHERE plant_id=? AND kind=? AND day=?",
                            (plant_id, kind, day.isoformat())))

    def delete_event(self, event_id: int) -> None:
        self._x("DELETE FROM events WHERE id=?", (event_id,))

    def events(self, plant_id: int | None = None, limit: int = 50) -> list[dict]:
        if plant_id is None:
            return self._q("SELECT e.*, p.name AS plant FROM events e JOIN plants p "
                           "ON p.id=e.plant_id ORDER BY e.day DESC, e.id DESC LIMIT ?", (limit,))
        return self._q("SELECT * FROM events WHERE plant_id=? ORDER BY day DESC, id DESC LIMIT ?",
                       (plant_id, limit))

    def last_events(self, plant_id: int) -> dict[str, dict]:
        """Newest event of each kind for a plant."""
        rows = self._q("SELECT * FROM events WHERE plant_id=? ORDER BY day, id", (plant_id,))
        return {r["kind"]: r for r in rows}

    # photos: every photo a plant has had, so its progress can be looked back on
    def add_photo(self, plant_id: int, file: str, day: date, ask_ai: bool = True) -> bool:
        """False if that file is already in the history."""
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO photos(plant_id,file,day,ai_state) "
                                   "VALUES(?,?,?,?)",
                                   (plant_id, file, day.isoformat(), "pending" if ask_ai else None))
            self._db.commit()
            return cur.rowcount > 0

    def photos(self, plant_id: int) -> list[dict]:
        """Newest first, with the analysis decoded."""
        rows = self._q("SELECT * FROM photos WHERE plant_id=? ORDER BY day DESC, id DESC", (plant_id,))
        for r in rows:
            r["analysis"] = json.loads(r["analysis"]) if r["analysis"] else None
        return rows

    def photo(self, photo_id: int) -> dict | None:
        r = self._q("SELECT * FROM photos WHERE id=?", (photo_id,))
        return r[0] if r else None

    def delete_photo(self, photo_id: int) -> None:
        self._x("DELETE FROM photos WHERE id=?", (photo_id,))

    def ask_ai(self, photo_id: int) -> None:
        self._x("UPDATE photos SET ai_state='pending', ai_since=NULL WHERE id=?", (photo_id,))

    def take_ai_work(self, stale_minutes: int = 10) -> dict | None:
        """Hand the oldest waiting photo to the worker (or one it took and never answered)."""
        with self._lock:
            r = self._q("SELECT * FROM photos WHERE ai_state='pending' OR (ai_state='working' "
                        "AND ai_since < datetime('now', ?)) ORDER BY id LIMIT 1",
                        (f"-{stale_minutes} minutes",))
            if not r:
                return None
            self._x("UPDATE photos SET ai_state='working', ai_since=datetime('now') WHERE id=?",
                    (r[0]["id"],))
            return r[0]

    def update_analysis(self, photo_id: int, analysis: dict) -> None:
        self._x("UPDATE photos SET analysis=? WHERE id=?", (json.dumps(analysis), photo_id))

    def set_analysis(self, photo_id: int, analysis: dict | None, error: str | None = None) -> None:
        self._x("UPDATE photos SET ai_state=?, analysis=? WHERE id=?",
                ("error" if error else "done", json.dumps({"error": error} if error else analysis),
                 photo_id))

    # settings
    def get(self, key: str, default: str | None = None) -> str | None:
        r = self._q("SELECT value FROM settings WHERE key=?", (key,))
        return r[0]["value"] if r else default

    def set(self, key: str, value: str | None) -> None:
        if value is None:
            self._x("DELETE FROM settings WHERE key=?", (key,))
        else:
            self._x("INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
