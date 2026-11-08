"""What the phone says about its surroundings (JARVIS Mobile 1.5 "Phone sensors"): validated, honest about age, and only ever coarse words.

The phone sends a ``context`` object with a check-in - only the kinds its owner switched on (network, headset, orientation, motion, light) - and a
``sensors`` object saying which sensors the phone has. The phone is untrusted input: anything outside the fixed vocabulary is dropped, never stored
or shown. A value older than ``FRESH_S`` is UNKNOWN now (it is shown with its age, never as the current state). Motion is "stationary" / "moving" from
one 1.5 s accelerometer sample: it is not an activity recognizer, and nothing here says walking or driving. No location is involved.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

FRESH_S = 900
VOCAB: dict[str, set[str] | type] = {
    "network": {"wifi", "cellular", "ethernet", "other", "none", "unknown"},
    "headset": bool,
    "orientation": {"portrait", "landscape"},
    "motion": {"stationary", "moving", "unknown"},
    "light": {"dark", "dim", "bright", "unknown"},
}
SENSORS = ("accelerometer", "gyroscope", "proximity", "light", "magnetometer")


def sanitize(report: Any) -> dict[str, Any]:
    """From a check-in report: {'context': {...valid kinds...}, 'sensors': {...}} - or {} when the report is not a full report (a bare check-in)."""
    if not isinstance(report, dict) or "app_version" not in report:       # only the phone's full report speaks for its context; a bare check-in changes nothing
        return {}
    ctx: dict[str, Any] = {}
    raw = report.get("context")
    if isinstance(raw, dict):
        for key, allowed in VOCAB.items():
            value = raw.get(key)
            if allowed is bool:
                if isinstance(value, bool):
                    ctx[key] = value
            elif isinstance(value, str) and value in allowed:
                ctx[key] = value
    sensors: dict[str, bool] = {}
    raw_s = report.get("sensors")
    if isinstance(raw_s, dict):
        sensors = {k: raw_s[k] for k in SENSORS if isinstance(raw_s.get(k), bool)}
    return {"context": ctx, "sensors": sensors}


def _age_s(state: dict, now: datetime) -> float | None:
    try:
        seen = datetime.fromisoformat(str(state.get("seen", "")).replace("Z", "+00:00"))
        return max(0.0, (now - (seen if seen.tzinfo else seen.replace(tzinfo=timezone.utc))).total_seconds())
    except ValueError:
        return None


def fresh_context(state: dict, now: datetime | None = None) -> dict[str, Any]:
    """The context kinds that are still current ({} when the phone has not reported lately or switched them all off)."""
    age = _age_s(state, now or datetime.now(timezone.utc))
    if age is None or age > FRESH_S:
        return {}
    return dict(state.get("context") or {})


def describe(name: str, state: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    age = _age_s(state, now)
    if not state or age is None:
        return f"{name}: no report from the phone yet."
    when = f"{int(age)} s ago" if age < 120 else f"{int(age // 60)} min ago"
    ctx = state.get("context") or {}
    have = state.get("sensors") or {}
    if age > FRESH_S:
        return f"{name}: last heard {when} - too old to tell anything about where it is now (unknown)."
    parts = []
    if "network" in ctx:
        parts.append({"wifi": "on Wi-Fi", "cellular": "on mobile data", "ethernet": "on a wired link", "none": "offline", "other": "on another network", "unknown": "network unknown"}[ctx["network"]])
    if "headset" in ctx:
        parts.append("headphones connected" if ctx["headset"] else "no headphones")
    if "orientation" in ctx:
        parts.append(ctx["orientation"])
    if "motion" in ctx:
        parts.append({"stationary": "not moving", "moving": "moving", "unknown": "motion unknown"}[ctx["motion"]])
    if "light" in ctx:
        parts.append(f"{ctx['light']} around it" if ctx["light"] != "unknown" else "light unknown")
    if state.get("charging") is not None:
        parts.append("charging" if state.get("charging") else "on battery")
    if isinstance(state.get("battery"), (int, float)):
        parts.append(f"battery {int(state['battery'])}%")
    missing = [k for k in SENSORS if have.get(k) is False]
    tail = f" (this phone has no {', '.join(missing)} sensor)" if missing else ""
    off = [k for k in VOCAB if k not in ctx]
    shared = f"; not shared by the phone: {', '.join(off)}" if off and ctx else ("; the phone shares no context (all switched off)" if not ctx else "")
    return f"{name} ({when}): " + (", ".join(parts) or "no details") + tail + shared + "."


def suggestions(state: dict, now: datetime | None = None) -> list[str]:
    """Offers only, from FRESH values; nothing acts by itself."""
    ctx = fresh_context(state, now)
    out = []
    if ctx.get("headset") is True:
        out.append("Headphones are connected: spoken replies can be used without disturbing anyone.")
    if ctx.get("motion") == "moving":
        out.append("The phone is moving: long spoken announcements on the PC may not suit right now (a quiet mode is available).")
    if ctx.get("network") == "wifi" and state.get("charging") is True:
        out.append("The phone is on Wi-Fi and charging: a good moment for background sync.")
    if ctx.get("network") in ("cellular", "none"):
        out.append("The phone is not on Wi-Fi: it may not reach the PC over the home network.")
    return out
