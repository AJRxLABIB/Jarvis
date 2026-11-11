"""Find out how an application on THIS PC could be driven - before anyone writes an adapter. Read-only; nothing is installed, generated or registered.

Checked locally (each check is injected, so it is testable off Windows):
* a tool Jarvis already has for it (``app_adapters.STRUCTURED_TOOLS`` / an app profile);
* a command-line program on PATH (``shutil.which``) - the most reliable route when it exists;
* a COM automation class in the registry (``<Name>.Application`` ProgID) - how Office-style programs are scripted;
* a registered URL scheme (``name:`` links) - can open the app at a place, cannot read results back;
* UI Automation as the fall-back, with how well this app is known to expose controls.
NOT checked, and said so: the vendor's web API / SDK documentation, licences and sign-in requirements (that needs a web search and your judgement).
The output is a ranked list of OPTIONS with what each can and cannot do. Turning one into a tool goes through the existing Foundry (draft -> validate ->
sandbox test -> your approval); this module never writes code and never stores credentials.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from typing import Callable


@dataclass
class Option:
    route: str               # existing tool | command line | COM automation | URL scheme | UI Automation
    detail: str
    can: str
    cannot: str
    reliability: int         # 5 best


def _reg_has(path: str) -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path):
            return True
    except Exception:
        return False


def _reg_url_scheme(name: str) -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, name) as k:
            winreg.QueryValueEx(k, "URL Protocol")
            return True
    except Exception:
        return False


def discover(app: str, which: Callable[[str], str | None] = shutil.which, has_progid: Callable[[str], bool] = _reg_has,
             has_scheme: Callable[[str], bool] = _reg_url_scheme) -> list[Option]:
    from app.core import app_adapters as A

    name = re.sub(r"[^a-z0-9_\-\. ]", "", (app or "").lower()).strip().removesuffix(".exe")
    if not name:
        return []
    out: list[Option] = []
    ad = A.ApplicationAdapter.detect_application(name)
    insp = ad.inspect_application()
    for tool in insp.structured:
        out.append(Option("existing tool", f"Jarvis already has '{tool}' for {insp.app}", "work with the program through its own interface", "anything that tool does not cover", 5))
    short = name.split()[0]
    exe = which(short) or which(name.replace(" ", ""))
    if exe:
        out.append(Option("command line", f"'{short}' is on PATH ({exe})", "run documented commands and read their output", "drive the window; commands must be checked one by one before use", 4))
    for progid in (f"{short.capitalize()}.Application", f"{short}.Application"):
        if has_progid(progid):
            out.append(Option("COM automation", f"registry class {progid}", "script the program's documents and objects", "work when the program blocks automation or is not installed for this user", 4))
            break
    if has_scheme(short):
        out.append(Option("URL scheme", f"'{short}:' links are registered", "open the program at a place or with a value", "read anything back or confirm what happened", 2))
    uia = insp.uia
    out.append(Option("UI Automation", f"controls exposed: {uia}" + (f" - {insp.advice}" if insp.advice and uia != "good" else ""),
                      "press named buttons and read visible text", "work when the program hides its controls; slower and less certain than an API", {"good": 3, "partial": 2}.get(uia, 1)))
    return sorted(out, key=lambda o: -o.reliability)


def describe(app: str, options: list[Option]) -> str:
    if not options:
        return "Tell me which program to look at."
    lines = [f"How '{app}' can be driven on this PC (best first):"]
    lines += [f"{i}. {o.route}: {o.detail}\n   can: {o.can}\n   cannot: {o.cannot}" for i, o in enumerate(options, 1)]
    lines.append("Not checked: the vendor's web API or SDK, its licence and sign-in needs - that takes a web search and your decision. Nothing was installed, generated or registered. "
                 "To turn an option into a tool, use the Foundry (it validates, tests in isolation and asks you before saving).")
    return "\n".join(lines)
