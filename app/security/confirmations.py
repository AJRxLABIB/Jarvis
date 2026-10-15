"""Asking the user to approve an action.

The executor creates a ``ConfirmationRequest`` and awaits a ``Confirmer``:
* ``ConsoleConfirmer``  - asks in the terminal (text chat)
* ``PendingConfirmer``  - hands the request to a UI thread that polls it
                          (voice terminal now, GUI dialog in Phase 9)
* ``DenyConfirmer``     - refuses everything (unattended automation)
Anything other than an explicit "yes" is a refusal.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable
import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.security.permissions import RiskLevel

_LEVEL_NAMES = {0: "safe", 1: "moderate", 2: "dangerous", 3: "critical"}


@dataclass(frozen=True)
class ConfirmationRequest:
    tool: str
    level: RiskLevel
    summary: str  # e.g. "Restart the computer now?"
    details: dict[str, Any] = field(default_factory=dict)
    # Every question has its own ID: an answer (a click, "yes", the phone's button) approves exactly
    # this request - never a different one that happens to be waiting when the answer arrives.
    id: str = field(default_factory=lambda: secrets.token_hex(6))
    run_id: str | None = field(default_factory=lambda: _run_id())

    @property
    def level_name(self) -> str:
        return _LEVEL_NAMES[int(self.level)]


def _run_id() -> str | None:
    from app.core.runs import current

    run = current()
    return run.id if run is not None else None


class Confirmer(Protocol):
    async def confirm(self, request: ConfirmationRequest) -> bool: ...


class DenyConfirmer:
    async def confirm(self, request: ConfirmationRequest) -> bool:
        return False


def is_yes(answer: str) -> bool:
    return answer.strip().lower() in ("y", "yes")


class ConsoleConfirmer:
    """Asks with input() in a worker thread so the event loop keeps running."""

    def __init__(self, ask: Callable[[str], str] = input, write: Callable[[str], Any] = print) -> None:
        self._ask = ask
        self._write = write

    async def confirm(self, request: ConfirmationRequest) -> bool:
        self._write(f"\n[confirm - {request.level_name}] {request.summary}")
        answer = await asyncio.to_thread(self._ask, "Type 'yes' to confirm, anything else cancels: ")
        return is_yes(answer)


class PendingConfirmer:
    """Bridges the async executor and a UI thread.

    The UI calls ``pending()`` regularly; when it returns a request it shows it
    and calls ``resolve(True/False)``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request: ConfirmationRequest | None = None
        self._future: concurrent.futures.Future[bool] | None = None

    async def confirm(self, request: ConfirmationRequest) -> bool:
        future: concurrent.futures.Future[bool] = concurrent.futures.Future()
        with self._lock:
            if self._future is not None:  # one question at a time
                return False
            self._request, self._future = request, future
        try:
            return await asyncio.wrap_future(future)
        finally:
            with self._lock:
                self._request, self._future = None, None

    def pending(self) -> ConfirmationRequest | None:
        with self._lock:
            if self._future is None or self._future.done():
                return None
            return self._request

    def resolve(self, approved: bool) -> None:
        """Answer the pending request. It stops being pending immediately, so a UI
        polling ``pending()`` right afterwards can never ask the same question twice."""
        with self._lock:
            future, self._request = self._future, None
        if future is not None and not future.done():
            future.set_result(bool(approved))
