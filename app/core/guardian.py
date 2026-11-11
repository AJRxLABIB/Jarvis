"""Cyber guardian: notice that something unusual is happening on this PC, collect the evidence, say how sure it is, recommend what to do - and do nothing else.

What it watches (opt-in: ``CYBER_GUARDIAN``, off by default; names, sizes and times only - never file contents):
  * file bursts       many files changed / deleted / given a never-seen extension in your own folders within one interval (ransomware-like, or just a bulk
                      job: a sync, an extract, a git checkout - the message says so)
  * startup entries   a program added itself to Windows startup, or an entry changed, compared with the baseline you accepted
  * network           a program started listening for connections from other devices; a program suddenly reaching many new places
  * backups           no backup, a stale one, or one that failed its restore test

What it never does: stop a program, delete or quarantine a file, change a security setting. It records an incident (evidence, confidence, what was NOT
seen) and recommends steps; any protective action is a normal tool call that asks you first. It is not antivirus and says so: it sees changes and
connections, not malware signatures.

Detectors are pure functions over data that the readers collect (psutil / registry / folder listings), so they are fully tested; the readers
themselves (``read_*``) need Windows and are HARDWARE-UNVERIFIED. The first scan only records a baseline and never raises an alarm.
"""

from __future__ import annotations

import ipaddress
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

MAX_FILES = 30_000
MAX_INCIDENTS = 200
BURST_MODIFIED = 60            # files modified in one scan interval that look like a burst
BURST_DELETED = 40
BURST_NEW_EXTENSION = 25       # files that now end in an extension this folder set never had
HIGH_MODIFIED = 250
NEW_DESTINATIONS = 8           # new remote addresses for one program in one scan
BACKUP_STALE_DAYS = 3

LOW, MEDIUM, HIGH = "low", "medium", "high"
SEVERITY_PRIORITY = {LOW: "normal", MEDIUM: "high", HIGH: "urgent"}
_BULK_NOTE = "a legitimate bulk job (cloud sync, unzipping, a git checkout, an installer) looks the same, so check what you were doing"
_UNUSUAL_PATH = re.compile(r"\\(?:temp|tmp|downloads|appdata\\local\\temp|programdata\\[^\\]+\\[^\\]+\.tmp)\\|powershell(?:\.exe)?\s+.*-e(?:nc|ncodedcommand)\b|"
                           r"\\users\\public\\|mshta|wscript|cscript|rundll32.*javascript", re.I)


@dataclass
class Incident:
    id: str
    ts: str
    kind: str                       # file_burst | startup_change | new_listener | many_destinations | backup
    severity: str                   # low | medium | high
    confidence: str                 # low | medium | high
    title: str
    evidence: list[str] = field(default_factory=list)
    recommended: list[str] = field(default_factory=list)
    not_seen: list[str] = field(default_factory=list)           # what the guardian could NOT determine (honest limits)
    status: str = "open"            # open | acknowledged | dismissed | contained
    key: str = ""                   # stable identity so the same thing is not reported again and again

    def line(self) -> str:
        return f"[{self.severity.upper()}/{self.confidence}] {self.title} ({self.status})"

    def detail(self) -> str:
        parts = [self.line()] + [f"  evidence: {e}" for e in self.evidence] + [f"  not seen: {n}" for n in self.not_seen] + [f"  suggestion: {r}" for r in self.recommended]
        return "\n".join(parts)


# ------------------------------------------------------------------ detectors (pure)

FileSnapshot = dict[str, tuple[int, int]]          # path -> (size, mtime_ns)


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def file_burst(before: FileSnapshot | None, now: FileSnapshot | None, known_extensions: set[str] | None = None) -> tuple[Incident | None, dict[str, int]]:
    """Compare two listings of the watched folders. ``before=None`` (first scan) only builds the baseline."""
    stats = {"modified": 0, "created": 0, "deleted": 0, "new_extension": 0}
    if before is None or now is None:
        return None, stats
    known = known_extensions if known_extensions is not None else {_ext(p) for p in before}
    new_ext: dict[str, int] = {}
    for path, sig in now.items():
        old = before.get(path)
        if old is None:
            stats["created"] += 1
            if _ext(path) and _ext(path) not in known:
                new_ext[_ext(path)] = new_ext.get(_ext(path), 0) + 1
        elif old != sig:
            stats["modified"] += 1
            if _ext(path) and _ext(path) not in known:
                new_ext[_ext(path)] = new_ext.get(_ext(path), 0) + 1
    stats["deleted"] = sum(1 for p in before if p not in now)
    stats["new_extension"] = max(new_ext.values(), default=0)
    big = stats["modified"] >= BURST_MODIFIED or stats["deleted"] >= BURST_DELETED or stats["new_extension"] >= BURST_NEW_EXTENSION
    if not big:
        return None, stats
    high = stats["modified"] >= HIGH_MODIFIED or stats["new_extension"] >= BURST_NEW_EXTENSION * 2 or (stats["new_extension"] >= BURST_NEW_EXTENSION
                                                                                                          and stats["modified"] >= BURST_MODIFIED)
    evidence = [f"{stats['modified']} file(s) modified, {stats['created']} created, {stats['deleted']} deleted since the last look"]
    if stats["new_extension"]:
        ext, n = max(new_ext.items(), key=lambda kv: kv[1])
        evidence.append(f"{n} file(s) now end in '{ext}', an extension that was not in these folders before")
    incident = Incident(
        "", "", "file_burst", HIGH if high else MEDIUM, "medium" if high else "low",
        "Many of your files changed at once" if high else "A burst of file changes in your folders", evidence,
        ["Check what you were doing: " + _BULK_NOTE + ".",
         "If you do not recognise it: disconnect from the network (say 'disable network actions'), then close the program named under suspects, and "
         "do NOT open the renamed files. I can show your latest verified backup."],
        ["which program changed the files (Windows does not tell without special auditing; the suspects below are a guess from disk activity)",
         "file contents (the guardian never reads them)"], key="file_burst")
    return incident, stats


def suspects(before_writes: dict[int, tuple[str, int]], now_writes: dict[int, tuple[str, int]], top: int = 3) -> list[str]:
    """Programs that wrote the most bytes between two readings (pid -> (name, total bytes written)). A correlation, labelled as such."""
    deltas = []
    for pid, (name, total) in now_writes.items():
        old = before_writes.get(pid)
        if old and old[0] == name and total > old[1]:
            deltas.append((total - old[1], name))
    deltas.sort(reverse=True)
    return [f"{name} (wrote {delta // 1048576} MB since the last look)" for delta, name in deltas[:top] if delta >= 1048576]


StartupEntry = tuple[str, str, str]                # (where, name, command)


def startup_changes(baseline: Iterable[StartupEntry] | None, now: Iterable[StartupEntry]) -> list[Incident]:
    """Entries added or changed since the baseline you accepted (None = first look, no alarm)."""
    if baseline is None:
        return []
    old = {(w, n): c for w, n, c in baseline}
    out = []
    for where, name, command in now:
        before = old.get((where, name))
        if before == command:
            continue
        unusual = bool(_UNUSUAL_PATH.search(command))
        what = "added itself to startup" if before is None else "changed its startup command"
        out.append(Incident("", "", "startup_change", MEDIUM if unusual else LOW, "medium" if unusual else "low",
                            f"'{name}' {what}", [f"where: {where}", f"command: {command[:160]}"] + (["it starts from an unusual place (temp / downloads / encoded command)"] if unusual else []),
                            ["If you just installed or updated that program, accept it (guardian baseline). Otherwise remove it from Task Manager > Startup apps."],
                            ["who made the change and when (Windows does not record it here)", "whether the file is signed"], key=f"startup:{where}:{name}"))
    return out


Drive = tuple[str, str]                            # (identity, label) of a removable drive


def usb_changes(baseline: Iterable[Drive] | None, now: Iterable[Drive]) -> tuple[list[Incident], list[Drive]]:
    """New removable drives since the baseline (None = first look, no alarm). Returns (incidents, the drives to remember).

    The identity is the drive letter + file system + size (+ volume serial where Windows gives it), so a different stick on the same letter and size can
    look the same - said in the incident. Plugging in your own stick is normal; the incident only makes it visible and suggests a scan.
    """
    now = list(now)
    if baseline is None:
        return [], now
    known = {ident for ident, _ in baseline}
    out = [Incident("", "", "usb_device", LOW, "medium", f"A removable drive was connected: {label}",
                    [f"identity: {ident}", "removable drives can carry malware or copy files; this one was not here at the last look"],
                    ["If it is yours, accept it (guardian baseline). If not, remove it and scan it with Windows Security before opening anything."],
                    ["the vendor / serial of the device (not exposed here)", "whether it holds an autorun file (not checked: contents are never read)"], key=f"usb:{ident}")
           for ident, label in now if ident not in known]
    return out, sorted({*map(tuple, baseline), *now})


Integrity = dict[str, tuple[str, int, float]]      # path -> (sha256, size, mtime)


def integrity_changes(baseline: dict[str, Any] | None, now: Integrity, missing: Iterable[str] = ()) -> tuple[list[Incident], dict[str, Any]]:
    """Watched files whose content hash changed (or that vanished). Only hashes, sizes and times are kept - never the content. None = first look."""
    new_base = {p: [h, s, m] for p, (h, s, m) in now.items()}
    if baseline is None:
        return [], new_base
    out = []
    for path, (digest, size, _mtime) in now.items():
        old = baseline.get(path)
        if old is None or old[0] == digest:
            continue
        out.append(Incident("", "", "file_changed", MEDIUM, "high", f"A watched file changed: {os.path.basename(path)}",
                            [f"path: {path}", f"size {old[1]} -> {size} bytes", "the content hash differs from the baseline (the check is exact)"],
                            ["If you or an installer changed it, accept the new state (guardian baseline). If not, restore it from a backup and find out what wrote it."],
                            ["who changed it and when (Windows does not record that here)", "what exactly changed (contents are not stored)"], key=f"integrity:{path}"))
    for path in missing:
        if path in baseline:
            out.append(Incident("", "", "file_changed", MEDIUM, "high", f"A watched file is gone: {os.path.basename(path)}", [f"path: {path}"],
                                ["Restore it from a backup if you did not remove it."], ["who removed it"], key=f"integrity-gone:{path}"))
            new_base[path] = baseline[path]
    return out, new_base


Connection = tuple[str, str, int, str, int, str]   # (program, local ip, local port, remote ip, remote port, status)


def _public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_unspecified)


def network_changes(baseline: dict[str, Any] | None, rows: Iterable[Connection]) -> tuple[list[Incident], dict[str, Any]]:
    """(incidents, new baseline). Baseline = {"listen": ["prog:port"], "dest": ["prog|ip"]}. None = first look, no alarm."""
    listen, dest = set(), set()
    for prog, lip, lport, rip, _rport, status in rows:
        if status == "LISTEN" and lip not in ("127.0.0.1", "::1"):
            listen.add(f"{prog}:{lport}")
        elif status == "ESTABLISHED" and _public(rip):
            dest.add(f"{prog}|{rip}")
    new_base = {"listen": sorted(listen), "dest": sorted(dest)}
    if baseline is None:
        return [], new_base
    out = []
    for item in sorted(listen - set(baseline.get("listen", []))):
        prog, port = item.rsplit(":", 1)
        out.append(Incident("", "", "new_listener", LOW, "low", f"'{prog}' started accepting connections from other devices on port {port}",
                            [f"{prog} listens on all network interfaces, port {port}"],
                            ["Fine for servers you started (a dev server, a game, file sharing). If you did not expect it, close the program or block the port in Windows Firewall."],
                            ["what the program does with the connection"], key=f"listen:{item}"))
    per_prog: dict[str, int] = {}
    for item in dest - set(baseline.get("dest", [])):
        per_prog[item.split("|")[0]] = per_prog.get(item.split("|")[0], 0) + 1
    for prog, n in sorted(per_prog.items()):
        if n >= NEW_DESTINATIONS:
            out.append(Incident("", "", "many_destinations", MEDIUM, "low", f"'{prog}' connected to {n} new places on the internet at once",
                                [f"{n} new remote addresses in one look"], ["Browsers and sync tools do this normally. For anything else, check what the program is."],
                                ["what data was sent (the guardian sees addresses, not contents)", "the domain names"], key=f"dest:{prog}"))
    return out, new_base


def backup_check(now: datetime, last_backup: datetime | None, restore_ok: bool | None) -> Incident | None:
    if last_backup is None:
        return Incident("", "", "backup", MEDIUM, "high", "There is no backup of your Jarvis data", ["no backup file was found"],
                        ["Turn on the automatic backup (AUTO_BACKUP) or say 'back up now'."], key="backup:none")
    if restore_ok is False:
        return Incident("", "", "backup", HIGH, "high", "Your latest backup failed its restore test", ["the restore test of the newest backup did not pass"],
                        ["Make a new backup now and keep the old one until it passes."], key="backup:bad")
    age = now - last_backup
    if age > timedelta(days=BACKUP_STALE_DAYS):
        return Incident("", "", "backup", LOW, "high", f"The newest backup is {age.days} days old", [f"newest backup: {last_backup:%Y-%m-%d %H:%M}"],
                        ["Say 'back up now'."], key="backup:stale")
    return None


# ------------------------------------------------------------------ incident store

INCIDENTS_KEY, STARTUP_KEY, NET_KEY = "guardian_incidents", "guardian_startup", "guardian_network"
USB_KEY, INTEGRITY_KEY = "guardian_usb", "guardian_integrity"


class Store:
    """Incidents and baselines in the existing settings store (no new table)."""

    def __init__(self, settings_store: Any, audit: Callable[..., None] | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self.kv, self.audit = settings_store, audit
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def incidents(self) -> list[Incident]:
        return [Incident(**d) for d in (self.kv.get(INCIDENTS_KEY, []) or [])]

    def _save(self, items: list[Incident]) -> None:
        self.kv.set(INCIDENTS_KEY, [i.__dict__ for i in items[-MAX_INCIDENTS:]])

    def add(self, incident: Incident, *, quiet_hours: float = 6.0) -> Incident | None:
        """Record it unless the same thing was reported (open or recently dismissed) in the last ``quiet_hours`` - no nagging."""
        items = self.incidents()
        now = self.clock()
        for old in items:
            if old.key == incident.key and old.status in ("open", "acknowledged", "dismissed", "contained"):
                try:
                    if old.status in ("open", "acknowledged") or now - datetime.fromisoformat(old.ts) < timedelta(hours=quiet_hours):
                        return None
                except ValueError:
                    return None
        incident.ts = now.isoformat(timespec="seconds")
        incident.id = f"G{len(items) + 1:04d}-{int(time.time()) % 100000}"
        items.append(incident)
        self._save(items)
        if self.audit is not None:
            self.audit("guardian_incident", details={"id": incident.id, "kind": incident.kind, "severity": incident.severity, "confidence": incident.confidence,
                                                    "title": incident.title})
        return incident

    def set_status(self, incident_id: str, status: str) -> Incident | None:
        items = self.incidents()
        for i in items:
            if i.id == incident_id or i.id.startswith(incident_id):
                i.status = status
                self._save(items)
                if self.audit is not None:
                    self.audit("guardian_status", details={"id": i.id, "status": status})
                return i
        return None

    def baseline(self, key: str):
        return self.kv.get(key, None)

    def set_baseline(self, key: str, value: Any) -> None:
        self.kv.set(key, value)


# ------------------------------------------------------------------ the scan

@dataclass
class Readers:
    """Where the facts come from. Defaults read this PC; tests pass fakes."""
    files: Callable[[], FileSnapshot | None]
    startup: Callable[[], list[StartupEntry]]
    connections: Callable[[], list[Connection]]
    writes: Callable[[], dict[int, tuple[str, int]]]
    last_backup: Callable[[], datetime | None]
    restore_ok: Callable[[], bool | None]
    holders: Callable[[], dict[str, list[str]]] = lambda: {}      # program -> files it has open in the watched folders right now
    drives: Callable[[], list[Drive]] | None = None                # removable drives now (None = not read)
    integrity: Callable[[], tuple[Integrity, list[str]]] | None = None   # (hashes of watched files that exist, watched paths that are missing)


class Guardian:
    def __init__(self, store: Store, readers: Readers, clock: Callable[[], datetime] | None = None) -> None:
        self.store, self.r = store, readers
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._files: FileSnapshot | None = None
        self._writes: dict[int, tuple[str, int]] = {}
        self._known_ext: set[str] = set()
        self.last_stats: dict[str, int] = {}
        self.last_scan: str = ""

    def scan(self) -> list[Incident]:
        """One pass over everything. Returns the NEW incidents (already recorded). Each reader failing is skipped, never fatal."""
        new: list[Incident] = []

        def keep(i: Incident | None) -> None:
            if i is not None:
                added = self.store.add(i)
                if added is not None:
                    new.append(added)

        try:
            snap = self.r.files()
            writes = self.r.writes()
            incident, self.last_stats = file_burst(self._files, snap, self._known_ext or None)
            if incident is not None:
                found = suspects(self._writes, writes)
                incident.evidence.append("suspects (from disk activity, a guess): " + ("; ".join(found) if found else "none stood out"))
                try:
                    open_now = describe_holders(self.r.holders())
                except Exception:
                    open_now = ""
                incident.evidence.append("open in your folders right now (direct, but only what is open at this moment): " + (open_now or "none seen"))
                keep(incident)
            if snap is not None:
                if self._files is None:
                    self._known_ext = {_ext(p) for p in snap}
                self._files, self._writes = snap, writes
        except Exception:
            pass
        try:
            entries = self.r.startup()
            base = self.store.baseline(STARTUP_KEY)
            for i in startup_changes([tuple(x) for x in base] if base is not None else None, entries):
                keep(i)
            if base is None:
                self.store.set_baseline(STARTUP_KEY, [list(e) for e in entries])
        except Exception:
            pass
        try:
            found, new_base = network_changes(self.store.baseline(NET_KEY), self.r.connections())
            for i in found:
                keep(i)
            old = self.store.baseline(NET_KEY)
            merged = new_base if old is None else {"listen": sorted(set(old.get("listen", [])) | set(new_base["listen"])),
                                                   "dest": sorted(set(old.get("dest", [])) | set(new_base["dest"]))[-2000:]}
            self.store.set_baseline(NET_KEY, merged)         # what was reported becomes normal: it is not reported again
        except Exception:
            pass
        if self.r.drives is not None:
            try:
                found, remember = usb_changes(self._drives_base(), self.r.drives())
                for i in found:
                    keep(i)
                self.store.set_baseline(USB_KEY, [list(d) for d in remember])
            except Exception:
                pass
        if self.r.integrity is not None:
            try:
                present, gone = self.r.integrity()
                found, base = integrity_changes(self.store.baseline(INTEGRITY_KEY), present, gone)
                for i in found:
                    keep(i)
                if self.store.baseline(INTEGRITY_KEY) is None or not found:
                    self.store.set_baseline(INTEGRITY_KEY, base)         # a reported change stays "changed" until you accept it
            except Exception:
                pass
        try:
            keep(backup_check(self.clock(), self.r.last_backup(), self.r.restore_ok()))
        except Exception:
            pass
        self.last_scan = self.clock().isoformat(timespec="seconds")
        return new

    def _drives_base(self):
        base = self.store.baseline(USB_KEY)
        return None if base is None else [tuple(d) for d in base]

    def accept_current(self) -> str:
        """Make what is running now the baseline (after you reviewed the incidents)."""
        self.store.set_baseline(STARTUP_KEY, [list(e) for e in self.r.startup()])
        _, base = network_changes(None, self.r.connections())
        self.store.set_baseline(NET_KEY, base)
        if self.r.drives is not None:
            self.store.set_baseline(USB_KEY, [list(d) for d in self.r.drives()])
        if self.r.integrity is not None:
            present, _ = self.r.integrity()
            self.store.set_baseline(INTEGRITY_KEY, {p: [h, s, m] for p, (h, s, m) in present.items()})
        return "The current startup entries, network listeners, removable drives and watched-file contents are now the baseline; only new changes are reported."

    def summary(self) -> str:
        items = self.store.incidents()
        open_items = [i for i in items if i.status == "open"]
        if not self.last_scan:
            head = "The guardian has not scanned yet in this run."
        else:
            head = f"Last scan {self.last_scan[11:16]} UTC."
        if not open_items:
            return head + " No open incidents. (This is change monitoring, not an antivirus.)"
        order = {HIGH: 0, MEDIUM: 1, LOW: 2}
        return head + f" {len(open_items)} open incident(s):\n" + "\n".join(i.line() + f" - {i.title}" for i in sorted(open_items, key=lambda x: order[x.severity])[:6])


# ------------------------------------------------------------------ real readers (Windows; HARDWARE-UNVERIFIED)


def watched_folders(setting: str = "") -> list[str]:
    if setting.strip():
        return [os.path.expandvars(os.path.expanduser(p.strip())) for p in setting.split(";") if p.strip()]
    home = os.path.expanduser("~")
    return [os.path.join(home, d) for d in ("Documents", "Desktop", "Pictures")]


def read_files(folders: list[str], limit: int = MAX_FILES) -> FileSnapshot | None:
    snap: FileSnapshot = {}
    seen_any = False
    for root in folders:
        if not os.path.isdir(root):
            continue
        seen_any = True
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in ("node_modules", "__pycache__", ".git")]
            for name in filenames:
                try:
                    st = os.stat(os.path.join(dirpath, name))
                except OSError:
                    continue
                snap[os.path.join(dirpath, name)] = (st.st_size, st.st_mtime_ns)
                if len(snap) >= limit:
                    return snap
    return snap if seen_any else None          # an unreadable / missing drive is not "everything was deleted"


def read_startup() -> list[StartupEntry]:
    entries: list[StartupEntry] = []
    try:
        import winreg

        for hive, hname in ((winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
            for sub in (r"Software\Microsoft\Windows\CurrentVersion\Run", r"Software\Microsoft\Windows\CurrentVersion\RunOnce"):
                try:
                    with winreg.OpenKey(hive, sub) as key:
                        i = 0
                        while True:
                            try:
                                name, value, _ = winreg.EnumValue(key, i)
                            except OSError:
                                break
                            entries.append((f"{hname}\\{sub.rsplit(chr(92), 1)[-1]}", str(name), str(value)))
                            i += 1
                except OSError:
                    continue
    except ImportError:
        pass
    for folder in (os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"), os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs\Startup")):
        try:
            for name in os.listdir(folder):
                if name.lower() != "desktop.ini":
                    entries.append(("Startup folder", name, os.path.join(folder, name)))
        except OSError:
            continue
    return entries


def read_connections() -> list[Connection]:
    import psutil

    rows: list[Connection] = []
    names: dict[int, str] = {}
    for c in psutil.net_connections(kind="inet"):
        if not c.laddr:
            continue
        pid = c.pid or 0
        if pid not in names:
            try:
                names[pid] = psutil.Process(pid).name() if pid else "?"
            except Exception:
                names[pid] = "?"
        rip, rport = (c.raddr.ip, c.raddr.port) if c.raddr else ("", 0)
        rows.append((names[pid][:60], c.laddr.ip, c.laddr.port, rip, rport, c.status.replace("LISTENING", "LISTEN")))
    return rows


def read_writes() -> dict[int, tuple[str, int]]:
    import psutil

    out: dict[int, tuple[str, int]] = {}
    for p in psutil.process_iter(["name", "io_counters"]):
        try:
            io = p.info.get("io_counters")
            if io is not None and p.info.get("name"):
                out[p.pid] = (str(p.info["name"])[:60], int(io.write_bytes))
        except Exception:
            continue
    return out


def describe_holders(holders: dict[str, list[str]], top: int = 3) -> str:
    """"name (3 files, e.g. a.docx)" for the programs holding the most files open. Names only."""
    ranked = sorted(holders.items(), key=lambda kv: len(kv[1]), reverse=True)[:top]
    return "; ".join(f"{name} ({len(files)} file{'s' if len(files) != 1 else ''}, e.g. {os.path.basename(files[0])})" for name, files in ranked if files)


def read_holders(folders: list[str]) -> dict[str, list[str]]:
    """Programs with files open inside the watched folders (psutil open_files). Windows may hide other users' / protected processes: those are skipped."""
    import psutil

    roots = [os.path.normcase(os.path.abspath(f)) for f in folders]
    out: dict[str, list[str]] = {}
    for p in psutil.process_iter(["name"]):
        try:
            mine = [f.path for f in p.open_files() if any(os.path.normcase(f.path).startswith(r) for r in roots)]
        except Exception:
            continue
        if mine:
            out.setdefault(str(p.info.get("name") or "?")[:60], []).extend(mine[:20])
    return out


def read_drives() -> list[Drive]:
    """Removable drives (psutil: 'removable' option on Windows). Identity includes the volume serial when Windows gives it."""
    import psutil

    out: list[Drive] = []
    for p in psutil.disk_partitions(all=False):
        if "removable" not in (p.opts or "").lower() and "cdrom" not in (p.opts or "").lower():
            continue
        serial = ""
        try:
            import ctypes

            number = ctypes.c_ulong(0)
            ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(p.mountpoint), None, 0, ctypes.byref(number), None, None, None, 0)
            serial = f"{number.value:08X}" if number.value else ""
        except Exception:
            pass
        try:
            size = psutil.disk_usage(p.mountpoint).total // 1_000_000_000
        except Exception:
            size = 0
        out.append((f"{p.mountpoint}|{p.fstype}|{size}GB" + (f"|{serial}" if serial else ""), f"{p.mountpoint} ({p.fstype}, {size} GB)"))
    return out


MAX_WATCHED, MAX_HASH_BYTES = 50, 50 * 1024 * 1024


def read_integrity(paths: list[str]) -> tuple[Integrity, list[str]]:
    import hashlib

    present: Integrity = {}
    gone: list[str] = []
    for path in paths[:MAX_WATCHED]:
        try:
            st = os.stat(path)
            if st.st_size > MAX_HASH_BYTES:
                continue
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            present[path] = (h.hexdigest(), st.st_size, st.st_mtime)
        except FileNotFoundError:
            gone.append(path)
        except OSError:
            continue
    return present, gone


# ------------------------------------------------------------------ the running instance

_INSTANCE: Guardian | None = None


def real_readers(settings: Any, database: Any, backup_folder: Any = None) -> Readers:
    from app.memory import backup
    from app.memory.settings_store import SettingsStore

    folders = watched_folders(getattr(settings, "guardian_folders", ""))
    watched = [p.strip() for p in str(getattr(settings, "guardian_integrity_files", "") or "").split(";") if p.strip()]

    def last_backup() -> datetime | None:
        if backup_folder is None:
            return None
        found = backup.list_backups(backup_folder)
        if not found:
            return None
        try:
            return datetime.strptime(found[0].stem.removeprefix(backup.PREFIX)[:15], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc).astimezone()
        except ValueError:
            return None

    def restore_ok() -> bool | None:
        saved = SettingsStore(database).get("backup_restore_test")
        return None if not isinstance(saved, dict) else bool(saved.get("ok"))

    return Readers(lambda: read_files(folders), read_startup, read_connections, read_writes, last_backup, restore_ok, lambda: read_holders(folders),
                   read_drives, (lambda: read_integrity(watched)) if watched else None)


def instance(settings: Any, database: Any, backup_folder: Any = None, *, audit: Callable[..., None] | None = None) -> Guardian:
    """The one guardian of this run (it must remember the previous file listing between scans)."""
    global _INSTANCE
    if _INSTANCE is None:
        from app.memory.settings_store import SettingsStore

        _INSTANCE = Guardian(Store(SettingsStore(database), audit), real_readers(settings, database, backup_folder))
    return _INSTANCE


def notify(database: Any, incidents: list[Incident]) -> None:
    """Tell the notification centre about NEW incidents (security category; the priority follows the severity)."""
    from app.core.notifications import NotificationCenter

    center = NotificationCenter(database)
    for i in incidents:
        center.add(f"Guardian: {i.title}", "; ".join(i.evidence[:2]) + (f" - {i.recommended[0]}" if i.recommended else ""), source="guardian",
                   category="security", priority=SEVERITY_PRIORITY[i.severity], action="show guardian incidents")
