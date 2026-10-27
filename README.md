# Plants

A small, self-hosted plant watering and feeding tracker. One Python service in Docker gives you:

- **A phone app** (installable PWA). It shows today's checklist grouped by room in your watering-route order.
  - Tick each plant as you go, or tick a whole room at once.
  - "Soil still damp" pushes that plant's next check back by about a third of its interval.
  - Each plant has a page with its photo history, care notes, log and settings.
- **Smart schedules.** Summer and winter intervals blend by month. Pot size and light are optional nudges.
  - Outdoor plants open to the sky get credit for rain and are held back when rain or frost is forecast. Weather comes from Open-Meteo, so no key is needed.
  - Feeding happens only in its season.
- **Claude as the gardener (MCP).** Connect Claude to `http://127.0.0.1:8772/mcp` and say "I got a Monstera for the kitchen". Claude works out the schedule for your climate, logs care, snoozes plants and attaches photos.
- **AI photo check-ups (optional).** `worker/photo_worker.py` sends each new photo to Claude Code (`claude -p`, on your own subscription) for a short verdict and suggestions, comparing it with the previous photo.
  - A suggestion can carry a one-tap change: a new interval, the pot, the light, a dated reminder such as "Repot in March", or moving the plant to the graveyard.
  - Nothing changes until you tap it.
- **Home Assistant (optional).**
  - A `sensor.plants_due` sensor.
  - A morning phone notification with a labelled picture of every plant that's due, and *All done* / *Tomorrow* / *List* buttons. The buttons come back over HA's websocket, so no automation is needed.

## Run it

```
cp .env.example .env      # set your location; HA settings are optional
docker compose up -d --build
docker exec plants python seed.py   # optional example rooms and plants
```

Open http://localhost:8772. To use it on your phone, put a reverse proxy with HTTPS in front of it and keep it on your LAN or VPN, because there is no login. Don't proxy `/mcp`.

Add the MCP to Claude Code:

```
claude mcp add --transport http plants http://127.0.0.1:8772/mcp
```

Photo worker (optional, runs on the host where `claude` is logged in): `python worker/photo_worker.py`. Use `--once` to test it.

Tests: `docker exec plants python /app/tests/test_schedule.py` (and the other files in `tests/`).

## Schedule rules (plants/schedule.py)

- **Seasons:** each plant has a summer and a winter watering interval in days. The interval slides between them by month: full summer Jun-Aug, full winter Dec-Feb. These are Irish/UK seasons, so adjust `schedule.py` if you're elsewhere.
- **Rain:** outdoor plants open to the sky get a `rain` event for any day with 4mm or more of rain.
- **No winter watering:** if `water_winter` is empty, rain covers winter and there are no reminders from Nov to Feb.
- **Outdoor holds:** outdoor plants are held when 4mm or more of rain is due in the next 24h, or when the night drops to 1°C or below.
- **Feeding:** only between `feed_from` and `feed_to`. The first feed of each season is due at the season's start.

Data (`plants.db` and photos) lives in `./data`. Back that folder up.
