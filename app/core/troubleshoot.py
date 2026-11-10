"""Guided troubleshooting: find what is wrong, explain it, and offer only SAFE repairs - each one previewed and approved.

It builds on ``app/core/diagnostics.py`` (Jarvis's own health) and adds the setup problems people actually hit: Python
version, missing packages, missing or misnamed models, the microphone, a missing ``.env``, unusable settings, services
that are configured but not answering, the database schema, and the phone remote.

Every finding says: the problem, the evidence, the likely cause, the recommended fix, WHAT THE FIX CHANGES (nothing /
files / configuration / external), a preview, whether it needs your approval, how it is verified and how it is undone.

Rules: a diagnosis never runs a command. Where the fix is a command (``pip install``, ``ollama pull``) the command is
SHOWN for you to run - Jarvis does not run arbitrary commands as a repair. The few repairs Jarvis can do itself are
listed in ``REPAIRS``; they touch only its own bookkeeping or create a file that does not exist yet, never delete your
data, never overwrite a file, and never touch a credential. After a repair the check is run again to prove it worked.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MIN_PYTHON = (3, 11)

# module -> (what needs it, required for the app to start?)
PACKAGES: dict[str, tuple[str, bool]] = {
    "pydantic": ("settings and tool arguments", True), "dotenv": ("reading .env", True), "httpx": ("web and local AI calls", True),
    "numpy": ("audio", True), "psutil": ("system monitoring", True), "PySide6": ("the window", True),
    "aiohttp": ("the phone remote", True), "cryptography": ("the phone remote's certificate", True),
    "sounddevice": ("the microphone and speakers", False), "faster_whisper": ("speech recognition", False),
    "edge_tts": ("neural voices", False), "piper": ("offline English voice", False),
    "sherpa_onnx": ("offline Bangla voice", False), "onnxruntime": ("wake-word detection", False),
    "playwright": ("browser automation", False), "PIL": ("screenshots", False), "pypdf": ("searching PDFs", False),
    "cv2": ("presence detection", False), "qrcode": ("the phone-pairing QR code", False),
    "kokoro_onnx": ("natural offline English voice", False),
}
WINDOWS_ONLY = {"win32com": ("Outlook and Windows control", False), "pycaw": ("volume control", False)}


@dataclass
class Finding:
    id: str
    problem: str
    evidence: str
    cause: str
    fix: str
    changes: str = "nothing"        # nothing | files | configuration | external
    command: str = ""               # shown, never run by Jarvis
    repair: str = ""                # id in REPAIRS if Jarvis can do it itself
    severity: str = "warning"       # problem | warning | info
    rollback: str = ""

    def line(self) -> str:
        how = (f"Jarvis can fix this after you approve it ('{self.repair}')" if self.repair
               else (f"Run yourself: {self.command}" if self.command else "Manual step"))
        return (f"[{self.severity.upper()}] {self.problem} - evidence: {self.evidence}. Likely cause: {self.cause}. "
                f"Fix: {self.fix} ({how}; changes: {self.changes}).")


@dataclass
class Environment:
    """Everything a check looks at, replaceable in tests."""
    python: tuple[int, int, int] = field(default_factory=lambda: tuple(sys.version_info[:3]))
    has_module: Callable[[str], bool] = field(default_factory=lambda: _has_module)   # factories: a plain function default
    platform: str = sys.platform                                                       # would bind as a method
    ollama_models: Callable[[str], list[str] | None] = field(default_factory=lambda: _ollama_models)
    input_devices: Callable[[], list[str] | None] = field(default_factory=lambda: _input_devices)


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _ollama_models(host: str) -> list[str] | None:
    """Installed model names, or None when Ollama does not answer (2-second local look, no other network)."""
    try:
        import httpx

        response = httpx.get(host.rstrip("/") + "/api/tags", timeout=2.0, trust_env=False)
        return [m.get("name", "") for m in response.json().get("models", [])]
    except Exception:
        return None


def _input_devices() -> list[str] | None:
    try:
        import sounddevice

        return [d["name"] for d in sounddevice.query_devices() if d.get("max_input_channels", 0) > 0]
    except Exception:
        return None


def scan(ctx: Any, env: Environment | None = None) -> list[Finding]:
    env = env or Environment()
    out: list[Finding] = []
    settings, paths = ctx.settings, getattr(ctx, "paths", None)

    if env.python[:2] < MIN_PYTHON:
        out.append(Finding("python_version", "Python is too old", f"running {'.'.join(map(str, env.python))}, needs "
                           f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}+", "an older Python was used to start Jarvis",
                           "install Python 3.11 or newer and recreate the .venv (see SETUP.md)", "external",
                           command="python -m venv .venv", severity="problem"))

    candidates = dict(PACKAGES)
    if env.platform == "win32":
        candidates.update(WINDOWS_ONLY)
    for module, (purpose, required) in candidates.items():
        if not env.has_module(module):
            out.append(Finding(f"package_{module}", f"A Python package is missing ({module})",
                               f"'{module}' cannot be found by this Python", f"the packages were not installed in this "
                               f"environment, or the wrong Python is running (it is needed for {purpose})",
                               "install the requirements into the project's .venv", "files",
                               command=r".\.venv\Scripts\python.exe -m pip install -r requirements.txt",
                               severity="problem" if required else "info",
                               rollback="pip uninstall of just that package, if it causes trouble"))

    if paths is not None:
        env_file = Path(paths.env_file)
        if not env_file.exists():
            out.append(Finding("env_missing", "There is no .env settings file", f"{env_file} does not exist",
                               "first start on this PC, or it was moved - defaults are being used",
                               "create it with every setting at its default (nothing is overwritten and it contains no secret)", "files",
                               repair="create_env", severity="info",
                               rollback="delete the new .env (Jarvis can do this if you have not edited it)"))
        data_dir = Path(paths.data_dir)
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            probe = data_dir / ".write-test"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            out.append(Finding("data_not_writable", "Jarvis cannot write to its data folder", f"{data_dir}: {exc.__class__.__name__}",
                               "the folder is read-only, on a missing drive, or protected by another program",
                               "move the project to a normal folder or fix the folder's permissions", "external",
                               severity="problem"))

    if ctx.database is not None:
        from app.memory.database import LATEST_SCHEMA_VERSION

        try:
            version = ctx.database.schema_version()
            if version != LATEST_SCHEMA_VERSION:
                out.append(Finding("db_schema", "The database is not at the current version",
                                   f"schema {version}, this version expects {LATEST_SCHEMA_VERSION}",
                                   "an update was interrupted or the database came from another version",
                                   "restart Jarvis (it migrates and keeps a .bak copy first); if that fails, restore the "
                                   "newest backup in data\\backups", "files", severity="problem",
                                   rollback="the automatic data\\assistant.v<N>.bak.db copy"))
        except Exception as exc:
            out.append(Finding("db_unreadable", "The database cannot be read", exc.__class__.__name__,
                               "the file is damaged or locked by another program",
                               "close other Jarvis windows; otherwise restore the newest backup in data\\backups", "files",
                               severity="problem"))

    if ctx.database is not None:
        try:
            from app.memory.settings_store import SettingsStore

            last = SettingsStore(ctx.database).get("backup_restore_test")
            if isinstance(last, dict) and last.get("ok") is False:
                out.append(Finding("restore_test_failed", "Your latest backup could not be restored in a test",
                                   str(last.get("message", ""))[:200],
                                   "the backup copy is damaged or incompatible, so it could not save you if needed",
                                   "make a fresh backup and test it: `python run.py --backup-db`", "files",
                                   command="python run.py --backup-db", severity="problem"))
        except Exception:
            pass

    if getattr(settings, "ai_provider", "") == "ollama":
        models = env.ollama_models(settings.ollama_host)
        if models is None:
            out.append(Finding("ollama_down", "The local AI (Ollama) is not answering", f"no reply from {settings.ollama_host}",
                               "Ollama is not running, or OLLAMA_HOST points at the wrong address",
                               "start Ollama, then check OLLAMA_HOST", "external",
                               command="ollama serve", severity="problem"))
        else:
            for label, name in (("main", settings.ai_model), ("fast", settings.ai_fast_model), ("smart", settings.ai_smart_model),
                                ("coding", settings.ai_coding_model), ("embedding", settings.embedding_model)):
                if name and name not in models and not any(m.split(":")[0] == name for m in models):
                    out.append(Finding(f"model_{label}", f"The {label} model '{name}' is not installed",
                                       f"Ollama has: {', '.join(models[:6]) or 'no models'}",
                                       "the model was never pulled, or the name in settings has a typo",
                                       "pull the model or change the setting to one you have", "external",
                                       command=f"ollama pull {name}", severity="problem" if label == "main" else "warning"))
            if not models:
                out.append(Finding("no_models", "Ollama is running but has no models", "the model list is empty",
                                   "nothing was pulled yet", "pull one model", "external", command="ollama pull qwen3:8b",
                                   severity="problem"))

    if env.has_module("sounddevice"):
        inputs = env.input_devices()
        if inputs is None:
            out.append(Finding("audio_unavailable", "The audio system could not be queried", "sounddevice failed to list devices",
                               "no audio driver, or the audio service is stopped", "check Windows Sound settings, then run "
                               "`python run.py --devices`", "nothing", command="python run.py --devices"))
        elif not inputs:
            out.append(Finding("no_microphone", "No microphone was found", "the list of input devices is empty",
                               "no microphone is connected, or Windows blocks microphone access for desktop apps",
                               "connect one and allow microphone access (Settings -> Privacy -> Microphone)", "configuration",
                               command="python run.py --mic-test"))

    if getattr(settings, "remote_enabled", False) and paths is not None:
        try:
            from app.remote.pairing import PairingStore

            if not PairingStore(paths.data_dir).devices:
                out.append(Finding("phone_unpaired", "The phone remote is on but no phone is paired", "0 paired devices",
                                   "pairing was not finished, or the pairing was revoked",
                                   "open Menu -> Phone remote and enter the one-time code on the phone (same Wi-Fi)", "nothing",
                                   severity="info"))
        except Exception:
            pass

    try:
        from app.core import integrations

        for row in integrations.evaluate_all(ctx):
            if row["state"] == "problem" and "package" not in row["detail"]:   # missing packages are reported above
                out.append(Finding(f"integration_{row['key']}", f"{row['name']} is set up but not usable", row["detail"],
                                   "a missing package, token or address", row["setup"] or "see Settings", "configuration"))
    except Exception:
        pass
    return out


# ------------------------------------------------------------------------------ repairs Jarvis may do itself


def _undo_file(ctx: Any) -> Path:
    return Path(ctx.paths.data_dir) / "troubleshoot-undo.json"


def _remember(ctx: Any, record: dict[str, Any]) -> None:
    path = _undo_file(ctx)
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        items = []
    items.append(record)
    path.write_text(json.dumps(items[-20:], indent=2), encoding="utf-8")


def preview(ctx: Any, repair_id: str) -> str:
    """What a repair would do - computed without changing anything."""
    if repair_id == "create_env":
        paths = ctx.paths
        return (f"Would create {paths.env_file} with every setting at its default. It only happens if the file does not exist; "
                "nothing is overwritten and it contains no secrets.")
    if repair_id == "clear_caches":
        from app.core import diagnostics

        root = _source_root()
        count = len(diagnostics.stale_caches(root)) if root else 0
        return f"Would delete {count} out-of-date Python cache file(s); Python rebuilds them. No data or settings change."
    if repair_id == "close_stuck":
        from app.core import diagnostics

        checks = diagnostics.check_stuck(ctx.database, None)
        return f"Would close requests and pause missions left 'running' by a crash. Now: {checks.detail}. Missions can be resumed."
    raise KeyError(repair_id)


def _source_root() -> Path | None:
    if getattr(sys, "frozen", False):
        return None
    import app

    return Path(app.__file__).resolve().parent


REPAIRS = ("create_env", "clear_caches", "close_stuck")


def apply(ctx: Any, repair_id: str, *, approved: bool, env: Environment | None = None) -> dict[str, Any]:
    """Do one repair - only if ``approved`` - then re-scan to verify. Returns what was done, the verification and the undo."""
    if repair_id not in REPAIRS:
        raise KeyError(repair_id)
    if not approved:
        return {"done": False, "reason": "not approved", "preview": preview(ctx, repair_id)}
    undo = ""
    if repair_id == "create_env":
        from app.config.env_template import default_env_text

        target = Path(ctx.paths.env_file)
        if target.exists():
            return {"done": False, "reason": "a .env already exists; it is never overwritten"}
        content = default_env_text().encode("utf-8")
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(content)
        tmp.replace(target)       # atomic: either the whole file exists or nothing does
        _remember(ctx, {"repair": "create_env", "path": str(target), "sha256": hashlib.sha256(content).hexdigest()})
        done, undo = f"created {target.name} with the default settings", "rollback_repair('create_env') deletes it if you have not edited it"
    elif repair_id == "clear_caches":
        from app.core import diagnostics

        root = _source_root()
        removed = 0
        for pyc in (diagnostics.stale_caches(root) if root else []):
            try:
                pyc.unlink()
                removed += 1
            except OSError:
                continue
        done, undo = f"cleared {removed} out-of-date Python cache file(s)", "not needed: Python rebuilds the caches"
    else:
        from app.core import diagnostics

        steps = diagnostics.repair(ctx, source_root=None)
        done, undo = ("; ".join(steps) or "nothing needed closing"), "paused missions can be resumed with 'continue my mission'"
    remaining = [f.id for f in scan(ctx, env) if f.repair == repair_id]
    return {"done": True, "what": done, "verified": not remaining, "still_reported": remaining, "undo": undo}


def rollback_repair(ctx: Any, repair_id: str) -> dict[str, Any]:
    """Undo ``create_env`` - only if the file is still exactly what Jarvis created (never deletes your edits)."""
    if repair_id != "create_env":
        return {"done": False, "reason": "that repair needs no undo"}
    try:
        items = json.loads(_undo_file(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"done": False, "reason": "nothing to undo"}
    records = [r for r in items if r.get("repair") == "create_env"]
    if not records:
        return {"done": False, "reason": "nothing to undo"}
    record = records[-1]
    target = Path(record["path"])
    if not target.is_file():
        return {"done": False, "reason": "the file is already gone"}
    if hashlib.sha256(target.read_bytes()).hexdigest() != record["sha256"]:
        return {"done": False, "reason": "the file was edited since Jarvis created it, so it was left alone"}
    target.unlink()
    return {"done": True, "what": f"deleted {target.name} (it was unchanged)"}


def describe(findings: list[Finding]) -> str:
    if not findings:
        return "No setup problems found."
    order = {"problem": 0, "warning": 1, "info": 2}
    return " ".join(f.line() for f in sorted(findings, key=lambda f: order.get(f.severity, 3)))


def to_dicts(findings: list[Finding]) -> list[dict[str, Any]]:
    return [asdict(f) for f in findings]
