"""Offline checks of when the phone gets nudged. Run: python tests/test_notify.py"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plants"))
from ha import should_notify  # noqa: E402

D = date(2026, 10, 22)


def test_rules():
    assert not should_notify("09:30", "09:30", D, None, set(), [], 2)            # nothing due
    assert not should_notify("08:00", "09:30", D, None, set(), [1], 2)           # too early
    assert should_notify("09:30", "09:30", D, None, set(), [1], 2)               # first nudge
    assert not should_notify("12:00", "09:30", D, "2026-10-22", {1}, [1], 2)     # once a day
    assert not should_notify("09:30", "09:30", D, "2026-10-21", {1}, [1], 2)     # still pending, 1 day
    assert should_notify("09:30", "09:30", D, "2026-10-20", {1}, [1], 2)         # pending 2 days: nag
    assert should_notify("09:30", "09:30", D, "2026-10-21", {1}, [1, 2], 2)      # new plant due


if __name__ == "__main__":
    test_rules()
    print("ok test_rules")
