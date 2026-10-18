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
    rows = [i.evaluate(ctx) for i in _REGISTRY.values()]
    saved = _saved_tests(ctx)
    for row in rows:
        test = saved.get(row["key"])
        # "ready" only means configured. The verdict is what a smoke test last found, if one was ever run.
        row["verdict"] = test["verdict"] if test and row["state"] == "ready" else _configured_verdict(row)
        row["tested_at"] = test["tested_at"] if test else ""
        row["test_detail"] = test["detail"] if test else ""
        row["end_to_end"] = "not tested"   # never claimed automatically: only a real workflow run can show it
    return rows


# --------------------------------------------------------------- smoke tests (these DO contact the service)

VERDICTS = ("not_configured", "credentials_missing", "configured", "auth_failed", "permission_denied", "unreachable",
            "degraded", "smoke_passed")
TRANSPORT = None  # tests replace this (httpx transport)


def _configured_verdict(row: dict[str, str]) -> str:
    if row["state"] == "ready":
        return "configured"
    if row["state"] == "off":
        return "not_configured"
    return "credentials_missing" if "token" in row["detail"] or "missing" in row["detail"] else "not_configured"


def _saved_tests(ctx: Any) -> dict[str, dict[str, str]]:
    database = getattr(ctx, "database", None)
    if database is None:
        return {}
    try:
        with database.connect() as conn:
            return {r["key"]: dict(r) for r in conn.execute("SELECT key, verdict, detail, tested_at FROM integration_tests")}
    except Exception:
        return {}


def _http(ctx: Any, url: str, headers: dict[str, str] | None = None) -> tuple[str, str]:
    """(verdict, detail) of one read-only GET, from the response alone."""
    import httpx

    try:
        with httpx.Client(transport=TRANSPORT, timeout=8.0, trust_env=False) as client:
            response = client.get(url, headers=headers or {})
    except httpx.TimeoutException:
        return "unreachable", "no answer within 8 seconds"
    except httpx.HTTPError:
        return "unreachable", "could not connect"
    if response.status_code == 401:
        return "auth_failed", "the service refused the credentials"
    if response.status_code == 403:
        return "permission_denied", "the credentials are valid but not allowed to do this"
    if response.status_code >= 500:
        return "degraded", f"the service answered with an error (HTTP {response.status_code})"
    if response.status_code >= 400:
        return "degraded", f"unexpected answer (HTTP {response.status_code})"
    return "smoke_passed", "answered and accepted the credentials"


def _smoke_ollama(ctx: Any) -> tuple[str, str]:
    import httpx

    host = ctx.settings.ollama_host.rstrip("/")
    try:
        with httpx.Client(transport=TRANSPORT, timeout=5.0, trust_env=False) as client:
            response = client.get(host + "/api/tags")
        names = [m.get("name", "") for m in response.json().get("models", [])]
    except httpx.HTTPError:
        return "unreachable", f"Ollama did not answer at {host}"
    except ValueError:
        return "degraded", "Ollama answered with something unexpected"
    wanted = ctx.settings.ai_model
    if wanted and wanted not in names:
        return "degraded", f"Ollama is running but the configured model '{wanted}' is not installed"
    return "smoke_passed", f"Ollama answered; {len(names)} model(s) installed"


def _smoke_home_assistant(ctx: Any) -> tuple[str, str]:
    s = ctx.settings
    return _http(ctx, s.home_assistant_url.rstrip("/") + "/api/", {"Authorization": f"Bearer {s.home_assistant_token}"})


def _smoke_github(ctx: Any) -> tuple[str, str]:
    return _http(ctx, "https://api.github.com/user", {"Authorization": f"Bearer {ctx.settings.github_token}",
                                                      "Accept": "application/vnd.github+json", "User-Agent": "jarvis-assistant"})


def _smoke_outlook(ctx: Any) -> tuple[str, str]:
    if sys.platform != "win32":
        return "not_configured", "Outlook is only available on Windows"
    try:
        import win32com.client

        win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    except Exception as exc:
        return "unreachable", f"Outlook could not be opened ({type(exc).__name__})"
    return "smoke_passed", "Outlook opened through Windows (calendar and mail not read)"


SMOKE: dict[str, Callable[[Any], tuple[str, str]]] = {
    "ollama": _smoke_ollama, "home_assistant": _smoke_home_assistant, "github": _smoke_github, "outlook": _smoke_outlook,
}


def run_smoke(ctx: Any, key: str) -> dict[str, str]:
    """Contact one configured service with a read-only request and remember what happened.

    Smoke = "it answered and accepted the credentials". It is NOT an end-to-end test: a real workflow (an actual
    reminder, an actual device command) is a separate thing and is never reported as passed from here.
    """
    integration = _REGISTRY.get(key)
    if integration is None:
        raise KeyError(key)
    row = integration.evaluate(ctx)
    if row["state"] != "ready":
        verdict, detail = _configured_verdict(row), f"not tested: {row['detail'] or row['state']}"
    elif key not in SMOKE:
        verdict, detail = "configured", "no smoke test exists for this service; it needs the real device or program"
    else:
        try:
            verdict, detail = SMOKE[key](ctx)
        except Exception as exc:  # a crashing check is a result, never a crash
            verdict, detail = "degraded", f"the check failed ({type(exc).__name__})"
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    database = getattr(ctx, "database", None)
    if database is not None and key in SMOKE and row["state"] == "ready":
        with database.transaction() as conn:
            conn.execute("INSERT INTO integration_tests (key, verdict, detail, tested_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET verdict = excluded.verdict, detail = excluded.detail, "
                         "tested_at = excluded.tested_at", (key, verdict, detail[:200], now))
    return {"key": key, "verdict": verdict, "detail": detail, "tested_at": now}


def describe(rows: list[dict[str, str]]) -> str:
    passed = [r["name"] for r in rows if r.get("verdict") == "smoke_passed"]
    ready = [r["name"] for r in rows if r["state"] == "ready" and r.get("verdict") != "smoke_passed"]
    todo = [r for r in rows if r["state"] in ("not_set_up", "problem")]
    parts = [f"Answered a smoke test: {', '.join(passed) if passed else 'none tested yet'}."]
    if ready:
        parts.append(f"Configured but not confirmed (or the last test failed): {', '.join(ready)}.")
    for row in rows:
        if row["state"] == "ready" and row.get("verdict") not in ("smoke_passed", "configured", None):
            parts.append(f"{row['name']}: {row['verdict'].replace('_', ' ')} ({row['test_detail']}).")
    parts.append("No full end-to-end test has been run for any service.")
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


def _github(ctx: Any) -> Status:
    if not getattr(ctx.settings, "github_token", ""):
        return Status("not_set_up", "no token")
    return Status("ready", "token is set (read-only use)")


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
    Integration("github", "GitHub", "read your notifications, assigned issues and pull requests",
                "Create a read-only fine-grained token on GitHub and save it as GITHUB_TOKEN in Settings.", _github),
    Integration("phone_remote", "Phone remote", "control Jarvis from your phone",
                "Turn on the phone remote in Settings, then pair with the code in Menu -> Phone remote.", _phone_remote),
    Integration("mcp", "MCP servers", "extra tools from other programs",
                "List servers in config\\mcp.json (see docs).", _mcp),
    Integration("outlook", "Outlook", "calendar and email", "Install and sign in to Outlook on this PC.", _outlook),
    Integration("browser", "Browser automation", "reading and using web pages", "Run `pip install playwright`.", _browser),
):
    register(_item)
