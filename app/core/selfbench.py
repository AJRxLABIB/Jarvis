"""Measure Jarvis itself: does it understand the commands it should, and where does the time go?

``python run.py --perf`` runs this with no microphone, no model and no internet:

* **Command suite** - each written command must map to the tool it should (the same matching the conversation uses).
  This is a regression check for accuracy: a change that breaks "open Chrome" shows up as a number going down.
* **Timings** - how long the main internal steps take on THIS PC (opening the database, building the tool list,
  matching commands, keyword search, indexing the code). The result is saved, and the next run says which steps got
  slower, so a slow change is found by measurement instead of by feeling.

Nothing here changes your data; the database test runs in a temporary folder.
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

# (what the user writes, tool that must be chosen, text its arguments must contain or "")
COMMAND_SUITE: tuple[tuple[str, str, str], ...] = (
    ("open chrome", "open_application", "chrome"),
    ("please open notepad", "open_application", "notepad"),
    ("close phone link", "close_application", "phone"),
    ("what time is it", "get_datetime", ""),
    ("how much battery do I have", "system_status", "battery"),
    ("lock my pc", "power_control", "lock"),
    ("volume up", "volume_control", "up"),
    ("pause the music", "media_control", ""),
    ("what's the weather like today", "get_weather", ""),
    ("minimize chrome", "window_control", "minimize"),
    ("organize my desktop", "organize_desktop", ""),
    ("play arijit singh songs", "play_music", "arijit"),
)
SLOWER_BY = 1.5      # a step is called "slower" when it took 50 % longer than the last run...
IGNORE_UNDER_MS = 5  # ...and more than a few milliseconds (tiny steps jitter)


def _known_app(name: str) -> str | None:
    wanted = name.lower().strip()
    return wanted if wanted in {"chrome", "notepad", "phone link", "calculator", "google chrome"} else None


def command_suite(known_app: Callable[[str], str | None] = _known_app) -> dict[str, Any]:
    from app.benchmark import understood

    misses, started = [], time.perf_counter()
    for text, tool, contains in COMMAND_SUITE:
        call = understood(text, known_app)
        ok = call is not None and call.name == tool and (
            not contains or contains in json.dumps(dict(call.arguments), ensure_ascii=False).lower())
        if not ok:
            misses.append({"command": text, "expected": tool, "got": call.name if call else None})
    total = len(COMMAND_SUITE)
    return {"total": total, "ok": total - len(misses), "accuracy": round(100 * (total - len(misses)) / total, 1),
            "misses": misses, "ms_per_command": round((time.perf_counter() - started) * 1000 / total, 2)}


def _timed(fn: Callable[[], Any]) -> float:
    started = time.perf_counter()
    fn()
    return round((time.perf_counter() - started) * 1000, 1)


def timings(source_root: Path | None = None) -> dict[str, float]:
    """Milliseconds for each measured step (smaller is better)."""
    from app.memory.database import Database
    from app.memory.semantic import keyword_score, query_terms

    results: dict[str, float] = {}

    def open_database() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Database(Path(tmp) / "perf.db").initialize()

    results["open_database"] = _timed(open_database)

    def build_tools() -> None:
        from app.tools.catalog import build_default_registry

        build_default_registry()

    results["build_tool_list"] = _timed(build_tools)

    def match_commands() -> None:
        for _ in range(20):
            command_suite()

    results["match_240_commands"] = _timed(match_commands)

    text = ("The tender budget is 5 crore and the deadline is 30 October. " * 40)

    def keyword_search() -> None:
        terms = query_terms("tender deadline budget")
        for _ in range(2000):
            keyword_score(terms, text)

    results["keyword_score_x2000"] = _timed(keyword_search)

    root = source_root or Path(__file__).resolve().parents[1]
    if root.is_dir():
        from app.core import codeindex

        results["index_source_code"] = _timed(lambda: codeindex.project_symbols(root))
    return results


def compare(previous: dict[str, float] | None, now: dict[str, float]) -> list[str]:
    """Steps that became clearly slower since ``previous`` (plain sentences)."""
    if not previous:
        return []
    slower = []
    for name, ms in now.items():
        before = previous.get(name)
        if before is not None and ms > before * SLOWER_BY and ms - before > IGNORE_UNDER_MS:
            slower.append(f"{name.replace('_', ' ')} got slower: {before:.0f} ms -> {ms:.0f} ms")
    return slower


def history_file(data_dir: Path) -> Path:
    return data_dir / "benchmark" / "perf-history.jsonl"


def last_run(data_dir: Path) -> dict[str, float] | None:
    path = history_file(data_dir)
    try:
        lines = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        return json.loads(lines[-1]).get("timings") if lines else None
    except (OSError, ValueError):
        return None


def run(data_dir: Path | None = None, out: Callable[[str], None] = print) -> dict[str, Any]:
    suite = command_suite()
    times = timings()
    notes = compare(last_run(data_dir), times) if data_dir is not None else []
    out(f"Commands understood: {suite['ok']}/{suite['total']} ({suite['accuracy']}%)")
    for miss in suite["misses"]:
        out(f"  MISS {miss['command']!r}: wanted {miss['expected']}, got {miss['got']}")
    for name, ms in times.items():
        out(f"  {name.replace('_', ' '):28} {ms:8.1f} ms")
    for note in notes:
        out(f"  SLOWER: {note}")
    if data_dir is not None:
        path = history_file(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "timings": times,
                                     "accuracy": suite["accuracy"]}) + "\n")
    return {"suite": suite, "timings": times, "slower": notes}
