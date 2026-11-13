"""Cognitive supervisor pieces: how much thinking a request deserves, which specialist roles it needs, whether to ask first, and a pre/post check.

Deterministic and local (no model call, no network), so it is cheap enough to run on every request and fully testable. It does NOT replace the
existing orchestrator, executor, model router or permission engine; it produces a small, explainable ``Plan`` they can consult:

* ``intents``      - splits "check my project, fix the main problems, then make sure nothing else breaks" into ordered objectives;
* ``classify``     - a reasoning LEVEL 1-5 with a budget (tokens, seconds, candidates, independent review, evidence gathering) from observable features:
                     number of objectives, hard / changing / risky verbs, length, regression wording. Low GPU memory or a power-saving mode CAPS the level
                     (and says so) instead of silently over-spending;
* ``brains``       - which of the specialist roles to wake up (research, reasoning, planning, critic, predictive, memory, verification ...);
* ``clarify``      - one short question, only when the ambiguity would change the result (an unresolved "it" next to a changing or risky verb);
* ``premortem`` / ``postcheck`` - the metacognitive checklist before and after: objective, evidence, assumptions, verification, what could go wrong;
                     a lesson is only offered for learning when the result was VERIFIED;
* ``should_stop``  - early stopping once enough independent candidates agree with enough confidence.

Honest status: the level is ADVISORY today. It is shown (``cognitive_plan`` tool, Analytics panel) and available to callers; it does not yet change which
model the conversation loop picks. Rules of thumb here are heuristics, not learned values; the thresholds are constants to tune from measured outcomes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LEVELS = {1: "instant", 2: "standard", 3: "advanced", 4: "deep", 5: "experimental"}
BUDGETS = {  # tokens, seconds, candidate approaches, independent review, gather evidence first
    1: (400, 15, 1, False, False),
    2: (1200, 40, 1, False, False),
    3: (3000, 90, 2, False, True),
    4: (6000, 240, 3, True, True),
    5: (12000, 600, 4, True, True),
}
READ = re.compile(r"\b(what|who|when|where|show|list|tell|explain|status|check|read|open|find|search|how many|how much)\b", re.I)
CHANGE = re.compile(r"\b(fix|edit|create|write|move|rename|install|update|change|set|add|build|run|start|stop|copy|save)\b", re.I)
RISK = re.compile(r"\b(delete|remove|erase|format|wipe|send|email|buy|purchase|pay|transfer|uninstall|overwrite|disable|kill|reset|publish|deploy|drop)\b", re.I)
HARD = re.compile(r"\b(investigate|diagnose|analy[sz]e|design|architect|refactor|debug|optimi[sz]e|research|compare|plan|evaluate|audit|migrate|root cause)\b", re.I)
SAFETY = re.compile(r"\b(nothing else breaks|without breaking|make sure (?:it|nothing)|regression|don'?t break|safely|verify)\b", re.I)
DEEP_WORDS = re.compile(r"\b(thoroughly|in depth|deeply|carefully|step by step|very important|critical)\b", re.I)
VERBS = r"(?:check|find|fix|make|run|send|update|create|delete|open|write|read|verify|test|list|show|summari[sz]e|email|compare|plan|install|move|rename|save)\b"
SPLIT = re.compile(r"\s*(?:;|\.\s+|,?\s+and then\s+|,?\s+then\s+|,?\s+after that\s+|,?\s+also\s+|,?\s+finally\s+|,\s+(?:and\s+)?(?=" + VERBS + r")|\s+and\s+(?=" + VERBS + r"))", re.I)
REFERENCE = re.compile(r"\b(it|that|this|them|those|these|the file|the folder|the project|the one)\b", re.I)


@dataclass
class Plan:
    level: int
    objectives: list[str]
    reasons: list[str]
    budget: dict
    brains: list[str]
    capped_by: str = ""
    clarification: str = ""
    risky: bool = False

    @property
    def name(self) -> str:
        return LEVELS[self.level]

    def text(self) -> str:
        b = self.budget
        lines = [f"Reasoning level {self.level} ({self.name}): " + "; ".join(self.reasons) + ".",
                 f"Budget: about {b['tokens']} tokens, {b['seconds']} s, {b['candidates']} candidate approach(es)"
                 + (", independent review" if b["review"] else "") + (", gather evidence first" if b["evidence"] else "") + "."]
        if self.capped_by:
            lines.append(f"Capped: {self.capped_by}.")
        if len(self.objectives) > 1:
            lines.append("Objectives, in order: " + " -> ".join(f"({i}) {o}" for i, o in enumerate(self.objectives, 1)))
        lines.append("Roles to use: " + ", ".join(self.brains) + ".")
        if self.clarification:
            lines.append("Ask first: " + self.clarification)
        lines.append("This level is advisory; permissions and approvals are unchanged.")
        return "\n".join(lines)


def intents(text: str) -> list[str]:
    parts = [p.strip(" ,.") for p in SPLIT.split(text or "") if p and p.strip(" ,.") and not re.fullmatch(r"(?i)and|then|also|finally", p.strip(" ,."))]
    return [p for p in parts if len(p) > 2][:8] or ([text.strip()] if (text or "").strip() else [])


def brains(objectives: list[str], level: int, risky: bool, changes: bool, hard: bool, research: bool = False) -> list[str]:
    out = ["context"]
    if level >= 2 or hard:
        out.append("reasoning")
    if len(objectives) > 1 or level >= 3:
        out.append("planning")
    if research or re.search(r"\b(research|source|evidence|compare|documentation|latest)\b", " ".join(objectives), re.I):
        out.append("research")
    if level >= 3:
        out.append("memory")
    if level >= 4 or risky:
        out.append("critic")
    if level >= 4:
        out.append("predictive")
    if changes or risky:
        out.append("verification")
    return list(dict.fromkeys(out))


def clarify(text: str, objectives: list[str], known_referents: tuple[str, ...] = ()) -> str:
    """A question only when an unresolved reference sits next to a changing or risky verb: guessing could do the wrong thing."""
    t = text or ""
    if not (CHANGE.search(t) or RISK.search(t)):
        return ""
    m = REFERENCE.search(t)
    if m and not known_referents and len(t.split()) <= 8:
        return f"Which one do you mean by '{m.group(0)}'?"
    if RISK.search(t) and len(t.split()) <= 3 and not known_referents:
        return "What exactly should it be applied to?"
    return ""


def classify(text: str, *, vram_free_gb: float | None = None, mode: str = "", known_referents: tuple[str, ...] = ()) -> Plan:
    t = text or ""
    objectives = intents(t)
    reasons: list[str] = []
    level = 1
    n = len(objectives)
    risky = bool(RISK.search(t))
    changes = bool(CHANGE.search(t))
    hard = bool(HARD.search(t))
    words = len(t.split())
    if n >= 2:
        level += 1
        reasons.append(f"{n} connected objectives")
    if hard:
        level += 1
        reasons.append("analysis / design wording")
    if changes and n >= 2:
        level += 1
        reasons.append("changes things in several steps")
    if SAFETY.search(t):
        level += 1
        reasons.append("must not break anything else")
    if words > 60:
        level += 1
        reasons.append("long request")
    if risky:
        if level < 3:
            level = 3
        reasons.append("an error would be costly (destructive or outward-facing verb)")
    if DEEP_WORDS.search(t):
        level += 1
        reasons.append("you asked for care")
    if not reasons:
        reasons.append("a simple request" if (READ.search(t) or words <= 12) else "ordinary request")
        level = 2 if words > 12 else 1
    level = max(1, min(5, level))
    if level == 5 and not (hard and n >= 3) and not DEEP_WORDS.search(t):
        level = 4
    capped = ""
    cap = 5
    if mode in ("low_resource", "battery", "low-resource", "battery_saver"):
        cap, capped = 2, "a power-saving mode is on"
    elif vram_free_gb is not None and vram_free_gb < 2.0:
        cap, capped = 3, f"only {vram_free_gb:.1f} GB of GPU memory is free"
    if level > cap:
        level = cap
    else:
        capped = ""
    tokens, secs, cands, review, evidence = BUDGETS[level]
    budget = {"tokens": tokens, "seconds": secs, "candidates": cands, "review": review, "evidence": evidence}
    return Plan(level, objectives, reasons, budget, brains(objectives, level, risky, changes, hard), capped, clarify(t, objectives, known_referents), risky)


def should_stop(confidences: list[float], level: int, agree: int | None = None) -> bool:
    """Early stopping: enough independent candidates, the best clearly confident, and (when counted) a majority agree."""
    need = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3}[level]
    if len(confidences) < need:
        return False
    best = max(confidences)
    if best < {1: 0.6, 2: 0.7, 3: 0.75, 4: 0.8, 5: 0.85}[level]:
        return False
    return agree is None or agree * 2 > len(confidences)


@dataclass
class Check:
    question: str
    answer: str
    ok: bool


def premortem(plan: Plan, *, evidence_have: bool = False, assumptions: list[str] | None = None, verify_with: str = "") -> list[Check]:
    """The questions Jarvis should be able to answer BEFORE an important step. ok=False means: stop and resolve it first."""
    assumptions = assumptions or []
    out = [Check("Do I understand the objective?", "; ".join(plan.objectives)[:160] or "no", bool(plan.objectives)),
           Check("Do I have enough information?", "yes" if (evidence_have or not plan.budget["evidence"]) else "not yet: gather evidence first", evidence_have or not plan.budget["evidence"]),
           Check("Which assumptions am I making?", "; ".join(assumptions) or "none stated", True),
           Check("Is a clarification needed?", plan.clarification or "no", not plan.clarification),
           Check("Should another role review this?", "yes (critic / verification)" if plan.budget["review"] or plan.risky else "no", True),
           Check("What could go wrong?", "a costly or hard-to-undo result" if plan.risky else "a wrong or incomplete result", True),
           Check("How will I verify the result?", verify_with or "not defined yet", bool(verify_with) or not (plan.risky or "verification" in plan.brains))]
    return out


@dataclass
class Post:
    completed: bool
    verified: bool
    plan_worked: bool
    unexpected: str = ""
    lesson: str = ""
    learnable: bool = False
    notes: list[str] = field(default_factory=list)


def postcheck(completed: bool, verified: bool, plan_worked: bool, unexpected: str = "", lesson: str = "") -> Post:
    """After the task. A lesson is only offered for learning when the outcome was verified - never from an unchecked success."""
    p = Post(completed, verified, plan_worked, unexpected, lesson)
    if completed and not verified:
        p.notes.append("completed but NOT verified: do not report it as done and do not learn from it")
    p.learnable = bool(lesson) and verified and completed
    if lesson and not p.learnable:
        p.notes.append("lesson held back until the result is verified")
    if unexpected:
        p.notes.append("unexpected: " + unexpected[:120])
    return p
