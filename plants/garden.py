"""The service's one brain: store + weather + schedule. The web page, Home Assistant and the
MCP tools all go through this, so they always agree."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import schedule
from store import Store
from weather import WeatherSource

log = logging.getLogger("plants")
TZ = ZoneInfo("Europe/Dublin")
USER_SOURCES = ("web", "ha", "mcp")   # ticks by a person, not imports or rain


def today() -> date:
    return datetime.now(TZ).date()


class Garden:
    def __init__(self, store: Store, weather: WeatherSource | None):
        self.store, self.weather = store, weather

    def _weather(self) -> schedule.Weather | None:
        return self.weather.get() if self.weather else None

    # reading
    def statuses(self, day: date | None = None) -> list[dict]:
        day = day or today()
        w = self._weather()
        out = []
        for p in self.store.plants():
            last = self.store.last_events(p["id"])
            s = schedule.status(p, last, day, w)
            s["done_today"] = [k for k in ("water", "feed", "skip")
                               if k in last and last[k]["day"] == day.isoformat()
                               and last[k]["source"] in USER_SOURCES]
            out.append(s)
        return out

    def board(self) -> dict:
        """Everything grouped by room in watering-route order, for the page and for Claude."""
        day = today()
        sts = self.statuses(day)
        rooms = []
        for r in self.store.rooms():
            plants = [s for s in sts if s["room_id"] == r["id"]]
            if plants:
                rooms.append({**r, "plants": plants,
                              "due": [s["id"] for s in plants if s["due"]]})
        w = self._weather()
        snooze_all = self.snoozed_all_until()
        due = [s for s in sts if s["due"]] if not snooze_all else []
        return {"today": day.isoformat(), "rooms": rooms, "due_count": len(due),
                "due": [{"id": s["id"], "name": s["name"], "room": s["room"], "todo": s["todo"]}
                        for s in due],
                "held": [{"id": s["id"], "name": s["name"], "hold": s["hold"]}
                         for s in sts if s["hold"]],
                "snoozed_all_until": snooze_all,
                "jobs": self.jobs(day),
                "advice": self.advice(day),
                "graveyard": self.store.graveyard(),
                "weather": None if not w else {
                    "rain_last_7_days_mm": round(sum(w.rain_by_day.values()), 1),
                    "rain_yesterday_mm": w.rain_by_day.get(day - timedelta(days=1)),
                    "rain_next_24h_mm": w.rain_next_24h, "min_tonight_c": w.min_tonight,
                    "frost_tonight": w.frost_tonight}}

    def jobs(self, day: date | None = None) -> list[dict]:
        """One-off jobs (e.g. 'Repot', set from photo advice) whose day has come."""
        day = (day or today()).isoformat()
        return [{"id": p["id"], "name": p["name"], "room": p["room"], "task": p["task"],
                 "task_on": p["task_on"]}
                for p in self.store.plants() if p["task"] and p["task_on"] and p["task_on"] <= day]

    ADVICE_DAYS = 60
    WORRYING = ("struggling", "poorly", "dead")

    def advice(self, day: date | None = None) -> list[dict]:
        """Photo advice worth a look from the home page: suggestions with a one-tap change not
        yet applied, or a worrying verdict. From each plant's newest photo in the last 60 days,
        until you hide it ('seen')."""
        day = day or today()
        out = []
        for p in self.store.plants():
            pics = self.store.photos(p["id"])
            ph = pics[0] if pics else None
            a = ph and ph["ai_state"] == "done" and ph["analysis"]
            if not a or a.get("seen") or (day - date.fromisoformat(ph["day"])).days > self.ADVICE_DAYS:
                continue
            items = [{"i": i, "text": s["text"], "label": s["change"]["label"]}
                     for i, s in enumerate(a.get("suggestions", []))
                     if s.get("change") and not s.get("applied")]
            worrying = a.get("verdict") in self.WORRYING
            if items or worrying:
                out.append({"id": p["id"], "name": p["name"], "room": p["room"], "photo": ph["file"],
                            "photo_id": ph["id"], "verdict": a.get("verdict"),
                            "summary": a.get("summary", "") if worrying else "", "items": items})
        return out

    def job_done(self, plant_id: int, source: str = "") -> None:
        p = self.store.plant(plant_id)
        if p and p["task"]:
            self.store.add_event(plant_id, "job", today(), note=p["task"], source=source)
            self.store.update_plant(plant_id, task=None, task_on=None)

    def snoozed_all_until(self) -> str | None:
        s = self.store.get("snoozed_all_until")
        return s if s and s >= today().isoformat() else None

    # writing
    def mark(self, plant_ids: list[int], what: str = "due", day: date | None = None,
             source: str = "", note: str = "") -> list[dict]:
        """what: 'due' (whatever each plant is due for; water if nothing is), 'water', 'feed',
        'both' or 'skip' (checked, soil still damp)."""
        day = day or today()
        sts = {s["id"]: s for s in self.statuses(day)}
        done = []
        for pid in plant_ids:
            s = sts.get(pid)
            if not s:
                continue
            if what == "due":
                kinds = s["todo"] or ["water"]
            elif what == "both":
                kinds = ["water", "feed"]
            elif what in ("water", "feed", "skip"):
                kinds = [what]
            else:
                raise ValueError("what must be due / water / feed / both / skip")
            for k in kinds:
                self.store.add_event(pid, k, day, note=note, source=source)
            self.store.update_plant(pid, snoozed_until=None)
            done.append({"id": pid, "name": s["name"], "logged": kinds})
        return done

    def mark_all_due(self, room_id: str | None = None, source: str = "") -> list[dict]:
        ids = [s["id"] for s in self.statuses()
               if (s["due"] or s["hold"]) and (room_id is None or s["room_id"] == room_id)]
        return self.mark(ids, "due", source=source)

    def snooze(self, plant_ids: list[int] | None, days: int = 1) -> str:
        until = (today() + timedelta(days=max(1, days) - 1)).isoformat()
        if plant_ids is None:
            self.store.set("snoozed_all_until", until)
        else:
            for pid in plant_ids:
                self.store.update_plant(pid, snoozed_until=until)
        return until

    def log_rain(self) -> list[dict]:
        """Turn real rain into 'rain' waterings for outdoor plants open to the sky."""
        w = self._weather()
        if not w:
            return []
        logged = []
        for p in self.store.plants():
            for d in schedule.rain_days_to_log(p, self.store.last_events(p["id"]), w):
                if not self.store.has_event(p["id"], "rain", d):
                    self.store.add_event(p["id"], "rain", d, note=f"{w.rain_by_day[d]:.1f}mm",
                                         source="weather")
                    logged.append({"plant": p["name"], "day": d.isoformat()})
        if logged:
            log.info("rain logged: %s", logged)
        return logged
