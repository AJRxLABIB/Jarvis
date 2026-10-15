"""Explainability: "why did you do that?" answered from what Jarvis recorded, never invented.

Every request is a run (app/core/runs.py). Every check the executor makes is written to the audit log with that
run's id: the risk level, whether it asked you and why, whether a plan was pre-approved, whether an agent contract
refused a tool, and whether the result was verified. This module turns that trace into plain sentences.

It does not claim to know the model's inner reasoning. It reports which actions ran for your request and why each
safety decision was made, because those are recorded facts. If something was not recorded, it says so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

LEVELS = {0: "safe", 1: "moderate", 2: "dangerous", 3: "critical"}
SOURCES = {"voice": "by voice", "typed": "typed", "text": "typed", "phone": "from your phone",
           "mobile": "from your phone", "automation": "by an automation", "mission": "as part of a mission"}
ERRORS = {
    "declined": "you said no, so it was not done",
    "blocked": "it was blocked by your permission settings or the emergency controls",
    "rate_limited": "too many actions came too fast, so this one was held back",
    "offline": "it needed the internet and the PC was offline",
    "agent_not_allowed": "the agent handling the task is not allowed to do that",
    "not_verified": "it ran, but the check afterwards showed it had not worked",
    "invalid_arguments": "the request was malformed, so nothing was run",
    "unknown_tool": "there is no such action",
}


@dataclass
class Step:
    tool: str
    level: int | None = None
    ok: bool | None = None
    error: str | None = None
    message: str = ""
    asked: bool = False
    answer: str | None = None      # "approved" / "denied"
    asked_because: str = ""        # e.g. the taint reason
    preapproved: bool = False      # part of a plan you approved once
    direct: bool = False           # a direct command in your own words, so it did not ask again
    refused_by: str = ""           # agent contract reason
    verified: bool | None = None
    verify_note: str = ""


def _details(entry: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(entry.get("details") or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def steps_from_trace(trace: list[dict[str, Any]]) -> list[Step]:
    """Group the audit entries of one run into one Step per tool call, in order."""
    steps: list[Step] = []
    open_steps: dict[str, Step] = {}
    for entry in trace:
        tool = entry.get("tool") or ""
        if not tool:
            continue
        step = open_steps.get(tool)
        if step is None:
            step = Step(tool)
            open_steps[tool] = step
            steps.append(step)
        details = _details(entry)
        event = entry.get("event")
        level = entry.get("permission_level")
        if level is not None:
            step.level = int(level)
        if event == "confirmation_requested":
            step.asked = True
        elif event == "confirmation_answered":
            step.answer = entry.get("decision")
        elif event == "taint_confirmation":
            step.asked_because = str(details.get("reason", ""))
        elif event == "confirmation_preapproved":
            step.preapproved = True
        elif event == "confirmation_skipped":
            step.direct = True
        elif event == "agent_contract_denied":
            step.refused_by = str(details.get("reason", ""))
        elif event == "verification":
            step.verified = bool(details.get("ok"))
            step.verify_note = str(details.get("found", ""))
        elif event == "tool_result":
            step.ok = bool(details.get("ok"))
            step.error = details.get("error")
            step.message = str(details.get("message") or "")
            del open_steps[tool]  # the next call to the same tool is a new step
    return steps


def describe_step(step: Step) -> str:
    level = LEVELS.get(step.level, "unknown") if step.level is not None else "unknown"
    parts = [f"{step.tool} ({level} risk)"]
    if step.refused_by:
        parts.append(f"was refused: {step.refused_by}")
        return " ".join(parts) + "."
    if step.asked:
        why = f" because {step.asked_because}" if step.asked_because else ""
        answer = {"approved": "you approved it", "denied": "you said no"}.get(step.answer or "", "no answer was recorded")
        parts.append(f"- I asked you first{why}; {answer}")
    elif step.preapproved:
        parts.append("- it was part of a plan you had approved, so I did not ask again")
    elif step.direct:
        parts.append("- you asked for it directly, so I did not ask again")
    elif step.level == 0:
        parts.append("- safe, so it ran without asking")
    if step.ok is True:
        parts.append("and it worked")
    elif step.ok is False:
        parts.append("but " + ERRORS.get(step.error or "", f"it failed ({step.error or 'no reason recorded'})"))
    if step.verified is True:
        parts.append("(checked afterwards: confirmed)")
    elif step.verified is False and step.error != "not_verified":
        parts.append(f"(checked afterwards: {step.verify_note or 'not confirmed'})")
    return " ".join(parts) + "."


def explain_run(run: dict[str, Any], trace: list[dict[str, Any]]) -> tuple[str, list[Step]]:
    """(plain explanation, the steps it is based on)."""
    source = SOURCES.get(str(run.get("source", "")), str(run.get("source") or "somehow"))
    text = str(run.get("text") or "").strip()
    head = f'You asked "{text[:160]}" ({source}).' if text else f"That request came {source}."
    steps = steps_from_trace(trace)
    if not steps:
        return head + " No action was taken for it - I only answered.", steps
    body = " ".join(f"{i}. {describe_step(s)}" for i, s in enumerate(steps, 1))
    note = (" I can explain the safety decisions because they are recorded; I cannot replay the model's own "
            "reasoning for picking these actions.")
    return f"{head} What I did: {body}{note}", steps


def pick_run(runs: list[dict[str, Any]], *, exclude: str | None = None, about: str = "") -> dict[str, Any] | None:
    """The most recent finished run that did something (skipping the current one). With ``about``, prefer the
    newest run whose request text or tools mention it."""
    candidates = [r for r in runs if r.get("id") != exclude and (r.get("tools") or "").strip()]
    if about:
        word = about.lower().strip()
        for run in candidates:
            if word in str(run.get("text", "")).lower() or word in str(run.get("tools", "")).lower():
                return run
    return candidates[0] if candidates else None
