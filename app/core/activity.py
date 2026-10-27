"""A tiny, thread-safe "what is Jarvis doing right now" flag the interface can poll (no Qt, no events).

``verifying`` counts the tool checks that are running ("did it really happen?"), so the status area can show the
Verifying state truthfully instead of guessing.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

_lock = threading.Lock()
_verifying = 0


@contextmanager
def verifying():
    global _verifying
    with _lock:
        _verifying += 1
    try:
        yield
    finally:
        with _lock:
            _verifying = max(0, _verifying - 1)


def is_verifying() -> bool:
    with _lock:
        return _verifying > 0
