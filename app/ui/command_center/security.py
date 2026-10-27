"""The security view's data. It reuses the facts app/core/security_center.py already gathers (paired phones and their
permissions, microphone / screen state, blocked actions, standing approvals, backup health, the activity-log check) and
adds the two things that page lacked: a history of SENSITIVE actions that were allowed, and the local-data settings.
Read-only: the emergency stop and every change go through the existing confirmed controls."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core import emergency, security_center
from app.core.security_center import Item, Section

LEVEL_GLYPH = {"ok": "✔", "note": "ℹ", "warn": "⚠"}
LOCAL_DATA = (("conversation_memory", "conversation memory kept on this PC"),
              ("clipboard_history", "clipboard history kept"),
              ("memory_suggestions", "offers to remember things you say"),
              ("screen_capture_enabled", "screen capture allowed"),
              ("presence_detection", "camera presence detection"),
              ("speech_dataset", "voice recordings kept for training (corrections / all)"))


@dataclass
class Overview:
    sections: list[Section] = field(default_factory=list)
    emergency: list[str] = field(default_factory=list)       # switches that are ON, in words

    @property
    def warnings(self) -> int:
        return sum(1 for s in self.sections for i in s.items if i.level == "warn")

    @property
    def headline(self) -> str:
        if self.emergency:
            return "Emergency stop is ACTIVE: " + ", ".join(self.emergency) + "."
        return f"{self.warnings} item(s) need a look." if self.warnings else "Nothing needs attention right now."

    def section(self, title_start: str) -> Section | None:
        return next((s for s in self.sections if s.title.lower().startswith(title_start.lower())), None)


def sensitive_history(database, limit: int = 12) -> Section:
    sec = Section("Sensitive actions that ran (last 7 days)")
    try:
        with database.connect() as conn:
            rows = conn.execute(
                "SELECT ts, tool, permission_level, decision FROM audit_log WHERE permission_level >= 2 AND "
                "decision IN ('allow', 'confirm', 'confirmed', 'approved') AND ts >= datetime('now', '-7 days') "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        for r in rows:
            sec.items.append(Item(f"{str(r['ts'])[:16]}  {r['tool'] or '?'}  ({r['decision']})", "note"))
        if not rows:
            sec.items.append(Item("no dangerous action ran", "ok"))
    except Exception:
        sec.items.append(Item("the audit log could not be read", "warn"))
    return sec


def local_data(settings) -> Section:
    sec = Section("Your data on this PC")
    for name, label in LOCAL_DATA:
        if not hasattr(settings, name):
            continue
        value = getattr(settings, name)
        on = bool(value) and str(value).lower() not in ("off", "false", "none", "")
        sec.items.append(Item(f"{label}: {'on' if on else 'off'}", "note" if on else "ok"))
    sec.items.append(Item("everything stays on this PC; the Privacy dashboard lists and deletes it", "ok"))
    return sec


def network_mode(settings) -> Section:
    """Strict offline mode in plain words, from the socket guard's own counters (nothing here is assumed)."""
    from app.utils import egress

    sec = Section("Network")
    state = egress.status()
    if settings.offline_mode == "on":
        if state["installed"]:
            sec.items.append(Item("Strict offline mode is ON: connections outside this PC and your network are refused", "ok"))
            sec.items.append(Item(f"{state['blocked']} connection(s) refused since start" +
                                  (f" (latest: {state['recent'][-1]})" if state["recent"] else ""), "note"))
        else:
            sec.items.append(Item("Offline mode is ON but the network guard is not installed", "warn"))
        if state["proxy"]:
            sec.items.append(Item("A web proxy is configured (" + ", ".join(state["proxy"]) + "): a proxy could forward "
                                  "traffic the guard cannot see", "warn"))
    elif settings.offline_mode == "auto":
        sec.items.append(Item("Online access is allowed; offline only while there is no internet (OFFLINE_MODE=on makes it strict)", "note"))
    else:
        sec.items.append(Item("Online access is allowed (OFFLINE_MODE=off)", "note"))
    return sec


def collect(database, settings, live: dict | None = None) -> Overview:
    if database is None:
        return Overview([Section("Security", [Item("the local database isn't available", "warn")])],
                        emergency.STATE.active())
    sections = security_center.collect(database, settings, live=live)
    sections.insert(1 if live else 0, local_data(settings))
    sections.append(network_mode(settings))
    sections.append(sensitive_history(database))
    return Overview(sections, emergency.STATE.active())
