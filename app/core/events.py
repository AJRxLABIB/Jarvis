"""The event bus: one place where "something happened" is announced, logged and reacted to.

Events (names below) are published by the parts that know: a run ended, a mission finished or paused, the
battery is low, a reminder fired, a security check stopped something, Jarvis was updated, an emergency control
was used. Subscribers (the notification center, automations later) get them in the order they subscribed;
one failing subscriber never stops the others or the publisher. Each event is also written to the ``events``
table (the newest MAX_KEPT), with the current run's ID - data is redacted first.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.utils.logging_setup import get_logger, log_event
from app.utils.redaction import redact_value

_log = get_logger("core.events")
MAX_KEPT = 2000

RUN_FINISHED = "RunFinished"
MISSION_COMPLETED, MISSION_PAUSED = "MissionCompleted", "MissionPaused"
BATTERY_LOW, PC_PROBLEM, DOWNLOAD_COMPLETED = "BatteryLow", "PcProblem", "DownloadCompleted"
REMINDER, SUGGESTION = "Reminder", "Suggestion"
SECURITY_EVENT, EMERGENCY = "SecurityEvent", "Emergency"
JARVIS_UPDATED, MODE_CHANGED = "JarvisUpdated", "ModeChanged"
BUILD_FAILED = "BuildFailed"       # a project Jarvis started crashed (dev_session); event_intel groups it with what changed
# round 75: the cross-feature events. Payloads carry ``correlation_id`` (one id for everything one request caused) and never raw images, audio or coordinates.
VISION_ANALYSIS_COMPLETED, MEDIA_PROCESSING_COMPLETED, MEDIA_SEARCH_MATCH_FOUND = "VisionAnalysisCompleted", "MediaProcessingCompleted", "MediaSearchMatchFound"
AGENT_TASK_COMPLETED, LOCATION_ENTERED, LOCATION_EXITED = "AgentTaskCompleted", "LocationEntered", "LocationExited"
SOUND_EVENT_DETECTED, INFERENCE_PROVIDER_CHANGED = "SoundEventDetected", "InferenceProviderChanged"
KNOWN = frozenset({VISION_ANALYSIS_COMPLETED, MEDIA_PROCESSING_COMPLETED, MEDIA_SEARCH_MATCH_FOUND, AGENT_TASK_COMPLETED, LOCATION_ENTERED, LOCATION_EXITED,
                   SOUND_EVENT_DETECTED, INFERENCE_PROVIDER_CHANGED, RUN_FINISHED, MISSION_COMPLETED, MISSION_PAUSED, BATTERY_LOW, PC_PROBLEM, DOWNLOAD_COMPLETED,
                   REMINDER, SUGGESTION, SECURITY_EVENT, EMERGENCY, JARVIS_UPDATED, MODE_CHANGED, BUILD_FAILED})


@dataclass(frozen=True)
class Event:
    name: str
    data: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Callable[[Event], None]]] = defaultdict(list)
        self.database = None

    def subscribe(self, name: str, handler: Callable[[Event], None]) -> None:
        """``name`` = an event name, or "*" for every event."""
        with self._lock:
            self._subscribers[name].append(handler)

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()
        self.database = None

    def publish(self, name: str, **data: Any) -> Event:
        from app.core.runs import current

        if name not in KNOWN:
            raise ValueError(f"unknown event {name!r}")  # a typo must not silently go nowhere
        run = current()
        event = Event(name, dict(redact_value(data)), run.id if run is not None else None)
        self._store(event)
        with self._lock:
            handlers = [*self._subscribers.get(name, ()), *self._subscribers.get("*", ())]
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                _log.warning("event subscriber failed for %s", name, exc_info=True)
        return event

    def _store(self, event: Event) -> None:
        if self.database is None:
            return
        from app.memory.database import utc_now_iso

        try:
            with self.database.transaction() as conn:
                conn.execute("INSERT INTO events (ts, name, run_id, data) VALUES (?, ?, ?, ?)",
                             (utc_now_iso(), event.name, event.run_id,
                              json.dumps(event.data, ensure_ascii=False, default=str)[:4000]))
                conn.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (MAX_KEPT,))
        except Exception:
            log_event(_log, "event_not_stored", name=event.name)

    def recent(self, limit: int = 50) -> list[dict]:
        if self.database is None:
            return []
        with self.database.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]


BUS = EventBus()
