from __future__ import annotations

from pathlib import Path

from app.core import selfbench


def test_the_command_suite_is_understood() -> None:
    result = selfbench.command_suite()
    assert result["misses"] == [], result["misses"]  # a broken command shows up here with its name
    assert result["accuracy"] == 100.0 and result["total"] == len(selfbench.COMMAND_SUITE)


def test_a_broken_matcher_is_reported(monkeypatch) -> None:
    import app.benchmark as bench

    monkeypatch.setattr(bench, "understood", lambda text, known: None)
    result = selfbench.command_suite()
    assert result["ok"] == 0 and len(result["misses"]) == len(selfbench.COMMAND_SUITE)


def test_compare_names_only_clear_slowdowns() -> None:
    before = {"a": 100.0, "b": 100.0, "tiny": 1.0, "gone": 5.0}
    now = {"a": 200.0, "b": 110.0, "tiny": 4.0, "new": 9.0}
    assert selfbench.compare(before, now) == ["a got slower: 100 ms -> 200 ms"]
    assert selfbench.compare(None, now) == []


def test_run_saves_history_and_finds_the_next_slowdown(tmp_path: Path) -> None:
    lines: list[str] = []
    first = selfbench.run(tmp_path, out=lines.append)
    assert first["suite"]["accuracy"] == 100.0 and set(first["timings"]) >= {"open_database", "build_tool_list"}
    assert selfbench.last_run(tmp_path) == first["timings"]
    assert selfbench.history_file(tmp_path).is_file() and any("Commands understood" in x for x in lines)
    selfbench.run(tmp_path, out=lines.append)
    assert len(selfbench.history_file(tmp_path).read_text().splitlines()) == 2


def test_nothing_is_saved_without_a_data_folder() -> None:
    assert selfbench.run(None, out=lambda _: None)["slower"] == []
