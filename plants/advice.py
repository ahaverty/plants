"""Turning a photo look-over's suggestion into an actual change, when you tap Apply.

The worker (worker/photo_worker.py) may attach a `change` to a suggestion:
{"field": ..., "value": "...", "label": "...", "task": "..."}. Nothing is applied without the
tap. Each applied change is logged as a 'change' event and remembers the old value so the
page can undo it."""

from __future__ import annotations

from datetime import date, timedelta

from garden import today

INTS = {"water_summer": (1, 120, "Summer watering"), "water_winter": (1, 180, "Winter watering"),
        "feed_days": (7, 365, "Feeding")}
CHOICES = {"pot": ("small", "medium", "large"), "light": ("low", "medium", "bright")}
FIELDS = (*INTS, *CHOICES, "archive", "reminder")


def check(change: dict) -> dict:
    """The change in the form it gets applied, or ValueError if it isn't one we can do."""
    f, v = change.get("field"), str(change.get("value", "")).strip().lower()
    if f in INTS:
        lo, hi, _ = INTS[f]
        if f == "feed_days" and v in ("never", "none", "stop"):
            return {"field": f, "value": None}
        n = int(v)
        if not lo <= n <= hi:
            raise ValueError(f"{f} must be {lo}-{hi} days")
        return {"field": f, "value": n}
    if f in CHOICES:
        if v not in CHOICES[f]:
            raise ValueError(f"{f} must be one of {CHOICES[f]}")
        return {"field": f, "value": v}
    if f == "archive":
        return {"field": f, "value": 1}
    if f == "reminder":
        d = date.fromisoformat(v)
        if not today() < d <= today() + timedelta(days=730):
            raise ValueError("reminder must be in the next two years")
        return {"field": f, "value": d.isoformat(),
                "task": (change.get("task") or "Check on it").strip()[:60]}
    raise ValueError(f"can't apply {f!r}")


def describe(p: dict, c: dict) -> str:
    f, v = c["field"], c["value"]
    if f in INTS:
        none = "rain only" if f == "water_winter" else "never"
        fmt = lambda x: none if x is None else f"every {x} days"
        return f"{INTS[f][2]}: {fmt(p[f])} to {fmt(v)}"
    if f in CHOICES:
        return f"{f.capitalize()}: {p[f] or 'not set'} to {v}"
    if f == "archive":
        return "Moved to the graveyard"
    d = date.fromisoformat(v)
    return f"Reminder: {c['task']} on {d.day} {d:%b %Y}"


def apply(store, plant_id: int, change: dict) -> dict:
    """Apply it; returns {"desc", "old", "event"} so it can be shown and undone."""
    p = store.plant(plant_id)
    c = check(change)
    desc = describe(p, c)
    f = c["field"]
    if f == "archive":
        old = {"archived": p["archived"], "archived_on": p["archived_on"]}
        store.update_plant(plant_id, archived=1)
    elif f == "reminder":
        old = {"task": p["task"], "task_on": p["task_on"]}
        store.update_plant(plant_id, task=c["task"], task_on=c["value"])
    else:
        old = {f: p[f]}
        store.update_plant(plant_id, **{f: c["value"]})
    ev = store.add_event(plant_id, "change", today(), note=desc + " (photo advice)", source="web")
    return {"desc": desc, "old": old, "event": ev}


def undo(store, plant_id: int, applied: dict) -> None:
    store.update_plant(plant_id, **applied["old"])
    store.delete_event(applied["event"])
