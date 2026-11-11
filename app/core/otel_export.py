"""Export one request's trace as OpenTelemetry (OTLP/JSON) to a local file - for any OTel-compatible viewer. No network, no new dependency.

Built from what Jarvis already records (``agent_runs`` + the audit log), so the trace is what really happened, not a reconstruction:
one root span for the request and one child span per tool call, with the GenAI semantic-convention names where they fit
(``gen_ai.operation.name = execute_tool``, ``gen_ai.tool.name``) plus ``jarvis.*`` attributes (risk level, decision, ok, error code).
Privacy: tool ARGUMENTS and the request text are left out unless you ask for them, and even then values under secret-looking names are replaced.
Result messages are never exported. Timing: a tool span ends at its audit timestamp and starts ``elapsed_ms`` earlier (the recorded duration).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

_SECRET = ("password", "token", "secret", "key", "passwd", "credential", "authorization")


def _ns(ts: str) -> int:
    try:
        return int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() * 1e9)
    except (ValueError, TypeError):
        return 0


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)[:300]}}


def redact(args: dict[str, Any]) -> dict[str, str]:
    return {str(k): ("<redacted>" if any(s in str(k).lower() for s in _SECRET) else str(v)[:120]) for k, v in (args or {}).items()}


def build(run: dict[str, Any], rows: list[dict[str, Any]], include_arguments: bool = False, service: str = "jarvis") -> dict[str, Any]:
    trace_id = hashlib.sha256(str(run["id"]).encode()).hexdigest()[:32]
    root_id = hashlib.sha256(f"{run['id']}:root".encode()).hexdigest()[:16]
    start = _ns(run.get("started_at", ""))
    end = _ns(run.get("ended_at") or "") or start + int((run.get("elapsed_ms") or 0) * 1e6)
    root_attrs = [_attr("jarvis.request.id", run["id"]), _attr("jarvis.request.source", run.get("source", "")), _attr("jarvis.request.state", run.get("state", ""))]
    if include_arguments:
        root_attrs.append(_attr("jarvis.request.text", run.get("text", "")))
    spans = [{"traceId": trace_id, "spanId": root_id, "name": "jarvis.request", "kind": 1, "startTimeUnixNano": str(start), "endTimeUnixNano": str(max(end, start)), "attributes": root_attrs,
              "status": {"code": 1 if run.get("state") in ("completed", "done", "ok") else 0}}]
    n = 0
    for r in rows:
        if r.get("event") != "tool_result":
            continue
        n += 1
        try:
            d = json.loads(r.get("details") or "{}")
        except ValueError:
            d = {}
        t_end = _ns(r.get("ts", ""))
        t_start = t_end - int((d.get("elapsed_ms") or 0) * 1e6)
        attrs = [_attr("gen_ai.operation.name", "execute_tool"), _attr("gen_ai.tool.name", r.get("tool") or ""), _attr("jarvis.tool.ok", bool(d.get("ok"))),
                 _attr("jarvis.decision", r.get("decision") or ""), _attr("jarvis.risk_level", int(r["permission_level"]) if r.get("permission_level") is not None else -1)]
        if d.get("error"):
            attrs.append(_attr("error.type", d["error"]))
        if include_arguments:
            attrs.append(_attr("gen_ai.tool.call.arguments", json.dumps(redact(d.get("arguments") or {}), ensure_ascii=False)))
        spans.append({"traceId": trace_id, "spanId": hashlib.sha256(f"{run['id']}:{n}".encode()).hexdigest()[:16], "parentSpanId": root_id, "name": f"execute_tool {r.get('tool') or ''}".strip(),
                      "kind": 1, "startTimeUnixNano": str(t_start), "endTimeUnixNano": str(t_end), "attributes": attrs, "status": {"code": 1 if d.get("ok") else 2}})
    return {"resourceSpans": [{"resource": {"attributes": [_attr("service.name", service)]}, "scopeSpans": [{"scope": {"name": "jarvis.audit"}, "spans": spans}]}]}


def export(db: Any, run_id: str, folder: Path, include_arguments: bool = False) -> tuple[Path, int]:
    with db.connect() as conn:
        run = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            raise LookupError(f"no recorded request '{run_id}'")
        rows = [dict(r) for r in conn.execute("SELECT ts, event, tool, permission_level, decision, details FROM audit_log WHERE run_id = ? ORDER BY id", (run_id,))]
    doc = build(dict(run), rows, include_arguments)
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in str(run_id) if c.isalnum() or c in "-_")[:60] or "request"
    path = folder / f"trace-{safe}.otlp.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return path, len(doc["resourceSpans"][0]["scopeSpans"][0]["spans"])
