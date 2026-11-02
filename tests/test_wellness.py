from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.core import wellness as W


class Mem:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


def test_off_by_default_and_nothing_is_recorded_until_enabled():
    st = Mem()
    with pytest.raises(PermissionError):
        W.log(st, "sleep_hours", 7)
    assert st.d == {} and W.summary(st) == "Wellness tracking is off." and W.insights(st) == []
    W.set_enabled(st, True)
    W.log(st, "sleep_hours", 7)
    assert W.KEY in st.d


def test_validation_and_delete():
    st = Mem(); W.set_enabled(st, True)
    for metric, value in (("sleep_hours", 30), ("energy", 9), ("heart_rate", 60)):
        with pytest.raises(ValueError):
            W.log(st, metric, value)
    W.log(st, "mood", 4)
    W.delete_all(st)
    assert st.d[W.KEY] == {}


def test_summary_compares_weeks_with_the_working_shown():
    st = Mem(); W.set_enabled(st, True)
    today = date(2026, 10, 29)
    for i in range(7):
        W.log(st, "sleep_hours", 8, today - timedelta(days=i))
        W.log(st, "sleep_hours", 6, today - timedelta(days=7 + i))
    text = W.summary(st, today)
    assert "average 8.0 over 7 day(s) (+2.0 vs the 7 day(s) before)" in text and "not medical advice" in text


def test_insight_needs_enough_paired_days_and_says_pattern_not_cause():
    st = Mem(); W.set_enabled(st, True)
    start = date(2026, 10, 1)
    assert "Not enough data" in W.insights(st)[0]
    for i in range(14):
        sleep = 8 if i % 2 == 0 else 5
        W.log(st, "sleep_hours", sleep, start + timedelta(days=i))
        W.log(st, "energy", 4 if sleep == 8 else 2, start + timedelta(days=i + 1))
    note = W.insights(st, date(2026, 10, 29))[0]
    assert "not a cause" in note and "4.0" in note and "2.0" in note and "paired days" in note


def test_tool_flow():
    from app.tools import wellness_tool as T

    st_ctx = SimpleNamespace(database=None)
    with pytest.raises(Exception):
        T.wellness(T.WellArgs(action="summary"), st_ctx)
    assert T._risk(T.WellArgs(action="enable"), st_ctx).name == "MODERATE" and T._risk(T.WellArgs(action="log"), st_ctx).name == "SAFE"
