"""Agent2Agent (A2A) CLIENT: discover an independent agent, decide whether it may be used, delegate one task, and treat everything it returns as untrusted.

Protocol facts used (checked against the A2A specification v1.0, a2a-protocol.org/latest/specification):
* the Agent Card is served at ``/.well-known/agent-card.json``; v1.0 lists ``supportedInterfaces`` (url + protocolBinding), ``skills`` and ``securitySchemes``
  (older 0.3 cards have a single ``url``; accepted, nothing else is guessed);
* JSON-RPC 2.0 over HTTP(S), PascalCase methods ``SendMessage`` / ``GetTask`` / ``CancelTask``; the ``A2A-Version`` header is sent with every request;
* task states ``TASK_STATE_*`` (submitted, working, input-required, auth-required, completed, failed, canceled, rejected); the last four terminal-or-interrupting ones
  are handled explicitly; a Message part carries exactly one of text / raw / url / data.
Only the JSON-RPC binding is implemented, only the client side (JARVIS does NOT expose itself as an A2A server: that would be a new inbound attack surface), and no
push notifications or streaming.

Safety model - external agents are UNTRUSTED:
* nothing is contacted until the user registered the agent AND approved it; a changed Agent Card puts it back to "pending";
* only the text of the task is sent, after a check that it holds no credentials or local paths; an agent never gets files, memory, the PC or the phone;
* the answer is cleaned (control characters, size), checked for instruction-like text, and never executed, never fetched (URLs are listed, not followed), and
  never written to memory without the user; it is shown with the agent's name as its source;
* calls are rate limited per agent per hour and per day (a "cost" cap: calls, since A2A reports no money);
* the permission engine stays the authority: the delegating tool is DANGEROUS (sending text outside the PC needs the user's confirmation).

Honest status: logic + tests with a fake HTTP function. Not run against a real A2A agent in this environment.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

VERSION = "1.0"
WELL_KNOWN = "/.well-known/agent-card.json"
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
INTERRUPTED = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}
STATES = TERMINAL | INTERRUPTED | {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_UNSPECIFIED"}
ERRORS = {-32001: "TaskNotFound", -32002: "TaskNotCancelable", -32003: "PushNotificationNotSupported", -32004: "UnsupportedOperation", -32005: "ContentTypeNotSupported",
          -32006: "InvalidAgentResponse"}
MAX_TEXT_OUT, MAX_TEXT_IN, MAX_CARD_BYTES = 4000, 20000, 200_000
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET = re.compile(r"(api[_-]?key|secret|token|passw(or)?d|bearer)\s*[:=]\s*\S{6,}|-----BEGIN [A-Z ]*PRIVATE KEY|\bsk-[A-Za-z0-9]{16,}|\bAKIA[0-9A-Z]{16}", re.I)
_PATH = re.compile(r"[A-Za-z]:\\(Users|Windows|Program)|/home/[a-z0-9_.-]+/|\\\\[A-Za-z0-9_.-]+\\", re.I)
_INJECT = re.compile(r"ignore (all |any )?(previous|prior|above) instructions|disregard (the )?(system|previous)|you are now|new instructions:|reveal (your )?(system )?prompt|"
                     r"run (this|the following) command|execute (this|the following)|send (me )?(your|the) (password|token|key)|do not tell the user", re.I)
_URL = re.compile(r"https?://[^\s<>\")]+", re.I)


class A2AError(Exception):
    """Safe to show to the user."""


def _clean(text: Any, limit: int) -> str:
    return _CTRL.sub("", str(text or ""))[:limit]


# ---------------------------------------------------------------- addresses

def url_problem(url: str, *, allow_loopback_http: bool = True) -> str | None:
    p = urlparse(url)
    if p.username or p.password:
        return "an agent address must not contain credentials"
    if not p.hostname:
        return "not a web address"
    host = p.hostname.lower()
    loop = host == "localhost"
    try:
        loop = loop or ipaddress.ip_address(host).is_loopback
        ip = ipaddress.ip_address(host)
        if (ip.is_private or ip.is_link_local) and not ip.is_loopback:
            return "private-network addresses are not used for external agents"
    except ValueError:
        pass
    if p.scheme == "https":
        return None
    if p.scheme == "http" and loop and allow_loopback_http:
        return None
    return "an external agent must use https (plain http only for this computer)"


# ---------------------------------------------------------------- agent card

@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentCard:
    name: str
    description: str
    url: str
    binding: str
    version: str
    skills: tuple[Skill, ...]
    auth: tuple[str, ...]
    digest: str                       # hash of the card as received: a change means "ask again"
    notes: tuple[str, ...] = ()


def parse_card(data: Any, raw: bytes | None = None) -> AgentCard:
    if not isinstance(data, dict):
        raise A2AError("The agent card is not a JSON object.")
    name = _clean(data.get("name"), 80).strip()
    if not name:
        raise A2AError("The agent card has no name.")
    notes: list[str] = []
    interfaces = data.get("supportedInterfaces")
    url, binding = "", ""
    if isinstance(interfaces, list):
        for it in interfaces:
            b = str((it or {}).get("protocolBinding", "")).lower().replace("-", "").replace("_", "")
            if b == "jsonrpc" and isinstance((it or {}).get("url"), str):
                url, binding = it["url"], "JSONRPC"
                break
        if not url:
            raise A2AError("This agent offers no JSON-RPC interface (only that binding is supported here).")
    elif isinstance(data.get("url"), str):
        url, binding = data["url"], "JSONRPC"
        notes.append("older card format (single url); assumed JSON-RPC")
    else:
        raise A2AError("The agent card lists no interface address.")
    problem = url_problem(url)
    if problem:
        raise A2AError(f"The agent's address is not acceptable: {problem}.")
    skills = tuple(Skill(_clean((s or {}).get("id"), 64), _clean((s or {}).get("name"), 80), _clean((s or {}).get("description"), 300),
                         tuple(_clean(t, 30) for t in ((s or {}).get("tags") or [])[:8]))
                   for s in (data.get("skills") or [])[:30] if isinstance(s, dict))
    schemes = data.get("securitySchemes")
    auth = tuple(sorted(str(k)[:40] for k in schemes)) if isinstance(schemes, dict) else ()
    digest = hashlib.sha256(raw if raw is not None else json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()
    return AgentCard(name, _clean(data.get("description"), 400), url, binding, _clean(data.get("version"), 30), skills, auth, digest, tuple(notes))


# ---------------------------------------------------------------- registry and trust

@dataclass
class Agent:
    id: str
    card: AgentCard
    trust: str = "pending"            # pending | approved | blocked
    calls: list[float] = field(default_factory=list)


class Registry:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}

    @staticmethod
    def agent_id(card: AgentCard) -> str:
        return hashlib.sha256(card.url.encode("utf-8")).hexdigest()[:12]

    def register(self, card: AgentCard) -> tuple[Agent, str]:
        """Registered agents start as 'pending'. The same address with a CHANGED card goes back to pending."""
        aid = self.agent_id(card)
        old = self.agents.get(aid)
        if old is None:
            self.agents[aid] = Agent(aid, card)
            return self.agents[aid], "registered (pending: it has not been approved to receive anything)"
        if old.card.digest != card.digest:
            self.agents[aid] = Agent(aid, card, "blocked" if old.trust == "blocked" else "pending", old.calls)
            return self.agents[aid], "its agent card changed since you last saw it: it is pending again and must be approved again"
        return old, "already registered"

    def set_trust(self, aid: str, trust: str) -> Agent:
        if trust not in ("approved", "blocked", "pending"):
            raise A2AError("trust must be approved, blocked or pending")
        agent = self.agents.get(aid)
        if agent is None:
            raise A2AError("no such agent")
        agent.trust = trust
        return agent

    def remove(self, aid: str) -> bool:
        return self.agents.pop(aid, None) is not None

    def dump(self) -> dict[str, Any]:
        return {a.id: {"trust": a.trust, "card": {**a.card.__dict__, "skills": [s.__dict__ for s in a.card.skills]}} for a in self.agents.values()}

    @classmethod
    def load(cls, data: Any) -> "Registry":
        reg = cls()
        for aid, item in (data or {}).items() if isinstance(data, dict) else ():
            try:
                c = item["card"]
                card = AgentCard(c["name"], c["description"], c["url"], c["binding"], c["version"], tuple(Skill(s["id"], s["name"], s["description"], tuple(s.get("tags", ()))) for s in c["skills"]),
                                 tuple(c["auth"]), c["digest"], tuple(c.get("notes", ())))
                reg.agents[aid] = Agent(aid, card, item.get("trust", "pending") if item.get("trust") in ("approved", "blocked", "pending") else "pending")
            except (KeyError, TypeError):
                continue
        return reg


# ---------------------------------------------------------------- what may leave, what may come back

def outgoing_problem(text: str) -> str | None:
    if not text.strip():
        return "nothing to send"
    if len(text) > MAX_TEXT_OUT:
        return f"the task is longer than {MAX_TEXT_OUT} characters"
    if _SECRET.search(text):
        return "the text looks like it contains a password, key or token"
    if _PATH.search(text):
        return "the text contains a path on this computer"
    return None


@dataclass
class Cleaned:
    text: str
    suspicious: list[str]
    urls: list[str]


def clean_result(text: Any) -> Cleaned:
    t = _clean(text, MAX_TEXT_IN)
    flags = ["instruction-like text (not followed)"] if _INJECT.search(t) else []
    return Cleaned(t, flags, _URL.findall(t)[:10])


# ---------------------------------------------------------------- JSON-RPC

PostFn = Callable[[str, dict[str, str], bytes, float], "tuple[int, bytes]"]


@dataclass
class TaskView:
    id: str
    state: str
    text: str
    artifacts: list[str]
    status_text: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def needs_input(self) -> bool:
        return self.state in INTERRUPTED


def _parts_text(parts: Any) -> str:
    out = []
    for p in parts or []:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            out.append(p["text"])
        elif isinstance(p, dict) and ("raw" in p or "url" in p):
            out.append("[a file part was returned and not opened]")
        elif isinstance(p, dict) and "data" in p:
            out.append("[structured data: " + json.dumps(p["data"])[:300] + "]")
    return "\n".join(out)


def parse_response(body: bytes) -> TaskView:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise A2AError("The agent answered with something that is not JSON.") from exc
    if not isinstance(data, dict):
        raise A2AError("The agent's answer has the wrong shape.")
    if "error" in data:
        err = data["error"] if isinstance(data["error"], dict) else {}
        code = err.get("code")
        raise A2AError(f"The agent returned an error ({ERRORS.get(code, code)}): {_clean(err.get('message'), 200)}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise A2AError("The agent's answer has no result.")
    if isinstance(result.get("task"), dict):
        result = result["task"]
    if isinstance(result.get("message"), dict):                           # a direct message answer, no task
        return TaskView("", "TASK_STATE_COMPLETED", _parts_text(result["message"].get("parts")), [])
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    state = status.get("state") if status.get("state") in STATES else "TASK_STATE_UNSPECIFIED"
    msg = status.get("message") if isinstance(status.get("message"), dict) else {}
    arts = [_parts_text(a.get("parts")) for a in (result.get("artifacts") or []) if isinstance(a, dict)]
    return TaskView(str(result.get("id", ""))[:80], state, "\n".join(x for x in arts if x), arts, _parts_text(msg.get("parts")))


def rpc_body(method: str, params: dict[str, Any]) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": uuid.uuid4().hex[:12], "method": method, "params": params}).encode("utf-8")


def send_params(text: str) -> dict[str, Any]:
    return {"message": {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"text": text}]}, "configuration": {"returnImmediately": True}}


@dataclass
class Delegation:
    agent: str
    state: str
    text: str
    status_text: str
    suspicious: list[str]
    urls: list[str]
    task_id: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "TASK_STATE_COMPLETED" and not self.error


class Delegator:
    def __init__(self, registry: Registry, post: PostFn, *, per_hour: int = 10, per_day: int = 40, timeout_s: float = 120.0, poll_s: float = 2.0,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.registry, self.post, self.per_hour, self.per_day, self.timeout_s, self.poll_s, self.clock, self.sleep = registry, post, per_hour, per_day, timeout_s, poll_s, clock, sleep

    def _call(self, agent: Agent, method: str, params: dict[str, Any]) -> TaskView:
        headers = {"Content-Type": "application/json", "A2A-Version": VERSION, "Accept": "application/json"}
        try:
            status, body = self.post(agent.card.url, headers, rpc_body(method, params), 30.0)
        except (OSError, TimeoutError) as exc:
            raise A2AError(f"Could not reach the agent ({exc.__class__.__name__}).") from exc
        if status in (401, 403):
            raise A2AError("The agent requires authentication that is not configured (credentials are never sent automatically).")
        if status >= 400 and not body:
            raise A2AError(f"The agent answered HTTP {status}.")
        return parse_response(body)

    def check(self, aid: str, text: str) -> Agent:
        agent = self.registry.agents.get(aid)
        if agent is None:
            raise A2AError("No such agent is registered.")
        if agent.trust != "approved":
            raise A2AError(f"'{agent.card.name}' is {agent.trust}: approve it first (nothing has been sent).")
        problem = outgoing_problem(text)
        if problem:
            raise A2AError(f"Not sent: {problem}.")
        now = self.clock()
        agent.calls = [t for t in agent.calls if now - t < 86400]
        if len([t for t in agent.calls if now - t < 3600]) >= self.per_hour or len(agent.calls) >= self.per_day:
            raise A2AError("Call limit for this agent reached (per hour / per day); nothing was sent.")
        return agent

    def delegate(self, aid: str, text: str) -> Delegation:
        agent = self.check(aid, text)
        agent.calls.append(self.clock())
        task = self._call(agent, "SendMessage", send_params(text))
        deadline = self.clock() + self.timeout_s
        while not task.terminal and not task.needs_input and task.id:
            if self.clock() >= deadline:
                try:
                    self._call(agent, "CancelTask", {"id": task.id})
                except A2AError:
                    pass
                return Delegation(agent.card.name, task.state, "", "", [], [], task.id, f"no answer within {self.timeout_s:.0f} s; cancellation was requested")
            self.sleep(self.poll_s)
            task = self._call(agent, "GetTask", {"id": task.id})
        c = clean_result(task.text or task.status_text)
        note = "" if task.terminal else ("the agent needs more input or authentication; nothing was provided" if task.needs_input else "")
        return Delegation(agent.card.name, task.state, c.text, _clean(task.status_text, 300), c.suspicious, c.urls, task.id, note)


# ---------------------------------------------------------------- combining answers

_NUM = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*(%|percent|ms|s|gb|mb|kb|x)?", re.I)


def find_conflicts(results: list[Delegation]) -> list[str]:
    """HEURISTIC: the same three-word context followed by clearly different numbers in two agents' answers. Flags where to look; decides nothing."""
    seen: dict[str, list[tuple[str, float]]] = {}
    for r in results:
        if not r.ok:
            continue
        for m in _NUM.finditer(r.text):
            before = re.findall(r"[A-Za-z]+", r.text[: m.start()].lower())[-3:]
            if len(before) == 3:
                try:
                    seen.setdefault(" ".join(before), []).append((r.agent, float(m.group(1).replace(",", "."))))
                except ValueError:
                    pass
    out = []
    for key, vals in seen.items():
        agents = {a for a, _ in vals}
        nums = [v for _, v in vals]
        if len(agents) > 1 and max(nums) > 0 and (max(nums) - min(nums)) / max(nums) > 0.1:
            out.append(f"After '{key}': " + ", ".join(f"{a} says {v:g}" for a, v in vals))
    return out


def consolidate(results: list[Delegation]) -> str:
    """Every answer with its source; failures and suspicious text named; possible disagreements listed. This is not a verified answer."""
    lines = ["Answers from external agents (untrusted; each is labelled with its source):"]
    for r in results:
        head = f"[{r.agent}] {r.state.replace('TASK_STATE_', '').lower()}"
        if r.error:
            lines.append(f"{head}: {r.error}")
            continue
        lines.append(f"{head}: {r.text or '(empty)'}")
        if r.suspicious:
            lines.append(f"  WARNING: {', '.join(r.suspicious)}")
        if r.urls:
            lines.append(f"  Links in the answer (not opened): {', '.join(r.urls[:3])}")
    conflicts = find_conflicts(results)
    lines.append("Possible disagreements (heuristic, check them): " + "; ".join(conflicts) if conflicts else "No numeric disagreements were noticed (this does not mean the answers are correct).")
    return "\n".join(lines)
