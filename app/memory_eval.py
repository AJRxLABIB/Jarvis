"""Memory self-check: does Jarvis's memory behave correctly over TIME? A small, deterministic test of the real memory code on a throw-away database.

    python run.py --memory-eval

It never touches your memories: every case runs on a temporary database. Cases (each has a defined start, an action and an expected end):
update wins over the old value, the old value stays in the history, a forgotten fact is gone (history included), a switched-off fact is not recalled,
an expired fact is not recalled, a private fact is left out of an export, recall finds the right fact among distractors, a second fact on another topic
does not overwrite the first, and a secret is refused.
This checks the memory STORE's rules. It is not LongMemEval and says nothing about how well the language model uses what is recalled.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass
class CaseResult:
    name: str
    passed: bool
    detail: str = ""


def _fresh(folder: Path, n: int):
    from app.memory.database import Database
    from app.memory.manager import MemoryManager

    db = Database(folder / f"m{n}.db")
    db.initialize()
    return MemoryManager(db)


def _update_wins(m: Any) -> str:
    m.remember("my office is on the second floor", topic="office floor")
    m.remember("my office is on the fifth floor", topic="office floor")
    got = m.by_topic("office floor")
    assert got is not None and "fifth" in got.content, "the newer value is not the current one"
    assert not any("second" in x.content for x in m.search("office floor")), "the old value is still recalled as current"
    return "newer value is current"


def _history_kept(m: Any) -> str:
    m.remember("my office is on the second floor", topic="office floor")
    m.remember("my office is on the fifth floor", topic="office floor")
    hist = m.history(m.by_topic("office floor"))
    assert any("second" in str(h) for h in hist), "the earlier wording is not in the history"
    return "old value kept as history, not as a current fact"


def _forget_is_gone(m: Any) -> str:
    mem, _ = m.remember("my locker number is forty two", topic="locker")
    m.forget(mem.id)
    assert m.get(mem.id) is None and m.search("locker") == [], "a forgotten fact can still be found"
    return "forgotten fact cannot be recalled"


def _disabled_hidden(m: Any) -> str:
    mem, _ = m.remember("I park in bay seven", topic="parking")
    m.set_disabled(mem, True)
    assert m.search("park bay") == [], "a switched-off fact is recalled"
    return "switched-off fact is not recalled"


def _expired_hidden(m: Any) -> str:
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    m.remember("the guest wifi name is harbour", topic="guest wifi", expires_at=past)
    assert m.search("guest wifi") == [] and m.by_topic("guest wifi") is None, "an expired fact is recalled"
    return "expired fact is not recalled"


def _private_not_exported(m: Any) -> str:
    m.remember("my sister is called Rima", topic="sister")
    m.remember("my blood group is O positive", topic="blood group", private=True)
    out = str(m.export())
    assert "Rima" in out and "blood" not in out.lower(), "a private fact is in the default export"
    return "private fact stays out of an export"


def _recall_among_distractors(m: Any) -> str:
    for i, city in enumerate(("Dhaka", "Sylhet", "Khulna", "Rajshahi", "Barisal")):
        m.remember(f"the {city} branch opens at {8 + i} in the morning", topic=f"{city} branch hours")
    top = m.search("when does the Khulna branch open", limit=1)
    assert top and "Khulna" in top[0].content, "the wrong fact is recalled first"
    return "right fact first among similar ones"


def _topics_independent(m: Any) -> str:
    m.remember("my laptop is a Lenovo", topic="laptop")
    m.remember("my phone is a Pixel", topic="phone")
    assert "Lenovo" in m.by_topic("laptop").content and "Pixel" in m.by_topic("phone").content, "one topic overwrote another"
    return "a new topic does not overwrite another"


def _secret_refused(m: Any) -> str:
    try:
        m.remember("my bank password is hunter2secret", topic="bank")
    except Exception:
        return "secret was refused"
    raise AssertionError("a password was stored as a memory")


CASES: tuple[tuple[str, Callable[[Any], str]], ...] = (
    ("update wins over the old value", _update_wins), ("old value is kept as history", _history_kept), ("forgotten fact is gone", _forget_is_gone),
    ("switched-off fact is not recalled", _disabled_hidden), ("expired fact is not recalled", _expired_hidden), ("private fact stays out of an export", _private_not_exported),
    ("recall among similar facts", _recall_among_distractors), ("topics do not overwrite each other", _topics_independent), ("a secret is refused", _secret_refused))


def run() -> list[CaseResult]:
    results = []
    with tempfile.TemporaryDirectory(prefix="jarvis-memeval-") as tmp:
        for n, (name, fn) in enumerate(CASES):
            try:
                results.append(CaseResult(name, True, fn(_fresh(Path(tmp), n))))
            except AssertionError as exc:
                results.append(CaseResult(name, False, str(exc)))
            except Exception as exc:
                results.append(CaseResult(name, False, f"could not run: {type(exc).__name__}: {str(exc)[:100]}"))
    return results


def report(results: list[CaseResult]) -> str:
    ok = sum(r.passed for r in results)
    lines = [f"Memory self-check: {ok} of {len(results)} cases passed (temporary database; your memories were not touched)."]
    lines += [f"  {'PASS' if r.passed else 'FAIL'}  {r.name}" + (f" - {r.detail}" if r.detail else "") for r in results]
    lines.append("This tests the memory store's rules, not how the language model uses recalled facts.")
    return "\n".join(lines)
