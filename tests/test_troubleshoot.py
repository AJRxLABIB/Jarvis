from __future__ import annotations

from pathlib import Path

import pytest

from app.config.settings import Settings
from app.core import troubleshoot as ts
from app.memory.database import Database
from app.tools.base import ToolContext, ToolError
from app.tools.diagnostics_tools import TroubleshootArgs, troubleshoot
from app.utils.paths import AppPaths


def make(tmp_path: Path, **settings) -> ToolContext:
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".env.example").write_text("AI_MODEL=\nHOME_ASSISTANT_TOKEN=\n", encoding="utf-8")
    paths = AppPaths.build(root)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(paths.data_dir / "t.db")
    db.initialize()
    return ToolContext(Settings(**settings), paths=paths, database=db)


def healthy_env(**over) -> ts.Environment:
    base = dict(python=(3, 12, 1), has_module=lambda m: True, platform="win32",
                ollama_models=lambda host: ["qwen3:8b", "nomic-embed-text:latest"], input_devices=lambda: ["Microphone"])
    base.update(over)
    return ts.Environment(**base)


def ids(findings) -> set[str]:
    return {f.id for f in findings}


def test_healthy_setup_reports_only_a_missing_env(tmp_path: Path) -> None:
    ctx = make(tmp_path, ai_model="qwen3:8b")
    found = ts.scan(ctx, healthy_env())
    assert ids(found) == {"env_missing"}                      # nothing else is wrong
    assert found[0].repair == "create_env" and found[0].changes == "files"


def test_old_python_and_missing_packages_show_commands_but_run_nothing(tmp_path: Path) -> None:
    ctx = make(tmp_path)
    env = healthy_env(python=(3, 9, 7), has_module=lambda m: m not in ("PySide6", "piper"))
    found = {f.id: f for f in ts.scan(ctx, env)}
    assert found["python_version"].severity == "problem"
    assert found["package_PySide6"].severity == "problem" and found["package_piper"].severity == "info"
    assert "pip install -r requirements.txt" in found["package_PySide6"].command
    assert found["package_PySide6"].repair == ""              # Jarvis never installs packages itself


def test_models_are_checked_against_what_ollama_has(tmp_path: Path) -> None:
    ctx = make(tmp_path, ai_model="qwen3:32b", ai_smart_model="qwen3:8b")
    found = {f.id: f for f in ts.scan(ctx, healthy_env())}
    assert "ollama pull qwen3:32b" == found["model_main"].command and found["model_main"].severity == "problem"
    assert "model_smart" not in found
    down = {f.id for f in ts.scan(ctx, healthy_env(ollama_models=lambda host: None))}
    assert "ollama_down" in down and "model_main" not in down
    empty = {f.id for f in ts.scan(ctx, healthy_env(ollama_models=lambda host: []))}
    assert "no_models" in empty


def test_microphone_problems(tmp_path: Path) -> None:
    ctx = make(tmp_path)
    assert "no_microphone" in ids(ts.scan(ctx, healthy_env(input_devices=lambda: [])))
    assert "audio_unavailable" in ids(ts.scan(ctx, healthy_env(input_devices=lambda: None)))
    assert "no_microphone" not in ids(ts.scan(ctx, healthy_env()))


def test_unpaired_phone_is_reported_only_when_the_remote_is_on(tmp_path: Path) -> None:
    assert "phone_unpaired" not in ids(ts.scan(make(tmp_path), healthy_env()))
    (tmp_path / "b").mkdir()
    assert "phone_unpaired" in ids(ts.scan(make(tmp_path / "b", remote_enabled=True), healthy_env()))


def test_every_finding_explains_itself(tmp_path: Path) -> None:
    ctx = make(tmp_path, ai_model="missing:1b")
    env = healthy_env(python=(3, 8, 0), has_module=lambda m: False, input_devices=lambda: [])
    for f in ts.scan(ctx, env):
        assert f.problem and f.evidence and f.cause and f.fix and f.changes in ("nothing", "files", "configuration", "external")
        assert f.severity in ("problem", "warning", "info") and f.line()


def test_repair_needs_approval_and_previews_without_changing(tmp_path: Path) -> None:
    ctx = make(tmp_path)
    assert "Would create" in ts.preview(ctx, "create_env")
    result = ts.apply(ctx, "create_env", approved=False)
    assert result["done"] is False and not Path(ctx.paths.env_file).exists()


def test_create_env_is_atomic_verified_and_never_overwrites(tmp_path: Path) -> None:
    ctx = make(tmp_path)
    result = ts.apply(ctx, "create_env", approved=True, env=healthy_env())
    assert result["done"] and result["verified"] is True and "env_missing" not in result["still_reported"]
    env_file = Path(ctx.paths.env_file)
    assert env_file.read_text(encoding="utf-8").startswith("AI_MODEL=")
    assert not list(env_file.parent.glob("*.tmp"))
    env_file.write_text("MY=settings\n", encoding="utf-8")
    again = ts.apply(ctx, "create_env", approved=True, env=healthy_env())
    assert again["done"] is False and env_file.read_text(encoding="utf-8") == "MY=settings\n"   # never overwritten


def test_rollback_deletes_only_an_unedited_file(tmp_path: Path) -> None:
    ctx = make(tmp_path)
    ts.apply(ctx, "create_env", approved=True, env=healthy_env())
    assert ts.rollback_repair(ctx, "create_env")["done"] is True and not Path(ctx.paths.env_file).exists()
    (tmp_path / "b").mkdir()
    ctx2 = make(tmp_path / "b")
    ts.apply(ctx2, "create_env", approved=True, env=healthy_env())
    Path(ctx2.paths.env_file).write_text("EDITED=1\n", encoding="utf-8")
    result = ts.rollback_repair(ctx2, "create_env")
    assert result["done"] is False and "edited" in result["reason"] and Path(ctx2.paths.env_file).exists()


def test_unknown_repair_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        ts.apply(make(tmp_path), "format_disk", approved=True)


def test_close_stuck_repair_is_verified(tmp_path: Path) -> None:
    ctx = make(tmp_path)
    with ctx.database.transaction() as conn:
        conn.execute("INSERT INTO agent_runs (id, source, state, started_at) VALUES ('old', 'typed', 'running', "
                     "'2020-01-01T00:00:00+00:00')")
    assert "close" in ts.preview(ctx, "close_stuck").lower() and "1 request" in ts.preview(ctx, "close_stuck")
    result = ts.apply(ctx, "close_stuck", approved=True, env=healthy_env())
    assert result["done"] and "closed 1 request" in result["what"]
    with ctx.database.connect() as conn:
        assert conn.execute("SELECT state FROM agent_runs WHERE id = 'old'").fetchone()[0] == "interrupted"


def test_tool_scan_preview_fix_and_missing_repair(tmp_path: Path, monkeypatch) -> None:
    ctx = make(tmp_path)
    env = healthy_env()
    monkeypatch.setattr(ts, "Environment", lambda: env)
    scanned = troubleshoot(TroubleshootArgs(action="scan"), ctx)
    assert "no .env settings file" in scanned.message and scanned.data["count"] >= 1
    assert "Would create" in troubleshoot(TroubleshootArgs(action="preview", repair="create_env"), ctx).message
    with pytest.raises(ToolError):
        troubleshoot(TroubleshootArgs(action="fix"), ctx)
    out = troubleshoot(TroubleshootArgs(action="fix", repair="create_env"), ctx)
    assert "Done: created .env" in out.message and Path(ctx.paths.env_file).exists()
    assert "Nothing was undone" not in troubleshoot(TroubleshootArgs(action="undo", repair="create_env"), ctx).message
