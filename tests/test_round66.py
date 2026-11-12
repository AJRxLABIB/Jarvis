"""Round 66: learning quality control (decay, error-propagation guard, conflicts, hard cases, reversible cleanup)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.core import learning_quality as Q

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def item(i, req="open notes", tool="open_app", kind="skill", uses=0, good=0, bad=0, days=0, retired=0):
    return {"id": i, "kind": kind, "request": req, "tool": tool, "content": "", "uses": uses, "good": good, "bad": bad, "retired": retired,
            "created": (NOW - timedelta(days=days)).isoformat()}


def turn(i, outcome, used=(), corrects=None, tool="open_app", request="r", days=0):
    return {"id": i, "ts": (NOW - timedelta(days=days)).isoformat(), "request": request, "outcome": outcome, "used": json.dumps(list(used)),
            "tools": json.dumps([{"name": tool, "ok": outcome == "good", "args": {}}]), "corrects": corrects}


def test_strength_decays_and_good_use_slows_it():
    fresh, old = item(1), item(1)
    assert Q.strength(fresh, fresh["created"], NOW) == 1.0
    assert Q.strength(old, (NOW - timedelta(days=60)).isoformat(), NOW) < 0.05
    proven = item(2, good=8)
    assert Q.strength(proven, (NOW - timedelta(days=60)).isoformat(), NOW) > Q.strength(old, (NOW - timedelta(days=60)).isoformat(), NOW)


def test_harmful_item_flagged_against_baseline():
    turns = [turn(n, "good") for n in range(10, 20)] + [turn(1, "bad", [7]), turn(2, "bad", [7]), turn(3, "good", [7])]
    assert Q.harmful(item(7), turns)
    assert not Q.harmful(item(8), turns)                  # never shown
    assert not Q.harmful(item(7), turns[:10] + [turn(1, "bad", [7])])   # too few uses


def test_conflict_keeps_better_record():
    a = item(1, "play music now", "play_music", uses=5, good=5)
    b = item(2, "play music now", "open_app", uses=5, good=1)
    pairs = Q.conflicts([a, b])
    assert pairs and pairs[0][0]["id"] == 1 and pairs[0][1]["id"] == 2
    assert not Q.conflicts([a, item(3, "play music now", "play_music")])        # same tool is no conflict
    assert not Q.conflicts([a, item(4, "delete old logs", "x")])                # different request
    assert not Q.conflicts([a, item(2, "play music now", "open_app", retired=1)])


def test_hard_cases_from_corrections():
    turns = [turn(1, "bad", tool="open_app", request="play jazz"), turn(2, "good", corrects=1, tool="play_music")]
    assert Q.hard_cases(turns) == [{"request": "play jazz", "expect_tool": "play_music", "wrong_tool": "open_app"}]
    assert Q.hard_cases([turn(1, "good")]) == []


def test_audit_and_describe():
    items = [item(1, days=90), item(2, "play music now", "play_music", uses=4, good=4), item(3, "play music now", "open_app", uses=4, good=1)]
    r = Q.audit(items, [], NOW)
    assert [d["id"] for d in r["dormant"]] == [1] and 3 in r["retire"] and 1 in r["retire"] and 2 not in r["retire"]
    assert "retire" in Q.describe(r)
    assert "healthy" in Q.describe(Q.audit([item(9, uses=2, good=2)], [], NOW))


class KV:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


def test_retire_and_undo_roundtrip(tmp_path):
    from app.memory.database import Database
    db = Database(tmp_path / "t.db"); db.initialize()
    with db.transaction() as c:
        for n in (1, 2):
            c.execute("INSERT INTO growth_items (kind, request, content, tool, created) VALUES ('skill', ?, 'x', 't', 'now')", (f"r{n}",))
    kv = KV()
    assert Q.retire(db, kv, [1, 2], "test") == 2 and Q.retire(db, kv, [1, 2], "again") == 0
    assert len(kv.d[Q.LEDGER_KEY]) == 1
    assert Q.restore_last(db, kv) == 2 and not kv.d[Q.LEDGER_KEY]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM growth_items WHERE retired = 0").fetchone()[0] == 2


# ---- round 67 ----------------------------------------------------------------------------------------------------------------------------

def _t(i, outcome, tool, args, request="open file report", err=None, minutes=0):
    return {"id": i, "ts": (NOW + timedelta(minutes=minutes)).isoformat(), "request": request, "outcome": outcome, "used": "[]", "corrects": None,
            "tools": json.dumps([{"name": tool, "args": args, "ok": outcome == "good", "error": err}])}


def test_reflect_failure_then_fix():
    bad = _t(1, "bad", "open_file", {"path": "rpt.docx"}, err="not_found")
    good = _t(2, "good", "open_file", {"path": "D:/rpt.docx"}, minutes=1)
    req, text, tool = Q.reflect(bad, good)
    assert tool == "open_file" and "not_found" in text and "D:/rpt.docx" in text
    assert Q.reflect(bad, _t(3, "good", "open_file", {"path": "D:/rpt.docx"}, minutes=60)) is None       # too long ago
    assert Q.reflect(_t(1, "bad", "open_file", {"p": 1}, err="declined"), good) is None                  # you said no: not a tool lesson
    assert Q.reflect(bad, _t(4, "good", "play_music", {"a": 1}, minutes=1)) is None


def test_scheduled_audit_runs_weekly(tmp_path):
    from app.memory.database import Database
    db = Database(tmp_path / "t.db"); db.initialize(); kv = KV()
    with db.transaction() as c:
        c.execute("INSERT INTO growth_items (kind, request, content, tool, created) VALUES ('skill','old','x','t','2020-01-01T00:00:00+00:00')")
    assert Q.scheduled(db, kv, NOW)["retire"]
    assert Q.scheduled(db, kv, NOW + timedelta(days=2)) is None       # not due again yet
    assert Q.scheduled(db, kv, NOW + timedelta(days=8))
    assert Q.scheduled(db, kv, (NOW + timedelta(days=30)).replace(tzinfo=None))


def test_growth_integration(tmp_path):
    from types import SimpleNamespace as NS
    from app.agent.growth import Growth
    from app.memory.database import Database
    db = Database(tmp_path / "g.db"); db.initialize(); g = Growth(db)
    ex = lambda name, ok, args, err=None: NS(call=NS(name=name, arguments=args), result=NS(ok=ok, error=err))
    g.record("open file report", [ex("open_file", False, {"path": "rpt.docx"}, "not_found")])
    g.record("open the file report from drive", [ex("open_file", True, {"path": "D:/rpt.docx"})])
    g.record("something else entirely", [])
    with db.connect() as c:
        kinds = [r["content"] for r in c.execute("SELECT content FROM growth_items WHERE kind='lesson'")]
    assert any("not_found" in k for k in kinds)
    assert g.relevant("open file report")                                # lesson is found again
    assert isinstance(g.cases(), list)


def _db(p):
    from app.memory.database import Database
    d = Database(p / "s.db"); d.initialize(); return d


def test_index_zone_blocks_history_indexing(tmp_path):
    import asyncio
    from app.core import privacy_zones as Z
    from app.memory.semantic import SemanticIndex
    async def emb(texts): return [[1.0, 0.0] for _ in texts]
    idx = SemanticIndex(_db(tmp_path), emb, "m")
    Z.configure(KV()); Z.reset_cache()
    Z.FACTS = lambda: Z.Facts("firefox", "Private Browsing")
    try:
        assert asyncio.run(idx.add_exchange("hello there friend", "hi")) is False
    finally:
        Z.FACTS = None; Z.reset_cache()
