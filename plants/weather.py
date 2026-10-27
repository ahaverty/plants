"""Rain and frost from Open-Meteo (free, no key). Past days are model estimates, not a rain
gauge, which is plenty for "did it properly rain on the pots"."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from schedule import FROST_C, Weather

log = logging.getLogger("plants.weather")
URL = "https://api.open-meteo.com/v1/forecast"
TZ = ZoneInfo("Europe/Dublin")


class WeatherSource:
    def __init__(self, lat: float, lon: float, ttl: int = 1800):
        self.lat, self.lon, self.ttl = lat, lon, ttl
        self._cache: tuple[float, Weather] | None = None

    def get(self) -> Weather | None:
        if self._cache and time.time() - self._cache[0] < self.ttl:
            return self._cache[1]
        try:
            w = self._fetch()
        except Exception as e:  # weather is a nice-to-have; schedules work without it
            log.warning("Open-Meteo failed: %s", e)
            return self._cache[1] if self._cache else None
        self._cache = (time.time(), w)
        return w

    def _fetch(self) -> Weather:
        r = httpx.get(URL, timeout=20, params={
            "latitude": self.lat, "longitude": self.lon, "timezone": "Europe/Dublin",
            "daily": "precipitation_sum", "hourly": "precipitation,temperature_2m",
            "past_days": 7, "forecast_days": 2})
        r.raise_for_status()
        return parse(r.json(), datetime.now(TZ))


def parse(j: dict, now: datetime) -> Weather:
    today = now.date()
    rain_by_day = {date.fromisoformat(d): float(mm or 0)
                   for d, mm in zip(j["daily"]["time"], j["daily"]["precipitation_sum"])
                   if date.fromisoformat(d) < today}   # whole past days only
    h = j["hourly"]
    times = [datetime.fromisoformat(t).replace(tzinfo=TZ) for t in h["time"]]
    nxt = sum(float(p or 0) for t, p in zip(times, h["precipitation"])
              if now <= t < now + timedelta(hours=24))
    morning = datetime.combine(today + timedelta(days=1), datetime.min.time(), TZ) + timedelta(hours=10)
    temps = [float(c) for t, c in zip(times, h["temperature_2m"])
             if now <= t <= morning and c is not None]
    tmin = min(temps) if temps else None
    return Weather(rain_by_day=rain_by_day, rain_next_24h=round(nxt, 1),
                   frost_tonight=tmin is not None and tmin <= FROST_C, min_tonight=tmin)
