"""The reliability lab must pass on a healthy system AND fail loudly when a safety rule is broken."""

from __future__ import annotations

from pathlib import Path

from app.core import reliability_lab as lab


def _case(result, suite_part: str, name_part: str):
    return next(c for c in result.cases if suite_part in c.suite and name_part in c.name)


def test_healthy_system_passes_with_no_incorrect_actions_and_hardware_is_not_claimed():
    result = lab.run()
    summary = result.summary()
    assert summary["passed"] == summary["cases"] and summary["incorrect_actions"] == 0, lab.report_lines(result)
    assert summary["evidence"] == "simulated"
    assert {n["suite"] for n in summary["not_run"]} >= {"windows ui automation", "phone transport and actions"}
    assert all(not c.incorrect_action for c in result.cases)


def test_lab_catches_a_permission_engine_that_allows_everything(monkeypatch):
    from app.security.permissions import Decision, PermissionEngine

    monkeypatch.setattr(PermissionEngine, "decide", lambda self, name, level: (Decision.ALLOW, level))
    result = lab.run(only="permissions")
    assert result.incorrect_actions >= 1
    assert not _case(result, "permissions", "denied when you say no").ok
    assert not _case(result, "permissions", "critical").ok


def test_lab_catches_broken_replay_protection(monkeypatch):
    from app.remote import mobile

    monkeypatch.setattr(mobile.MobileRegistry, "_use_nonce", lambda self, device_id, nonce: None)
    result = lab.run(only="phone")
    case = _case(result, "phone", "replay")
    assert not case.ok and case.incorrect_action and result.incorrect_actions >= 1


def test_lab_catches_a_side_effect_tool_mislabelled_as_read_only(monkeypatch):
    """If a state-changing tool were classed as read-only, recovery would re-run it after a crash; the lab must say so."""
    from app.security.permissions import RiskLevel

    real = lab._canaries

    def mislabelled():
        ran, tools = real()
        import dataclasses

        return ran, [dataclasses.replace(t, level=RiskLevel.SAFE) if t.name == "canary_moderate" else t for t in tools]

    monkeypatch.setattr(lab, "_canaries", mislabelled)
    result = lab.run(only="recovery")
    case = _case(result, "recovery", "side-effect")
    assert not case.ok and case.incorrect_action


def test_a_crashing_case_is_a_failure_not_a_pass(monkeypatch):
    def boom(tmp):
        return [("explodes", lambda: (_ for _ in ()).throw(RuntimeError("bang")))]

    monkeypatch.setitem(lab.SUITES, "boom", boom)
    result = lab.run(only="boom")
    assert len(result.cases) == 1 and not result.cases[0].ok and "bang" in result.cases[0].detail


def test_compare_reports_only_real_regressions(tmp_path: Path):
    before = {"incorrect_actions": 0, "suites": {"memory": {"rate": 100.0, "ms": 100.0},
                                                  "commands": {"rate": 100.0, "ms": 10.0}}}
    now = {"incorrect_actions": 1, "suites": {"memory": {"rate": 66.7, "ms": 100.0},
                                               "commands": {"rate": 100.0, "ms": 500.0}}}
    notes = lab.compare(before, now)
    assert any("INCORRECT" in n for n in notes) and any("memory" in n for n in notes) and any("commands" in n for n in notes)
    assert lab.compare(before, before) == [] and lab.compare(None, now) == []


def test_history_round_trip(tmp_path: Path):
    summary = lab.run(only="commands").summary()
    lab.save(tmp_path, summary)
    saved = lab.last_run(tmp_path)
    assert saved["cases"] == 1 and saved["evidence"] == "simulated" and "not_run" not in saved
