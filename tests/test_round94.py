"""Round 94: execution laboratory (policy, workspace, static analysis, sandbox file, artifacts, registry) and action evidence (verdicts, store)."""

from pathlib import Path

import pytest

from app.core import action_evidence as AE, exec_lab as XL
from app.memory.database import Database


# ------------------------------------------------------------------ backends + policy
def test_home_edition_has_no_strong_isolation_and_says_why():
    b = {x.name: x for x in XL.detect_backends("CoreSingleLanguage", sandbox_installed=False, wsl=True)}
    assert not b["windows_sandbox"].available and "Pro" in b["windows_sandbox"].reason
    assert b["restricted_workspace"].available and "NOT a security boundary" in b["restricted_workspace"].reason
    assert b["wsl2"].isolation == "restricted" and "not an isolation boundary" in b["wsl2"].reason
    pro = {x.name: x for x in XL.detect_backends("Professional", sandbox_installed=False)}
    assert not pro["windows_sandbox"].available and "not turned on" in pro["windows_sandbox"].reason
    assert {x.name: x for x in XL.detect_backends("Professional", True)}["windows_sandbox"].available


def test_high_risk_never_falls_back_to_weak_isolation():
    home = XL.detect_backends("Core", False)
    assert XL.classify("check if this installer is safe") == "high" and XL.classify("run the tests") == "medium" and XL.classify("read the config") == "low"
    d = XL.decide("high", home)
    assert not d.allowed and d.backend == "" and "will not be run in a weaker" in d.reason and "static analysis" in d.alternative
    assert XL.decide("high", XL.detect_backends("Enterprise", True)).backend == "windows_sandbox"
    med = XL.decide("medium", home)
    assert med.allowed and med.backend == "restricted_workspace" and med.needs_approval
    assert not XL.decide("medium", home, own_project=False).allowed          # someone else's code = untrusted
    low = XL.decide("low", home)
    assert low.allowed and not low.needs_approval and "NOT possible" in d.text()


# ------------------------------------------------------------------ workspace
def make_project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / "pkg").mkdir(parents=True)
    (p / ".git").mkdir()
    (p / ".git" / "config").write_text("secret remote", encoding="utf-8")
    (p / ".env").write_text("TOKEN=abc", encoding="utf-8")
    (p / "id_rsa").write_text("key", encoding="utf-8")
    (p / "pkg" / "ok.py").write_text("import subprocess\nimport json\n\ndef f():\n    return eval('1+1')\n", encoding="utf-8")
    (p / "pkg" / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    (p / "requirements.txt").write_text("requests>=2\nhttpx\n", encoding="utf-8")
    (p / "setup.exe").write_bytes(b"MZ" + b"\0" * 50)
    return p


def test_workspace_copies_without_secrets_and_never_touches_the_original(tmp_path: Path):
    proj = make_project(tmp_path)
    ws = XL.Workspace.create(tmp_path / "lab", "inspect", "low", "static")
    n = ws.add_input(proj)
    names = {p.relative_to(ws.input).as_posix() for p in ws.input.rglob("*") if p.is_file()}
    assert n == 4 and "proj/pkg/ok.py" in names
    assert not any(".env" in x or "id_rsa" in x or ".git" in x for x in names)
    assert any(".env" in s for s in ws.manifest.skipped) and (ws.path / "manifest.json").is_file()
    assert (proj / ".env").read_text(encoding="utf-8") == "TOKEN=abc"
    with pytest.raises(ValueError):
        ws.add_input(tmp_path / "missing")
    ws.destroy()
    assert not ws.path.exists() and proj.exists()


def test_static_analysis_reports_and_runs_nothing(tmp_path: Path):
    ws = XL.Workspace.create(tmp_path / "lab", "inspect", "low", "static")
    ws.add_input(make_project(tmp_path))
    a = XL.analyze(ws.input)
    text = XL.describe_analysis(a)
    assert any("bad.py" in e for e in a["syntax_errors"])
    assert any("subprocess" in e for e in a["attention"]) and any("eval" in e for e in a["attention"])
    assert a["executables"][0]["pe_file"] and a["executables"][0]["signature"] == "not checked"
    assert any("requests" in d for d in a["dependencies"]) and "nothing was executed" in text and "NOT checked" in text
    empty = tmp_path / "e"
    empty.mkdir()
    assert "not proof of safety" in XL.describe_analysis(XL.analyze(empty))


def test_sandbox_file_is_locked_down(tmp_path: Path):
    ws = XL.Workspace.create(tmp_path / "lab", "x", "high", "windows_sandbox")
    xml = XL.wsb_config(ws, "cmd /c echo <hi>")
    assert "<Networking>Disable</Networking>" in xml and xml.count("<ReadOnly>true</ReadOnly>") == 1 and "<ClipboardRedirection>Disable" in xml
    assert "&lt;hi&gt;" in xml and "<ProtectedClient>Enable" in xml
    ws.manifest.network = True
    assert "<Networking>Default</Networking>" in XL.wsb_config(ws)


def test_artifacts_only_export_named_files_and_limits(tmp_path: Path):
    ws = XL.Workspace.create(tmp_path / "lab", "x", "medium", "restricted_workspace")
    (ws.output / "report.txt").write_text("ok", encoding="utf-8")
    art = XL.Artifacts(ws)
    assert [a["name"] for a in art.listing()] == ["report.txt"]
    dest = tmp_path / "out"
    dest.mkdir()
    assert art.export(["report.txt"], dest) == ["report.txt"] and (dest / "report.txt").is_file()
    for bad in ("../manifest.json", "nope.txt"):
        with pytest.raises(ValueError):
            art.export([bad], dest)
    assert XL.limits_exceeded(ws.manifest) == []
    assert len(XL.limits_exceeded(ws.manifest, elapsed=9999, memory_mb=99999, children=500)) == 3


class Mem:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


def test_registry_lifecycle_is_checked():
    reg = XL.Registry(Mem())
    m = XL.Manifest("abc", "p", "low", "static")
    reg.add(m, "/x")
    reg.move("abc", "isolated")
    reg.move("abc", "running")
    with pytest.raises(ValueError):
        reg.move("abc", "completed")                 # must be verified first
    reg.move("abc", "verifying")
    assert reg.move("abc", "completed")["history"][-1] == "completed"
    with pytest.raises(ValueError):
        reg.move("abc", "running")
    assert reg.remove("abc") and reg.get("abc") is None


# ------------------------------------------------------------------ evidence
def ev(status, source="filesystem", verifier="claim_verify.check_file", **kw):
    return AE.Evidence(action="save_report", verifier=verifier, source=source, status=status, **kw)


def test_self_report_is_never_proof_and_conflicts_are_reported():
    assert AE.verdict([]).status == "unverified"
    own = AE.verdict([ev("pass", source="self_report", verifier="tool")])
    assert own.status == "unverified" and not own.green and "not proof" in own.text()
    assert AE.verdict([ev("pass")]).green
    assert AE.verdict([ev("pass"), ev("unreadable", verifier="reopen")]).status == "partially_verified"
    assert AE.verdict([ev("fail", observed="file missing")]).status == "failed"
    assert AE.verdict([ev("pass"), ev("fail", verifier="git", source="git")]).status == "conflicting"
    assert AE.verdict([ev("unreadable")]).status == "unverified"
    assert AE.verdict([ev("pass", source="self_report"), ev("fail")]).status == "failed"


def test_verifier_registry_and_recovery_options():
    assert AE.verifier_for("file")[2] and AE.verifier_for("git")[0] == "claim_verify.check_git"
    name, why, ok = AE.verifier_for("email")
    assert not ok and "not proof" in why
    assert not AE.verifier_for("teleport")[2]
    failed = AE.verdict([ev("fail")])
    assert any("retry once" in o for o in AE.recovery_options("file", failed))
    assert any("do NOT repeat" in o for o in AE.recovery_options("email", failed))
    assert AE.recovery_options("file", AE.verdict([ev("pass")])) == []
    assert any("ask the user" in o for o in AE.recovery_options("email", AE.verdict([])))


def test_store_roundtrip_stats_and_validation(tmp_path: Path):
    db = Database(tmp_path / "j.db")
    db.initialize()
    st = AE.Store(db)
    st.add(ev("pass", execution="m1", target="a.docx", observed="all checks passed"))
    st.add(ev("pass", source="self_report", verifier="tool", execution="m2"))
    st.add(AE.Evidence(action="commit", verifier="claim_verify.check_git", source="git", status="fail", execution="m1", observed="on main, expected dev"))
    assert st.stats() == {"verified": 1, "partially_verified": 0, "unverified": 1, "failed": 1, "conflicting": 0}
    rows = st.for_action("save_report", "m1")
    assert len(rows) == 1 and rows[0].digest and rows[0].target == "a.docx"
    assert "NOT independent" in AE.describe(st.recent()) and AE.describe([]) == "No evidence recorded."
    with pytest.raises(ValueError):
        st.add(ev("maybe"))
    with pytest.raises(ValueError):
        st.add(AE.Evidence(action="x", verifier="v", source="", status="pass"))
    assert st.prune(1e12) == 3


def test_executor_world_checks_leave_evidence_rows(tmp_path: Path):
    import asyncio

    from app.agent.tool_manager import ToolExecutor, ToolRegistry
    from app.ai.base import ToolCall
    from app.config.settings import Settings
    from app.security.permissions import PermissionEngine, PermissionsConfig, RiskLevel
    from app.tools.base import NoArgs, Tool, ToolContext, ToolResult

    db = Database(tmp_path / "e.db")
    db.initialize()
    ok = lambda a, c: ToolResult.success("done")  # noqa: E731
    reg = ToolRegistry()
    for t in (Tool("proves", "p", ok, NoArgs, RiskLevel.SAFE, verify=lambda a, r, c: (True, "found")),
              Tool("lies", "l", ok, NoArgs, RiskLevel.SAFE, verify=lambda a, r, c: (False, "nothing there")),
              Tool("plain", "n", ok, NoArgs, RiskLevel.SAFE)):
        reg.register(t)
    ex = ToolExecutor(reg, PermissionEngine(PermissionsConfig()), ToolContext(Settings(), database=db))
    for name in ("proves", "lies", "plain"):
        asyncio.run(ex.execute(ToolCall(name, {}), user_command=True))
    st = AE.Store(db)
    assert AE.verdict(st.for_action("proves")).green
    assert AE.verdict(st.for_action("lies")).status == "failed" and "nothing there" in st.for_action("lies")[0].observed
    assert st.for_action("plain") == []                       # no world check = no evidence, so it can never show as verified


def test_tech_decisions_keep_history_and_go_stale():
    from app.core import knowledge_action as KA

    st = KA.TechDecisions(Mem())
    st.record("OpenGCUA", "rejected", "no public code yet", revisit_days=30, now=0)
    assert st.due(now=10 * 86400) == [] and len(st.due(now=31 * 86400)) == 1
    d = st.record("OpenGCUA", "accepted", "code released and it runs", now=40 * 86400)
    assert "rejected: no public code yet" in d["previous"] and len(st.all()) == 1
    assert st.find("should we adopt opengcua now?")[0]["verdict"] == "accepted" and st.find("nothing relevant") == []
    assert "DUE" in KA.describe_decision({"technology": "X", "verdict": "deferred", "reason": "r", "at": 0, "revisit_at": 1}, now=100)
    for bad in (("X", "maybe", "r"), ("", "accepted", "r"), ("X", "accepted", "")):
        with pytest.raises(ValueError):
            st.record(*bad)
    assert st.remove("opengcua") and not st.remove("opengcua")
