from datetime import datetime

from app.utils.timefmt import time12


def test_twelve_hour_clock_without_leading_zero() -> None:
    assert time12(datetime(2026, 10, 17, 13, 38, 40)) == "1:38 PM"
    assert time12(datetime(2026, 10, 17, 0, 5)) == "12:05 AM"
    assert time12(datetime(2026, 10, 17, 12, 0)) == "12:00 PM"
    assert time12(datetime(2026, 10, 17, 9, 7, 3), seconds=True) == "9:07:03 AM"
