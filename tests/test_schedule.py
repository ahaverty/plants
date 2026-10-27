"""Offline checks of the schedule rules. Run: python tests/test_schedule.py"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plants"))
import schedule as s  # noqa: E402


def plant(**kw):
    p = dict(id=1, name="P", room="Kitchen", room_id="kitchen", photo=None, outdoor=0,
             exposure="open", water_summer=7, water_winter=14, feed_days=30, feed_from=3,
             feed_to=9, pot=None, light=None, kind="normal", snoozed_until=None)
    p.update(kw)
    return p


def ev(kind, day, i=1):
    return {kind: {"id": i, "kind": kind, "day": day}}


def test_intervals():
    p = plant()
    assert s.water_interval(p, date(2026, 7, 1)) == 7
    assert s.water_interval(p, date(2026, 1, 1)) == 14
    assert s.water_interval(p, date(2026, 10, 1)) == round(14 - 7 * 0.35)
    assert s.water_interval(plant(pot="small", light="bright"), date(2026, 7, 1)) == round(7 * .85 * .9)
    assert s.water_interval(plant(water_winter=None), date(2026, 12, 1)) is None


def test_due_and_skip():
    p = plant(feed_days=None)
    today = date(2026, 7, 10)
    st = s.status(p, ev("water", "2026-07-03"), today)
    assert st["todo"] == ["water"] and st["due"]
    last = {**ev("water", "2026-07-03"), **ev("skip", "2026-07-10", 2)}
    st = s.status(p, last, today)
    assert st["water_due"] == "2026-07-12" and not st["due"]


def test_feed_season():
    p = plant()
    st = s.status(p, {**ev("water", "2026-11-01"), **ev("feed", "2026-08-01", 2)}, date(2026, 11, 2))
    assert st["feed_due"] is None and "Mar" in st["feed_note"]
    st = s.status(p, {**ev("water", "2026-06-01"), **ev("feed", "2026-05-01", 2)}, date(2026, 6, 2))
    assert st["feed_due"] == "2026-05-31" and "feed" in st["todo"]
    # fed last October: first feed of the new season is due on 1 March
    st = s.status(p, {**ev("water", "2027-03-05"), **ev("feed", "2026-10-14", 2)}, date(2027, 3, 10))
    assert st["feed_due"] == "2027-03-01" and "feed" in st["todo"]


def test_outdoor_rain_and_holds():
    p = plant(outdoor=1, feed_days=None)
    w = s.Weather(rain_by_day={date(2026, 7, 5): 6.0, date(2026, 7, 6): 1.0}, rain_next_24h=8)
    assert s.rain_days_to_log(p, ev("water", "2026-07-01"), w) == [date(2026, 7, 5)]
    assert s.rain_days_to_log(plant(outdoor=1, exposure="covered"), {}, w) == []
    st = s.status(p, ev("water", "2026-07-01"), date(2026, 7, 10), w)
    assert st["todo"] == ["water"] and st["hold"] and not st["due"]
    frost = s.Weather(rain_by_day={}, frost_tonight=True, min_tonight=-1)
    st = s.status(plant(outdoor=1, exposure="covered", feed_days=None), ev("water", "2026-07-01"), date(2026, 7, 10), frost)
    assert "Frost" in st["hold"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
