"""One page that answers "is my Jarvis safe right now?" from facts the app already records. Read-only.

It does not grant, change or revoke anything itself (the buttons in the window call the existing, confirmed controls:
emergency stop, the phones dialog, the privacy dialog). Every line comes from real data: the tamper-evident activity
log, the audit log's blocked decisions, the phone registry and its refused requests, the settings that guard the PC,
and the last backup restore test. A section with nothing to report says so instead of inventing content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# setting -> (the SAFE value, why it matters)
GUARDS: dict[str, tuple[Any, str]] = {
    "confirm_power_actions": (True, "asks before shut down / restart / sleep"),
    "update_require_signature": (True, "only installs updates whose signature checks out"),
    "voice_confirm": (True, "voice yes/no answers are accepted for approvals"),
}


@dataclass
class Item:
    text: str
    level: str = "ok"          # ok | note | warn


@dataclass
class Section:
    title: str
    items: list[Item] = field(default_factory=list)


def _settings_section(settings) -> Section:
    sec = Section("Guards on this PC")
    for name, (safe, why) in GUARDS.items():
        value = getattr(settings, name, None)
        if value is None:
            continue
        if name == "update_require_signature" and not getattr(settings, "update_manifest_url", ""):
            sec.items.append(Item("updates: no update feed is configured, so nothing can be installed automatically", "ok"))
            continue
        sec.items.append(Item(f"{name.replace('_', ' ')}: {'on' if value else 'OFF'} ({why})", "ok" if value == safe else "warn"))
    if bool(getattr(settings, "remote_enabled", False)):
        https = bool(getattr(settings, "remote_https", True))
        sec.items.append(Item(f"phone remote: on, {'encrypted (TLS)' if https else 'NOT encrypted'}", "ok" if https else "warn"))
    else:
        sec.items.append(Item("phone remote: off", "ok"))
    for name, label in (("screen_capture_enabled", "screen capture"), ("presence_detection", "camera presence detection"),
                        ("speaker_verification", "only-my-voice check")):
        if hasattr(settings, name):
            on = bool(getattr(settings, name))
            sec.items.append(Item(f"{label}: {'on' if on else 'off'}", "note" if on else "ok"))
    sec.items.append(Item(f"offline mode: {getattr(settings, 'offline_mode', 'auto')}", "ok"))
    level = getattr(settings, "autonomy", "routine")
    sec.items.append(Item({"observe": "autonomy: observe - Jarvis only reads, it changes nothing",
                           "approval": "autonomy: approval - every change asks you first",
                           }.get(level, "autonomy: routine - low-risk actions run, risky ones ask"), "ok"))
    return sec


def live_section(live: dict) -> Section:
    """What is switched on RIGHT NOW (from the running window, not from settings): listening, capture, sharing,
    remote access, waiting approvals. Anything that sees or hears you is a 'note', so it stands out."""
    sec = Section("Right now")
    if live.get("mic_muted"):
        sec.items.append(Item("microphone: muted - nothing listens, not even the wake word", "ok"))
    else:
        sec.items.append(Item("microphone: " + ("listening for the wake word (hands-free)" if live.get("hands_free")
                                                else "only when you press Talk"), "note" if live.get("hands_free") else "ok"))
    if live.get("recording"):
        sec.items.append(Item("microphone: recording your command now", "note"))
    sec.items.append(Item(f"screen capture: {'allowed' if live.get('screen_capture') else 'off'}",
                          "note" if live.get("screen_capture") else "ok"))
    for phone in live.get("sharing_phones", []):
        sec.items.append(Item(f"phone screen: {phone} is sharing its screen with this PC", "note"))
    if live.get("remote"):
        sec.items.append(Item(f"phone remote: listening on the home network ({live['remote']})", "note"))
    pending = live.get("pending_approval")
    sec.items.append(Item(f"waiting for your approval: {pending}" if pending else "no approval is waiting",
                          "warn" if pending else "ok"))
    if live.get("mode"):
        sec.items.append(Item(f"mode: {live['mode']}", "ok"))
    return sec


def collect(database, settings, *, days: int = 7, live: dict | None = None) -> list[Section]:
    sections: list[Section] = [live_section(live)] if live else []

    log = Section("Activity log")
    try:
        from app.core import auditchain

        report = auditchain.verify(database)
        log.items.append(Item(report.describe(), "ok" if report.ok else "warn"))
    except Exception as exc:
        log.items.append(Item(f"could not be checked ({exc.__class__.__name__})", "warn"))
    sections.append(log)

    sections.append(_settings_section(settings))

    standing = Section("Standing approvals")
    try:
        from app.security.grants import GrantStore

        active = GrantStore(database).active()
        if not active:
            standing.items.append(Item("none: I ask before every moderate action", "ok"))
        for grant in active:
            standing.items.append(Item(grant.describe() + "  (say 'end all approvals' to cancel)", "note"))
    except Exception as exc:
        standing.items.append(Item(f"could not be read ({exc.__class__.__name__})", "warn"))
    sections.append(standing)

    blocked = Section(f"Blocked actions (last {days} days)")
    try:
        with database.connect() as conn:
            rows = conn.execute(
                "SELECT ts, tool, decision, details FROM audit_log WHERE decision = 'block' AND ts >= datetime('now', ?) "
                "ORDER BY id DESC LIMIT 15", (f"-{int(days)} days",)).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM audit_log WHERE decision = 'block' AND ts >= datetime('now', ?)",
                                 (f"-{int(days)} days",)).fetchone()[0]
        for r in rows:
            try:
                why = json.loads(r["details"] or "{}").get("message", "")
            except ValueError:
                why = ""
            blocked.items.append(Item(f"{str(r['ts'])[:16]}  {r['tool'] or '?'} was blocked. {str(why)[:100]}".strip(), "note"))
        if total > len(rows):
            blocked.items.append(Item(f"... and {total - len(rows)} more", "note"))
        if not rows:
            blocked.items.append(Item("nothing was blocked", "ok"))
    except Exception:
        blocked.items.append(Item("the audit log could not be read", "warn"))
    sections.append(blocked)

    phones = Section("Phones")
    try:
        from app.remote.mobile import MobileRegistry

        registry = MobileRegistry(database)
        devices = registry.all()
        for d in devices:
            level = "ok" if d.trust in ("trusted", "restricted") else "note"
            phones.items.append(Item(f"{d.name}: {d.trust}, can: {', '.join(d.permissions) or 'nothing'}; "
                                     f"last seen {(d.last_seen or 'never')[:16]}", level))
        if not devices:
            phones.items.append(Item("no phone is paired", "ok"))
        refused = [e for e in registry.security_events(30) if e["kind"] not in ("trust_trusted", "permissions")]
        for e in refused[:8]:
            phones.items.append(Item(f"{str(e['at'])[:16]}  phone event: {e['kind']} {str(e.get('detail', ''))[:60]}", "note"))
    except Exception:
        phones.items.append(Item("the phone registry could not be read", "warn"))
    sections.append(phones)

    backups = Section("Backups")
    try:
        from app.memory.settings_store import SettingsStore

        last = SettingsStore(database).get("backup_restore_test")
        if isinstance(last, dict):
            backups.items.append(Item(f"last restore test ({str(last.get('at', ''))[:16]}): "
                                      f"{'passed' if last.get('ok') else 'FAILED'}. {str(last.get('message', ''))[:160]}",
                                      "ok" if last.get("ok") else "warn"))
        else:
            backups.items.append(Item("no backup has been restore-tested yet (the next daily backup does it, or run "
                                      "`python run.py --test-restore`)", "note"))
    except Exception:
        backups.items.append(Item("unknown", "warn"))
    sections.append(backups)
    return sections


def describe(sections: list[Section]) -> str:
    marks = {"ok": "  ok    ", "note": "  note  ", "warn": "  CHECK "}
    lines: list[str] = []
    for sec in sections:
        lines.append(sec.title.upper())
        lines += [marks[i.level] + i.text for i in sec.items]
        lines.append("")
    warns = sum(1 for s in sections for i in s.items if i.level == "warn")
    lines.append(f"{warns} item(s) need a look." if warns else "Nothing needs attention right now.")
    return "\n".join(lines)
