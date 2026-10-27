"""Example starting data: a few rooms and plants so the page isn't empty. Edit the lists to
match your own house, or skip this and add plants through Claude (MCP) or the page.
Safe to re-run: does nothing if plants exist.

    docker exec plants python seed.py
"""

from __future__ import annotations

from datetime import date

import schedule
from server import DATA
from store import Store

ROOMS = [  # id, name, route order, floor, outdoor
    ("kitchen", "Kitchen", 1, "downstairs", False),
    ("garden", "Garden", 2, "outside", True),
    ("bedroom", "Bedroom", 3, "upstairs", False),
]
HOUSE = dict(feed_days=30, feed_from=3, feed_to=9)
SUCC = dict(feed_days=60, feed_from=4, feed_to=9)
# name, room, species, summer, winter, feed, extra fields, care
PLANTS = [
    ("Pothos", "kitchen", "Epipremnum aureum", 8, 14, HOUSE, {},
     "Water when the top third of the soil is dry. Droopy leaves = thirsty, yellow leaves = too wet."),
    ("Snake plant", "bedroom", "Dracaena trifasciata", 14, 35, SUCC, {},
     "Very drought tolerant: soak, then leave it until fully dry."),
    ("Hydrangea", "garden", "Hydrangea macrophylla", 3, 21, dict(feed_days=14, feed_from=4, feed_to=7),
     {}, "Thirsty in summer, leafless in winter. Rain counts as watering."),
]


def main() -> None:
    store = Store(DATA / "plants.db")
    if store.plants(include_archived=True):
        print("plants already exist, nothing to do")
        return
    for r in ROOMS:
        store.upsert_room(*r)
    today = date.today()
    for name, room, species, summer, winter, feed, extra, care in PLANTS:
        fields = dict(name=name, room_id=room, species=species, water_summer=summer,
                      water_winter=winter, care=care, **(feed or {"feed_days": None}), **extra)
        pid = store.add_plant(**fields)
        store.add_event(pid, "water", today, note="starting point", source="seed")
        if feed:
            store.add_event(pid, "feed", today, note="starting point", source="seed")
        print(f"{name:20} {room:10} every {schedule.water_interval(store.plant(pid), today)} days")


if __name__ == "__main__":
    main()
