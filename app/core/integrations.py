"""Integrations in one place: what is connected, what isn't, and why.

Every outside service Jarvis can use (Home Assistant, the Ollama AI, MCP servers, the phone remote, Outlook, ...)
describes itself with an ``Integration``: a name, what it is for, how to set it up, and a ``check`` that says whether
it is configured and (cheaply, locally) whether it looks usable. ``register`` adds a new one - a plug-in or a future
service needs no change to the rest of the code.

Rules: a check never calls the network, never opens a connection and never returns a secret - it only reads settings
and local state. Whether a service really answers is shown by using it (or by its own health tool). A check that
crashes is reported as "could not be checked", never hides the others.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Status:
    state: str          # "ready" | "not_set_up" | "problem" | "off"
    detail: str = ""


@dataclass(frozen=True)
class Integration:
    key: str
    name: str
    purpose: str
    setup: str                                  # one plain sentence on how to connect it
    check: Callable[[Any], Status]              # (ToolContext) -> Status

    def evaluate(self, ctx: Any) -> dict[str, str]:
        try:
            status = self.check(ctx)
        except Exception as exc:  # one broken check must not hide the rest
            status = Status("problem", f"could not be checked ({type(exc).__name__})")
        return {"key": self.key, "name": self.name, "purpose": self.purpose, "state": status.state,
                "detail": status.detail, "setup": "" if status.state == "ready" else self.setup}


_REGISTRY: dict[str, Integration] = {}


def register(integration: Integration) -> None:
    _REGISTRY[integration.key] = integration


def all_integrations() -> list[Integration]:
    return list(_REGISTRY.values())


def evaluate_all(ctx: Any) -> list[dict[str, str]]:
    return [i.evaluate(ctx) for i in _REGISTRY.values()]


def describe(rows: list[dict[str, str]]) -> str:
    ready = [r["name"] for r in rows if r["state"] == "ready"]
    todo = [r for r in rows if r["state"] in ("not_set_up", "problem")]
    parts = [f"Connected: {', '.join(ready) if ready else 'nothing yet'}."]
    for row in todo:
        parts.append(f"{row['name']}: {row['detail'] or row['state'].replace('_', ' ')}. {row['setup']}".strip())
    return " ".join(parts)


# ------------------------------------------------------------------ built-in checks


def _home_assistant(ctx: Any) -> Status:
    s = ctx.settings
    if not s.home_assistant_url and not s.home_assistant_token:
        return Status("not_set_up", "no address or token")
    if not s.home_assistant_url or not s.home_assistant_token:
        return Status("problem", "the address or the token is missing")
    return Status("ready", "address and token are set")


def _ollama(ctx: Any) -> Status:
    health = getattr(getattr(ctx, "ai", None), "health", None)
    summary = health.summary() if health is not None and hasattr(health, "summary") else {}
    failing = [m for m, info in summary.items() if info.get("skipped_for", 0) > 0]
    if failing:
        return Status("problem", f"failing recently: {', '.join(failing)}")
    return Status("ready", f"host {ctx.settings.ollama_host}")


def _phone_remote(ctx: Any) -> Status:
    from app.core import emergency

    if emergency.STATE.flags.get("remote_disabled"):
        return Status("off", "switched off in the emergency controls")
    if not ctx.settings.remote_enabled:
        return Status("off", "not enabled")
    count = 0
    if ctx.paths is not None:
        from app.remote.pairing import PairingStore

        count = len(PairingStore(ctx.paths.data_dir).devices)
    return Status("ready", f"{count} paired phone(s)") if count else Status("not_set_up", "no phone is paired yet")


def _mcp(ctx: Any) -> Status:
    if not ctx.settings.mcp_enabled:
        return Status("off", "switched off in settings")
    if ctx.paths is None:
        return Status("not_set_up", "no servers listed")
    from app.mcp.tools import load_config

    servers = load_config(ctx.paths.config_dir)
    if not servers:
        return Status("not_set_up", "no servers listed in config\\mcp.json")
    hub = getattr(getattr(ctx, "executor", None), "mcp_hub", None)
    status = getattr(hub, "status", {}) or {}
    broken = [name for name, text in status.items() if str(text).startswith("failed")]
    if broken:
        return Status("problem", f"could not start: {', '.join(broken)}")
    return Status("ready", f"{len(servers)} server(s) listed")


def _outlook(ctx: Any) -> Status:
    if sys.platform != "win32":
        return Status("off", "Windows only")
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return Status("problem", "the pywin32 package is missing")
    return Status("ready", "Windows can talk to Outlook; it must be installed and signed in")


def _browser(ctx: Any) -> Status:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return Status("problem", "the playwright package is missing")
    return Status("ready", f"browser channel '{ctx.settings.browser_channel}'")


for _item in (
    Integration("ollama", "Local AI (Ollama)", "answers and tool choice", "Start Ollama and run `ollama pull qwen3:8b`.",
                _ollama),
    Integration("home_assistant", "Home Assistant", "smart-home control",
                "Set HOME_ASSISTANT_URL and HOME_ASSISTANT_TOKEN in Settings.", _home_assistant),
    Integration("phone_remote", "Phone remote", "control Jarvis from your phone",
                "Turn on the phone remote in Settings, then pair with the code in Menu -> Phone remote.", _phone_remote),
    Integration("mcp", "MCP servers", "extra tools from other programs",
                "List servers in config\\mcp.json (see docs).", _mcp),
    Integration("outlook", "Outlook", "calendar and email", "Install and sign in to Outlook on this PC.", _outlook),
    Integration("browser", "Browser automation", "reading and using web pages", "Run `pip install playwright`.", _browser),
):
    register(_item)
