"""Plugin capability scan: what does a plugin's source ACTUALLY do, before you trust it? Reads files only (AST), never imports or runs them.

Detected from imports and calls: ``network`` (socket, http, urllib, requests, httpx, aiohttp, websockets, ftplib, smtplib ...), ``process`` (subprocess, os.system,
os.popen, pty), ``native`` (ctypes, cffi), ``dynamic`` (eval, exec, compile, __import__, importlib), ``filesystem`` (shutil, os.remove / unlink, rmtree).
A plugin's manifest may declare ``capabilities``; anything detected but not declared is a MISMATCH. A static scan is a review aid, not a sandbox:
obfuscated code can hide things, so the verdict says "found" / "nothing found", never "safe".
"""

from __future__ import annotations

import ast
from pathlib import Path

CAPS = {
    "network": {"socket", "http", "urllib", "requests", "httpx", "aiohttp", "websockets", "websocket", "ftplib", "smtplib", "imaplib", "telnetlib", "paho", "ssl"},
    "process": {"subprocess", "pty", "multiprocessing"},
    "native": {"ctypes", "cffi", "win32api", "win32com", "pythoncom"},
    "dynamic": {"importlib", "runpy", "pickle", "marshal"},
    "filesystem": {"shutil"},
}
CALLS = {"eval": "dynamic", "exec": "dynamic", "compile": "dynamic", "__import__": "dynamic", "system": "process", "popen": "process", "startfile": "process",
         "remove": "filesystem", "unlink": "filesystem", "rmtree": "filesystem", "rmdir": "filesystem"}


def scan(source: str) -> dict[str, list[str]]:
    """{capability: [evidence...]} for one plugin's source text. Raises SyntaxError for code that does not parse."""
    found: dict[str, list[str]] = {}

    def add(cap: str, why: str) -> None:
        bucket = found.setdefault(cap, [])
        if why not in bucket and len(bucket) < 6:
            bucket.append(why)

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            names = []
        for full in names:
            root = full.split(".")[0]
            for cap, mods in CAPS.items():
                if root in mods:
                    add(cap, f"imports {full}")
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in CALLS:
                add(CALLS[name], f"calls {name}()")
    return found


def check(folder: Path) -> list[dict]:
    """One row per plugin file: detected capabilities, declared ones (from the manifest) and the mismatch."""
    import json

    rows = []
    folder = Path(folder)
    if not folder.is_dir():
        return rows
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        row: dict = {"name": path.stem, "detected": {}, "declared": None, "undeclared": [], "problem": ""}
        try:
            row["detected"] = scan(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError) as exc:
            row["problem"] = f"could not read it as Python ({str(exc)[:60]})"
        try:
            manifest = json.loads(path.with_name(path.stem + ".plugin.json").read_text(encoding="utf-8"))
            if isinstance(manifest.get("capabilities"), list):
                row["declared"] = sorted(str(c) for c in manifest["capabilities"])
        except (OSError, ValueError):
            pass
        row["undeclared"] = sorted(c for c in row["detected"] if row["declared"] is not None and c not in row["declared"])
        rows.append(row)
    return rows


def describe(rows: list[dict]) -> str:
    if not rows:
        return "No plugins found."
    lines = []
    for r in rows:
        if r["problem"]:
            lines.append(f"{r['name']}: {r['problem']}")
            continue
        caps = ", ".join(f"{c} ({'; '.join(w)})" for c, w in r["detected"].items()) or "nothing found by the scan"
        extra = f" MISMATCH - not declared: {', '.join(r['undeclared'])}." if r["undeclared"] else ("" if r["declared"] is not None else " (manifest declares no capabilities)")
        lines.append(f"{r['name']}: {caps}.{extra}")
    return "\n".join(lines) + "\nThis is a static review aid, not a guarantee: read the code before trusting a plugin."
