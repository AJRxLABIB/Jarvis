"""``a2a_status`` (registered outside agents and their trust) and ``a2a_manage`` (register / approve / block / remove / delegate). External agents are untrusted
(core/a2a.py): everything that contacts one is DANGEROUS, because it sends text outside this PC. Off unless A2A_ENABLED=true."""

from __future__ import annotations

import asyncio
from typing import Literal

import httpx
from pydantic import Field

from app.core import a2a
from app.memory.settings_store import SettingsStore
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult

KEY = "a2a_registry"
MAX_CARD = a2a.MAX_CARD_BYTES


def _registry(ctx: ToolContext) -> a2a.Registry:
    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    return a2a.Registry.load(SettingsStore(ctx.database).get(KEY))


def _enabled(ctx: ToolContext) -> None:
    if not getattr(ctx.settings, "a2a_enabled", False):
        raise ToolError("Outside agents are switched off. Set A2A_ENABLED=true in .env (or the settings) to allow contacting agents you registered.", "not_enabled")


def _get(url: str) -> bytes:
    with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False, trust_env=False, headers={"User-Agent": "JarvisAssistant-A2A"}) as c:
        with c.stream("GET", url) as r:
            if r.status_code != 200:
                raise a2a.A2AError(f"The agent card could not be fetched (HTTP {r.status_code}).")
            data = b""
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > MAX_CARD:
                    raise a2a.A2AError("The agent card is too large.")
    return data


def _post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0), follow_redirects=False, trust_env=False) as c:
        r = c.post(url, headers=headers, content=body)
        return r.status_code, r.content[: a2a.MAX_TEXT_IN * 4]


def a2a_status(_: NoArgs, ctx: ToolContext) -> ToolResult:
    reg = _registry(ctx)
    if not reg.agents:
        return ToolResult.success("No outside agents are registered" + ("." if getattr(ctx.settings, "a2a_enabled", False) else " (and the feature is switched off: A2A_ENABLED)."), agents=0)
    lines = [f"{a.id}: {a.card.name} - {a.trust} - {len(a.card.skills)} skill(s) - {a.card.url}" for a in reg.agents.values()]
    return ToolResult.success("Outside agents (untrusted; none receives anything until approved):\n" + "\n".join(lines), agents=len(reg.agents))


class A2AArgs(ToolArgs):
    action: Literal["register", "approve", "block", "remove", "delegate"] = Field(description="register = fetch an agent's card (nothing else is sent); approve / block = trust decision; delegate = send ONE task")
    address: str = Field(default="", max_length=300, description="register: the agent's base address (https://host); its card is read from /.well-known/agent-card.json")
    agent_id: str = Field(default="", max_length=20, description="approve / block / remove / delegate: the agent id from a2a_status")
    task: str = Field(default="", max_length=4000, description="delegate: the task text to send (no passwords, keys or local paths)")


async def a2a_manage(args: A2AArgs, ctx: ToolContext) -> ToolResult:
    _enabled(ctx)
    reg = _registry(ctx)
    store = SettingsStore(ctx.database)
    try:
        if args.action == "register":
            base = args.address.strip().rstrip("/")
            problem = a2a.url_problem(base)
            if problem:
                raise a2a.A2AError(f"Not contacted: {problem}.")
            raw = await asyncio.to_thread(_get, base + a2a.WELL_KNOWN)
            import json

            card = a2a.parse_card(json.loads(raw.decode("utf-8", "replace")), raw)
            agent, note = reg.register(card)
            store.set(KEY, reg.dump())
            skills = ", ".join(s.name or s.id for s in card.skills[:5]) or "none listed"
            return ToolResult.success(f"{card.name} ({agent.id}): {note}. Skills: {skills}. Authentication it declares: {', '.join(card.auth) or 'none'}. "
                                      "Its description is the agent's own claim, not verified.", agent=agent.id)
        if args.action in ("approve", "block"):
            agent = reg.set_trust(args.agent_id, "approved" if args.action == "approve" else "blocked")
            store.set(KEY, reg.dump())
            return ToolResult.success(f"'{agent.card.name}' is now {agent.trust}.")
        if args.action == "remove":
            if not reg.remove(args.agent_id):
                raise a2a.A2AError("no such agent")
            store.set(KEY, reg.dump())
            return ToolResult.success("Removed.")
        delegator = a2a.Delegator(reg, _post)
        result = await asyncio.to_thread(delegator.delegate, args.agent_id, args.task)
        store.set(KEY, reg.dump())                                       # keeps the call counters
        try:
            from app.core import events

            events.BUS.publish(events.AGENT_TASK_COMPLETED, agent=result.agent, state=result.state, ok=result.ok)
        except Exception:
            pass
        return ToolResult.success(a2a.consolidate([result]), ok=result.ok)
    except a2a.A2AError as exc:
        raise ToolError(str(exc), "a2a_refused") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ToolError(f"The agent could not be reached or answered badly ({exc.__class__.__name__}).", "a2a_unreachable") from exc


TOOLS: tuple[Tool, ...] = (
    Tool("a2a_status", "List the outside AI agents registered with Jarvis, their skills and whether each is approved ('which agents do I have', 'agent status').", a2a_status, NoArgs, RiskLevel.SAFE, "web", timeout=10),
    Tool("a2a_manage", "Register an outside A2A agent, approve or block it, remove it, or send it one task. Outside agents are untrusted and get only the task text; their answers are shown "
         "with their source and never acted on.", a2a_manage, A2AArgs, RiskLevel.DANGEROUS, "web", timeout=150),
)
