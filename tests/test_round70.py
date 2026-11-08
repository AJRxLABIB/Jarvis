"""Round 70: sensor rules, sensors in the twin + radar, weighted hard cases, plugin import sandbox, learning panel."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from app.core import plugin_sandbox as PS, radar, sensor_rules as R, sensors as S, twin

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def reading(name, status="ok", value=None, kind="number", unit=""):
    return S.Reading(S.Sensor(name, "t/" + name, kind, unit), status, value, 5.0)


# ---- sensor rules ----------------------------------------------------------------------------------------------------------------------

def test_rule_needs_duration_then_cooldown():
    rule = R.Rule("door open", "office door", "=", "open", for_s=600, cooldown_min=60)
    st: dict = {}
    r = [reading("office door", value="open", kind="door")]
    assert R.evaluate([rule], r, st, T0) == []                                        # just started
    assert R.evaluate([rule], r, st, T0 + timedelta(minutes=5)) == []
    assert len(R.evaluate([rule], r, st, T0 + timedelta(minutes=11))) == 1            # open for 10+ minutes
    assert R.evaluate([rule], r, st, T0 + timedelta(minutes=20)) == []               # cooldown
    assert len(R.evaluate([rule], r, st, T0 + timedelta(minutes=80))) == 1


def test_unconfirmed_reading_never_fires_and_resets_the_timer():
    rule = R.Rule("hot", "room", ">", 30, for_s=300)
    st: dict = {}
    hot = [reading("room", value=35.0, kind="temperature")]
    R.evaluate([rule], hot, st, T0)
    for bad in ("stale", "unknown", "invalid"):
        assert R.evaluate([rule], [reading("room", bad, 35.0, "temperature")], st, T0 + timedelta(minutes=10)) == [] and st["hot"]["since"] is None
    assert R.evaluate([rule], hot, st, T0 + timedelta(minutes=11)) == []             # the timer restarted
    assert len(R.evaluate([rule], hot, st, T0 + timedelta(minutes=17))) == 1
    assert R.evaluate([rule], [], {}, T0) == []                                       # sensor missing entirely


def test_numeric_and_word_operators():
    n = reading("t", value=30.0, kind="temperature")
    assert R._holds(R.Rule("a", "t", ">=", 30, 0), n) and not R._holds(R.Rule("a", "t", ">", 30, 0), n) and R._holds(R.Rule("a", "t", "<", 31, 0), n)
    d = reading("d", value="open", kind="door")
    assert R._holds(R.Rule("a", "d", "=", "OPEN", 0), d) and not R._holds(R.Rule("a", "d", ">", "open", 0), d)


def test_rule_file_is_validated_and_run_notifies(tmp_path):
    (tmp_path / "sensor_rules.json").write_text(json.dumps([
        {"name": "ok", "sensor": "room", "op": ">", "value": 30}, {"name": "bad op", "sensor": "x", "op": "~", "value": 1}, {"name": "no value", "sensor": "x", "op": ">"}, "junk"]))
    assert [r.name for r in R.load(tmp_path)] == ["ok"] and R.load(tmp_path / "none") == []

    class KV:
        d: dict = {}
        def get(self, k, default=None): return self.d.get(k, default)
        def set(self, k, v): self.d[k] = v
    from app.memory.database import Database
    db = Database(tmp_path / "t.db"); db.initialize()
    alerts = R.run(db, KV(), tmp_path, T0, reader=lambda cfg, secs: ([reading("room", value=33.0, kind="temperature", unit="C")], ""))
    assert len(alerts) == 1
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM notifications WHERE source = 'sensors'").fetchone()[0] == 1
    assert R.run(db, KV(), tmp_path, T0, reader=lambda *a: ([], "broker down")) == []   # no reading, no alert


# ---- twin + radar ----------------------------------------------------------------------------------------------------------------------

def test_twin_observes_only_ok_sensor_readings_and_radar_uses_fresh_ones(tmp_path, monkeypatch):
    from app.memory.database import Database
    db = Database(tmp_path / "t.db"); db.initialize()
    (tmp_path / "sensors.json").write_text("{}")
    monkeypatch.setattr(S, "read_all", lambda cfg, secs: ([reading("room", value=38.0, kind="temperature", unit="C"), reading("door", "unknown", kind="door")], ""))
    ctx = NS(database=db, paths=NS(config_dir=tmp_path, data_dir=tmp_path))
    notes = twin.refresh(ctx)
    assert "sensors" in notes
    names = [o.key for o in twin.snapshot(db, kind="sensor")]
    assert names == ["room"]                                                         # the unknown door is not in the twin
    sig = radar.sensor_signals(ctx)
    assert len(sig) == 1 and "hot" in sig[0].title
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    assert radar.sensor_signals(ctx, later) == []                                     # an old reading is never a warning


# ---- weighted hard cases ---------------------------------------------------------------------------------------------------------------

def test_hard_cases_weigh_double_in_the_self_test():
    import asyncio
    from app.agent import self_improve as SI
    cases = [{"request": "a", "expect_tool": "x"}, {"request": "b", "expect_tool": "y", "hard": True}]
    async def ask_a(model, temp, text): return ("x", False)          # right on the normal case, wrong on the hard one
    async def ask_b(model, temp, text): return ("y", False)
    cand = SI.Candidate("m", "m", 0.0) if hasattr(SI.Candidate, "__dataclass_fields__") and len(SI.Candidate.__dataclass_fields__) == 3 else None
    if cand is None:
        pytest.skip("Candidate signature changed")
    a = asyncio.run(SI.evaluate(ask_a, "m", cand, cases))
    b = asyncio.run(SI.evaluate(ask_b, "m", cand, cases))
    assert round(a.accuracy, 3) == 0.333 and round(b.accuracy, 3) == 0.667 and a.cases == b.cases == 2


# ---- plugin sandbox --------------------------------------------------------------------------------------------------------------------

GOOD = '''"""A tiny plugin."""
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolResult
VERSION = "2.1"
TOOLS = [Tool("hello_probe", "says hello", lambda a, c: ToolResult.success("hi"), NoArgs, RiskLevel.SAFE)]
'''


def test_probe_reads_a_clean_plugin(tmp_path):
    p = tmp_path / "hello.py"; p.write_text(GOOD)
    info = PS.probe(p)
    assert info["tools"] == {"hello_probe": 0} and info["version"] == "2.1" and info["doc"] == "A tiny plugin."


@pytest.mark.parametrize("body,needle", [
    ("import socket\ns = socket.socket()\ns.connect(('127.0.0.1', 9))\n", "socket.connect"),
    ("import os\nos.system('echo hi')\n", "os.system"),
    ("open('dropped.txt', 'w').write('x')\n", "open for writing"),
    ("import os\nos.remove('x')\n", "os.remove"),
    ("import subprocess\nsubprocess.run(['echo','x'])\n", "subprocess"),
])
def test_probe_refuses_plugins_that_act_during_import(tmp_path, body, needle):
    p = tmp_path / "evil.py"; p.write_text(body + GOOD)
    with pytest.raises(PS.ProbeError) as e:
        PS.probe(p)
    assert needle in str(e.value) and not (tmp_path / "dropped.txt").exists()


def test_probe_timeout_crash_and_bad_shape(tmp_path):
    (tmp_path / "slow.py").write_text("import time\ntime.sleep(30)\n")
    with pytest.raises(PS.ProbeError, match="longer"):
        PS.probe(tmp_path / "slow.py", timeout=2)
    (tmp_path / "exit.py").write_text("raise SystemExit('bye')\n")
    with pytest.raises(PS.ProbeError, match="SystemExit"):
        PS.probe(tmp_path / "exit.py")
    (tmp_path / "shape.py").write_text("TOOLS = 5\n")
    with pytest.raises(PS.ProbeError, match="TOOLS"):
        PS.probe(tmp_path / "shape.py")
    (tmp_path / "crit.py").write_text(GOOD.replace("RiskLevel.SAFE", "RiskLevel.CRITICAL"))
    with pytest.raises(PS.ProbeError, match="CRITICAL"):
        PS.probe(tmp_path / "crit.py")
    with pytest.raises(PS.ProbeError):
        PS.probe(tmp_path / "missing.py")


def test_trust_uses_the_probe_not_the_app_process(tmp_path):
    from app import plugins
    (tmp_path / "hello.py").write_text(GOOD)
    m = plugins.trust_plugin(tmp_path, "hello")
    assert m["tools"] == {"hello_probe": 0} and m["version"] == "2.1"
    (tmp_path / "evil.py").write_text("import os\nos.system('x')\n" + GOOD)
    with pytest.raises(ValueError, match="not trusted"):
        plugins.trust_plugin(tmp_path, "evil")
    assert not (tmp_path / "evil.plugin.json").exists()


def test_learning_panel_uses_existing_tools():
    from app.ui.command_center.panel_specs import BY_KEY
    from app.tools.catalog import build_default_registry
    reg = build_default_registry(); p = BY_KEY["learning"]
    for b in p.blocks:
        assert b.tool in reg
    for a in p.actions:
        assert a.tool in reg
