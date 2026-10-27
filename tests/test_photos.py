"""Offline checks of the photo history and the AI work queue. Run: python tests/test_photos.py"""

import sys
import tempfile
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plants"))
import advice  # noqa: E402
import photos  # noqa: E402
from garden import Garden, today  # noqa: E402
from store import Store  # noqa: E402
from PIL import Image  # noqa: E402


def jpeg() -> bytes:
    import io
    b = io.BytesIO()
    Image.new("RGB", (40, 30), (40, 120, 60)).save(b, "JPEG")
    return b.getvalue()


def test_history_and_queue():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        d = Path(d)
        s = Store(d / "plants.db")
        s.upsert_room("kitchen", "Kitchen", 1, "downstairs", False)
        pid = s.add_plant(name="Cactus", room_id="kitchen")
        pics = d / "photos"
        pics.mkdir()
        # what was on disk before the history existed: a seeded photo and an old upload
        (pics / "cactus.jpg").write_bytes(jpeg())
        old = int(time.mktime((2026, 10, 20, 12, 0, 0, 0, 0, -1)))
        (pics / f"plant-{pid}-{old}.jpg").write_bytes(jpeg())
        s.update_plant(pid, photo="cactus.jpg")
        assert photos.backfill(s, pics) == 2
        assert photos.backfill(s, pics) == 0                    # only once
        h = s.photos(pid)
        assert [x["ai_state"] for x in h] == [None, None]       # old ones aren't sent for advice
        assert h[0]["day"] >= "2026-10-20"

        new = photos.add(s, pics, pid, jpeg())
        assert s.plant(pid)["photo"] == new
        h = s.photos(pid)
        assert h[0]["file"] == new and h[0]["ai_state"] == "pending" and len(h) == 3

        w = s.take_ai_work()
        assert w["file"] == new and s.take_ai_work() is None    # handed out once
        s.set_analysis(w["id"], {"verdict": "poorly", "summary": "x", "compared": "", "suggestions": []})
        assert s.photos(pid)[0]["analysis"]["verdict"] == "poorly"
        assert not s.add_photo(pid, new, date.today())          # no duplicates


def test_apply_and_jobs():
    from datetime import timedelta
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = Store(Path(d) / "plants.db")
        s.upsert_room("bathroom", "Bathroom", 1, "upstairs", False)
        pid = s.add_plant(name="Aloe", room_id="bathroom", water_summer=14, water_winter=24)
        g = Garden(s, None)
        a = advice.apply(s, pid, {"field": "water_winter", "value": "35", "label": "x"})
        assert s.plant(pid)["water_winter"] == 35 and "24 days to every 35" in a["desc"]
        assert s.events(pid)[0]["kind"] == "change"
        advice.undo(s, pid, a)
        assert s.plant(pid)["water_winter"] == 24 and not s.events(pid)
        for bad in ({"field": "water_winter", "value": "0"}, {"field": "pot", "value": "huge"},
                    {"field": "room_id", "value": "kitchen"}, {"field": "reminder", "value": "2020-01-01"}):
            try:
                advice.check(bad)
                raise AssertionError(bad)
            except ValueError:
                pass
        soon = (today() + timedelta(days=3)).isoformat()
        advice.apply(s, pid, {"field": "reminder", "value": soon, "task": "Repot", "label": "x"})
        assert s.plant(pid)["task"] == "Repot" and g.jobs() == []          # not due yet
        assert [j["task"] for j in g.jobs(today() + timedelta(days=3))] == ["Repot"]
        g.job_done(pid)
        assert s.plant(pid)["task"] is None and s.events(pid)[0]["kind"] == "job"

        # home page advice: unapplied one-tap changes or a worrying verdict, until hidden
        s.add_photo(pid, "x.jpg", today(), ask_ai=False)
        ph = s.photos(pid)[0]
        sug = [{"kind": "water", "text": "t", "change": {"field": "pot", "value": "large", "label": "L"}}]
        s.set_analysis(ph["id"], {"verdict": "fine", "summary": "", "compared": "", "suggestions": sug})
        s._x("UPDATE photos SET ai_state='done' WHERE id=?", (ph["id"],))
        assert [x["items"][0]["label"] for x in g.advice()] == ["L"]
        s.update_analysis(ph["id"], {"verdict": "fine", "suggestions": [{**sug[0], "applied": {"desc": "x"}}]})
        assert g.advice() == []
        s.update_analysis(ph["id"], {"verdict": "poorly", "summary": "sad", "suggestions": []})
        assert g.advice()[0]["summary"] == "sad"
        s.update_analysis(ph["id"], {"verdict": "poorly", "seen": True, "suggestions": []})
        assert g.advice() == []


if __name__ == "__main__":
    test_history_and_queue()
    print("ok test_history_and_queue")
    test_apply_and_jobs()
    print("ok test_apply_and_jobs")
