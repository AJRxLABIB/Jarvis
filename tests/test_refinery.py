from datetime import date
from types import SimpleNamespace

import pytest

from app.core import refinery as R


class Mem:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


read = lambda p: p.read_text(encoding="utf-8")      # noqa: E731


def test_extraction_has_provenance_and_honest_confidence():
    text = "Budget: 5000\nDeadline: 2026-11-02\n- [ ] email the client\nWe decided to use Postgres\nthe report is due by 5 Nov 2026\nnothing here\n"
    facts = {f.kind + str(f.line): f for f in R.extract(text, "/d/a.md", "abc123")}
    assert facts["value1"].value == "5000" and facts["value1"].line == 1 and facts["value1"].version == "abc123"
    assert facts["deadline2"].value == "2026-11-02" and facts["deadline5"].value == "2026-11-05"
    assert facts["task3"].confidence == 0.9 and facts["decision4"].confidence == 0.7 and facts["decision4"].needs_review
    assert len(facts) == 5 and R.parse_date("soon") is None


def test_incremental_run_stale_removed_and_unchanged(tmp_path):
    (tmp_path / "a.md").write_text("Budget: 5000\n- [ ] call the bank\n")
    st = Mem()
    r1 = R.run(st, [tmp_path], reader=read)
    assert (r1.new, r1.facts_added) == (1, 2)
    r2 = R.run(st, [tmp_path], reader=read)
    assert (r2.unchanged, r2.facts_added, r2.new, r2.changed) == (1, 0, 0, 0)       # nothing re-read, nothing duplicated
    (tmp_path / "a.md").write_text("Budget: 5000\n")
    r3 = R.run(st, [tmp_path], reader=read)
    assert r3.changed == 1 and r3.facts_stale == 1
    assert [f.status for f in R.all_facts(st) if f.kind == "task"] == ["stale"]
    (tmp_path / "a.md").unlink()
    assert R.run(st, [tmp_path], reader=read).removed == 1
    assert {f.status for f in R.all_facts(st)} == {"stale"}


def test_sensitive_excluded_unreadable_and_missing_folder(tmp_path):
    (tmp_path / "ok.txt").write_text("Owner: Rina\n"); (tmp_path / "secret.txt").write_text("Owner: X\n"); (tmp_path / "bad.txt").write_text("x")
    def reader(p):
        if p.name == "bad.txt": raise ValueError("boom")
        return p.read_text()
    r = R.run(Mem(), [tmp_path, tmp_path / "nope"], reader=reader, sensitive=lambda p: p.name == "secret.txt")
    assert r.scanned == 2 and r.skipped == 1 and r.facts_added == 1 and len(r.errors) == 2


def test_conflicts_and_review_queue_never_trust_silently(tmp_path):
    (tmp_path / "a.md").write_text("Budget: 5000\nDeadline: 2026-11-02\n")
    (tmp_path / "b.md").write_text("Budget: 7000\nBudget: 5000\n")
    st = Mem(); R.run(st, [tmp_path], reader=read)
    facts = R.all_facts(st)
    assert len(R.conflicts(facts)) == 1
    queue = R.review_queue(facts, date(2026, 10, 29))
    reasons = " | ".join(w for _, w in queue)
    assert "conflicts with" in reasons and "deadline in 4 day(s)" in reasons
    assert all(f.status == "extracted" for f in facts)                              # nothing is confirmed by the pipeline itself


def test_only_confirmed_facts_reach_memory(tmp_path):
    (tmp_path / "a.md").write_text("Owner: Rina\n")
    st = Mem(); R.run(st, [tmp_path], reader=read)
    fid = R.all_facts(st)[0].id
    remembered = []
    mem = SimpleNamespace(remember=lambda text, **kw: remembered.append((text, kw)))
    with pytest.raises(ValueError, match="Confirm the fact first"):
        R.promote(st, mem, fid)
    R.set_status(st, fid[:6], "confirmed")
    text = R.promote(st, mem, fid)
    assert "a.md, line 1" in text and "confirmed by you" in text and remembered[0][1]["provenance"] == "suggestion"
    R.set_status(st, fid, "rejected")
    with pytest.raises(ValueError):
        R.promote(st, mem, fid)
    with pytest.raises(KeyError):
        R.set_status(st, "zzzzzz", "confirmed")


def test_changed_file_keeps_the_users_decision_on_a_surviving_fact(tmp_path):
    (tmp_path / "a.md").write_text("Owner: Rina\nBudget: 1\n")
    st = Mem(); R.run(st, [tmp_path], reader=read)
    owner = next(f for f in R.all_facts(st) if f.key == "owner")
    R.set_status(st, owner.id, "confirmed")
    (tmp_path / "a.md").write_text("Owner: Rina\nBudget: 2\n")
    R.run(st, [tmp_path], reader=read)
    assert next(f for f in R.all_facts(st) if f.id == owner.id).status == "confirmed"
