"""Round 73 (PC side of JARVIS Mobile 1.5): phone context validation, freshness, check-in storage, tool."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.core import phone_context as P

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def test_sanitize_keeps_only_the_known_vocabulary():
    rep = {"app_version": "1.5.0", "context": {"network": "wifi", "headset": True, "orientation": "portrait", "motion": "swimming", "light": "dim", "evil": "x", "extra": [1]},
           "sensors": {"accelerometer": True, "light": False, "lidar": True, "gyroscope": "yes"}}
    out = P.sanitize(rep)
    assert out["context"] == {"network": "wifi", "headset": True, "orientation": "portrait", "light": "dim"}      # 'swimming' is not a state; unknown keys dropped
    assert out["sensors"] == {"accelerometer": True, "light": False}
    assert P.sanitize({"battery": 50}) == {} and P.sanitize("x") == {} and P.sanitize(None) == {}                 # a bare check-in says nothing about context
    assert P.sanitize({"app_version": "1.5.0"}) == {"context": {}, "sensors": {}}                                 # a full report with nothing shared clears it
    assert P.sanitize({"app_version": "1", "context": {"headset": "true", "network": 5}})["context"] == {}


def state(age_s, **kw):
    return {"seen": (NOW - timedelta(seconds=age_s)).isoformat(), **kw}


def test_stale_context_is_unknown_not_current():
    s = state(60, context={"network": "wifi", "motion": "moving"}, sensors={"light": False}, charging=True, battery=80)
    text = P.describe("Pixel", s, NOW)
    assert "on Wi-Fi" in text and "moving" in text and "charging" in text and "battery 80%" in text and "no light sensor" in text and "not shared" in text
    old = P.describe("Pixel", state(3600, context={"network": "wifi"}), NOW)
    assert "too old" in old and "unknown" in old and "Wi-Fi" not in old
    assert P.fresh_context(state(3600, context={"network": "wifi"}), NOW) == {} and P.fresh_context(s, NOW)["network"] == "wifi"
    assert "no report" in P.describe("Pixel", {}, NOW) and "shares no context" in P.describe("P", state(5, context={}), NOW)


def test_suggestions_only_from_fresh_values():
    s = state(30, context={"headset": True, "motion": "moving", "network": "wifi"}, charging=True)
    out = " ".join(P.suggestions(s, NOW))
    assert "Headphones" in out and "moving" in out and "background sync" in out
    assert P.suggestions(state(5000, context={"headset": True}), NOW) == []
    assert "not on Wi-Fi" in " ".join(P.suggestions(state(5, context={"network": "cellular"}), NOW))


def test_check_in_stores_validated_context_and_a_full_report_clears_it(tmp_path):
    from app.memory.database import Database
    from app.remote.phone_link import PhoneLink
    db = Database(tmp_path / "t.db"); db.initialize()
    link = PhoneLink(db)
    link.collect("dev12345", {"app_version": "1.5.0", "battery": 70, "charging": False, "context": {"network": "cellular", "motion": "flying"}, "sensors": {"proximity": True}})
    st = link.state("dev12345")
    assert st["context"] == {"network": "cellular"} and st["sensors"] == {"proximity": True} and st["battery"] == 70
    link.collect("dev12345", {})                                              # a bare check-in keeps what it said
    assert link.state("dev12345")["context"] == {"network": "cellular"}
    link.collect("dev12345", {"app_version": "1.5.0", "battery": 69})        # full report, nothing shared: cleared
    assert link.state("dev12345")["context"] == {}


def test_phone_context_tool(tmp_path):
    from types import SimpleNamespace as NS
    from app.memory.database import Database
    from app.remote.phone_link import PhoneLink
    from app.tools.phone_context_tool import phone_context
    db = Database(tmp_path / "t.db"); db.initialize()
    ctx = NS(database=db)
    assert "No trusted phone" in phone_context(None, ctx).message
    with db.connect() as cn:
        cols = [r[1] for r in cn.execute("PRAGMA table_info(mobile_devices)")]
    need = {"id": "dev12345", "name": "Pixel", "public_key": "k", "fingerprint": "fp1", "trust": "trusted"}
    fill = {c: need.get(c, "x") for c in cols if c in need or c in ("created", "created_at", "paired_at", "last_seen")}
    with db.transaction() as c:
        c.execute(f"INSERT INTO mobile_devices ({','.join(fill)}) VALUES ({','.join('?' * len(fill))})", list(fill.values()))
    PhoneLink(db).collect("dev12345", {"app_version": "1.5.0", "context": {"headset": True, "network": "wifi"}})
    res = phone_context(None, ctx)
    assert "Pixel" in res.message and "headphones connected" in res.message and "Headphones are connected" in res.message
