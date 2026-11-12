"""Round 72: diagnostic investigation (evidence -> ranked causes), deadline risk, scheduler wiring (loops, threads, mode hold)."""
from __future__ import annotations

import asyncio
import threading
from datetime import date


from app.core import deadlines as D, investigate as I, resources as R


def ev(source, text, key="", direct=False, at=""):
    return I.Evidence("*", source, text, at, direct=direct, key=key)


def col(*items, gaps=()):
    return lambda domains: (list(items), list(gaps))


# ---- investigation ---------------------------------------------------------------------------------------------------------------------

def test_domains_from_symptom_and_default():
    assert "voice" in I.domains_for("my microphone is not working") and "database" in I.domains_for("database is locked")
    assert I.domains_for("something odd") == ["model", "memory", "disk"]


def test_confirmed_needs_a_direct_error_not_just_a_hint():
    log = ev("log", "2026-11-08 10:00:01 ERROR httpx.ConnectError: connection refused (ollama)", direct=True, at="2026-11-08 10:00:01")
    inv = I.investigate("answers are very slow", [col(log)])
    top = inv.hypotheses[0]
    assert top.id == "ollama_down" and top.confidence == I.CONFIRMED and "check that Ollama is running" in top.steps[0]
    hint_only = I.investigate("answers are very slow", [col(ev("resource", "only 0.4 GB GPU memory free", key="vram_low"))])
    assert hint_only.hypotheses[0].id == "gpu_oom" and hint_only.hypotheses[0].confidence == I.POSSIBLE


def test_two_independent_sources_make_it_strongly_supported():
    inv = I.investigate("it freezes and lags", [col(ev("resource", "only 0.5 GB GPU memory free", key="vram_low")), col(ev("event", "PcProblem: gpu busy", key="vram_low"))])
    assert inv.hypotheses[0].confidence == I.STRONG


def test_no_match_is_unknown_not_a_guess_and_gaps_are_shown():
    inv = I.investigate("answers are slow", [col(gaps=["GPU memory (no NVIDIA reading)"]), lambda d: 1 / 0])
    assert inv.hypotheses[0].confidence == I.UNKNOWN and any("failed" in g for g in inv.gaps)
    text = inv.text()
    assert "No cause was found" in text and "Could not read" in text and "GPU memory" in text


def test_only_relevant_domains_are_considered():
    inv = I.investigate("my phone will not pair", [col(ev("resource", "only 0.4 GB GPU memory free", key="vram_low"), ev("check", "no input device", key="no_input_device", direct=True))])
    assert all(h.id not in ("gpu_oom", "mic_missing") for h in inv.hypotheses)


def test_recheck_reports_resolved_unchanged_or_cannot_tell():
    bad = ev("log", "out of memory while loading", direct=True)
    inv = I.investigate("freezes with an out of memory error", [col(bad)])
    assert I.recheck(inv, [col(bad)]) == {"gpu_oom": "unchanged"}
    assert I.recheck(inv, [col()]) == {"gpu_oom": "resolved"}
    assert I.recheck(inv, [col(gaps=["log"])]) == {"gpu_oom": "cannot_tell"}


def test_log_collector_reads_errors_and_hides_secrets(tmp_path):
    (tmp_path / "jarvis.log").write_text("2026-11-08 09:00:00 INFO fine\n2026-11-08 09:00:05 ERROR sqlite3.OperationalError: database is locked token=abc123\n")
    items, gaps = I.log_collector(tmp_path)(["database"])
    assert len(items) == 1 and "abc123" not in items[0].text and "[hidden]" in items[0].text and gaps == []
    inv = I.investigate("database is locked", [I.log_collector(tmp_path)])
    assert inv.hypotheses[0].id == "db_locked" and inv.hypotheses[0].confidence == I.CONFIRMED
    assert I.log_collector(tmp_path / "none")(["x"]) == ([], ["no log file"])


def test_resource_and_finding_collectors():
    from types import SimpleNamespace as NS
    snap = NS(ram_free_gb=0.5, ram_total_gb=16.0, disk_free_gb=1.0)
    c = I.resource_collector(lambda: snap, lambda: R.Gpu())
    items, gaps = c(["memory", "disk", "model"])
    assert {e.key for e in items} == {"ram_low", "disk_low"} and any("GPU" in g for g in gaps)
    f = NS(id="no_input_device", problem="No microphone", evidence="0 devices", severity="problem")
    assert I.finding_collector(lambda: [f])(["voice"])[0][0].key == "no_input_device"


# ---- deadline risk ---------------------------------------------------------------------------------------------------------------------

TODAY = date(2026, 11, 8)


def test_deadline_rules():
    todos = [{"id": 1, "text": "pay rent", "due": "2026-11-05", "done": 0}, {"id": 2, "text": "call mum", "due": "2026-11-09", "done": 0},
             {"id": 3, "text": "later", "due": "2026-12-30", "done": 0}, {"id": 4, "text": "done one", "due": "2026-11-01", "done": 1}, {"id": 5, "text": "no date", "due": None, "done": 0}]
    todos += [{"id": 10 + i, "text": f"t{i}", "due": "2026-11-10", "done": 0} for i in range(7)]
    goals = [{"title": "thesis", "deadline": "2026-11-12", "progress": 20, "status": "active", "created_at": "2026-10-01T00:00:00", "tasks": [
                {"id": 1, "text": "write ch2", "done": 0, "depends_on": 9}, {"id": 2, "text": "ok", "done": 1, "depends_on": None}]},
             {"title": "late goal", "deadline": "2026-11-01", "progress": 50, "status": "active", "created_at": "2026-10-01T00:00:00", "tasks": []},
             {"title": "fine", "deadline": "2027-03-01", "progress": 10, "status": "active", "created_at": "2026-11-01T00:00:00", "tasks": []},
             {"title": "dropped", "deadline": "2026-10-01", "progress": 0, "status": "dropped", "created_at": "2026-09-01T00:00:00", "tasks": []}]
    kinds = {(r.kind, r.text.split("'")[1] if "'" in r.text else "") for r in D.assess(todos, goals, TODAY)}
    assert ("overdue", "pay rent") in kinds and ("due_soon", "call mum") in kinds and ("overdue", "late goal") in kinds
    assert ("due_soon", "thesis") in kinds and ("behind_pace", "thesis") in kinds and ("blocked", "write ch2") in kinds
    assert not any(k[1] in ("later", "done one", "no date", "dropped", "fine") for k in kinds)
    assert any(r.kind == "overloaded" for r in D.assess(todos, goals, TODAY))
    assert D.assess(todos, goals, TODAY)[0].level == "high"
    assert "Not measured" in D.describe([]) and "rule:" in D.describe(D.assess(todos, goals, TODAY))


def test_load_reads_the_real_tables(tmp_path):
    from app.memory.database import Database
    db = Database(tmp_path / "t.db"); db.initialize()
    with db.transaction() as c:
        c.execute("INSERT INTO todos (text, due, created_at) VALUES ('old', '2026-11-01', '2026-10-01')")
        c.execute("INSERT INTO goals (title, deadline, created_at, updated_at) VALUES ('g', '2026-11-20', '2026-10-01', '2026-10-01')")
    todos, goals = D.load(db)
    assert [r.kind for r in D.assess(todos, goals, TODAY)].count("overdue") == 1 and goals[0]["tasks"] == []


# ---- scheduler wiring ------------------------------------------------------------------------------------------------------------------

def test_hold_from_another_thread_releases_jobs_on_their_own_loop():
    async def go():
        s = R.WorkloadScheduler(max_concurrent=2)
        s.hold(R.P3)
        async def w(): return "ran"
        job = s.submit(R.P3, "indexing", w)
        await asyncio.sleep(0.02)
        assert job.state == "queued"
        t = threading.Thread(target=lambda: s.hold(None))                  # the upkeep worker thread
        t.start(); t.join()
        return await asyncio.wait_for(job.future, 2)
    assert asyncio.run(go()) == "ran"


def test_apply_mode_hold_and_shared_scheduler():
    s = R.scheduler()
    assert s is R.scheduler()
    R.apply_mode_hold(False, True)
    assert s.stats()["held_from"] == R.P3
    R.apply_mode_hold(True, True)
    assert s.stats()["held_from"] is None


def test_index_documents_runs_through_the_scheduler(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from app.tools import knowledge as K

    calls = []

    class FakeIndex:
        async def index_folder(self, folder):
            calls.append(str(folder))
            return {"indexed": 2, "unchanged": 0, "skipped": 0, "passages": 5, "removed": 0}

    monkeypatch.setattr(K, "semantic_index", lambda ctx: FakeIndex())
    seen = []
    real = R.WorkloadScheduler.run
    async def spy(self, priority, name, work, vram_gb=0.0, timeout=None):
        seen.append((priority, name))
        return await real(self, priority, name, work, vram_gb, timeout)
    monkeypatch.setattr(R.WorkloadScheduler, "run", spy)
    res = asyncio.run(K.index_documents(K.IndexArgs(folder=str(tmp_path)), NS(settings=NS(), paths=None)))
    assert res.ok and "2 new" in res.message and calls and seen == [(R.P2, "index documents")]
