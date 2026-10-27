"""When each plant next needs water or feed.

Rules, kept deliberately simple:
- Each plant has a summer and a winter watering interval. The interval for today slides
  between them by month (Irish seasons: full summer Jun-Aug, full winter Dec-Feb).
- Optional pot size and light nudge the interval (small pots and bright windows dry faster).
  Left blank they change nothing.
- The clock restarts at the last watering, or the last rain for outdoor plants open to the
  sky. "Checked, still damp" (a skip) pushes the next check out by about a third of the
  interval instead of a full one.
- water_winter NULL means rain looks after it in winter: no reminders while the season is
  mostly winter (Nov-Feb).
- Feeding only happens between feed_from and feed_to (inclusive months).
- Outdoor plants are held (not nagged about) when real rain is forecast in the next day or a
  frost is due tonight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# How "summery" each month is in Ireland, 0 = winter interval, 1 = summer interval.
SEASON = {1: 0.0, 2: 0.0, 3: 0.25, 4: 0.5, 5: 0.8, 6: 1.0, 7: 1.0, 8: 1.0,
          9: 0.6, 10: 0.35, 11: 0.1, 12: 0.0}
POT = {"small": 0.85, "medium": 1.0, "large": 1.2}
LIGHT = {"low": 1.15, "medium": 1.0, "bright": 0.9}
RAIN_MM = 4.0         # a day with at least this much rain counts as a watering
FROST_C = 1.0         # tonight's minimum at or below this holds outdoor watering
SKIP_FRACTION = 0.35  # "still damp" pushes the next check out by this share of the interval
MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class Weather:
    rain_by_day: dict[date, float]   # past days, mm
    rain_next_24h: float = 0.0
    frost_tonight: bool = False
    min_tonight: float | None = None


def gets_rain(p: dict) -> bool:
    return bool(p["outdoor"]) and p["exposure"] == "open"


def water_interval(p: dict, today: date) -> int | None:
    w = SEASON[today.month]
    winter = p["water_winter"]
    if winter is None:
        if w < 0.3:
            return None
        winter = p["water_summer"] * 3
    days = winter + (p["water_summer"] - winter) * w
    days *= POT.get(p["pot"] or "", 1.0) * LIGHT.get(p["light"] or "", 1.0)
    return max(1, round(days))


def in_feed_season(p: dict, month: int) -> bool:
    a, b = p["feed_from"], p["feed_to"]
    return a <= month <= b if a <= b else (month >= a or month <= b)


def feed_season_start(p: dict, today: date) -> date:
    """First day of the feeding season we are in (call only when in season)."""
    y = today.year if today.month >= p["feed_from"] else today.year - 1
    return date(y, p["feed_from"], 1)


def status(p: dict, last: dict[str, dict], today: date, weather: Weather | None = None) -> dict:
    """Everything the UI, Home Assistant and Claude need about one plant today."""
    out: dict = {"id": p["id"], "name": p["name"], "room": p["room"], "room_id": p["room_id"],
                 "photo": p["photo"], "outdoor": bool(p["outdoor"]), "kind": p["kind"]}

    # Water
    interval = water_interval(p, today)
    anchor_ev = max((last[k] for k in ("water", "rain") if k in last),
                    key=lambda e: (e["day"], e["id"]), default=None)
    skip = last.get("skip")
    out["last_watered"] = anchor_ev["day"] if anchor_ev else None
    out["last_watered_by"] = anchor_ev["kind"] if anchor_ev else None
    out["interval_days"] = interval
    if interval is None:
        out["water_due"] = None
        out["water_note"] = "Rain looks after it until spring"
    else:
        if anchor_ev is None:
            nxt = today
        else:
            nxt = date.fromisoformat(anchor_ev["day"]) + timedelta(days=interval)
        if skip and (anchor_ev is None or skip["day"] > anchor_ev["day"]):
            nxt = date.fromisoformat(skip["day"]) + timedelta(
                days=max(2, round(interval * SKIP_FRACTION)))
        out["water_due"] = nxt.isoformat()
        out["water_note"] = ""

    # Feed
    if not p["feed_days"]:
        out["feed_due"], out["feed_note"] = None, "No feeding"
    elif not in_feed_season(p, today.month):
        out["feed_due"] = None
        out["feed_note"] = f"Feeding paused until {MONTHS[p['feed_from']]}"
    else:
        fed = last.get("feed")
        nxt = date.fromisoformat(fed["day"]) + timedelta(days=p["feed_days"]) if fed else today
        # A feed last season doesn't count: the first feed of a season is due at its start.
        out["feed_due"] = max(nxt, feed_season_start(p, today)).isoformat()
        out["feed_note"] = ""
    out["last_fed"] = last["feed"]["day"] if "feed" in last else None

    # Due today?
    todo = []
    if out["water_due"] and out["water_due"] <= today.isoformat():
        todo.append("water")
    if out["feed_due"] and out["feed_due"] <= today.isoformat():
        todo.append("feed")
    out["todo"] = todo
    out["overdue_days"] = max(((today - date.fromisoformat(out[f"{t}_due"])).days for t in todo),
                              default=0)
    snoozed = p["snoozed_until"] and p["snoozed_until"] >= today.isoformat()
    out["snoozed_until"] = p["snoozed_until"] if snoozed else None

    hold = None
    if todo and p["outdoor"] and weather:
        if weather.frost_tonight:
            hold = f"Frost tonight ({weather.min_tonight:.0f}°C), leave it"
        elif gets_rain(p) and weather.rain_next_24h >= RAIN_MM and todo == ["water"]:
            hold = f"{weather.rain_next_24h:.0f}mm of rain on the way"
    out["hold"] = hold
    out["due"] = bool(todo) and not snoozed and not hold
    nexts = [d for d in (out["water_due"], out["feed_due"]) if d]
    out["next_due"] = min(nexts) if nexts else None
    return out


def rain_days_to_log(p: dict, last: dict[str, dict], weather: Weather) -> list[date]:
    """Rainy days since the plant was last watered (or rained on), for outdoor open plants."""
    if not gets_rain(p):
        return []
    since = max((last[k]["day"] for k in ("water", "rain") if k in last), default="")
    return sorted(d for d, mm in weather.rain_by_day.items()
                  if mm >= RAIN_MM and d.isoformat() > since)
