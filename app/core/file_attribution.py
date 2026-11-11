"""Which program changed which file - read from Windows' own records, when you have switched them on. Jarvis installs nothing and traces nothing itself.

Two sources, both set up by YOU as administrator (``SETUP`` has the exact commands):
* Windows audit policy - the Security log, event 4663 ("an attempt was made to access an object") for folders you chose to audit;
* Sysmon (a free Microsoft tool) - its log, event 11 (file created) and 23 / 26 (file deleted).
``parse`` turns the events' XML into (time, program, file, what happened); ``summarise`` answers "who touched this folder / file". Reading the Security
log needs an administrator: without it the reader says so instead of returning an empty answer. If neither source is on, the answer is "not set up",
never "nothing happened". Only metadata is read (paths and program names) - never file contents.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable
from xml.etree import ElementTree

SETUP = """To let Windows record which program changes files (run in an ADMINISTRATOR PowerShell; pick ONE):

A) Built-in auditing, for one folder (replace the path):
   auditpol /set /subcategory:"File System" /success:enable
   $p = "D:\\Important"
   $acl = Get-Acl $p -Audit
   $rule = New-Object System.Security.AccessControl.FileSystemAuditRule("Everyone","Write,Delete,DeleteSubdirectoriesAndFiles","ContainerInherit,ObjectInherit","None","Success")
   $acl.AddAuditRule($rule); Set-Acl $p $acl
   Jarvis must then run as administrator to read the Security log.

B) Sysmon (download from Microsoft Sysinternals), default configuration:
   sysmon64.exe -accepteula -i
   It logs file creation with the program's name for the whole PC.

To switch A off again: auditpol /set /subcategory:"File System" /success:disable   To remove B: sysmon64.exe -u"""

# access-mask bits of event 4663 that mean "changed"
_WRITE, _APPEND, _DELETE = 0x2, 0x4, 0x10000
_NS = re.compile(r"\{[^}]*\}")


@dataclass(frozen=True)
class Change:
    when: str
    program: str
    path: str
    action: str            # written | deleted | created
    source: str            # audit | sysmon


def _data(event: ElementTree.Element) -> tuple[int, str, dict[str, str]]:
    eid, when, data = 0, "", {}
    for el in event.iter():
        tag = _NS.sub("", el.tag)
        if tag == "EventID":
            eid = int((el.text or "0").strip() or 0)
        elif tag == "TimeCreated":
            when = el.attrib.get("SystemTime", "")[:19]
        elif tag == "Data" and el.attrib.get("Name"):
            data[el.attrib["Name"]] = (el.text or "").strip()
    return eid, when, data


def parse(xml_text: str) -> list[Change]:
    """Events as XML (one <Event> after another, or wrapped) -> changes. Unknown or read-only events are skipped."""
    text = (xml_text or "").strip().lstrip("\ufeff")
    if not text:
        return []
    try:
        root = ElementTree.fromstring(f"<Events>{text}</Events>" if not text.startswith("<Events") else text)
    except ElementTree.ParseError:
        return []
    out = []
    for ev in root:
        eid, when, d = _data(ev)
        if eid == 4663:
            try:
                mask = int(d.get("AccessMask", "0"), 16)
            except ValueError:
                continue
            action = "deleted" if mask & _DELETE else "written" if mask & (_WRITE | _APPEND) else ""
            if action and d.get("ObjectName") and d.get("ObjectType", "File") == "File":
                out.append(Change(when, os.path.basename(d.get("ProcessName", "").replace("\\", "/")) or "unknown", d["ObjectName"], action, "audit"))
        elif eid in (11, 23, 26) and d.get("TargetFilename"):
            out.append(Change(when, os.path.basename(d.get("Image", "").replace("\\", "/")) or "unknown", d["TargetFilename"], "created" if eid == 11 else "deleted", "sysmon"))
    return out


def under(path: str, folder: str) -> bool:
    p, f = path.replace("/", "\\").lower().rstrip("\\"), folder.replace("/", "\\").lower().rstrip("\\")
    return p == f or p.startswith(f + "\\")


def summarise(changes: list[Change], folder: str = "", top: int = 5) -> str:
    rows = [c for c in changes if not folder or under(c.path, folder)]
    if not rows:
        return "No recorded file change" + (f" under {folder}" if folder else "") + " in the events that were read."
    by: dict[str, dict[str, int]] = {}
    for c in rows:
        by.setdefault(c.program, {}).setdefault(c.action, 0)
        by[c.program][c.action] += 1
    ranked = sorted(by.items(), key=lambda kv: -sum(kv[1].values()))[:top]
    lines = [f"{len(rows)} recorded change(s)" + (f" under {folder}" if folder else "") + ", by program:"]
    lines += [f"  {prog}: " + ", ".join(f"{n} {a}" for a, n in sorted(acts.items())) for prog, acts in ranked]
    last = sorted(rows, key=lambda c: c.when)[-1]
    lines.append(f"Most recent: {last.when} - {last.program} {last.action} {last.path}")
    return "\n".join(lines)


def read(run: Callable[[str], str], scripts: dict[str, str]) -> tuple[list[Change], list[str]]:
    """(changes, notes). ``run`` runs one of the fixed PowerShell scripts. A source that is off or unreadable becomes a note, not silence."""
    changes: list[Change] = []
    notes: list[str] = []
    for key, label in (("file_audit_events", "Windows auditing (Security log)"), ("sysmon_file_events", "Sysmon")):
        try:
            got = parse(run(scripts[key]))
        except Exception as exc:
            msg = str(exc).lower()
            notes.append(f"{label}: " + ("needs Jarvis to run as administrator" if "access" in msg or "unauthorized" in msg or "privilege" in msg
                                        else "not set up or no events" if "no events" in msg or "not found" in msg or "could not be found" in msg or "no answer" in msg
                                        else f"could not be read ({str(exc)[:80]})"))
            continue
        if not got:
            notes.append(f"{label}: no file events found (not set up, or nothing recorded yet)")
        changes += got
    return changes, notes


def describe(changes: list[Change], notes: list[str], folder: str = "") -> str:
    if not changes:
        return "Windows has no record of which program changed files here.\n" + "\n".join(f"  {n}" for n in notes) + "\nSay 'file attribution setup' for how to switch recording on."
    return summarise(changes, folder) + ("\n" + "\n".join(f"  note - {n}" for n in notes) if notes else "") + "\nSource: Windows' own event records (paths and program names only)."


def scripts() -> dict[str, Any]:
    from app.core import pc_checkup

    return pc_checkup.EVENT_SCRIPTS
