"""AI and Models: role recommendations from facts only (capabilities, memory fit, health, YOUR benchmark)."""

from __future__ import annotations

from types import SimpleNamespace

from app.ai import roles
from app.ai.base import ModelInfo
from app.ai.switching import Evidence

GB = 1_000_000_000


def _models():
    return [ModelInfo("big:14b", 9 * GB, capabilities=frozenset({"completion", "tools"})),
            ModelInfo("mid:8b", 5 * GB, capabilities=frozenset({"completion", "tools", "thinking"})),
            ModelInfo("small:3b", 2 * GB, capabilities=frozenset({"completion", "tools"})),
            ModelInfo("huge:70b", 40 * GB, capabilities=frozenset({"completion", "tools"})),
            ModelInfo("eyes:3b", 3 * GB, capabilities=frozenset({"completion", "vision"})),
            ModelInfo("nomic-embed-text", GB // 4, capabilities=frozenset({"embedding"}))]


def test_without_a_benchmark_it_does_not_guess() -> None:
    facts = roles.facts(_models(), {}, {}, 16 * GB)
    picks = {p.role: p for p in roles.recommend(facts)}
    assert picks["general"].model is None and "no benchmark yet" in picks["general"].reason
    assert picks["vision"].model == "eyes:3b" and picks["embedding"].model == "nomic-embed-text"
    assert next(f for f in facts if f.name == "huge:70b").fits is False                    # can't load here


def test_with_evidence_it_picks_from_scores_and_speed() -> None:
    evidence = {"big:14b": Evidence("big:14b", 0.90, 0), "mid:8b": Evidence("mid:8b", 0.86, 0),
                "small:3b": Evidence("small:3b", 0.60, 0), "huge:70b": Evidence("huge:70b", 0.99, 0)}
    health = {"mid:8b": {"calls": 20, "failures": 0, "avg_seconds": 1.2}, "big:14b": {"avg_seconds": 3.5}}
    picks = {p.role: p for p in roles.recommend(roles.facts(_models(), evidence, health, 16 * GB))}
    assert picks["general"].model == "big:14b"                  # huge:70b scores higher but can't fit in 16 GB
    assert picks["fast"].model == "mid:8b"                       # close enough and quicker
    assert picks["smart"].model is None
    unsafe = {"big:14b": Evidence("big:14b", 0.95, 2), "mid:8b": Evidence("mid:8b", 0.8, 0)}
    assert {p.role: p for p in roles.recommend(roles.facts(_models(), unsafe, {}, 16 * GB))}["general"].model == "mid:8b"
    text = roles.report(roles.facts(_models(), evidence, health, 16 * GB), list(picks.values()), {"general": "small:3b"})
    assert "TOO BIG" in text and "benchmark 90%" in text


def test_models_dialog_shows_and_applies(tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.config.settings import Settings
    from app.ui.models_dialog import ModelsDialog

    QApplication.instance() or QApplication([])
    env = tmp_path / ".env"
    env.write_text("AI_MODEL=small:3b\n", encoding="utf-8")
    applied = []
    controller = SimpleNamespace(ctx=SimpleNamespace(settings=Settings(ai_model="small:3b"),
                                                     paths=SimpleNamespace(env_file=env, data_dir=tmp_path)),
                                 apply_settings=lambda s, f: (applied.append(f), setattr(controller.ctx, "settings", s)))
    evidence = {"big:14b": Evidence("big:14b", 0.9, 0)}
    dialog = ModelsDialog(controller, models=_models(), evidence=evidence, health={}, total_ram=16 * GB)
    assert dialog.table.rowCount() == 6 and "big:14b" in dialog.roles_label.text()
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    assert dialog.apply_recommended(confirm=False)
    assert "AI_MODEL=big:14b" in env.read_text() and applied and "ai_model" in applied[-1]
    dialog.table.selectRow(5)                                   # the embedding model can't be the main model
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    assert dialog.use_selected() is False


def test_same_model_ignores_latest_tag() -> None:
    picks = [roles.Pick("general", "jarvis:latest", ""), roles.Pick("vision", "eyes:3b", "")]
    assert roles.changes(picks, {"general": "jarvis", "vision": ""}) == {"VISION_MODEL": "eyes:3b"}
