"""What the PC does with place events a phone reports in its check-in (``report["location_events"]``): validate, apply the user's rules, tell the event bus.

Only events arrive - ``{id, place, kind, t, accuracy_m}`` - never coordinates. A bad or repeated event is dropped with a logged reason; nothing here raises into the
check-in. Rule actions only SAY something (a notification) or suggest a profile name; no tool runs from here."""

from __future__ import annotations

import time
from typing import Any

from app.core import events, geofence
from app.memory.settings_store import SettingsStore
from app.utils.logging_setup import get_logger

_log = get_logger("location")
KEY = "location_hub"
MAX_PER_CHECKIN = 10


def process(db: Any, raw: Any, clock=time.time) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        return []
    store = SettingsStore(db)
    hub = geofence.Hub.load(store.get(KEY), clock=clock)
    if not hub.places:
        return []
    for old in store.get(KEY + "_seen") or []:
        hub._seen.append(str(old))                                         # ids already handled in earlier check-ins
    out = []
    changed = False
    for ev in raw[:MAX_PER_CHECKIN]:
        result = hub.handle(ev if isinstance(ev, dict) else {})
        if not result["ok"]:
            _log.info("location event dropped: %s", result["reason"])
            continue
        changed = True
        kind = ev["kind"]
        name = hub.places[ev["place"]].name
        try:
            events.BUS.publish(events.LOCATION_ENTERED if kind in ("enter", "dwell") else events.LOCATION_EXITED, place=name, kind=kind, correlation_id=str(ev["id"]),
                               actions=[a["say"] for a in result["actions"] if a["say"]])
        except Exception:
            _log.warning("could not publish the location event", exc_info=True)
        out.append({"place": name, "kind": kind, "actions": result["actions"]})
    if changed:
        store.set(KEY + "_seen", list(hub._seen)[-100:])
    return out
