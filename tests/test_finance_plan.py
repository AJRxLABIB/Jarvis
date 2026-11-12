from datetime import date

import pytest

from app.core import finance_plan as fp
from app.core.finance import Finance
from app.memory.database import Database


class Mem:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "a.db"); db.initialize()
    return Finance(db), Mem(), tmp_path


def test_amount_and_date_parsing():
    assert fp._parse_amount("-1,250.50") == -1250.5 and fp._parse_amount("(300)") == -300 and fp._parse_amount("1.250,50") == 1250.5
    assert fp._parse_amount("BDT 2,000") == 2000 and fp._parse_amount("abc") is None
    assert fp._parse_date("05/10/2026").day == 5 and fp._parse_date("2026-10-05").month == 10 and fp._parse_date("soon") is None


def test_import_previews_then_commits_and_never_doubles(env):
    fin, _, tmp = env
    (tmp / "b.csv").write_text("Date,Description,Amount\n2026-10-02,Lunch cafe,-250.50\n2026-10-03,Salary,50000\n"
                               "2026-10-05,Uber ride,-300\n2026-10-05,Uber ride,-300\nbad,x,1\n", encoding="utf-8")
    pre = fp.import_csv(fin, tmp / "b.csv")
    assert pre.dry_run and pre.imported == 3 and fin.recent() == []                  # preview adds nothing; two real identical rides both count
    done = fp.import_csv(fin, tmp / "b.csv", commit=True)
    assert done.imported == 3 and len(fin.recent()) == 3 and len(done.skipped) == 1  # salary (a credit) ignored, bad date reported
    again = fp.import_csv(fin, tmp / "b.csv", commit=True)
    assert again.imported == 0 and again.duplicates == 3 and len(fin.recent()) == 3


def test_import_positive_expenses_and_bad_files(env):
    fin, _, tmp = env
    (tmp / "c.csv").write_text("Date;Details;Debit\n05/10/2026;Pharmacy;120,00\n", encoding="utf-8")
    assert fp.import_csv(fin, tmp / "c.csv", commit=True).imported == 1
    (tmp / "d.csv").write_text("foo,bar\n1,2\n", encoding="utf-8")
    assert "date and an amount" in fp.import_csv(fin, tmp / "d.csv").skipped[0]
    (tmp / "e.csv").write_text("Date,Amount\n", encoding="utf-8")
    assert fp.import_csv(fin, tmp / "e.csv").skipped == ["no data rows"]


def test_recurring_upcoming_and_month_end_clamp(env):
    _, st, _ = env
    fp.add_recurring(st, "Rent", 15000, 31, "bills")
    fp.add_recurring(st, "Internet", 1200, 5)
    assert fp.due_date({"day": 31}, 2026, 2) == date(2026, 2, 28)
    due = fp.upcoming(st, date(2026, 10, 28), 10)
    assert [i["name"] for _, i in due] == ["Rent", "Internet"] and due[0][0] == date(2026, 10, 31) and due[1][0] == date(2026, 11, 5)
    with pytest.raises(ValueError):
        fp.add_recurring(st, "", 10, 3)
    assert fp.remove_item(st, "recurring", "rent") and not fp.remove_item(st, "recurring", "rent")


def test_savings_progress(env):
    _, st, _ = env
    fp.add_savings(st, "Laptop", 60000, "2027-02-28", saved=12000)
    row = fp.savings_status(st, date(2026, 10, 29))[0]
    assert row["left"] == 48000 and row["months_left"] == 4 and row["per_month_needed"] == 12000 and row["percent"] == 20
    with pytest.raises(ValueError):
        fp.add_savings(st, "x", 0)


def test_forecast_shows_its_arithmetic_and_assumptions(env):
    from datetime import datetime, timezone

    fin, st, _ = env
    fin.add(1000, "lunch", "food", when=datetime(2026, 10, 3, tzinfo=timezone.utc))
    fin.add(500, "bus", "transport", when=datetime(2026, 10, 6, tzinfo=timezone.utc))
    fp.add_recurring(st, "Rent", 15000, 1, "bills")
    fp.add_recurring(st, "Internet", 1200, 28, "bills")
    fin.set_budget("food", 5000)
    f = fp.forecast(fin, st, date(2026, 10, 10))
    pace = 1500 / 10
    assert f.daily_pace == pace and f.recurring_remaining == 1200
    assert round(f.projected_total) == round(1500 + pace * 21 + 1200)
    assert "estimate" in f.describe() and "your pace so far continues" in f.describe()
    early = fp.forecast(fin, st, date(2026, 10, 2))
    assert any("very uncertain" in a for a in early.assumptions)


def test_scenarios_compare_with_the_same_assumptions(env):
    from datetime import datetime, timezone

    fin, st, _ = env
    fin.add(3000, "groceries", "food", when=datetime(2026, 10, 2, tzinfo=timezone.utc))
    base = fp.forecast(fin, st, date(2026, 10, 10)).projected_total
    text = fp.scenario(fin, st, date(2026, 10, 10), cut={"food": 50})
    assert "food spending 50% lower" in text and "-" in text.split("(")[1]
    worse = fp.forecast(fin, st, date(2026, 10, 10), extra_recurring=700).projected_total
    assert worse == base + 700


def test_tool_import_asks_and_preview_is_safe(env):
    from types import SimpleNamespace

    from app.tools import finance_plan_tool as T

    fin, _, tmp = env
    (tmp / "b.csv").write_text("Date,Description,Amount\n2026-10-02,Lunch,-250\n", encoding="utf-8")
    policy = SimpleNamespace(resolve=lambda p: p)
    ctx = SimpleNamespace(database=fin.db, settings=SimpleNamespace(currency="BDT"), get_path_policy=lambda: policy)
    r = T.finance_plan(T.PlanArgs(action="import_preview", path=str(tmp / "b.csv")), ctx)
    assert r.data["dry_run"] and fin.recent() == []
    assert T._risk(T.PlanArgs(action="import", path="x"), ctx).name == "MODERATE" and T._risk(T.PlanArgs(action="forecast"), ctx).name == "SAFE"
    assert T.finance_plan(T.PlanArgs(action="import", path=str(tmp / "b.csv")), ctx).data["imported"] == 1
