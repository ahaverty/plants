"""MCP tools so Claude can add plants (and work out their schedules), log care and snooze."""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

import photos
from garden import Garden, today

PLACE = os.environ.get("PLANTS_PLACE", "Ireland")   # climate the schedules are worked out for
# The host folder mounted at PLANTS_HOST_ROOT, spelled as the host sees it (e.g. C:\Users\me),
# so plant_photo(path=...) can read files from the host.
HOST_PREFIX = os.environ.get("PLANTS_HOST_PREFIX", "").rstrip("\\/")

INSTRUCTIONS = (
    "A plant watering and feeding tracker. The owner wants it low maintenance: "
    "just keep the plants alive, no fuss. plants_board shows what is due today, grouped by room "
    "in their watering route order. Log care with "
    "plant_log ('due' logs whatever is due). When they add a plant, YOU work out the schedule "
    f"for {PLACE}: water_summer / water_winter in days (water_winter null = rain looks "
    "after it in winter, for established outdoor plants in the ground), feed_days and the "
    "feeding months, and a one or two sentence `care` note on why. Outdoor plants open to the "
    "sky get rain credited automatically, so give them their dry-weather interval. Pot size "
    "and light are optional nudges; only ask about them if they would really matter. Photos "
    "help them recognise plants: attach one with plant_photo when they send one. Every photo is "
    "kept as a dated history (plant_get `photos`, newest first, with any AI analysis).")


def register(mcp: FastMCP, g: Garden, photo_dir: Path, host_root: Path | None) -> None:
    store = g.store

    def find(plant: str | int) -> dict:
        plants = store.plants(include_archived=True)
        if isinstance(plant, int) or str(plant).isdigit():
            hit = [p for p in plants if p["id"] == int(plant)]
        else:
            key = str(plant).strip().lower()
            hit = [p for p in plants if p["name"].lower() == key] or \
                  [p for p in plants if key in p["name"].lower() or key in p["species"].lower()]
            live = [p for p in hit if not p["archived"]]
            hit = live or hit
        if len(hit) != 1:
            names = ", ".join(f"{p['name']} ({p['room']}, id {p['id']})" for p in hit) or "none"
            raise ToolError(f"'{plant}' matches {len(hit)} plants: {names}. Use the id.")
        return hit[0]

    def room_id(room: str) -> str:
        r = store.find_room(room)
        if not r:
            raise ToolError(f"No room '{room}'. Rooms: {', '.join(x['name'] for x in store.rooms())}. "
                            "Add one with room_set.")
        return r["id"]

    def parse_day(s: str | None) -> date:
        if not s or s == "today":
            return today()
        return date.fromisoformat(s)

    def detail(p: dict) -> dict:
        st = {s["id"]: s for s in g.statuses()}.get(p["id"])
        fields = {k: p[k] for k in ("id", "name", "species", "room", "exposure", "in_ground",
                                    "water_summer", "water_winter", "feed_days", "feed_from",
                                    "feed_to", "pot", "light", "kind", "care", "notes",
                                    "photo", "archived", "archived_on", "task", "task_on")}
        pics = [{k: x[k] for k in ("id", "file", "day", "ai_state", "analysis")}
                for x in store.photos(p["id"])]
        return {**fields, "status": st, "history": store.events(p["id"], limit=15), "photos": pics}

    @mcp.tool
    def plants_board() -> dict:
        """What needs water or feed today, grouped by room in watering-route order, plus held
        outdoor plants (rain forecast / frost) and the weather used."""
        return g.board()

    @mcp.tool
    def plants_list() -> list[dict]:
        """Every plant with its room, next water/feed date and today's interval."""
        return [{k: s[k] for k in ("id", "name", "room", "water_due", "feed_due", "interval_days",
                                   "last_watered", "last_watered_by", "feed_note", "water_note",
                                   "todo", "hold", "snoozed_until")} for s in g.statuses()]

    @mcp.tool
    def plant_get(plant: Annotated[str, Field(description="Name or id")]) -> dict:
        """One plant's settings, today's status and recent history."""
        return detail(find(plant))

    @mcp.tool
    def plant_add(
        name: str, room: str,
        water_summer: Annotated[int, Field(description="Days between waterings Jun-Aug")],
        water_winter: Annotated[int | None, Field(description="Days in Dec-Feb; null = rain covers winter (established outdoor in-ground only)")],
        care: Annotated[str, Field(description="1-2 sentences: why this schedule, what to watch for")],
        species: str = "",
        feed_days: Annotated[int | None, Field(description="Days between feeds in season; null = never")] = 30,
        feed_from: Annotated[int, Field(ge=1, le=12)] = 3,
        feed_to: Annotated[int, Field(ge=1, le=12)] = 9,
        exposure: Literal["open", "covered"] = "open",
        in_ground: bool = False,
        pot: Literal["small", "medium", "large"] | None = None,
        light: Literal["low", "medium", "bright"] | None = None,
        kind: Literal["normal", "establishing", "cutting"] = "normal",
        notes: str = "",
        last_watered: Annotated[str | None, Field(description="ISO date; omit = due today")] = None,
        last_fed: str | None = None,
    ) -> dict:
        """Add a plant with a schedule you have worked out for it (local climate, PLANTS_PLACE)."""
        pid = store.add_plant(name=name, species=species, room_id=room_id(room),
                              water_summer=water_summer, water_winter=water_winter,
                              feed_days=feed_days, feed_from=feed_from, feed_to=feed_to,
                              exposure=exposure, in_ground=int(in_ground), pot=pot, light=light,
                              kind=kind, care=care, notes=notes)
        if last_watered:
            store.add_event(pid, "water", parse_day(last_watered), source="mcp")
        if last_fed:
            store.add_event(pid, "feed", parse_day(last_fed), source="mcp")
        return detail(store.plant(pid))

    @mcp.tool
    def plant_update(
        plant: str, name: str | None = None, species: str | None = None,
        room: str | None = None, water_summer: int | None = None,
        water_winter: Annotated[int | None, Field(description="-1 = set to null (rain covers winter)")] = None,
        feed_days: Annotated[int | None, Field(description="0 = never feed")] = None,
        feed_from: int | None = None, feed_to: int | None = None,
        exposure: Literal["open", "covered"] | None = None, in_ground: bool | None = None,
        pot: Literal["small", "medium", "large", "none"] | None = None,
        light: Literal["low", "medium", "bright", "none"] | None = None,
        kind: Literal["normal", "establishing", "cutting"] | None = None,
        care: str | None = None, notes: str | None = None, archived: bool | None = None,
    ) -> dict:
        """Change any of a plant's settings. archived=true moves it to the graveyard (died / given
        away; history is kept and it shows under Graveyard on the page), archived=false restores it."""
        p = find(plant)
        f: dict = {}
        for k, v in dict(name=name, species=species, water_summer=water_summer,
                         feed_from=feed_from, feed_to=feed_to, exposure=exposure, kind=kind,
                         care=care, notes=notes).items():
            if v is not None:
                f[k] = v
        if room is not None:
            f["room_id"] = room_id(room)
        if water_winter is not None:
            f["water_winter"] = None if water_winter < 0 else water_winter
        if feed_days is not None:
            f["feed_days"] = feed_days or None
        if in_ground is not None:
            f["in_ground"] = int(in_ground)
        if archived is not None:
            f["archived"] = int(archived)
        if pot is not None:
            f["pot"] = None if pot == "none" else pot
        if light is not None:
            f["light"] = None if light == "none" else light
        store.update_plant(p["id"], **f)
        return detail(store.plant(p["id"]))

    @mcp.tool
    def plant_log(
        plants: Annotated[list[str], Field(description="Names or ids; ['all due'] for everything due today")],
        what: Literal["due", "water", "feed", "both", "skip"] = "due",
        day: Annotated[str | None, Field(description="ISO date, default today")] = None,
        note: str = "",
    ) -> list[dict]:
        """Log care. 'due' = whatever each plant was due for. 'skip' = checked, soil still
        damp: pushes the next check out by about a third of the interval."""
        if plants == ["all due"]:
            return g.mark_all_due(source="mcp")
        return g.mark([find(x)["id"] for x in plants], what, parse_day(day), source="mcp", note=note)

    @mcp.tool
    def plant_undo(event_id: int) -> str:
        """Delete one logged event (ids are in plant_get history)."""
        store.delete_event(event_id)
        return "deleted"

    @mcp.tool
    def plant_snooze(
        plants: Annotated[list[str] | None, Field(description="Names or ids; null = everything")] = None,
        days: Annotated[int, Field(ge=1, le=30, description="1 = rest of today")] = 1,
    ) -> str:
        """Hide plants (or the whole list and the morning notification) for a while."""
        ids = None if plants is None else [find(x)["id"] for x in plants]
        return f"snoozed until end of {g.snooze(ids, days)}"

    @mcp.tool
    def plant_photo(
        plant: str,
        path: Annotated[str | None, Field(description="Image file on the host, under PLANTS_HOST_PREFIX")] = None,
        url: str | None = None,
    ) -> dict:
        """Set a plant's photo from a file on the PC or an image URL. The old one stays in its
        photo history and the new one is queued for an AI look-over (shown on the plant page)."""
        p = find(plant)
        if path:
            norm = path.replace("\\", "/")
            prefix = HOST_PREFIX.replace("\\", "/")
            if not prefix or host_root is None or not norm.lower().startswith(prefix.lower() + "/"):
                raise ToolError(f"path must be under {HOST_PREFIX or 'PLANTS_HOST_PREFIX (not set)'}")
            m = re.match(r"^/*(.*)$", norm[len(prefix):])
            raw = (host_root / m.group(1).replace("\\", "/")).read_bytes()
        elif url:
            raw = httpx.get(url, timeout=30, follow_redirects=True).raise_for_status().content
        else:
            raise ToolError("give path or url")
        return {"photo": photos.add(store, photo_dir, p["id"], raw)}

    @mcp.tool
    def rooms_list() -> list[dict]:
        """Rooms in watering-route order (sort), with floor and outdoor flag."""
        return store.rooms()

    @mcp.tool
    def room_set(room_id: str, name: str, sort: int, floor: str = "", outdoor: bool = False) -> list[dict]:
        """Add or change a room. sort sets the watering route order (lower first)."""
        store.upsert_room(room_id.strip().lower(), name, sort, floor, outdoor)
        return store.rooms()
