"""Offline checks of the notification picture. Run: python tests/test_notify_image.py"""

import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plants"))
from PIL import Image  # noqa: E402

import notify_image as ni  # noqa: E402

BOARD = {"rooms": [
    {"name": "Kitchen", "due": [2, 3], "plants": [
        {"id": 1, "name": "Not due", "photo": None, "todo": []},
        {"id": 2, "name": "Cactus", "photo": "c.jpg", "todo": ["water"]},
        {"id": 3, "name": "Moneymaker", "photo": None, "todo": ["water", "feed"]}]},
    {"name": "Office", "due": [7], "plants": [
        {"id": 7, "name": "Autograph Tree", "photo": "missing.jpg", "todo": ["water"]}]}]}


def test_order_and_render():
    items = ni.due_items(BOARD)
    assert [i["id"] for i in items] == [2, 3, 7]
    assert items[2]["room"] == "Office"
    with tempfile.TemporaryDirectory() as d:
        Image.new("RGB", (300, 200), (0, 120, 0)).save(Path(d) / "c.jpg")
        for n in range(1, 10):
            img = Image.open(io.BytesIO(ni.render((items * 4)[:n], Path(d))))
            assert img.format == "JPEG" and 1 <= img.width / img.height <= 3.1, (n, img.size)
    assert ni.version(items) != ni.version(items[:2])


if __name__ == "__main__":
    test_order_and_render()
    print("ok test_order_and_render")
