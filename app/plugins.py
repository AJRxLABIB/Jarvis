"""Your own tools as plugins (PLUGINS_ENABLED=false by default).

Put a Python file in the data folder's ``plugins`` directory (e.g. %LOCALAPPDATA%\\JarvisAssistant\\
data\\plugins\\hello.py) that defines ``TOOLS`` like the built-in tool modules (app/tools/*.py).
Each tool keeps its own permission level, so dangerous actions still ask first, and every call
goes through the same validation, permissions and audit log.

**Manifests (3.13):** a plugin only loads after you trusted it in the window (Menu -> Plugins -> Trust). That
writes ``<name>.plugin.json``: the file's sha256 and the exact tools with their risk levels. On every start:

* a file that changed since you trusted it is NOT run (not even imported) - trust it again after checking it;
* a plugin can't add tools you didn't trust, or lower a tool's risk level, or declare a CRITICAL tool.

A plugin is ordinary Python code running with your rights: only trust files you wrote or checked. Jarvis never
creates, downloads, edits or trusts plugins itself, and trusting is not possible by voice.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.tools.base import Tool
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("plugins")


@dataclass(frozen=True)
class PluginReport:
    loaded: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]


MANIFEST_SUFFIX = ".plugin.json"


def manifest_path(path: Path) -> Path:
    return path.with_name(path.stem + MANIFEST_SUFFIX)


def file_sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _import(path: Path):
    spec = importlib.util.spec_from_file_location(f"jarvis_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError("not a Python module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = getattr(module, "TOOLS", None)
    if not isinstance(tools, (list, tuple)) or not all(isinstance(t, Tool) for t in tools):
        raise TypeError("TOOLS must be a list of app.tools.base.Tool")
    for tool in tools:
        if int(tool.level) >= 3:
            raise ValueError(f"{tool.name} is CRITICAL: plugins can't have blocked-level tools")
    return module, tools


def _detected(path: Path) -> set[str]:
    try:
        from app.core import plugin_check

        return set(plugin_check.scan(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def trust_plugin(folder: Path, name: str) -> dict:
    """From the window only (a person at the PC): imports the plugin in a RESTRICTED CHILD PROCESS (core/plugin_sandbox.py) to list its tools,
    then writes the manifest. Raises ValueError when it doesn't load, times out or tries anything blocked while loading."""
    path = Path(folder) / f"{name}.py"
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = file_sha(path)
    from app.core import plugin_sandbox

    try:
        info = plugin_sandbox.probe(path)                  # its code runs in a restricted child process first, never inside Jarvis
    except plugin_sandbox.ProbeError as exc:
        raise ValueError(f"not trusted: {exc}") from exc
    manifest = {"name": name, "version": info.get("version", "1.0"), "description": info.get("doc", ""),
                "sha256": digest, "tools": {n: int(v) for n, v in info["tools"].items()},
                "capabilities": sorted(_detected(path)),
                "trusted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    from app.security import manifest_sign

    manifest["sig"] = manifest_sign.sign(Path(folder), manifest)   # only this installation can produce it (app/security/manifest_sign.py)
    manifest_path(path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log_event(_log, "plugin_trusted", plugin=name, tools=sorted(manifest["tools"]))
    return manifest


def plugin_status(folder: Path) -> list[dict]:
    """[{name, status: trusted / changed / untrusted, version, tools}] - reads files only, runs nothing."""
    out = []
    folder = Path(folder)
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        manifest = _manifest(path)
        status = "untrusted" if manifest is None else "trusted" if manifest.get("sha256") == file_sha(path) \
            else "changed"
        if status == "trusted" and _signature_problem(folder, manifest):
            status = "changed"                                      # a manifest this installation did not sign is not believed
        out.append({"name": path.stem, "status": status, "version": (manifest or {}).get("version", ""),
                    "tools": sorted((manifest or {}).get("tools", {}))})
    return out


def _signature_problem(folder: Path, manifest: dict) -> str:
    """'' when the manifest's signature is valid. Manifests written before signing existed (no signing key on this PC yet) are adopted ONCE: they are
    signed in place on first sight, and from then on every manifest must carry a valid signature."""
    from app.security import manifest_sign

    folder = Path(folder)
    if "sig" not in manifest and not manifest_sign.has_key(folder):
        return ""                                                   # legacy install, nothing signed yet: adopted by ``_adopt_legacy``
    ok, why = manifest_sign.verify(folder, manifest)
    return "" if ok else why


def _adopt_legacy(folder: Path) -> int:
    """First start after signing was introduced: sign the manifests that were already trusted (file hash still matching). Returns how many."""
    from app.security import manifest_sign

    folder = Path(folder)
    if manifest_sign.has_key(folder):
        return 0
    adopted = 0
    for path in sorted(folder.glob("*.py")):
        manifest = _manifest(path)
        if manifest is None or "sig" in manifest or manifest.get("sha256") != file_sha(path):
            continue
        manifest["sig"] = manifest_sign.sign(folder, manifest)
        manifest_path(path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        adopted += 1
    if adopted:
        log_event(_log, "plugin_manifests_signed", count=adopted)
    return adopted


def _manifest(path: Path) -> dict | None:
    try:
        data = json.loads(manifest_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("tools"), dict) else None


def load_plugins(folder: Path, registry) -> PluginReport:
    loaded, failed = [], []
    folder = Path(folder)
    if not folder.is_dir():
        return PluginReport((), ())
    try:
        _adopt_legacy(folder)
    except Exception:
        _log.warning("could not sign existing plugin manifests", exc_info=True)
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            manifest = _manifest(path)
            if manifest is None:
                raise PermissionError("not trusted yet (Menu -> Plugins -> Trust)")
            if manifest.get("sha256") != file_sha(path):
                raise PermissionError("the file changed since you trusted it; check it and trust it again")
            problem = _signature_problem(folder, manifest)
            if problem:
                raise PermissionError(problem)
            _module, tools = _import(path)
            approved: dict = manifest["tools"]
            for tool in tools:
                if tool.name not in approved:
                    raise PermissionError(f"{tool.name} wasn't in what you trusted")
                if int(tool.level) < int(approved[tool.name]):
                    raise PermissionError(f"{tool.name} lowered its risk level")
                if tool.name in registry:
                    raise ValueError(f"a tool called {tool.name} already exists")
            for tool in tools:
                registry.register(tool)
            loaded.append(path.stem)
        except Exception as exc:
            failed.append((path.stem, str(exc)[:200]))
            _log.warning("plugin %s failed to load: %s", path.name, exc)
    log_event(_log, "plugins_loaded", loaded=loaded, failed=[f[0] for f in failed])
    return PluginReport(tuple(loaded), tuple(failed))
