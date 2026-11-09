"""Why did a run fail? Names the EARLIEST failure, ranks the possible causes by the evidence in the record, and offers recovery options with a level.

Builds on ``core/replay.py`` (the reconstructed timeline and its first divergence) and ``core/retry.py`` (error kinds). Adds what was missing:

* twelve cause classes, each hypothesis carrying the evidence lines that support it and a confidence that is never "certain" - a record only ever shows
  symptoms. A later failing call is listed as "possibly a consequence", never claimed to be one.
* recovery LEVELS: 0 observe, 1 harmless diagnostic, 2 reversible change (needs authorisation by policy), 3 high impact (explicit confirmation),
  4 forbidden (never offered, never run: weakening security, bypassing sign-in, deleting the audit trail, widening permissions, unrestricted commands).
  ``may_run_unasked`` is True only for levels 0 and 1. This module proposes and classifies; it never runs anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core import replay, retry

CLASSES = {
    "incorrect_planning": "the plan itself was wrong (an unknown tool, or a step the situation did not allow)",
    "invalid_tool_arguments": "the arguments were missing or rejected",
    "tool_implementation_failure": "the tool itself broke (an internal error rather than a wrong request)",
    "permission_failure": "a permission or approval stopped it",
    "resource_exhaustion": "memory, disk or another resource ran out",
    "model_misunderstanding": "the model produced a malformed or mistaken call",
    "incorrect_visual_interpretation": "what was read from the screen did not match the screen",
    "application_state_mismatch": "the application was not in the state the step assumed",
    "network_failure": "the network or a connection failed",
    "dependency_failure": "something the tool depends on is missing or broken",
    "external_service_failure": "a service it called (for example the local AI server) was unavailable or errored",
    "incomplete_verification": "the action ran, but its effect was not confirmed afterwards",
}
LEVELS = {0: "observe only", 1: "harmless diagnostic", 2: "reversible change (authorisation by policy)", 3: "high impact (explicit confirmation)", 4: "forbidden"}

_FORBIDDEN = re.compile(r"(disable|turn off|stop|uninstall)\s+(the\s+)?(antivirus|defender|firewall|security|guardian|emergency)|bypass|skip\s+(the\s+)?(sign-?in|login|approval|confirm)|"
                        r"(delete|clear|wipe|truncate|edit)\s+(the\s+)?(audit|log\s*chain|history of approvals)|(grant|widen|raise|change)\s+(all\s+)?permissions?|exfiltrat|"
                        r"rm\s+-rf|format\s+[a-z]:|run\s+any(thing)?\s+command|unrestricted", re.I)
_HIGH = re.compile(r"(delete|remove|uninstall|overwrite|format|shut\s*down|restart|reboot|send|pay|purchase|install)", re.I)
_CHANGE = re.compile(r"(set|change|edit|write|create|restore|re-?run|retry|repeat|fix|update|reinstall|enable|clear cache|restart the (app|service))", re.I)


def level_of(action: str) -> int:
    """The level of a proposed action, from its wording. Unknown wording is treated as a change (2), never as harmless."""
    if _FORBIDDEN.search(action):
        return 4
    if _HIGH.search(action):
        return 3
    if _CHANGE.search(action):
        return 2
    if re.search(r"^(look|check|read|inspect|list|show|compare|collect|measure|wait|ask)\b", action.strip(), re.I):
        return 1 if re.search(r"^(check|read|inspect|list|collect|measure|compare|look)", action.strip(), re.I) else 0
    return 2


def may_run_unasked(level: int) -> bool:
    return level in (0, 1)


@dataclass
class Hypothesis:
    cls: str
    evidence: list[str]
    score: float
    confidence: str            # low | medium | high  (never certain)

    @property
    def explains(self) -> str:
        return CLASSES[self.cls]


@dataclass
class Option:
    level: int
    text: str

    @property
    def allowed_unasked(self) -> bool:
        return may_run_unasked(self.level)


@dataclass
class Diagnosis:
    run_id: str
    first: replay.Call | None
    hypotheses: list[Hypothesis] = field(default_factory=list)
    options: list[Option] = field(default_factory=list)
    later_failures: list[int] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.first is None

    def describe(self) -> str:
        if self.first is None:
            return f"Run {self.run_id}: no failing step in the record" + (f" (but note: {'; '.join(self.gaps)})." if self.gaps else ".")
        c = self.first
        out = [f"Run {self.run_id}: the first step that did not do what it should was #{c.n} '{c.tool}'."]
        if self.hypotheses:
            out.append("Possible causes, strongest evidence first (a record shows symptoms, so none of these is certain):")
            for h in self.hypotheses[:4]:
                out.append(f"  - {h.cls.replace('_', ' ')} [{h.confidence}]: {h.explains}. Evidence: {'; '.join(h.evidence)}")
        else:
            out.append("The record does not show enough to name a cause. Nothing should be changed until more evidence is collected.")
        if self.later_failures:
            out.append("Later failing steps (possibly consequences of #%d, not proven): %s." % (c.n, ", ".join(f"#{n}" for n in self.later_failures)))
        if self.options:
            out.append("Recovery options:")
            out += [f"  - L{o.level} ({LEVELS[o.level]}): {o.text}" + ("" if o.allowed_unasked else " - needs your OK") for o in self.options]
        if self.gaps:
            out.append("Gaps in the record: " + "; ".join(self.gaps) + ".")
        return "\n".join(out)


def _text(c: replay.Call) -> str:
    return f"{c.error or ''} {c.message or ''} {c.verify_note or ''}".lower()


def hypotheses_for(c: replay.Call) -> list[Hypothesis]:
    """Every class the evidence supports for ONE call, strongest first."""
    t, code = _text(c), (c.error or "").lower()
    policy = retry.classify(c.error, c.message)
    found: dict[str, tuple[float, list[str]]] = {}

    def add(cls: str, weight: float, why: str) -> None:
        score, ev = found.get(cls, (0.0, []))
        found[cls] = (score + weight, ev + [why])

    if c.refused_by or policy == "permission":
        add("permission_failure", 0.9, f"refused/blocked ({c.refused_by or code})")
    if code == "unknown_tool":
        add("incorrect_planning", 0.8, "the tool name does not exist")
        add("model_misunderstanding", 0.3, "an unknown tool name is also what a mistaken model call looks like")
    elif policy == "validation":
        add("invalid_tool_arguments", 0.8, f"rejected as invalid ({code or 'validation message'})")
        add("model_misunderstanding", 0.3, "malformed arguments often come from the model")
        if code in ("ambiguous", "not_found"):
            add("incorrect_planning", 0.3, "the step referred to something that was not there or not unique")
    if policy == "resource" or re.search(r"out of memory|not enough memory|disk full|no space", t):
        add("resource_exhaustion", 0.85, "memory/disk shortage reported")
    if re.search(r"connection|offline|dns|unreachable|network|timed out|timeout", t) or code in ("offline", "timeout"):
        add("network_failure", 0.55 if "timeout" in t or code == "timeout" else 0.75, "connection/timeout wording in the result")
    if re.search(r"ollama|503|502|service unavailable|api error|server error|embedding_unavailable", t):
        add("external_service_failure", 0.7, "a service it calls reported an error")
    if re.search(r"no module named|not installed|modulenotfound|dll|missing dependency|could not find .*(executable|library)", t):
        add("dependency_failure", 0.8, "a missing module, library or program")
    if re.search(r"ocr|on screen|screenshot|could not (see|find).*(button|text|element)|not_visible|coordinates", t) or code == "not_visible":
        add("incorrect_visual_interpretation", 0.6, "the step relied on reading the screen and the element was not found")
    if re.search(r"window|focus|not open|still open|already (open|running)|dialog|state", t) or code in ("still_open", "still_exists"):
        add("application_state_mismatch", 0.55, "the application was not in the expected state")
    if code == "internal" or re.search(r"traceback|exception|unexpected error|typeerror|keyerror|attributeerror", t):
        add("tool_implementation_failure", 0.7, "an internal error rather than a rejected request")
    if code == "not_verified" or (c.ok and c.verified is False):
        add("incomplete_verification", 0.8, "ran, but the check afterwards did not confirm the effect")
    if c.ok is None:
        add("tool_implementation_failure", 0.2, "no result was recorded (a crash or stop is more likely than a tool bug)")
    out = []
    for cls, (score, ev) in found.items():
        score = min(score, 0.95)
        out.append(Hypothesis(cls, ev, round(score, 2), "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"))
    return sorted(out, key=lambda h: -h.score)


def options_for(c: replay.Call, hyps: list[Hypothesis]) -> list[Option]:
    top = hyps[0].cls if hyps else ""
    opts = [Option(1, f"Check the real state of what '{c.tool}' was meant to change before doing anything else")]
    table = {
        "permission_failure": [Option(0, "Decide whether this action should be allowed at all; no permission is widened to get past it"), Option(3, "Ask again and approve it on screen if you do want it")],
        "invalid_tool_arguments": [Option(0, "Correct the missing or invalid detail, then ask again")],
        "resource_exhaustion": [Option(1, "Check free RAM / VRAM / disk (resources, system_status)"), Option(2, "Close heavy programs or pick a smaller model, then retry")],
        "network_failure": [Option(1, "Check connectivity and whether the other end answers"), Option(2, "Retry once the connection is back (only if the action is safe to repeat)")],
        "external_service_failure": [Option(1, "Check the service is running (service_health)"), Option(2, "Restart the service, then retry")],
        "dependency_failure": [Option(1, "Run the plugin/dependency check"), Option(3, "Install the missing dependency (needs your confirmation)")],
        "incorrect_visual_interpretation": [Option(1, "Take a fresh screenshot and re-read the screen; the old coordinates are stale"), Option(2, "Retry with the accessibility (UI Automation) route")],
        "application_state_mismatch": [Option(1, "Look at the window as it is now"), Option(2, "Bring the application to the expected state, then retry")],
        "incomplete_verification": [Option(1, "Look at the real state: the check, not the action, may be what failed")],
        "tool_implementation_failure": [Option(1, "Collect the log for this call and see whether it repeats"), Option(0, "Record it as a known issue; never patch the code automatically from this diagnosis")],
        "incorrect_planning": [Option(0, "Ask again with a clearer goal; an unknown step cannot be repaired by repeating it")],
        "model_misunderstanding": [Option(0, "Rephrase the request or give the missing detail")],
    }
    opts += table.get(top, [])
    if c.repeatable:
        opts.append(Option(1, "Re-run this read-only call once to see whether it was temporary"))
    return [o for o in opts if o.level != 4]                       # level 4 is never offered


def diagnose(timeline: replay.Timeline) -> Diagnosis:
    div = replay.find_divergence(timeline)
    if div is None:
        return Diagnosis(timeline.run_id, None, gaps=list(timeline.gaps))
    first = next(c for c in timeline.calls if c.n == div.call)
    hyps = hypotheses_for(first)
    later = [c.n for c in timeline.calls if c.n > first.n and (c.ok is False or c.refused_by or c.verified is False)]
    return Diagnosis(timeline.run_id, first, hyps, options_for(first, hyps), later, list(timeline.gaps))


def evidence_record(d: Diagnosis) -> dict[str, Any]:
    """What to keep as a known-issue record (no arguments, no file contents: only the tool, the class and the code)."""
    if d.first is None:
        return {}
    return {"run": d.run_id, "step": d.first.n, "tool": d.first.tool, "error": d.first.error, "class": d.hypotheses[0].cls if d.hypotheses else "unknown",
            "confidence": d.hypotheses[0].confidence if d.hypotheses else "none"}
