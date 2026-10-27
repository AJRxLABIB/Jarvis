"""Goal contracts: turn an informal request into an explicit, checkable goal - and later compare what really
happened against it.

Three parts, all deterministic (no model call, no network, nothing stored):

* ``compile``  - request -> outcome, constraints you stated, what would count as done (acceptance criteria), the
  assumptions made, and the questions that MATERIALLY matter. A question is raised only when guessing could hurt
  (a destructive action with no target, which of several projects, when a reminder should fire); otherwise a stated,
  reversible assumption is used instead of nagging.
* ``check_plan`` - a plan that breaks something you said ("without installing anything", "don't delete") is reported
  before it runs.
* ``evaluate`` - the evidence ledger: every criterion is judged from the evidence actually collected as verified,
  strongly supported, tentative, unknown or contradicted. No percentages are invented: there is no calibration data
  to justify them. A goal whose acceptance criteria could not be derived is "unverifiable", never "complete".

Evidence kinds, strongest first: ``verified`` (a tool's own checked result), ``observation`` (seen directly),
``user`` (you said so), ``document`` (found in a local file), ``hypothesis`` (a model's guess). A model's claim can
never make a criterion verified.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

# ------------------------------------------------------------------ evidence

DIRECT = ("verified", "observation")
KINDS = ("verified", "observation", "user", "document", "hypothesis")
VERIFIED, SUPPORTED, TENTATIVE, UNKNOWN, CONTRADICTED = "verified", "strongly supported", "tentative", "unknown", "contradicted"


@dataclass(frozen=True)
class Evidence:
    claim: str                 # the criterion key it speaks to
    kind: str                  # one of KINDS
    supports: bool = True
    source: str = ""           # e.g. "step 2: open_application"
    at: str = ""               # ISO time; newer direct evidence beats older


def judge(claim: str, evidence: Iterable[Evidence]) -> str:
    """The status of one claim. The newest direct observation decides; without one, the weaker kinds speak."""
    mine = [e for e in evidence if e.claim == claim and e.kind in KINDS]
    if not mine:
        return UNKNOWN
    direct = sorted((e for e in mine if e.kind in DIRECT), key=lambda e: e.at)
    if direct:
        return VERIFIED if direct[-1].supports else CONTRADICTED
    soft = [e for e in mine if e.kind in ("user", "document")]
    if soft:
        newest = sorted(soft, key=lambda e: e.at)[-1]
        return SUPPORTED if newest.supports else CONTRADICTED
    return TENTATIVE          # only hypotheses: worth checking, never a result


# ------------------------------------------------------------------ the contract


@dataclass(frozen=True)
class Criterion:
    key: str
    text: str
    tools: tuple[str, ...]          # a successful call of one of these (with `subject` in its arguments) is evidence
    subject: str = ""
    required: bool = True


@dataclass(frozen=True)
class Question:
    text: str
    why: str                         # safety | correctness | privacy | cost


@dataclass(frozen=True)
class Conflict:
    kind: str                        # duplicate | busy_day | violates_constraint
    text: str


@dataclass(frozen=True)
class Active:
    """Something already on your plate (a mission, to-do, reminder or goal) that a new request is compared with."""
    kind: str
    title: str
    due: date | None = None


@dataclass
class GoalContract:
    request: str
    outcome: str
    constraints: dict[str, str] = field(default_factory=dict)
    criteria: list[Criterion] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    project: str = ""
    deadline: date | None = None

    @property
    def can_proceed(self) -> bool:
        return not self.questions

    def describe(self) -> str:
        lines = [f"Goal: {self.outcome}"]
        if self.project:
            lines.append(f"Project: {self.project}")
        if self.deadline:
            lines.append(f"Deadline: {self.deadline.isoformat()}")
        if self.constraints:
            lines.append("You said: " + "; ".join(self.constraints.values()))
        if self.criteria:
            lines.append("Done when: " + "; ".join(c.text + ("" if c.required else " (optional)") for c in self.criteria))
        else:
            lines.append("I can't tell what would count as done - say what the result should be.")
        for a in self.assumptions:
            lines.append(f"Assuming: {a}")
        for r in self.risks:
            lines.append(f"Care: {r}")
        for c in self.conflicts:
            lines.append(f"Conflict ({c.kind}): {c.text}")
        for q in self.questions:
            lines.append(f"Question ({q.why}): {q.text}")
        return "\n".join(lines)


# ------------------------------------------------------------------ compiling

_WORD = re.compile(r"[a-z0-9][a-z0-9.+#_-]*")
_STOP = {"the", "a", "an", "my", "me", "to", "please", "jarvis", "and", "then", "for", "of", "in", "on", "it", "that", "this",
         "can", "you", "could", "would", "i", "want", "need", "now"}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

_CONSTRAINTS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("no_install", re.compile(r"\b(?:without|don'?t|do not|no)\s+(?:install\w*|download\w*)", re.I), "install nothing"),
    ("no_delete", re.compile(r"\b(?:don'?t|do not|never|without)\s+(?:delet\w+|remov\w+|erasing)|\bkeep (?:everything|all)", re.I),
     "delete nothing"),
    ("local_only", re.compile(r"\b(?:offline|locally|no internet|without (?:the )?internet|don'?t (?:use )?(?:the )?(?:internet|web))", re.I),
     "stay offline / local"),
    ("ask_first", re.compile(r"\b(?:ask me (?:first|before)|check with me|confirm with me)", re.I), "ask you before acting"),
    ("read_only", re.compile(r"\b(?:just (?:look|check|read|show)|read[- ]only|don'?t change anything|without changing)", re.I),
     "change nothing"),
)
# a tool a constraint forbids
_FORBIDS = {"no_install": ("install_software", "winget_install", "pip_install"), "no_delete": ("delete_item", "delete_todo"),
            "local_only": ("research_project", "web_search", "get_news", "get_weather", "browser_open", "browser_read"),
            "read_only": ("delete_item", "move_item", "edit_code", "safe_edit", "git_commit", "send_email", "set_reminder",
                          "add_todo", "create_file", "write_file")}

_OPEN = re.compile(r"\b(?:open|launch|start)\s+(?:up\s+)?(?:the\s+|my\s+)?([a-z][a-z0-9 .+#_-]{1,30}?)(?:\s+(?:and|then|so|to)\b|[,.!?]|$)", re.I)
_REMIND = re.compile(r"\bremind(?:er)?\b|\bset (?:a |an )?(?:reminder|alarm)", re.I)
_TODO = re.compile(r"\b(?:add|create|make|put)\b.*\b(?:to-?do|task)\b|\bto-?do\b.*\badd\b", re.I)
_TESTS = re.compile(r"\b(?:run|do|check)\b.*\b(?:tests?|lint|checks?)\b", re.I)
_GIT = re.compile(r"\bgit status\b|\bwhat changed\b.*\b(?:repo|project)\b", re.I)
_DELETE = re.compile(r"\b(?:delete|remove|erase|wipe|clean\s*up|get rid of|clear out)\b", re.I)
_EMAIL = re.compile(r"\b(?:send|email|mail)\b.*\b(?:email|mail|message)\b|\bemail\b", re.I)
_DEV_READY = re.compile(r"\b(?:ready|prepare|set up|setup|get)\b.*\b(?:develop\w*|dev environment|coding|workspace)\b", re.I)
_PROJECT_REF = re.compile(r"\b(?:my|the|this)\s+project\b|\bdevelopment\b|\bdev environment\b|\bfinish my project\b", re.I)
_TIME = re.compile(r"\b(?:at|by|in|on|tomorrow|today|tonight|next|every|daily|weekly|\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d+\s*(?:min|minute|hour|day)s?)\b", re.I)
_CONCRETE_TARGET = re.compile(r"(?:[a-z]:\\|/|\\|\.\w{2,4}\b|\bfile\s+\w|\bfolder\s+\w|\b(?:called|named)\s+\w|\"[^\"]+\"|'[^']+')", re.I)


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1}


def parse_deadline(text: str, today: date) -> date | None:
    t = text.lower()
    if re.search(r"\btoday\b|\btonight\b", t):
        return today
    if re.search(r"\btomorrow\b", t):
        return today + timedelta(days=1)
    if re.search(r"\b(?:this week|by the end of the week|end of (?:the )?week)\b", t):
        return today + timedelta(days=(4 - today.weekday()) % 7)      # that Friday (today if it is Friday)
    if re.search(r"\bnext week\b", t):
        return today + timedelta(days=7 - today.weekday() + 4)
    m = re.search(r"\b(?:by|on|before|until)\s+(?:next\s+)?(" + "|".join(_WEEKDAYS) + r")\b", t)
    if m:
        ahead = (_WEEKDAYS.index(m.group(1)) - today.weekday()) % 7
        return today + timedelta(days=ahead or 7)
    m = re.search(r"\b(?:by|on|before)\s+(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def compile(request: str, *, projects: Iterable[str] = (), active: Iterable[Active] = (), today: date | None = None,
            known_app: Callable[[str], str | None] | None = None) -> GoalContract:
    text = " ".join(request.split())
    today = today or datetime.now().date()
    contract = GoalContract(request=text, outcome=text.rstrip(" .!?"))
    projects = [p for p in projects if p]

    for key, pattern, label in _CONSTRAINTS:
        if pattern.search(text):
            contract.constraints[key] = label
    contract.deadline = parse_deadline(text, today)

    named = [p for p in projects if p.lower() in text.lower()]
    needs_project = bool(_PROJECT_REF.search(text) or _DEV_READY.search(text))
    if named:
        contract.project = named[0]
    elif needs_project:
        if len(projects) == 1:
            contract.project = projects[0]
            contract.assumptions.append(f"you mean the project {projects[0]} (the only one I know about)")
        elif len(projects) > 1:
            contract.questions.append(Question("Which project: " + ", ".join(projects[:5]) + "?", "correctness"))
        else:
            contract.questions.append(Question("I don't know any project yet - which folder is it?", "correctness"))

    _criteria(contract, text, known_app)
    _clarify(contract, text)
    _conflicts(contract, active)
    return contract


def _criteria(contract: GoalContract, text: str, known_app: Callable[[str], str | None] | None) -> None:
    add = contract.criteria.append
    for match in _OPEN.finditer(text):
        name = match.group(1).strip().lower()
        if name in ("a", "an", "the", "it", "up") or (known_app is not None and known_app(name) is None
                                                       and name.split()[0] in ("project", "development", "dev")):
            continue
        add(Criterion(f"open:{name}", f"{name} is open", ("open_application",), name))
    if _REMIND.search(text):
        add(Criterion("reminder", "the reminder exists", ("set_reminder",)))
    if _TODO.search(text):
        add(Criterion("todo", "the task is on your list", ("add_todo",)))
    if _TESTS.search(text):
        add(Criterion("checks", "the project's checks ran and passed", ("check_project",)))
    if _GIT.search(text):
        add(Criterion("git", "the repository state was read", ("git_status",)))
    if _EMAIL.search(text):
        contract.risks.append("sends something outside this PC: it always asks you first")
        add(Criterion("email", "the email was sent", ("send_email",)))
    if _DELETE.search(text):
        contract.risks.append("deleting: it always asks, and goes to the Recycle Bin where possible")
        add(Criterion("delete", "the named items are gone", ("delete_item",)))
    if _DEV_READY.search(text):
        add(Criterion("workspace", "the project workspace was identified", ("project_profile",), contract.project))
        add(Criterion("workspace_checks", "the project's checks pass", ("check_project",), "", required=False))


def _clarify(contract: GoalContract, text: str) -> None:
    if _DELETE.search(text) and not _CONCRETE_TARGET.search(text):
        contract.questions.append(Question("Which files or folders exactly? I won't delete anything on a guess.", "safety"))
    if _EMAIL.search(text) and not re.search(r"\b\S+@\S+\.\S+|\bto\s+[A-Z]\w+|\bto\s+(?:my|the)\s+\w+", text):
        contract.questions.append(Question("Who should it go to?", "privacy"))
    if _REMIND.search(text) and not _TIME.search(text):
        contract.questions.append(Question("When should I remind you?", "correctness"))
    if contract.deadline and contract.deadline < datetime.now().date() and "deadline" not in contract.constraints:
        pass                                                    # a past date is reported by conflicts, not guessed away


def _conflicts(contract: GoalContract, active: Iterable[Active]) -> None:
    mine = tokens(contract.outcome)
    same_day = 0
    for item in active:
        theirs = tokens(item.title)
        if mine and theirs:
            overlap = len(mine & theirs) / len(mine | theirs)
            if overlap >= 0.6:
                contract.conflicts.append(Conflict("duplicate", f"this looks like your existing {item.kind} '{item.title}'"))
        if contract.deadline and item.due == contract.deadline:
            same_day += 1
    if same_day >= 3:
        contract.conflicts.append(Conflict("busy_day", f"{same_day} other things are already due on {contract.deadline}"))
    if contract.deadline and contract.deadline < datetime.now().date():
        contract.conflicts.append(Conflict("busy_day", f"{contract.deadline} is already in the past"))


def check_plan(contract: GoalContract, tools: Iterable[str]) -> list[Conflict]:
    """Planned tool names that break what you said. A constraint you gave NOW always beats a remembered preference."""
    used = set(tools)
    out = []
    for key in contract.constraints:
        broken = sorted(used & set(_FORBIDS.get(key, ())))
        if broken:
            out.append(Conflict("violates_constraint", f"you said to {contract.constraints[key]}, but the plan uses {', '.join(broken)}"))
    return out


# ------------------------------------------------------------------ evaluating


@dataclass(frozen=True)
class CriterionResult:
    criterion: Criterion
    status: str
    evidence: tuple[str, ...] = ()


@dataclass
class Completion:
    verdict: str                    # complete | incomplete | contradicted | unverifiable
    results: list[CriterionResult]

    def describe(self) -> str:
        if self.verdict == "unverifiable":
            return "I can't verify this goal: no acceptance criteria could be derived from the request."
        lines = [f"Goal {self.verdict}."]
        for r in self.results:
            tag = "" if r.criterion.required else " (optional)"
            lines.append(f"  [{r.status}] {r.criterion.text}{tag}" + (f" - {'; '.join(r.evidence)}" if r.evidence else ""))
        return "\n".join(lines)


def evaluate(contract: GoalContract, evidence: Iterable[Evidence]) -> Completion:
    evidence = list(evidence)
    results = []
    for c in contract.criteria:
        status = judge(c.key, evidence)
        results.append(CriterionResult(c, status, tuple(e.source for e in evidence if e.claim == c.key and e.source)))
    required = [r for r in results if r.criterion.required]
    if not required:
        return Completion("unverifiable", results)
    if any(r.status == CONTRADICTED for r in required):
        return Completion("contradicted", results)
    if all(r.status == VERIFIED for r in required):
        return Completion("complete", results)
    return Completion("incomplete", results)


def evidence_from_steps(contract: GoalContract, steps: Iterable[Any]) -> list[Evidence]:
    """Mission steps -> evidence. A step that finished is a direct observation (the executor already verified its own
    effect); a failed step is evidence AGAINST. Steps that never ran say nothing."""
    out = []
    for step in steps:
        state = getattr(step, "state", "")
        if state not in ("done", "failed"):
            continue
        tool = getattr(step, "tool", "")
        args = json.dumps(getattr(step, "arguments", {}) or {}, ensure_ascii=False).lower()
        for c in contract.criteria:
            if tool in c.tools and (not c.subject or c.subject.lower() in args):
                out.append(Evidence(c.key, "verified" if state == "done" else "observation", state == "done",
                                    f"step {getattr(step, 'n', '?')}: {tool} {state}", f"{getattr(step, 'n', 0):06d}"))
    return out
