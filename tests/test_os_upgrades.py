"""Autonomy levels enforced by the executor, duplicate finder, batch rename with preview / conflicts / undo, and the
local expense records."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.ai.base import ToolCall
from app.config.settings import Settings
from app.security import autonomy


# ------------------------------------------------------------------ autonomy (pure)
def test_autonomy_rules() -> None:
    assert autonomy.normalise("Observe") == "observe" and autonomy.normalise("bogus") == "routine"
    assert autonomy.blocked_in("observe", "open_application") is not None
    assert autonomy.blocked_in("observe", "get_weather") is None and autonomy.blocked_in("observe", "emergency_control") is None
    assert autonomy.blocked_in("observe", "batch_rename", {"apply": False}) is None          # a preview only reads
    assert autonomy.blocked_in("observe", "batch_rename", {"apply": True}) is not None
    assert autonomy.blocked_in("routine", "delete_item") is None                              # routine: normal checks
    assert autonomy.must_ask("approval", "volume_control") and not autonomy.must_ask("approval", "get_datetime")
    assert not autonomy.must_ask("routine", "volume_control")


# ------------------------------------------------------------------ autonomy through the real executor
def _executor(tmp_path: Path, level: str, answers: list[bool]):
    from app.agent.tool_manager import ToolExecutor
    from app.tools.base import ToolContext
    from app.tools.catalog import build_default_registry

    asked = []

    class Confirmer:
        async def confirm(self, request):
            asked.append(request.tool)
            return answers.pop(0) if answers else False

    from app.config.files import PermissionsConfig
    from app.security.permissions import PermissionEngine

    settings = Settings(autonomy=level, data_dir=tmp_path)
    ctx = ToolContext(settings=settings)
    return ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx,
                        confirmer=Confirmer()), asked


def _run(executor, name: str, arguments: dict, **kw):
    return asyncio.run(executor.execute(ToolCall(name, arguments), **kw))


def test_observe_mode_reads_but_changes_nothing(tmp_path: Path) -> None:
    executor, asked = _executor(tmp_path, "observe", [])
    assert _run(executor, "calculate", {"expression": "6*7"}).result.ok
    blocked = _run(executor, "volume_control", {"action": "mute"}, user_command=True)
    assert not blocked.result.ok and blocked.result.error == "observe_mode" and asked == []


def test_approval_mode_asks_even_for_safe_changes_and_direct_commands(tmp_path: Path) -> None:
    executor, asked = _executor(tmp_path, "approval", [False])
    refused = _run(executor, "volume_control", {"action": "mute"}, user_command=True)
    assert asked == ["volume_control"] and not refused.result.ok              # asked, and "no" means not done
    assert _run(executor, "calculate", {"expression": "1+1"}).result.ok and asked == ["volume_control"]


def test_the_model_cannot_change_the_autonomy_level() -> None:
    import app.tools.voice_control as vc

    source = Path(vc.__file__).read_text(encoding="utf-8")
    assert '"autonomy"' not in source                    # change_setting has no entry for it: only the window can


# ------------------------------------------------------------------ duplicates
def test_duplicates_are_found_by_content_and_nothing_is_deleted(tmp_path: Path) -> None:
    from app.tools.file_batch import duplicate_groups

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.txt").write_text("same content")
    (tmp_path / "two.txt").write_text("same content")
    (tmp_path / "three.txt").write_text("same size!!!")              # same size, different content
    (tmp_path / "solo.txt").write_text("unique")
    groups, partial = duplicate_groups(tmp_path)
    assert not partial and len(groups) == 1 and {p.name for p in groups[0]} == {"one.txt", "two.txt"}
    assert all(p.exists() for p in tmp_path.rglob("*.txt"))


# ------------------------------------------------------------------ batch rename
def _rename_args(tmp_path, **kw):
    from app.tools.file_batch import BatchRenameArgs

    return BatchRenameArgs(folder=str(tmp_path), **kw)


class Ctx:
    def __init__(self, root: Path) -> None:
        from app.security.validator import PathPolicy

        self.root = root
        self.policy = PathPolicy(known={"DESKTOP": root}, safe_roots=[root])

    def get_path_policy(self):
        return self.policy


def test_preview_changes_nothing_then_apply_renames_and_undo_restores(tmp_path: Path) -> None:
    from app.core.undo import inverse_of
    from app.tools.file_batch import batch_rename

    for name in ("IMG_001.jpg", "IMG_002.jpg", "notes.txt"):
        (tmp_path / name).write_text(name)
    ctx = Ctx(tmp_path)
    preview = batch_rename(_rename_args(tmp_path, match="*.jpg", find="IMG_", replace="Trip-"), ctx)
    assert preview.ok and preview.data["count"] == 2 and (tmp_path / "IMG_001.jpg").exists()
    args = _rename_args(tmp_path, match="*.jpg", find="IMG_", replace="Trip-", apply=True)
    done = batch_rename(args, ctx)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Trip-001.jpg", "Trip-002.jpg", "notes.txt"]
    inverse = inverse_of(ToolCall("batch_rename", args.model_dump()), done)
    from app.tools.file_batch import BatchRenameArgs

    batch_rename(BatchRenameArgs(**inverse.call.arguments), ctx)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["IMG_001.jpg", "IMG_002.jpg", "notes.txt"]


def test_a_clash_renames_nothing(tmp_path: Path) -> None:
    from app.tools.base import ToolError
    from app.tools.file_batch import batch_rename

    (tmp_path / "a.txt").write_text("1")
    (tmp_path / "b.txt").write_text("2")
    (tmp_path / "new-a.txt").write_text("already here")
    with pytest.raises(ToolError, match="already exists"):
        batch_rename(_rename_args(tmp_path, match="a.txt", prefix="new-", apply=True), Ctx(tmp_path))
    assert (tmp_path / "a.txt").exists()
    (tmp_path / "new-a.txt").unlink()
    with pytest.raises(ToolError, match="both become"):
        batch_rename(_rename_args(tmp_path, pairs=[[str(tmp_path / "a.txt"), "same.txt"],
                                                   [str(tmp_path / "b.txt"), "same.txt"]], apply=True), Ctx(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt", "b.txt"]


def test_numbering_swaps_and_a_rule_is_required(tmp_path: Path) -> None:
    from app.tools.base import ToolError
    from app.tools.file_batch import batch_rename

    for name in ("b.png", "a.png"):
        (tmp_path / name).write_text(name)
    batch_rename(_rename_args(tmp_path, match="*.png", number=True, base_name="Photo", apply=True), Ctx(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Photo 001.png", "Photo 002.png"]
    assert (tmp_path / "Photo 001.png").read_text() == "a.png"                  # in name order
    with pytest.raises(ToolError):
        batch_rename(_rename_args(tmp_path), Ctx(tmp_path))


# ------------------------------------------------------------------ expenses
def test_expenses_budgets_and_report(tmp_path: Path) -> None:
    from app.core.finance import Finance, guess_category
    from app.memory.database import Database

    db = Database(tmp_path / "x.db")
    db.initialize()
    finance = Finance(db)
    assert guess_category("lunch with Rafi") == "food" and guess_category("uber home") == "transport"
    finance.set_budget("food", 1000)
    finance.add(450, "lunch")
    finance.add(400, "dinner")
    finance.add(120, "bus", category="transport")
    report = finance.report()
    assert report.total == 970 and report.by_category == {"food": 850.0, "transport": 120.0} and report.count == 3
    assert report.warnings() == ["food: 850 of 1,000 - 85% used"]
    newest = finance.recent(1)[0]
    assert finance.delete(newest["id"]) and not finance.delete(newest["id"])
    with pytest.raises(ValueError):
        finance.add(-5, "refund")
