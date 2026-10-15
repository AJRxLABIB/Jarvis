"""Runs an asyncio event loop in a background thread.

Synchronous code (the terminal UI now, the Qt GUI later) submits coroutines
and keeps its own thread free for input handling.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class AsyncRunner:
    def __init__(self, name: str = "asyncio-loop") -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name=name, daemon=True)
        self._started = False

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self) -> "AsyncRunner":
        if not self._started:
            self._thread.start()
            self._started = True
        return self

    def submit(self, coro: Coroutine[Any, Any, T]) -> "concurrent.futures.Future[T]":
        self.start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        return self.submit(coro).result(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        if not self._started or self._loop.is_closed():
            return

        async def _cancel_all() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_cancel_all(), self._loop).result(timeout)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout)
        if not self._thread.is_alive():
            self._loop.close()

    def __enter__(self) -> "AsyncRunner":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
