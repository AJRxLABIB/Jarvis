from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.security.permissions import PermissionEngine, RiskLevel
from app.security.validator import PathPolicy, Zone, check_component
from app.tools import filesystem, winapi
from app.tools.base import ToolContext, ToolError
from app.tools.catalog import build_default_registry
from tests.test_tools import ScriptedConfirmer


class HomePath(type(Path())):  # a Path that can also carry the fake Recycle Bin / "opened" lists
    recycled: list
    opened: list


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = HomePath(tmp_path / "home")
    for name in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"):
        (home / name).mkdir(parents=True)
    (tmp_path / "Windows" / "System32").mkdir(parents=True)
    (tmp_path / "Elsewhere").mkdir()
    (home / "Downloads" / "report.pdf").write_bytes(b"%PDF")
    (home / "Downloads" / "old.pdf").write_bytes(b"%PDF old")
    (home / "Documents" / "notes.txt").write_text("hello")
    (home / "Documents" / "Projects").mkdir()
    (home / "Documents" / "Projects" / "plan.docx").write_bytes(b"docx")
    (home / "Desktop" / "setup.exe").write_bytes(b"MZ")
    old = home / "Downloads" / "old.pdf"
    os.utime(old, (old.stat().st_atime - 86400 * 30, old.stat().st_mtime - 86400 * 30))
    recycled: list[str] = []

    def recycle(path: str) -> None:
        recycled.append(path)
        target = Path(path)
        if target.is_dir():
            import shutil
            shutil.rmtree(target)
        else:
            target.unlink()

    opened: list[str] = []
    monkeypatch.setattr(winapi, "recycle", recycle)
    monkeypatch.setattr(winapi, "start_file", opened.append)
    home.recycled = recycled  # type: ignore[attr-defined]
    home.opened = opened  # type: ignore[attr-defined]
    return home


def policy_for(home: Path) -> PathPolicy:
    known = {"HOME": home, **{k: home / n for k, n in (("DESKTOP", "Desktop"), ("DOCUMENTS", "Documents"),
             ("DOWNLOADS", "Downloads"), ("PICTURES", "Pictures"), ("MUSIC", "Music"), ("VIDEOS", "Videos"))}}
    perms = PermissionsConfig(safe_roots=["{DESKTOP}", "{DOCUMENTS}", "{DOWNLOADS}", "{PICTURES}"])
    return PathPolicy.build(perms, known=known, protected=[home.parent / "Windows"])


def run(home: Path, tool: str, confirm: bool = True, **arguments):
    ctx = ToolContext(Settings(), path_policy=policy_for(home))
    confirmer = ScriptedConfirmer(confirm)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx, confirmer=confirmer)
    execution = asyncio.run(ex.execute(ToolCall(tool, arguments)))
    return execution, confirmer


# ------------------------------------------------------------------ path policy


def test_resolve_known_folders_and_bare_names(home: Path) -> None:
    policy = policy_for(home)
    assert policy.resolve("Downloads") == (home / "Downloads").resolve()
    assert policy.resolve("my documents/Projects") == (home / "Documents" / "Projects").resolve()
    assert policy.resolve("{DESKTOP}") == (home / "Desktop").resolve()
    assert policy.resolve("report.pdf", must_exist=True) == (home / "Downloads" / "report.pdf").resolve()
    assert policy.resolve("New Folder") == (home / "Desktop" / "New Folder").resolve()  # default for new items
    assert policy.resolve("~/Documents/notes.txt", must_exist=True).name == "notes.txt"
    assert policy.resolve(str(home / "Documents" / "notes.txt"), must_exist=True).name == "notes.txt"


def test_ambiguous_and_missing_names(home: Path) -> None:
    (home / "Desktop" / "report.pdf").write_bytes(b"x")
    with pytest.raises(ToolError, match="more than one place"):
        policy_for(home).resolve("report.pdf", must_exist=True)
    with pytest.raises(ToolError, match="Which folder"):
        policy_for(home).resolve("missing.pdf", must_exist=True)


@pytest.mark.parametrize(
    "bad", ["Documents/../../Windows", "..", "\\\\server\\share\\x", "//server/share", "notes.txt:secret",
            "CON", "nul.txt", "Documents/COM1", "bad|name", "what?.txt", "trailing. ", "x" * 500],
)
def test_rejected_paths(home: Path, bad: str) -> None:
    with pytest.raises(ToolError):
        policy_for(home).resolve(bad)


def make_directory_link(link: Path, target: Path) -> None:
    """A junction on Windows (no admin rights needed, so it is the realistic attack),
    a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_link_escape_is_resolved_before_checks(home: Path) -> None:
    link = home / "Desktop" / "shortcut_to_windows"
    make_directory_link(link, home.parent / "Windows")
    policy = policy_for(home)
    resolved = policy.resolve("Desktop/shortcut_to_windows/System32")
    assert policy.zone(resolved) == Zone.PROTECTED
    assert policy.risk(resolved, "write") == RiskLevel.CRITICAL
    # and through the tools: writing via the link is blocked, not treated as "Desktop"
    execution, confirmer = run(home, "create_item", path="Desktop/shortcut_to_windows/evil.txt", kind="file")
    assert execution.result.error == "blocked" and not confirmer.requests
    assert not (home.parent / "Windows" / "evil.txt").exists()


def test_zones_and_levels(home: Path) -> None:
    p = policy_for(home)
    docs, other, system = home / "Documents", home.parent / "Elsewhere", home.parent / "Windows" / "System32"
    assert (p.zone(docs / "a"), p.zone(other / "a"), p.zone(system / "a")) == (Zone.SAFE, Zone.OUTSIDE, Zone.PROTECTED)
    assert p.risk(docs / "a", "write") == RiskLevel.SAFE
    assert p.risk(other / "a", "write") == RiskLevel.DANGEROUS
    assert p.risk(other / "a", "read") == RiskLevel.MODERATE
    assert p.risk(docs / "a", "delete") == RiskLevel.DANGEROUS
    assert p.risk(docs.resolve(), "delete") == RiskLevel.CRITICAL  # a known folder itself
    assert p.risk(Path(home.anchor), "delete") == RiskLevel.CRITICAL  # a drive root


def test_component_checks() -> None:
    assert check_component("Report 2026.pdf") == "Report 2026.pdf"
    for bad in ("", "..", "a:b", "aux", "LPT1.txt", "name.", "../x", "a\\b", "a/b"):
        with pytest.raises(ToolError):
            check_component(bad)


# ---------------------------------------------------------------- read tools


def test_open_folder_and_file(home: Path) -> None:
    execution, _ = run(home, "open_path", path="Downloads")
    assert execution.result.ok and home.opened[-1] == str((home / "Downloads").resolve())
    execution, confirmer = run(home, "open_path", path="notes.txt")
    assert execution.result.ok and not confirmer.requests


def test_opening_an_executable_asks_first(home: Path) -> None:
    execution, confirmer = run(home, "open_path", confirm=False, path="setup.exe")
    assert execution.result.error == "declined" and home.opened == []
    assert "program or script" in confirmer.requests[0].summary


def test_list_folder(home: Path) -> None:
    result = run(home, "list_folder", path="Documents", sort_by="name")[0].result
    assert result.ok and [i["name"] for i in result.data["items"]] == ["notes.txt", "Projects"]


def test_find_files(home: Path) -> None:
    result = run(home, "find_files", extension=".PDF")[0].result
    assert result.ok and result.data["total"] == 2 and result.data["files"][0]["name"] == "report.pdf"  # newest first
    recent = run(home, "find_files", extension="pdf", modified_within_days=7)[0].result
    assert recent.data["total"] == 1
    assert run(home, "find_files", folder="Documents", name_contains="plan")[0].result.data["files"][0]["name"] == "plan.docx"
    assert run(home, "find_files")[0].result.error == "missing_query"


def test_file_info_and_folder_size(home: Path) -> None:
    result = run(home, "file_info", path="Documents")[0].result
    assert result.ok and result.data["files"] == 2 and result.data["folders"] == 1


def test_reading_outside_safe_folders_asks(home: Path) -> None:
    (home.parent / "Elsewhere" / "x.txt").write_text("x")
    execution, confirmer = run(home, "list_folder", confirm=False, path=str(home.parent / "Elsewhere"))
    assert execution.result.error == "declined" and "outside your usual folders" in confirmer.requests[0].summary


# ---------------------------------------------------------------- write tools


def test_create_folder_and_file(home: Path) -> None:
    execution, confirmer = run(home, "create_item", path="Desktop/Projects")
    assert execution.result.ok and (home / "Desktop" / "Projects").is_dir() and not confirmer.requests
    assert run(home, "create_item", path="Desktop/Projects")[0].result.error == "exists"
    run(home, "create_item", path="Documents/todo.txt", kind="file", text="buy milk")
    assert (home / "Documents" / "todo.txt").read_text() == "buy milk"
    assert run(home, "create_item", path="Documents/notes.txt", kind="file", text="x")[0].result.error == "exists"
    assert (home / "Documents" / "notes.txt").read_text() == "hello"  # never overwritten


def test_creating_scripts_or_writing_protected_areas(home: Path) -> None:
    execution, confirmer = run(home, "create_item", confirm=False, path="Desktop/run.bat", kind="file", text="del *")
    assert execution.result.error == "declined" and not (home / "Desktop" / "run.bat").exists()
    execution, confirmer = run(home, "create_item", path=str(home.parent / "Windows" / "evil"))
    assert execution.result.error == "blocked" and not confirmer.requests
    execution, _ = run(home, "create_item", confirm=False, path=str(home.parent / "Elsewhere" / "new"))
    assert execution.result.error == "declined"  # outside safe folders: dangerous, asks


def test_rename_keeps_extension_and_asks(home: Path) -> None:
    execution, confirmer = run(home, "rename_item", path="report.pdf", new_name="final report")
    assert execution.result.ok and (home / "Downloads" / "final report.pdf").exists()
    assert confirmer.requests[0].summary.startswith("Rename 'report.pdf' to 'final report.pdf'")
    assert run(home, "rename_item", path="notes.txt", new_name="../escape")[0].result.error == "invalid_path"
    execution, confirmer = run(home, "rename_item", confirm=False, path="notes.txt", new_name="notes.bat")
    assert confirmer.requests[0].level == RiskLevel.DANGEROUS  # turning a file into a script


def test_move_and_copy(home: Path) -> None:
    execution, confirmer = run(home, "move_item", source="report.pdf", destination="Documents")
    assert execution.result.ok and (home / "Documents" / "report.pdf").exists() and not (home / "Downloads" / "report.pdf").exists()
    assert "Move 'report.pdf'" in confirmer.requests[0].summary
    execution, confirmer = run(home, "move_item", source="notes.txt", destination="Desktop", mode="copy")
    assert execution.result.ok and (home / "Documents" / "notes.txt").exists() and (home / "Desktop" / "notes.txt").exists()
    assert not confirmer.requests  # copying inside safe folders is safe
    # now there are two notes.txt: a bare name is ambiguous, so the tool asks which one
    assert run(home, "move_item", source="notes.txt", destination="Desktop")[0].result.error == "ambiguous"
    assert run(home, "move_item", source="Documents/notes.txt", destination="Desktop")[0].result.error == "exists"
    assert run(home, "move_item", source="Documents/Projects", destination="Documents/Projects")[0].result.error in ("exists", "invalid_path")


def test_moving_out_of_protected_or_a_known_folder_is_blocked(home: Path) -> None:
    (home.parent / "Windows" / "System32" / "kernel.dll").write_bytes(b"x")
    execution, _ = run(home, "move_item", source=str(home.parent / "Windows" / "System32" / "kernel.dll"), destination="Desktop")
    assert execution.result.error == "blocked"
    assert run(home, "move_item", source="Documents", destination="Desktop")[0].result.error == "blocked"


# ------------------------------------------------------------------- delete


def test_delete_goes_to_recycle_bin_after_confirmation(home: Path) -> None:
    execution, confirmer = run(home, "delete_item", confirm=False, path="notes.txt")
    assert execution.result.error == "declined" and (home / "Documents" / "notes.txt").exists()
    execution, confirmer = run(home, "delete_item", confirm=True, path="notes.txt")
    assert execution.result.ok and "Recycle Bin" in confirmer.requests[0].summary
    assert home.recycled == [str((home / "Documents" / "notes.txt").resolve())]


def test_delete_blocks_known_folders_protected_and_mass_deletion(home: Path) -> None:
    assert run(home, "delete_item", path="Documents")[0].result.error == "blocked"
    big = home / "Downloads" / "big"
    big.mkdir()
    for i in range(filesystem.MASS_DELETE_LIMIT + 5):
        (big / f"f{i}.txt").write_text("x")
    assert run(home, "delete_item", path="Downloads/big")[0].result.error == "blocked"
    (home.parent / "Windows" / "System32" / "x.sys").write_bytes(b"x")
    assert run(home, "delete_item", path=str(home.parent / "Windows" / "System32" / "x.sys"))[0].result.error == "blocked"
    assert home.recycled == []


def test_model_cannot_skip_confirmation_with_extra_fields(home: Path) -> None:
    execution, _ = run(home, "delete_item", path="notes.txt", confirmed=True, force=True)
    assert execution.result.error == "invalid_arguments" and (home / "Documents" / "notes.txt").exists()


def test_assistant_data_is_protected(home: Path, tmp_path: Path) -> None:
    from app.utils.paths import AppPaths

    paths = AppPaths.build(tmp_path / "app")
    paths.ensure_dirs()
    paths.env_file.write_text("SECRET=1")
    ctx = ToolContext(Settings(), paths=paths)
    policy = PathPolicy.from_context(ctx)
    assert policy.zone(paths.env_file.resolve()) == Zone.PROTECTED
    assert policy.risk(paths.data_dir.resolve() / "assistant.db", "delete") == RiskLevel.CRITICAL


# ------------------------------------------------- small-model argument mistakes


@pytest.mark.parametrize(
    "arguments",
    [
        {"extension": "pdf", "folder": "Downloads"},
        {"extension": "PDFs", "folder": "download"},
        {"name_contains": "PDF files", "folder": "Downloads"},  # type put into the name
        {"file_type": ".pdf", "path": "Downloads"},  # invented field names
        {"query": "pdf", "location": "Downloads"},
    ],
)
def test_find_pdfs_in_downloads_however_the_model_phrases_it(home: Path, arguments: dict) -> None:
    result = run(home, "find_files", **arguments)[0].result
    assert result.ok, result.message
    assert result.data["total"] == 2, result.message
    assert "type .pdf" in result.message


def test_find_message_names_every_filter(home: Path) -> None:
    result = run(home, "find_files", extension="pdf", folder="Downloads", modified_within_days=3)[0].result
    assert result.data["total"] == 1 and "changed in the last 3 day(s)" in result.message


def test_missing_file_fails_without_a_confusing_question(home: Path) -> None:
    for tool, arguments in (("file_info", {"path": "Downloads/file.pdf"}), ("delete_item", {"path": "nothing.txt"}),
                            ("open_path", {"path": "Documents/ghost.docx"}), ("find_files", {"folder": "Nowhere", "extension": "pdf"}),
                            ("rename_item", {"path": "ghost.txt", "new_name": "x"}),
                            ("move_item", {"source": "ghost.txt", "destination": "Documents"})):
        execution, confirmer = run(home, tool, confirm=True, **arguments)
        assert execution.result.error == "not_found", (tool, execution.result)
        assert confirmer.requests == [], tool  # nothing to confirm for something that doesn't exist


def test_create_existing_fails_before_asking(home: Path) -> None:
    execution, confirmer = run(home, "create_item", path="Documents/notes.txt", kind="file")
    assert execution.result.error == "exists" and not confirmer.requests


def test_exact_call_from_the_field_report(home: Path) -> None:
    """find_files(folder='Downloads', extension='pdf', limit=100, modified_within_days=None, name_contains=None)"""
    result = run(home, "find_files", folder="Downloads", extension="pdf", limit=100,
                 modified_within_days=None, name_contains=None)[0].result
    assert result.ok and result.data["total"] == 2


def test_limits_are_clamped_but_identifiers_are_not(home: Path) -> None:
    assert run(home, "list_folder", path="Documents", limit=0)[0].result.ok  # -> 1
    assert run(home, "list_processes", limit=500)[0].result.ok  # -> 15
    execution, confirmer = run(home, "end_process", pid=0)
    assert execution.result.error == "invalid_arguments" and not confirmer.requests  # never "nearest pid"
    assert "call the tool again" in execution.result.message


def test_clamped_fields_have_a_standard_schema() -> None:
    spec = [s for s in build_default_registry().specs() if s.name == "find_files"][0]
    limit = spec.parameters["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 50, 15) and "ge" not in limit


def test_placeholder_values_mean_not_given(home: Path) -> None:
    """find_files(extension='mp4', folder='Downloads', limit='10', modified_within_days='0', name_contains='')"""
    (home / "Downloads" / "clip.mp4").write_bytes(b"x")
    result = run(home, "find_files", extension="mp4", folder="Downloads", limit="10",
                 modified_within_days="0", name_contains="")[0].result
    assert result.ok and result.data["total"] == 1 and "changed in" not in result.message
    assert run(home, "find_files", extension="pdf", folder="Downloads", name_contains="null")[0].result.data["total"] == 2
