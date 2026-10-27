"""Home Assistant link: a sensor.plants_due entity, one morning notification with buttons, and
the buttons' replies (heard over HA's websocket, so HA needs no automation for them)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime

import httpx
import websockets

import notify_image
from garden import TZ, Garden, today

log = logging.getLogger("plants.ha")
TAG = "plants-due"
ACT_DONE, ACT_SNOOZE = "PLANTS_DONE", "PLANTS_SNOOZE"


def summary_lines(board: dict) -> list[str]:
    lines = []
    for r in board["rooms"]:
        names = []
        for s in r["plants"]:
            if s["id"] in r["due"]:
                names.append(s["name"] + (" + feed" if "feed" in s["todo"] else "")
                             if "water" in s["todo"] else s["name"] + " (feed only)")
        if names:
            lines.append(f"{r['name']}: {', '.join(names)}")
    return lines


def should_notify(now_hhmm: str, notify_at: str, day: date, last_on: str | None,
                  last_ids: set[int], due_ids: list[int], every_days: int) -> bool:
    """Morning nudge when plants first come due, then again every `every_days` days while
    anything is still pending. A plant newly due gets a nudge the next morning it can."""
    if not due_ids or now_hhmm < notify_at:
        return False
    if last_on is None:
        return True
    if last_on == day.isoformat():
        return False
    if set(due_ids) - last_ids:
        return True
    return (day - date.fromisoformat(last_on)).days >= every_days


class HomeAssistant:
    def __init__(self, garden: Garden, url: str, token: str, notify: str, public_url: str,
                 notify_at: str = "09:30", every_days: int = 2):
        self.g, self.url, self.token = garden, url.rstrip("/"), token
        self.notify_service, self.public_url, self.notify_at = notify, public_url.rstrip("/"), notify_at
        self.every_days = every_days
        self.h = {"Authorization": f"Bearer {token}"}

    async def _post(self, path: str, body: dict) -> None:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(self.url + path, headers=self.h, json=body)
            r.raise_for_status()

    async def push_state(self, board: dict) -> None:
        lines = summary_lines(board)
        await self._post("/api/states/sensor.plants_due", {
            "state": board["due_count"],
            "attributes": {"friendly_name": "Plants due", "icon": "mdi:sprout",
                           "unit_of_measurement": "plants", "summary": "\n".join(lines),
                           "plants": [d["name"] for d in board["due"]],
                           "held": [f"{h['name']}: {h['hold']}" for h in board["held"]],
                           "snoozed_until": board["snoozed_all_until"],
                           "jobs": [f"{j['task']}: {j['name']}" for j in board.get("jobs", [])],
                           "url": self.public_url + "/"}})

    def image_url(self, board: dict) -> str:
        """Grid of every due plant's photo, labelled, in the text list's order. The phone
        fetches it from the public URL (LAN or VPN), like the page itself."""
        items = notify_image.due_items(board)
        ids = ",".join(str(i["id"]) for i in items)
        return f"{self.public_url}/api/notify-image.jpg?ids={ids}&v={notify_image.version(items)}"

    async def send_notification(self, board: dict) -> None:
        lines = summary_lines(board)
        n = board["due_count"]
        data = {"tag": TAG, "notification_icon": "mdi:sprout", "clickAction": self.public_url + "/",
                "url": self.public_url + "/",
                "actions": [{"action": ACT_DONE, "title": "✅ All done"},
                            {"action": ACT_SNOOZE, "title": "😴 Tomorrow"},
                            {"action": "URI", "title": "🪴 List", "uri": self.public_url + "/"}]}
        data["image"] = self.image_url(board)
        await self._post(f"/api/services/notify/{self.notify_service}", {
            "title": f"🪴 {n} plant{'s' if n != 1 else ''} to water" if n else "🪴 Plants",
            "message": "\n".join(lines) or "Nothing due", "data": data})

    async def send_job_notifications(self, board: dict) -> None:
        """One phone notification per one-off job (e.g. 'Repot', from photo advice), on its day."""
        if datetime.now(TZ).strftime("%H:%M") < self.notify_at:
            return
        st = self.g.store
        for j in board.get("jobs", []):
            key = f"job_notified_{j['id']}"
            if st.get(key) == j["task_on"]:
                continue
            await self._post(f"/api/services/notify/{self.notify_service}", {
                "title": f"🪴 {j['task']}: {j['name']}", "message": f"{j['room']}. Tick it off on the plant page.",
                "data": {"tag": f"plants-job-{j['id']}", "notification_icon": "mdi:sprout",
                         "clickAction": self.public_url + "/", "url": self.public_url + "/"}})
            st.set(key, j["task_on"])

    async def clear_notification(self) -> None:
        await self._post(f"/api/services/notify/{self.notify_service}",
                         {"message": "clear_notification", "data": {"tag": TAG}})

    async def refresh(self, clear_if_none: bool = True) -> dict:
        """Recompute, update the sensor, and drop the phone notification once nothing is due."""
        board = self.g.board()
        await self.push_state(board)
        if clear_if_none and board["due_count"] == 0 and self.g.store.get("notified_on"):
            await self.clear_notification()
            self.g.store.set("notified_on", None)
            self.g.store.set("notified_ids", None)
        return board

    # loops
    async def tick_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.g.log_rain)
                board = await self.refresh()
                st = self.g.store
                last_on = st.get("notified_on")
                last_ids = {int(i) for i in (st.get("notified_ids") or "").split(",") if i}
                due_ids = [d["id"] for d in board["due"]]
                if should_notify(datetime.now(TZ).strftime("%H:%M"), self.notify_at, today(),
                                 last_on, last_ids, due_ids, self.every_days):
                    await self.send_notification(board)
                    st.set("notified_on", today().isoformat())
                    st.set("notified_ids", ",".join(map(str, due_ids)))
                await self.send_job_notifications(board)
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(600)

    async def listen_loop(self) -> None:
        ws_url = self.url.replace("http", "ws", 1) + "/api/websocket"
        while True:
            try:
                async with websockets.connect(ws_url, max_size=2**22) as ws:
                    await ws.recv()
                    await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                    if json.loads(await ws.recv()).get("type") != "auth_ok":
                        raise RuntimeError("HA websocket auth failed")
                    await ws.send(json.dumps({"id": 1, "type": "subscribe_events",
                                              "event_type": "mobile_app_notification_action"}))
                    log.info("listening for notification buttons")
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") != "event":
                            continue
                        action = msg["event"]["data"].get("action")
                        if action == ACT_DONE:
                            done = await asyncio.to_thread(self.g.mark_all_due, None, "ha")
                            log.info("All done from phone: %s", [d["name"] for d in done])
                            await self.refresh()
                            await self.clear_notification()
                        elif action == ACT_SNOOZE:
                            self.g.snooze(None, days=1)
                            log.info("snoozed until tomorrow from phone")
                            await self.refresh(clear_if_none=False)
                            await self.clear_notification()
            except Exception as e:
                log.warning("HA websocket: %s; retrying in 30s", e)
                await asyncio.sleep(30)
