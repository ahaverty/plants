"""Photo worker: gives an AI look-over to new plant photos, on the host PC.

The plants app (in Docker) queues each new photo. This worker polls
http://127.0.0.1:8772/api/photos/work every few seconds, asks Claude (Claude Code,
`claude -p`, on your own Claude subscription, Sonnet, with tools, MCP servers, settings and session
history all off) for a verdict and suggestions with a JSON schema (prompt.md), and posts the
answer back to /api/photos/<id>/analysis. The plant page shows it under the photo.
Keep it running with any process supervisor (Task Scheduler, systemd, ...).

  python worker\\photo_worker.py           run forever
  python worker\\photo_worker.py --once    do at most one photo and exit (testing)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
URL = os.environ.get("PLANTS_URL", "http://127.0.0.1:8772").rstrip("/")
CLAUDE = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local" / "bin" / "claude.exe"))
NO_WINDOW = 0x08000000      # CREATE_NO_WINDOW: no console flashing up from pythonw

log = logging.getLogger("photo-worker")

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["thriving", "fine", "struggling", "poorly", "dead", "unclear"]},
        "summary": {"type": "string"},
        "compared": {"type": "string"},
        "suggestions": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["placement", "water", "repot", "prune", "pests",
                                                "feed", "replace", "photo", "other"]},
            "text": {"type": "string"},
            # optional: what the app can do for you with one tap (plants/advice.py checks it)
            "change": {"type": "object", "properties": {
                "field": {"type": "string", "enum": ["water_summer", "water_winter", "feed_days", "pot",
                                                     "light", "archive", "reminder"]},
                "value": {"type": "string"},
                "label": {"type": "string"},
                "task": {"type": "string"}}, "required": ["field", "value", "label"]}},
            "required": ["kind", "text"]}},
    },
    "required": ["verdict", "summary", "compared", "suggestions"],
}


def call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def describe(task: dict) -> str:
    p, w = task["plant"], task["watering"]
    if p["outdoor"]:
        where = "outdoors, " + ("in the ground" if p["in_ground"] else "in a pot")
        if p["exposure"] == "covered":
            where += ", under cover"
    else:
        where = "indoors"
    out = [f"Plant: {p['name']}" + (f" ({p['species']})" if p["species"] else ""),
           f"Room: {p['room']} ({where})",
           f"Photo taken: {task['day']}. Tracked since {p['created']}."]
    if p["kind"] != "normal":
        out.append(f"Stage: {p['kind']}")
    out.append(f"Today: {task['today']}.")
    out.append(f"Settings: water_summer={p['water_summer']} days, water_winter="
               f"{p['water_winter'] if p['water_winter'] is not None else 'none (rain covers winter)'}, "
               f"feed_days={p['feed_days'] if p['feed_days'] is not None else 'never'} "
               f"(months {p['feed_from']}-{p['feed_to']}), pot={p['pot'] or 'not set'}, "
               f"light={p['light'] or 'not set'}")
    if p.get("task"):
        out.append(f"Already set reminder: {p['task']} on {p['task_on']}")
    if w.get("interval_days"):
        out.append(f"Watered every {w['interval_days']} days at the moment, "
                   f"last {w.get('last_watered') or 'unknown'}.")
    if w.get("feed_note"):
        out.append(f"Feeding: {w['feed_note']}")
    if p["care"]:
        out.append(f"Care note: {p['care']}")
    if p["notes"]:
        out.append(f"Owner's notes: {p['notes']}")
    if task["recent_care"]:
        out.append("Recent care log: " + "; ".join(task["recent_care"]))
    if task.get("previous"):
        out.append(f"The FIRST image is an earlier photo from {task['previous']['day']}. "
                   f"The SECOND image is the new photo from {task['day']}: judge that one.")
    else:
        out.append("No earlier photo to compare with.")
    return "\n".join(out)


def ask_claude(task: dict) -> dict:
    imgs = ([task["previous"]["image"]] if task.get("previous") else []) + [task["image"]]
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
               for b in imgs]
    content.append({"type": "text", "text": describe(task)})
    cmd = [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--model", "sonnet", "--tools", "", "--setting-sources", "", "--strict-mcp-config",
           "--disable-slash-commands", "--no-session-persistence",
           "--system-prompt-file", str(HERE / "prompt.md"), "--json-schema", json.dumps(SCHEMA)]
    msg = {"type": "user", "message": {"role": "user", "content": content}}
    t = time.time()
    p = subprocess.run(cmd, input=json.dumps(msg) + "\n", capture_output=True, text=True, encoding="utf-8",
                       timeout=300, cwd=str(HERE), creationflags=NO_WINDOW)
    for line in reversed(p.stdout.splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "result":
            u = e.get("usage") or {}
            log.info("claude: %.0fs, in %s+%s cached, out %s", time.time() - t,
                     u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
                     u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0))
            if e.get("structured_output"):
                return e["structured_output"]
            return json.loads(e.get("result") or "{}")
    raise RuntimeError(f"claude gave no result (exit {p.returncode}): {p.stderr.strip()[-300:]}")


def do(task: dict) -> None:
    phid = task["id"]
    try:
        res = ask_claude(task)
        log.info("photo %s (%s): %s, %d suggestions", phid, task["plant"]["name"], res.get("verdict"),
                 len(res.get("suggestions", [])))
        call("POST", f"/api/photos/{phid}/analysis", {"analysis": res})
    except subprocess.TimeoutExpired:
        call("POST", f"/api/photos/{phid}/analysis", {"error": "Looking at it took too long"})
    except Exception as e:
        log.warning("photo %s failed: %s", phid, e)
        call("POST", f"/api/photos/{phid}/analysis", {"error": "Claude couldn't look at it"})


def main() -> None:
    logging.basicConfig(filename=str(HERE / "worker.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if sys.stdout and sys.stdout.isatty():
        logging.getLogger().addHandler(logging.StreamHandler())
    once = "--once" in sys.argv
    log.info("photo worker started")
    while True:
        try:
            task = call("GET", "/api/photos/work").get("task")
            if task:
                do(task)
                if once:
                    return
                continue        # there may be more
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.debug("plants app not reachable: %s", e)
        if once:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
