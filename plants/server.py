"""Plants service: phone page + JSON API + MCP (/mcp) + Home Assistant link, one process.

Runs in Docker in WSL (see ../docker-compose.yml), bound to 127.0.0.1:8772. nginx serves the
page (keep it on your LAN or VPN) and should NOT expose /mcp.
"""

from __future__ import annotations

import asyncio
import base64
import json
import contextlib
import logging
import os
from datetime import date
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import advice
import notify_image
import photos
import tools
from garden import Garden
from garden import USER_SOURCES
from garden import today as g_today
from store import Store
from weather import WeatherSource

HERE = Path(__file__).parent
log = logging.getLogger("plants")
E = os.environ.get
DATA = Path(E("PLANTS_DATA", str(HERE.parent / "data")))
PHOTOS = DATA / "photos"


def build_app() -> Starlette:
    from fastmcp import FastMCP

    store = Store(DATA / "plants.db")
    weather = WeatherSource(float(E("PLANTS_LAT", "53.35")), float(E("PLANTS_LON", "-6.26")))
    g = Garden(store, weather)
    ha = None
    if E("HA_TOKEN"):
        from ha import HomeAssistant
        ha = HomeAssistant(g, E("HA_URL", "http://homeassistant.local:8123"), E("HA_TOKEN"),
                           E("HA_NOTIFY", "notify"),
                           E("PLANTS_PUBLIC_URL", "http://localhost:8772"),
                           E("PLANTS_NOTIFY_AT", "09:30"), int(E("PLANTS_NOTIFY_EVERY_DAYS", "2")))

    mcp = FastMCP("plants", instructions=tools.INSTRUCTIONS)
    host_root = Path(E("PLANTS_HOST_ROOT")) if E("PLANTS_HOST_ROOT") else None
    tools.register(mcp, g, PHOTOS, host_root)
    mcp_app = mcp.http_app(path="/mcp")

    async def changed() -> None:
        if ha:
            try:
                await ha.refresh()
            except Exception as e:
                log.warning("HA refresh failed: %s", e)

    async def page(_r: Request):
        return FileResponse(HERE / "web" / "index.html", headers={"Cache-Control": "no-cache"})

    async def board(_r: Request):
        return JSONResponse(g.board())

    async def plant(r: Request):
        pid = int(r.path_params["pid"])
        p = store.plant(pid)
        if not p:
            return JSONResponse({"error": "not found"}, 404)
        if r.method == "PATCH":
            body = await r.json()
            store.update_plant(pid, **{k: v for k, v in body.items() if k != "id"})
            await changed()
            p = store.plant(pid)
        st = {s["id"]: s for s in g.statuses()}.get(pid)
        pics = store.photos(pid)
        age = (g_today() - date.fromisoformat(pics[0]["day"])).days if pics else None
        return JSONResponse({**p, "status": st, "history": store.events(pid, 20),
                             "rooms": store.rooms(), "photos": pics, "photo_age_days": age,
                             "photo_nudge": age is not None and age >= photos.NUDGE_DAYS})

    async def add_plant(r: Request):
        b = await r.json()
        room = store.find_room(b.get("room", ""))
        if not b.get("name") or not room:
            return JSONResponse({"error": "name and a known room are needed"}, 400)
        pid = store.add_plant(name=b["name"], room_id=room["id"], species=b.get("species", ""),
                              water_summer=7, water_winter=14,
                              care="Default schedule: ask Claude to set this plant's schedule.")
        await changed()
        return JSONResponse({"id": pid})

    async def mark(r: Request):
        b = await r.json()
        if b.get("room_id") or b.get("all"):
            done = g.mark_all_due(b.get("room_id"), source="web")
        else:
            day = date.fromisoformat(b["day"]) if b.get("day") else None
            done = g.mark([int(i) for i in b["ids"]], b.get("what", "due"), day, source="web")
        await changed()
        return JSONResponse({"done": done})

    async def snooze(r: Request):
        b = await r.json()
        ids = None if b.get("ids") is None else [int(i) for i in b["ids"]]
        until = g.snooze(ids, int(b.get("days", 1)))
        await changed()
        return JSONResponse({"until": until})

    async def unsnooze_all(_r: Request):
        store.set("snoozed_all_until", None)
        await changed()
        return JSONResponse({"ok": True})

    async def undo(r: Request):
        store.delete_event(int(r.path_params["eid"]))
        await changed()
        return JSONResponse({"ok": True})

    async def photo_upload(r: Request):
        pid = int(r.path_params["pid"])
        form = await r.form()
        raw = await form["photo"].read()
        name = await asyncio.to_thread(photos.add, store, PHOTOS, pid, raw)
        await changed()
        return JSONResponse({"photo": name})

    async def photo_item(r: Request):
        """PATCH {main: true} makes it the plant's photo, {ask_ai: true} (re)asks for advice.
        DELETE takes it out of the history (the file stays on disk)."""
        ph = store.photo(int(r.path_params["phid"]))
        if not ph:
            return JSONResponse({"error": "not found"}, 404)
        p = store.plant(ph["plant_id"])
        if r.method == "DELETE":
            store.delete_photo(ph["id"])
            if p["photo"] == ph["file"]:
                rest = store.photos(p["id"])
                store.update_plant(p["id"], photo=rest[0]["file"] if rest else None)
        else:
            b = await r.json()
            if b.get("main"):
                store.update_plant(p["id"], photo=ph["file"])
            if b.get("ask_ai"):
                store.ask_ai(ph["id"])
        await changed()
        return JSONResponse({"ok": True})

    async def photo_work(_r: Request):
        """For worker/photo_worker.py on the PC: the next photo to look over, with the plant's
        details and its previous photo (if one is older than a week) to compare against."""
        ph = store.take_ai_work()
        if not ph:
            return JSONResponse({"task": None})
        p = store.plant(ph["plant_id"])
        st = {s["id"]: s for s in g.statuses()}.get(p["id"]) or {}
        older = [x for x in store.photos(p["id"])
                 if (date.fromisoformat(ph["day"]) - date.fromisoformat(x["day"])).days >= 7]
        b64 = lambda f: base64.b64encode((PHOTOS / f).read_bytes()).decode()
        return JSONResponse({"task": {
            "id": ph["id"], "day": ph["day"], "image": b64(ph["file"]),
            "previous": {"day": older[0]["day"], "image": b64(older[0]["file"])} if older else None,
            "plant": {k: p[k] for k in ("name", "species", "room", "outdoor", "exposure", "in_ground",
                                         "pot", "light", "kind", "care", "notes", "created",
                                         "water_summer", "water_winter", "feed_days", "feed_from",
                                         "feed_to", "task", "task_on")},
            "today": g_today().isoformat(),
            "watering": {k: st.get(k) for k in ("interval_days", "last_watered", "water_due",
                                                 "feed_note")},
            "recent_care": [f"{e['day']} {e['kind']}" for e in store.events(p["id"], 10)],
        }})

    async def photo_apply(r: Request):
        """Do what suggestion #i says ({i}), or put it back ({i, undo: true})."""
        ph = store.photo(int(r.path_params["phid"]))
        b = await r.json()
        a = json.loads(ph["analysis"]) if ph and ph["analysis"] else None
        try:
            if b.get("seen"):               # hidden from the home page's advice list
                a["seen"] = True
                store.update_analysis(ph["id"], a)
                return JSONResponse({"ok": True})
            sug = a["suggestions"][int(b["i"])]
            if b.get("undo"):
                advice.undo(store, ph["plant_id"], sug.pop("applied"))
            else:
                if sug.get("applied"):
                    return JSONResponse({"error": "already done"}, 400)
                sug["applied"] = advice.apply(store, ph["plant_id"], sug["change"])
        except (TypeError, KeyError, IndexError, ValueError) as e:
            return JSONResponse({"error": f"Can't do that one: {e}"}, 400)
        store.update_analysis(ph["id"], a)
        await changed()
        return JSONResponse({"ok": True, "desc": sug.get("applied", {}).get("desc")})

    async def job_done(r: Request):
        g.job_done(int(r.path_params["pid"]), source="web")
        await changed()
        return JSONResponse({"ok": True})

    async def photo_analysis(r: Request):
        b = await r.json()
        store.set_analysis(int(r.path_params["phid"]), b.get("analysis"), b.get("error"))
        return JSONResponse({"ok": True})

    async def unmark(r: Request):
        """Untick: remove what was logged today for these plants."""
        b = await r.json()
        day = g_today().isoformat()
        for pid in b["ids"]:
            for e in store.events(int(pid), 10):
                if e["day"] == day and e["kind"] in ("water", "feed", "skip")                         and e["source"] in USER_SOURCES:
                    store.delete_event(e["id"])
        await changed()
        return JSONResponse({"ok": True})

    async def notify_image_jpg(r: Request):
        """The notification's picture: ?ids=6,9 in that order (default: what is due now).
        ?v= is only a cache-buster for the phone."""
        ids = [int(i) for i in r.query_params.get("ids", "").split(",") if i.strip().isdigit()]
        if ids:
            by_id = {s["id"]: s for s in g.statuses()}
            items = [{"id": i, "name": by_id[i]["name"], "room": by_id[i]["room"],
                      "photo": by_id[i]["photo"], "todo": by_id[i]["todo"]}
                     for i in ids if i in by_id]
        else:
            items = notify_image.due_items(g.board())
        jpg = await asyncio.to_thread(notify_image.render, items, PHOTOS)
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    async def health(_r: Request):
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp_app.lifespan(app):
            tasks = [asyncio.create_task(ha.tick_loop()), asyncio.create_task(ha.listen_loop())] if ha else []
            try:
                yield
            finally:
                for t in tasks:
                    t.cancel()

    PHOTOS.mkdir(parents=True, exist_ok=True)
    if n := photos.backfill(store, PHOTOS):
        log.info("photo history: backfilled %d photos from disk", n)
    return Starlette(routes=[
        Route("/", page),
        Route("/health", health),
        Route("/manifest.json", lambda r: FileResponse(HERE / "web" / "manifest.json",
                                                       media_type="application/manifest+json")),
        Route("/sw.js", lambda r: FileResponse(HERE / "web" / "sw.js", headers={"Cache-Control": "no-cache"})),
        Mount("/icons", app=StaticFiles(directory=str(HERE / "web" / "icons"))),
        Route("/api/unmark", unmark, methods=["POST"]),
        Route("/api/board", board),
        Route("/api/notify-image.jpg", notify_image_jpg),
        Route("/api/plants", add_plant, methods=["POST"]),
        Route("/api/plants/{pid:int}", plant, methods=["GET", "PATCH"]),
        Route("/api/plants/{pid:int}/photo", photo_upload, methods=["POST"]),
        Route("/api/photos/work", photo_work),
        Route("/api/photos/{phid:int}", photo_item, methods=["PATCH", "DELETE"]),
        Route("/api/photos/{phid:int}/apply", photo_apply, methods=["POST"]),
        Route("/api/plants/{pid:int}/job-done", job_done, methods=["POST"]),
        Route("/api/photos/{phid:int}/analysis", photo_analysis, methods=["POST"]),
        Route("/api/mark", mark, methods=["POST"]),
        Route("/api/snooze", snooze, methods=["POST"]),
        Route("/api/unsnooze", unsnooze_all, methods=["POST"]),
        Route("/api/events/{eid:int}", undo, methods=["DELETE"]),
        Mount("/photos", app=StaticFiles(directory=str(PHOTOS))),
        Mount("/", app=mcp_app),
    ], lifespan=lifespan)


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(build_app(), host=E("PLANTS_BIND", "127.0.0.1"), port=int(E("PLANTS_PORT", "8772")),
                proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
