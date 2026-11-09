"""JARVIS as an A2A (Agent2Agent) SERVER: another agent on THIS PC can ask it for a few read-only things. Off by default (A2A_SERVER_ENABLED).

It is the same trust model as the local MCP server (app/mcp/server.py), on purpose, because a stranger-agent is a bigger risk than a person at the keyboard:

* 127.0.0.1 only; requests with a foreign Host / Origin are refused (a web page in your browser cannot call it); every JSON-RPC call needs ``Authorization: Bearer
  <token>`` (only the token's sha256 is stored; ``python run.py --a2a-server-token`` shows it once). The agent card (``/.well-known/agent-card.json``) is public on
  loopback and says only what the agent is and which skills it offers.
* SKILLS = a short whitelist of tools (A2A_SERVER_TOOLS, default: a few status tools), each registered SAFE with no argument-dependent risk: nothing can write, delete,
  send, open an app or change settings through it. Calls go through the normal executor (validation, permissions, rate limit, timeout, audit) as an AI-origin call.
* There is NO free-text instruction path: a message is only accepted as a data part ``{"skill": "<id>", "arguments": {...}}`` (or a text that is exactly a skill id).
  Anything else is REJECTED with the list of skills. That is deliberate: free text from another agent would be a prompt-injection channel into the assistant.
* Answers are secret-redacted and capped; its own rate limit (20 calls / minute), body cap (64 KB), and a bounded in-memory task list (100 tasks, 1 hour).

Protocol (A2A v1.0 JSON-RPC binding): SendMessage, GetTask, ListTasks, CancelTask. SendStreamingMessage / SubscribeToTask / push notifications are not offered
(capabilities say so) and answer UnsupportedOperation. Tasks finish inside the call, so a finished task can't be cancelled (TaskNotCancelable). This was written from
the published spec and tested with local test clients and this app's own A2A client; it has NOT been tried against a third-party A2A agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from app.ai.base import ToolCall
from app.mcp.server import DEFAULT_TOOLS, MAX_BODY, MAX_REPLY, exposable, host_ok, origin_ok
from app.utils.logging_setup import get_logger, log_event
from app.utils.redaction import redact

_log = get_logger("a2a.server")
VERSION = "1.0"
TOKEN_FILE = "a2a_server.token"
RATE_PER_MINUTE = 20
MAX_TASKS, TASK_TTL_S = 100, 3600
CARD_PATH, RPC_PATH = "/.well-known/agent-card.json", "/a2a"

# JSON-RPC / A2A error codes (A2A v1.0 spec, section 5.4)
TASK_NOT_FOUND, TASK_NOT_CANCELABLE, UNSUPPORTED, EXTENDED_CARD_MISSING, VERSION_NOT_SUPPORTED = -32001, -32002, -32004, -32007, -32009
STATES = ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED",
          "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED")


# ---- token (only a hash is stored) ------------------------------------------------------------------------------------------------------------

def create_token(data_dir: Path) -> str:
    token = "ja2a_" + secrets.token_urlsafe(32)
    path = Path(data_dir) / TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(hashlib.sha256(token.encode()).hexdigest(), encoding="utf-8")
    return token


def token_hash(data_dir: Path) -> str:
    try:
        return (Path(data_dir) / TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def revoke_token(data_dir: Path) -> bool:
    try:
        (Path(data_dir) / TOKEN_FILE).unlink()
        return True
    except OSError:
        return False


def token_ok(header: str, stored_hash: str) -> bool:
    if not stored_hash or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(hashlib.sha256(header[7:].strip().encode()).hexdigest(), stored_hash)


# ---- the protocol core (no sockets: tests call handle() directly) -----------------------------------------------------------------------------

def _ok(mid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def parse_request(message: Any) -> tuple[str, str] | None:
    """The skill and its arguments requested by an incoming message, or None when it is not a skill call (-> rejected). Returns (skill, json-encoded arguments)."""
    if not isinstance(message, dict) or not isinstance(message.get("parts"), list) or not (1 <= len(message["parts"]) <= 5):
        return None
    for part in message["parts"]:
        if not isinstance(part, dict):
            continue
        data = part.get("data")
        if isinstance(data, dict) and isinstance(data.get("skill"), str):
            args = data.get("arguments") or {}
            return (data["skill"], json.dumps(args)) if isinstance(args, dict) else None
    texts = [p.get("text") for p in message["parts"] if isinstance(p, dict) and isinstance(p.get("text"), str)]
    if len(texts) == 1 and 0 < len(texts[0].strip()) <= 64 and " " not in texts[0].strip():
        return texts[0].strip(), "{}"
    return None


class A2AGateway:
    def __init__(self, registry: Any, execute: Callable[[ToolCall], Any], names: tuple[str, ...] | list[str], base_url: str = "http://127.0.0.1:8767",
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self.registry, self.execute, self.names, self.base_url, self.clock, self.wall = registry, execute, tuple(names), base_url.rstrip("/"), clock, wall
        self.calls: deque[float] = deque()
        self.tasks: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self.lock = threading.Lock()

    # -- agent card
    def card(self) -> dict[str, Any]:
        skills = []
        for name, tool in exposable(self.registry, self.names).items():
            skills.append({"id": name, "name": name.replace("_", " "), "description": tool.description[:300], "tags": ["read-only", "local"],
                           "inputModes": ["application/json"], "outputModes": ["text/plain"]})
        return {"name": "JARVIS (read-only)", "description": "A local personal assistant on one PC. It only answers a short list of read-only status skills; it cannot change anything. "
                "Send a data part {\"skill\": \"<id>\", \"arguments\": {...}}.", "version": "1",
                "supportedInterfaces": [{"url": self.base_url + RPC_PATH, "protocolBinding": "JSONRPC", "protocolVersion": VERSION}],
                "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
                "defaultInputModes": ["application/json", "text/plain"], "defaultOutputModes": ["text/plain"], "skills": skills,
                "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer", "description": "Token created on the PC with `python run.py --a2a-server-token`."}}},
                "securityRequirements": [{"schemes": {"bearer": {"list": []}}}]}

    # -- tasks
    def _rate_ok(self) -> bool:
        now = self.clock()
        while self.calls and now - self.calls[0] > 60:
            self.calls.popleft()
        if len(self.calls) >= RATE_PER_MINUTE:
            return False
        self.calls.append(now)
        return True

    def _store(self, task: dict[str, Any]) -> None:
        with self.lock:
            now = self.wall()
            for tid in [t for t, v in self.tasks.items() if now - v["_t"] > TASK_TTL_S]:
                del self.tasks[tid]
            self.tasks[task["id"]] = task
            while len(self.tasks) > MAX_TASKS:
                self.tasks.popitem(last=False)

    @staticmethod
    def _public(task: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in task.items() if not k.startswith("_")}

    def _make(self, state: str, text: str, context_id: str, artifact: str = "") -> dict[str, Any]:
        task: dict[str, Any] = {"id": str(uuid.uuid4()), "contextId": context_id or str(uuid.uuid4()),
                                "status": {"state": state, "message": {"role": "ROLE_AGENT", "parts": [{"text": text}], "messageId": str(uuid.uuid4())},
                                           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.wall()))}, "_t": self.wall()}
        if artifact:
            task["artifacts"] = [{"artifactId": str(uuid.uuid4()), "name": "result", "parts": [{"text": artifact}]}]
        return task

    def _send(self, mid: Any, params: dict[str, Any]) -> dict:
        message = params.get("message")
        context = message.get("contextId") if isinstance(message, dict) and isinstance(message.get("contextId"), str) else ""
        skills = exposable(self.registry, self.names)
        listing = ", ".join(sorted(skills)) or "(none)"
        request = parse_request(message)
        if request is None:
            task = self._make("TASK_STATE_REJECTED", "This agent does not take free-text instructions. Send a data part {\"skill\": \"<id>\", \"arguments\": {...}}. Skills: " + listing, context)
        elif request[0] not in skills:
            task = self._make("TASK_STATE_REJECTED", f"'{request[0][:40]}' is not a skill of this agent. Skills: {listing}", context)
        elif not self._rate_ok():
            task = self._make("TASK_STATE_REJECTED", "Rate limit: too many calls, slow down.", context)
        else:
            name = request[0]
            try:
                result = self.execute(ToolCall(name, json.loads(request[1]))).result
                text = redact(result.to_model_text())[:MAX_REPLY]
                task = self._make("TASK_STATE_COMPLETED" if result.ok else "TASK_STATE_FAILED", "Done." if result.ok else "The skill reported an error.", context, text)
                log_event(_log, "a2a_server_call", skill=name, ok=bool(result.ok))
            except Exception:
                _log.warning("a2a server call failed", exc_info=True)
                task = self._make("TASK_STATE_FAILED", "The skill failed.", context)
        self._store(task)
        return _ok(mid, {"task": self._public(task)})

    def handle(self, message: Any, version: str = "") -> dict | None:
        """One JSON-RPC message -> reply (None for notifications)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _err(None, -32600, "invalid request")
        mid, method, params = message.get("id"), message["method"], message.get("params") or {}
        if not isinstance(params, dict):
            return _err(mid, -32602, "invalid params")
        if mid is None:
            return None
        if version and version.strip().split(".")[:2] != VERSION.split("."):
            return _err(mid, VERSION_NOT_SUPPORTED, f"A2A version {version[:10]} is not supported (this agent speaks {VERSION})")
        if method == "SendMessage":
            return self._send(mid, params)
        if method == "GetTask":
            task = self.tasks.get(str(params.get("id", "")))
            return _ok(mid, self._public(task)) if task else _err(mid, TASK_NOT_FOUND, "task not found")
        if method == "ListTasks":
            with self.lock:
                items = [self._public(t) for t in list(self.tasks.values())[-50:]]
            return _ok(mid, {"tasks": items, "nextPageToken": "", "pageSize": len(items), "totalSize": len(items)})
        if method == "CancelTask":
            task = self.tasks.get(str(params.get("id", "")))
            return _err(mid, TASK_NOT_CANCELABLE, "the task has already finished") if task else _err(mid, TASK_NOT_FOUND, "task not found")
        if method in ("SendStreamingMessage", "SubscribeToTask") or "PushNotification" in method:
            return _err(mid, UNSUPPORTED, "streaming and push notifications are not offered by this agent")
        if method == "GetExtendedAgentCard":
            return _err(mid, EXTENDED_CARD_MISSING, "no extended agent card is configured")
        return _err(mid, -32601, "method not supported")


# ---- HTTP transport ---------------------------------------------------------------------------------------------------------------------------

class A2AServer:
    def __init__(self, registry: Any, executor: Any, data_dir: Path, port: int = 8767, names: tuple[str, ...] | list[str] = DEFAULT_TOOLS, timeout: float = 20.0) -> None:
        self.data_dir, self.port, self.timeout = Path(data_dir), port, timeout
        self._loop = asyncio.new_event_loop()
        self._executor = executor
        self.gateway = A2AGateway(registry, self._run_call, names, f"http://127.0.0.1:{port}")
        self._http: ThreadingHTTPServer | None = None

    def _run_call(self, call: ToolCall):
        return asyncio.run_coroutine_threadsafe(self._executor.execute(call), self._loop).result(self.timeout)

    def start(self) -> int:
        threading.Thread(target=self._loop.run_forever, name="a2a-server-loop", daemon=True).start()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                return

            def _send(self, code: int, body: dict | None = None) -> None:
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _guard(self) -> bool:
                if not host_ok(self.headers.get("Host", "")) or not origin_ok(self.headers.get("Origin", "")):
                    self._send(403, {"error": "forbidden"})
                    return False
                return True

            def do_GET(self) -> None:
                if not self._guard():
                    return
                if self.path.split("?")[0] == CARD_PATH:
                    return self._send(200, server.gateway.card())
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                if not self._guard():
                    return
                if self.path.split("?")[0] != RPC_PATH:
                    return self._send(404, {"error": "not found"})
                if not token_ok(self.headers.get("Authorization", ""), token_hash(server.data_dir)):
                    return self._send(401, {"error": "a valid token is required"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return self._send(400, {"error": "bad length"})
                if length <= 0 or length > MAX_BODY:
                    return self._send(413, {"error": "body too large"})
                try:
                    message = json.loads(self.rfile.read(length))
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
                reply = server.gateway.handle(message, self.headers.get("A2A-Version", ""))
                self._send(202 if reply is None else 200, reply)

        self._http = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)      # loopback only, never 0.0.0.0
        self.port = self._http.server_address[1]
        self.gateway.base_url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._http.serve_forever, name="a2a-server", daemon=True).start()
        log_event(_log, "a2a_server_started", port=self.port, skills=sorted(exposable(self.gateway.registry, self.gateway.names)))
        return self.port

    def stop(self) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
        self._loop.call_soon_threadsafe(self._loop.stop)
