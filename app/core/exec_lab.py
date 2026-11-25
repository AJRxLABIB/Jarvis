"""Execution Laboratory: experiments on COPIES, in a workspace of their own, with an isolation level that is stated honestly and never silently lowered.

What this module is:
* ``detect_backends`` - which isolation this PC really offers. Windows Sandbox and Hyper-V need Windows Pro / Enterprise / Education; on Home editions they
  are reported as unavailable (with the reason), not faked. WSL2 is listed as a development convenience, NOT a security boundary.
* ``classify`` / ``decide`` - LOW (static inspection of approved copies) needs no isolation; MEDIUM (tests, builds of your own project) may use the
  restricted workspace WITH approval; HIGH (unknown executables, installers, untrusted plugins, anything privileged) requires STRONG isolation. When strong
  isolation is not available the answer is "not possible here" plus a safe alternative (static analysis) - never a quiet fallback to a weaker level.
* ``Workspace`` - a per-experiment folder (input / output / manifest). Inputs are COPIED (size-capped, links refused) and secrets are left out
  (.env, keys, tokens, browser profiles, .git internals); the real project is never the working directory.
* ``analyze`` - LOW-risk static analysis that runs nothing: hashes, file kinds, Python syntax, calls that deserve attention (process start, eval/exec, network,
  native code), declared dependencies. For an executable it reports hash, size and whether it is a PE file; it does NOT check signatures or behaviour.
* ``wsb_config`` - a Windows Sandbox configuration for one experiment: networking off, input mapped read-only, output mapped writable, no clipboard / printer
  / audio / video / GPU sharing, protected client on. Jarvis writes the file; starting it is your action.
* ``Registry`` - experiment records with a checked lifecycle; ``limits_exceeded`` - resource-limit decisions; ``Artifacts`` - list and export ONLY approved
  files from the output folder.

Honest status: pure logic + file handling, tested on temp folders. Nothing here executes experiment code. Running tests inside the restricted workspace goes
through the existing reviewed command executor (developer tools); running inside Windows Sandbox is UNVERIFIED (no such PC was available) and one clean run
never proves software is safe.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape

STRONG_EDITIONS = ("professional", "enterprise", "education", "proworkstation", "server", "iotenterprise")
STATES = ("preparing", "isolated", "running", "testing", "verifying", "awaiting_approval", "completed", "failed", "terminated")
FLOW = {"preparing": ("isolated", "failed", "terminated"), "isolated": ("running", "failed", "terminated"), "running": ("testing", "verifying", "failed", "terminated"),
        "testing": ("verifying", "failed", "terminated"), "verifying": ("awaiting_approval", "completed", "failed", "terminated"),
        "awaiting_approval": ("completed", "failed", "terminated"), "completed": (), "failed": (), "terminated": ()}
SECRET_NAMES = re.compile(r"(^\.env|\.pem$|\.key$|\.pfx$|\.p12$|\.jks$|\.keystore$|^id_(rsa|ed25519|ecdsa)|credentials|secrets?\.(json|ya?ml|toml)$|token|cookies|login data|\.kdbx$)", re.I)
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ssh", ".aws", ".gnupg", "dist", "build"}
MAX_COPY_BYTES = 200 * 1024 * 1024
MAX_FILES = 5000
KEY = "exec_lab_experiments"
KEEP = 50
HIGH = re.compile(r"\b(unknown|untrusted|installer|install script|setup\.exe|\.msi|\.exe|plugin|privileged|admin|driver|malware|suspicious|safe to run|is this (app|program) safe)\b", re.I)
MEDIUM = re.compile(r"\b(run|execute|test|pytest|build|compile|npm|pip install|start|launch|debug|reproduce)\b", re.I)
RISKY_CALLS = {"os.system": "starts a process", "subprocess": "starts a process", "eval": "runs text as code", "exec": "runs text as code", "socket": "network access",
               "requests": "network access", "urllib": "network access", "httpx": "network access", "ctypes": "native code", "winreg": "registry access",
               "shutil.rmtree": "deletes folders", "os.remove": "deletes files", "pickle": "can run code when loading"}


@dataclass
class Backend:
    name: str
    isolation: str          # none | restricted | strong
    available: bool
    reason: str


@dataclass
class Decision:
    allowed: bool
    risk: str
    backend: str
    needs_approval: bool
    reason: str
    alternative: str = ""

    def text(self) -> str:
        head = f"{self.risk.upper()} risk: " + (f"can run with '{self.backend}'" + (" after your approval" if self.needs_approval else "") if self.allowed else "NOT possible on this PC")
        return head + f". {self.reason}" + (f" Alternative: {self.alternative}" if self.alternative else "")


def detect_backends(edition: str, sandbox_installed: bool, hyperv: bool = False, wsl: bool = False) -> list[Backend]:
    """What isolation exists, from facts read on the machine (see ``read_host``). Pure."""
    ed = (edition or "").lower()
    strong_ok = any(e in ed for e in STRONG_EDITIONS)
    why_not = f"needs Windows Pro, Enterprise or Education; this PC has '{edition or 'an unknown edition'}'"
    return [
        Backend("static", "none", True, "inspection only: nothing is executed"),
        Backend("restricted_workspace", "restricted", True, "a copy in its own folder + the reviewed command runner; NOT a security boundary"),
        Backend("windows_sandbox", "strong", strong_ok and sandbox_installed,
                "ready" if strong_ok and sandbox_installed else (why_not if not strong_ok else "supported by this edition but the 'Windows Sandbox' feature is not turned on")),
        Backend("hyper_v", "strong", strong_ok and hyperv, "ready (a VM must be prepared by you)" if strong_ok and hyperv else (why_not if not strong_ok else "Hyper-V is not enabled")),
        Backend("wsl2", "restricted", wsl, "development convenience only; not an isolation boundary" if wsl else "WSL is not installed"),
    ]


def read_host() -> tuple[str, bool, bool, bool]:
    """(edition, sandbox installed, hyper-v present, wsl present) without starting any process. Non-Windows: ('not Windows', False, False, False)."""
    if os.name != "nt":
        return "not Windows", False, False, False
    edition = ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            edition = str(winreg.QueryValueEx(k, "EditionID")[0])
    except OSError:
        pass
    sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    return edition, (sys32 / "WindowsSandbox.exe").is_file(), (sys32 / "vmms.exe").is_file(), (sys32 / "wsl.exe").is_file()


def classify(operation: str) -> str:
    t = operation or ""
    if HIGH.search(t):
        return "high"
    return "medium" if MEDIUM.search(t) else "low"


def decide(risk: str, backends: list[Backend], own_project: bool = True) -> Decision:
    """Pick a backend for a risk level. HIGH never falls back below strong isolation; MEDIUM on someone else's code is treated as HIGH."""
    if risk == "medium" and not own_project:
        risk = "high"
    strong = [b for b in backends if b.isolation == "strong" and b.available]
    if risk == "low":
        return Decision(True, "low", "static", False, "Only inspects approved copies; nothing runs.")
    if risk == "medium":
        if strong:
            return Decision(True, "medium", strong[0].name, True, "Strong isolation is available, so it is used even for your own project.")
        return Decision(True, "medium", "restricted_workspace", True, "Runs on a COPY of your own project with the reviewed command runner. This limits accidents, it does not contain hostile code.")
    if strong:
        return Decision(True, "high", strong[0].name, True, "Untrusted code only runs under strong isolation, with networking off unless you allow it.")
    missing = next((b.reason for b in backends if b.name == "windows_sandbox"), "no strong isolation backend")
    return Decision(False, "high", "", True, f"Untrusted code needs strong isolation and none is available ({missing}). It will not be run in a weaker environment.",
                    "static analysis of a copy (hashes, file kinds, risky calls), or run it yourself in a virtual machine.")


@dataclass
class Manifest:
    id: str
    purpose: str
    risk: str
    backend: str
    network: bool = False
    seconds: int = 600
    memory_mb: int = 2048
    cleanup: str = "delete workspace when closed"
    created: float = 0.0
    inputs: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class Workspace:
    """``<root>/<id>/{input,output}`` + manifest.json. Inputs are copies; the original is never the working folder."""

    def __init__(self, root: Path, manifest: Manifest) -> None:
        self.manifest = manifest
        self.path = Path(root) / manifest.id
        self.input, self.output = self.path / "input", self.path / "output"

    @classmethod
    def create(cls, root: Path, purpose: str, risk: str, backend: str, **limits: Any) -> "Workspace":
        m = Manifest(uuid.uuid4().hex[:12], purpose[:200], risk, backend, created=time.time(), **limits)
        ws = cls(root, m)
        ws.input.mkdir(parents=True)
        ws.output.mkdir()
        ws.save()
        return ws

    def save(self) -> None:
        (self.path / "manifest.json").write_text(json.dumps(asdict(self.manifest), indent=1), encoding="utf-8")

    def add_input(self, source: Path) -> int:
        """Copy a file or folder in. Secrets, links, virtual environments and VCS internals are left out and listed in ``manifest.skipped``. Returns files copied."""
        source = Path(source)
        if source.is_symlink() or not source.exists():
            raise ValueError("the source does not exist or is a link")
        copied, total = 0, 0
        items = [(source, Path(source.name))] if source.is_file() else []
        if source.is_dir():
            for base, dirs, names in os.walk(source, followlinks=False):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (Path(base) / d).is_symlink()]
                for n in names:
                    p = Path(base) / n
                    items.append((p, Path(source.name) / p.relative_to(source)))
        for src, rel in items:
            if src.is_symlink() or SECRET_NAMES.search(src.name):
                self.manifest.skipped.append(rel.as_posix())
                continue
            size = src.stat().st_size
            if copied >= MAX_FILES or total + size > MAX_COPY_BYTES:
                self.manifest.skipped.append(f"{rel.as_posix()} (size / count limit)")
                continue
            dest = self.input / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            try:
                dest.chmod(stat.S_IREAD)
            except OSError:
                pass
            copied, total = copied + 1, total + size
        self.manifest.inputs.append(source.name)
        self.manifest.skipped = self.manifest.skipped[:200]
        self.save()
        return copied

    def destroy(self) -> None:
        def unlock(func: Callable, path: str, _exc: Any) -> None:
            os.chmod(path, stat.S_IWRITE)
            func(path)

        if self.path.is_dir():
            shutil.rmtree(self.path, onerror=unlock)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def analyze(folder: Path, limit: int = 400) -> dict[str, Any]:
    """Static look at a workspace input folder. Executes nothing; a finding is a pointer for a person, not a verdict."""
    files = [p for p in sorted(Path(folder).rglob("*")) if p.is_file()][:limit]
    out: dict[str, Any] = {"files": len(files), "syntax_errors": [], "attention": [], "dependencies": [], "executables": [], "hashes": {}}
    for p in files:
        rel = p.relative_to(folder).as_posix()
        suffix = p.suffix.lower()
        if suffix == ".py":
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as exc:
                out["syntax_errors"].append(f"{rel}: line {exc.lineno}: {exc.msg}")
                continue
            seen = set()
            for node in ast.walk(tree):
                name = ""
                if isinstance(node, ast.Import):
                    name = node.names[0].name
                elif isinstance(node, ast.ImportFrom):
                    name = node.module or ""
                elif isinstance(node, ast.Call):
                    f = node.func
                    name = f"{getattr(f.value, 'id', '')}.{f.attr}" if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                for key, why in RISKY_CALLS.items():
                    if name and (name == key or name.split(".")[0] == key) and (rel, key) not in seen:
                        seen.add((rel, key))
                        out["attention"].append(f"{rel}: {key} ({why})")
        elif suffix in (".exe", ".dll", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".scr"):
            with p.open("rb") as f:
                pe = f.read(2) == b"MZ"
            out["executables"].append({"file": rel, "bytes": p.stat().st_size, "sha256": _sha(p), "pe_file": pe, "signature": "not checked", "behaviour": "not observed"})
        if p.name in ("requirements.txt", "pyproject.toml", "package.json"):
            text = p.read_text(encoding="utf-8", errors="replace")[:20000]
            names = re.findall(r'^\s*([A-Za-z0-9_.\-]+)\s*(?:[=<>~!\[]|$)', text, re.M) if p.name == "requirements.txt" else re.findall(r'"([@A-Za-z0-9_./\-]+)"\s*:\s*"[\^~\d<>=*]', text)
            out["dependencies"] += [f"{rel}: {n}" for n in names[:60]]
        if len(out["hashes"]) < 50:
            out["hashes"][rel] = _sha(p)[:16]
    out["attention"] = out["attention"][:80]
    return out


def describe_analysis(a: dict[str, Any]) -> str:
    lines = [f"Static analysis of {a['files']} file(s) - nothing was executed."]
    lines += [f"  syntax error - {e}" for e in a["syntax_errors"][:8]]
    lines += [f"  look at - {e}" for e in a["attention"][:12]]
    lines += [f"  executable - {e['file']} ({e['bytes']} bytes, sha256 {e['sha256'][:16]}..., {'PE file' if e['pe_file'] else 'script / not PE'}; signature and behaviour NOT checked)" for e in a["executables"][:6]]
    if a["dependencies"]:
        lines.append(f"  declares {len(a['dependencies'])} dependencies (not installed, not audited here)")
    if not (a["syntax_errors"] or a["attention"] or a["executables"]):
        lines.append("  nothing stood out. That is not proof of safety: static inspection misses a lot.")
    return "\n".join(lines)


def wsb_config(ws: Workspace, command: str = "") -> str:
    """A Windows Sandbox (.wsb) file for this experiment: input read-only, output writable, no networking unless the manifest allows it, nothing else shared."""
    m = ws.manifest
    logon = f"<LogonCommand><Command>{escape(command)}</Command></LogonCommand>" if command else ""
    return (
        "<Configuration>"
        f"<Networking>{'Default' if m.network else 'Disable'}</Networking>"
        "<VGpu>Disable</VGpu><AudioInput>Disable</AudioInput><VideoInput>Disable</VideoInput>"
        "<ClipboardRedirection>Disable</ClipboardRedirection><PrinterRedirection>Disable</PrinterRedirection><ProtectedClient>Enable</ProtectedClient>"
        f"<MemoryInMB>{int(m.memory_mb)}</MemoryInMB>"
        "<MappedFolders>"
        f"<MappedFolder><HostFolder>{escape(str(ws.input))}</HostFolder><SandboxFolder>C:\\lab\\input</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>"
        f"<MappedFolder><HostFolder>{escape(str(ws.output))}</HostFolder><SandboxFolder>C:\\lab\\output</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>"
        "</MappedFolders>"
        f"{logon}</Configuration>")


def limits_exceeded(m: Manifest, *, elapsed: float = 0, memory_mb: float = 0, output_bytes: int = 0, children: int = 0, max_output_bytes: int = 500 * 1024 * 1024,
                    max_children: int = 50) -> list[str]:
    """Reasons to terminate an experiment (empty = within limits)."""
    out = []
    if elapsed > m.seconds:
        out.append(f"ran {elapsed:.0f} s, limit {m.seconds} s")
    if memory_mb > m.memory_mb:
        out.append(f"used {memory_mb:.0f} MB, limit {m.memory_mb} MB")
    if output_bytes > max_output_bytes:
        out.append("wrote more output than allowed")
    if children > max_children:
        out.append(f"started {children} processes, limit {max_children}")
    return out


class Artifacts:
    """What an experiment left in its output folder; only files you name are copied out."""

    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    def listing(self) -> list[dict[str, Any]]:
        return [{"name": p.relative_to(self.ws.output).as_posix(), "bytes": p.stat().st_size, "sha256": _sha(p)[:16]}
                for p in sorted(self.ws.output.rglob("*")) if p.is_file() and not p.is_symlink()][:500]

    def export(self, names: list[str], destination: Path) -> list[str]:
        known = {a["name"] for a in self.listing()}
        done = []
        for n in names:
            if n not in known or ".." in Path(n).parts or Path(n).is_absolute():
                raise ValueError(f"'{n}' is not an artifact of this experiment")
            target = Path(destination) / Path(n).name
            shutil.copyfile(self.ws.output / n, target)
            done.append(target.name)
        return done


class Registry:
    def __init__(self, store: Any) -> None:
        self.store = store

    def _all(self) -> list[dict]:
        data = self.store.get(KEY, [])
        return data if isinstance(data, list) else []

    def add(self, m: Manifest, path: str) -> None:
        self.store.set(KEY, (self._all() + [{"id": m.id, "purpose": m.purpose, "risk": m.risk, "backend": m.backend, "state": "preparing", "path": path,
                                             "created": m.created, "history": ["preparing"], "note": ""}])[-KEEP:])

    def get(self, eid: str) -> dict | None:
        return next((e for e in self._all() if e["id"] == eid), None)

    def move(self, eid: str, state: str, note: str = "") -> dict:
        items = self._all()
        for e in items:
            if e["id"] == eid:
                if state not in FLOW.get(e["state"], ()):
                    raise ValueError(f"an experiment cannot go from '{e['state']}' to '{state}'")
                e["state"], e["note"] = state, note[:200]
                e["history"] = (e.get("history", []) + [state])[-12:]
                self.store.set(KEY, items)
                return e
        raise ValueError("no such experiment")

    def remove(self, eid: str) -> bool:
        items = self._all()
        kept = [e for e in items if e["id"] != eid]
        self.store.set(KEY, kept)
        return len(kept) != len(items)

    def all(self) -> list[dict]:
        return self._all()
