"""Runs: every request gets an ID, a source and a record - the start of the agent trace.

A run starts when a request arrives (you speak or type, the phone sends one, an automation fires) and
ends with its result. Everything the executor does during the run is audited under the run's ID, so
"what did Jarvis do for that request?" has an exact answer (``audit_log.run_id``).

The current run is a context variable: code deep inside a tool doesn't need it passed along, and it
follows the request into worker threads (``asyncio.to_thread`` copies the context).

The run also carries the request's **source** and its **taint** (app/security/taint.py): whether
untrusted text (web pages, emails, messages, documents, the screen) is in the conversation.
"""

from __future__ import annotations

import contextvars
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from app.utils.logging_setup import get_logger, log_event
from app.utils.redaction import redact

if TYPE_CHECKING:
    from app.memory.database import Database
    from app.security.taint import Taint

_log = get_logger("core.runs")

Source = Literal["voice", "text", "phone", "automation", "cli", "internal"]
_CURRENT: contextvars.ContextVar["Run | None"] = contextvars.ContextVar("jarvis_run", default=None)


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


@dataclass
class Run:
    source: Source
    text: str = ""
    id: str = field(default_factory=new_id)
    taint: "Taint | None" = None
    user_words: frozenset[str] = frozenset()
    started: float = field(default_factory=time.monotonic)
    tools: list[str] = field(default_factory=list)
    confirmed_after_untrusted: bool = False  # the call running now was approved because of the taint rule
    completed: dict[str, str] = field(default_factory=dict)  # one-shot calls already done in this request (no repeats)


def current() -> Run | None:
    return _CURRENT.get()


@contextmanager
def running(run: Run, store: "RunStore | None" = None) -> Iterator[Run]:
    """``with running(Run("voice", text)):`` - the run is current inside the block and recorded."""
    token = _CURRENT.set(run)
    if store is not None:
        store.start(run)
    outcome = "cancelled"
    try:
        yield run
        outcome = "completed"
    except BaseException as exc:
        outcome = "cancelled" if type(exc).__name__ == "CancelledError" else "failed"
        raise
    finally:
        _CURRENT.reset(token)
        elapsed = int((time.monotonic() - run.started) * 1000)
        log_event(_log, "run_finished", run_id=run.id, source=run.source, outcome=outcome, elapsed_ms=elapsed,
                  tools=len(run.tools))
        if store is not None:
            store.finish(run, outcome, elapsed)


class RunStore:
    """agent_runs table (schema 8). Only a redacted, shortened request text is kept."""

    TEXT_LIMIT = 300

    def __init__(self, database: "Database") -> None:
        self._db = database

    def start(self, run: Run) -> None:
        from app.memory.database import utc_now_iso

        try:
            with self._db.transaction() as conn:
                conn.execute("INSERT INTO agent_runs (id, source, text, state, started_at) VALUES (?, ?, ?, ?, ?)",
                             (run.id, run.source, redact(run.text)[: self.TEXT_LIMIT], "running", utc_now_iso()))
        except Exception:
            _log.warning("could not record the run", exc_info=True)

    def finish(self, run: Run, outcome: str, elapsed_ms: int) -> None:
        from app.memory.database import utc_now_iso

        try:
            with self._db.transaction() as conn:
                conn.execute("UPDATE agent_runs SET state = ?, ended_at = ?, elapsed_ms = ?, tools = ? WHERE id = ?",
                             (outcome, utc_now_iso(), elapsed_ms, ",".join(run.tools)[:500], run.id))
        except Exception:
            _log.warning("could not finish the run record", exc_info=True)

    def recent(self, limit: int = 20) -> list[dict]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM agent_runs ORDER BY started_at DESC, rowid DESC LIMIT ?",
                                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def trace(self, run_id: str) -> list[dict]:
        """Everything the executor recorded for one run, in order."""
        with self._db.connect() as conn:
            rows = conn.execute("SELECT ts, event, tool, permission_level, decision, details FROM audit_log "
                                "WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def mark_interrupted(self) -> int:
        """At start: runs still 'running' from a crash or a hard exit are closed as interrupted."""
        try:
            with self._db.transaction() as conn:
                return conn.execute("UPDATE agent_runs SET state = 'interrupted' WHERE state = 'running'").rowcount
        except Exception:
            return 0
