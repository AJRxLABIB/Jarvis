"""Adaptive preferences you can read, change, switch off and delete.

Two ways in, and they are NOT equal:

* **explicit** - you said it ("keep responses shorter", "use Jarvis as my default workspace"). It is confirmed at once.
* **observed** - a repeated correction noticed by Jarvis. It becomes a *candidate* that is never applied; after three
  sightings Jarvis may ASK whether to remember it. One interaction never becomes a rule, and nothing observed is applied
  until you confirm it.

Only a fixed list of style / default / workflow keys exists. Nothing here can touch permissions: a request like "never ask
me for approval" is refused, and profiles (development, study, research, planning, gaming, general) change style, tools and
context only. Precedence, strongest first:

    1 security and platform rules (``FIXED``, not overridable)  2 what you said for THIS task  3 the active profile
    4 confirmed long-term preferences (project scope beats global)  5 safe defaults

Switching adaptation off makes ``resolve`` return only the defaults, the security rules and this task's own instruction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.tools.base import ToolError

if TYPE_CHECKING:
    from app.memory.database import Database

OBSERVED_TO_ASK = 3
FIXED = {"approvals": "always required for risky actions", "emergency_stop": "always available"}   # layer 1
DEFAULTS: dict[str, Any] = {"response_length": "normal", "show_error_first": False, "default_project": "",
                            "auto_organize": True, "prefer_local": True, "ask_before_config": True,
                            "language": "auto"}
ALLOWED_KEYS = frozenset(DEFAULTS) | {"default_tools", "context_sources"}
PROFILES: dict[str, dict[str, Any]] = {
    "general": {},
    "development": {"show_error_first": True, "context_sources": "project,git,code"},
    "study": {"response_length": "long", "context_sources": "notes,documents"},
    "research": {"response_length": "long", "context_sources": "documents,notes,memory"},
    "planning": {"response_length": "short", "context_sources": "goals,tasks,calendar"},
    "gaming": {"response_length": "short", "context_sources": ""},
}
_REFUSE = re.compile(
    r"\b(?:never|don'?t|do not|stop|quit|no need to|no more|not to)\s+(?:ask|asking|confirm\w*|prompt\w*|check with)\b|"
    r"\bwithout\s+(?:asking|confirm\w*|approval|permission)\b|\bskip\s+(?:the\s+)?(?:confirm\w*|approvals?|prompts?)\b|"
    r"\bdisable\s+(?:the\s+)?(?:security|firewall|approvals?|confirm\w*|emergency)\b|\bturn\s+off\s+(?:the\s+)?(?:security|approvals?|confirm\w*)\b|"
    r"\bauto[- ]?approve\b|\bgrant\s+(?:yourself|jarvis)\b|\bbypass\b|\bfull[- ]power\b", re.I)

# (key, pattern, value or None = captured group 1, text)
_EXPLICIT: tuple[tuple[str, re.Pattern[str], Any, str], ...] = (
    ("response_length", re.compile(r"\b(?:keep|make)\b.{0,25}\b(?:shorter|brief|short)\b|\bbe (?:brief|concise)\b|\bshorter (?:answers|responses|replies)\b", re.I),
     "short", "Keep responses short."),
    ("response_length", re.compile(r"\b(?:more|longer|fuller)\b.{0,20}\b(?:detail|answers|responses|explanations?)\b|\bexplain more\b", re.I),
     "long", "Give fuller, more detailed responses."),
    ("show_error_first", re.compile(r"\b(?:show|give|print)\b.{0,20}\b(?:full )?error\b.{0,25}\bbefore\b", re.I), True,
     "Show the full error before suggesting a fix."),
    ("default_project", re.compile(r"\buse\s+(.+?)\s+as my default\s+(?:development\s+)?(?:project|workspace)\b", re.I), None,
     "Use {value} as the default development workspace."),
    ("auto_organize", re.compile(r"\b(?:don'?t|do not|never)\s+(?:automatically|auto)\s+organi[sz]e\b", re.I), False,
     "Never organise files automatically."),
    ("prefer_local", re.compile(r"\bprefer\s+local\b", re.I), True, "Prefer local solutions."),
    ("ask_before_config", re.compile(r"\bask me before (?:changing|changes to|modifying)\s+(?:configs?|configurations?|settings)\b", re.I),
     True, "Ask before changing configuration."),
)


@dataclass
class Preference:
    id: int
    key: str
    text: str
    value: str
    scope: str
    source: str
    status: str
    seen: int


@dataclass
class Resolved:
    values: dict[str, Any]
    layers: dict[str, str]
    fixed: dict[str, str] = field(default_factory=lambda: dict(FIXED))
    adaptation: bool = True

    def describe(self) -> str:
        lines = [f"{k} = {v}   ({self.layers[k]})" for k, v in self.values.items() if self.layers.get(k) != "default"]
        lines = lines or ["No personal preferences apply (defaults)."]
        if not self.adaptation:
            lines.append("Adaptation is switched off.")
        return "\n".join(lines)


def parse_statement(text: str) -> tuple[str, Any, str] | None:
    """(key, value, readable text) for a preference you stated; None when it isn't one. Raises when it asks for
    something a preference may never do (turning approvals off)."""
    if _REFUSE.search(text):
        raise ToolError("Preferences can't switch off approvals or security. Standing approvals have their own settings, "
                        "and risky actions always ask.", "refused")
    for key, pattern, value, readable in _EXPLICIT:
        match = pattern.search(text)
        if match:
            captured = value if value is not None else match.group(1).strip(" .\"'")
            return key, captured, readable.format(value=captured)
    return None


class PreferenceStore:
    def __init__(self, database: "Database", clock=lambda: datetime.now(timezone.utc)) -> None:
        self._db, self._clock = database, clock

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    @staticmethod
    def _row(r: Any) -> Preference:
        return Preference(r["id"], r["key"], r["text"], r["value"], r["scope"], r["source"], r["status"], r["seen"])

    # -------------------------------------------------------------- adaptation switch
    def enabled(self) -> bool:
        with self._db.connect() as conn:
            row = conn.execute("SELECT value FROM preferences WHERE key = '_adaptation' AND scope = 'system'").fetchone()
        return not (row is not None and row["value"] == "off")

    def set_enabled(self, on: bool) -> None:
        now = self._now()
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO preferences (key, text, value, scope, source, status, created_at, updated_at) "
                         "VALUES ('_adaptation', 'adaptation switch', ?, 'system', 'explicit', 'confirmed', ?, ?) "
                         "ON CONFLICT (key, scope) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         ("on" if on else "off", now, now))

    # -------------------------------------------------------------- writing
    def _check(self, key: str, scope: str) -> None:
        if key not in ALLOWED_KEYS:
            raise ToolError(f"'{key}' is not something a preference can change.", "invalid")
        if scope != "global" and not re.fullmatch(r"(?:profile|project):[\w .-]{1,50}", scope):
            raise ToolError("Scope is 'global', 'profile:<name>' or 'project:<name>'.", "invalid")
        if scope.startswith("profile:") and scope.split(":", 1)[1].lower() not in PROFILES:
            raise ToolError("Profiles: " + ", ".join(PROFILES) + ".", "invalid")

    def learn(self, statement: str, scope: str = "global") -> Preference:
        """You stated it: stored confirmed."""
        parsed = parse_statement(statement)
        if parsed is None:
            raise ToolError("I didn't recognise a preference there. Try: 'keep responses shorter', 'show the full error "
                            "before suggesting a fix', 'prefer local solutions'.", "not_understood")
        key, value, text = parsed
        return self.set(key, value, scope, text, source="explicit")

    def set(self, key: str, value: Any, scope: str = "global", text: str = "", source: str = "explicit") -> Preference:
        self._check(key, scope)
        now = self._now()
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO preferences (key, text, value, scope, source, status, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?) ON CONFLICT (key, scope) DO UPDATE SET "
                         "text = excluded.text, value = excluded.value, source = excluded.source, status = 'confirmed', "
                         "updated_at = excluded.updated_at", (key, (text or f"{key} = {value}")[:200], str(value), scope,
                                                              source, now, now))
        return self.find(key, scope)                                    # type: ignore[return-value]

    def observe(self, key: str, value: Any, text: str, scope: str = "global") -> Preference:
        """A correction was noticed. Records a CANDIDATE only; it is never applied and never confirms itself."""
        self._check(key, scope)
        now = self._now()
        existing = self.find(key, scope)
        if existing is not None and existing.status in ("confirmed", "rejected"):
            return existing                                              # your decision stands; nothing is overwritten
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO preferences (key, text, value, scope, source, status, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, 'observed', 'candidate', ?, ?) ON CONFLICT (key, scope) DO UPDATE SET "
                         "seen = seen + 1, updated_at = excluded.updated_at", (key, text[:200], str(value), scope, now, now))
        return self.find(key, scope)                                     # type: ignore[return-value]

    def confirm(self, pref_id: int) -> Preference:
        return self._set_status(pref_id, "confirmed")

    def reject(self, pref_id: int) -> Preference:
        return self._set_status(pref_id, "rejected")

    def _set_status(self, pref_id: int, status: str) -> Preference:
        self.get(pref_id)
        with self._db.transaction() as conn:
            conn.execute("UPDATE preferences SET status = ?, updated_at = ? WHERE id = ? AND scope != 'system'",
                         (status, self._now(), pref_id))
        return self.get(pref_id)

    def remove(self, pref_id: int) -> None:
        self.get(pref_id)
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM preferences WHERE id = ? AND scope != 'system'", (pref_id,))

    # -------------------------------------------------------------- reading
    def get(self, pref_id: int) -> Preference:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM preferences WHERE id = ? AND scope != 'system'", (pref_id,)).fetchone()
        if row is None:
            raise ToolError("There's no such preference.", "not_found")
        return self._row(row)

    def find(self, key: str, scope: str = "global") -> Preference | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM preferences WHERE key = ? AND scope = ?", (key, scope)).fetchone()
        return self._row(row) if row else None

    def list(self, status: str | None = None) -> list[Preference]:
        sql = "SELECT * FROM preferences WHERE scope != 'system'" + (" AND status = ?" if status else "") + " ORDER BY id"
        with self._db.connect() as conn:
            rows = conn.execute(sql, (status,) if status else ()).fetchall()
        return [self._row(r) for r in rows]

    def questions(self) -> list[str]:
        """Patterns seen often enough to ASK about. Asking changes nothing."""
        return [f"You've corrected this {p.seen} times: \"{p.text}\" Should I remember it? (confirm preference {p.id})"
                for p in self.list("candidate") if p.seen >= OBSERVED_TO_ASK]

    def stats(self) -> dict[str, int]:
        items = self.list()
        return {"confirmed": sum(p.status == "confirmed" for p in items), "candidates": sum(p.status == "candidate" for p in items),
                "rejected": sum(p.status == "rejected" for p in items),
                "observations": sum(p.seen for p in items if p.source == "observed")}

    # -------------------------------------------------------------- precedence
    def resolve(self, profile: str = "general", task: dict[str, Any] | None = None, project: str = "") -> Resolved:
        values, layers = dict(DEFAULTS), {k: "default" for k in DEFAULTS}
        adaptive = self.enabled()
        if adaptive:
            confirmed = [p for p in self.list("confirmed") if p.key in ALLOWED_KEYS]
            for scope_ok, label in ((lambda p: p.scope == "global", "long-term preference"),
                                    (lambda p: project and p.scope.lower() == f"project:{project}".lower(),
                                     "project preference")):
                for p in confirmed:
                    if scope_ok(p):
                        values[p.key], layers[p.key] = _typed(p.value, DEFAULTS.get(p.key)), label
            name = profile.lower()
            for key, value in PROFILES.get(name, {}).items():
                values[key], layers[key] = value, f"{name} profile"
            for p in confirmed:
                if p.scope.lower() == f"profile:{name}":
                    values[p.key], layers[p.key] = _typed(p.value, DEFAULTS.get(p.key)), f"{name} profile (yours)"
        for key, value in (task or {}).items():
            if key in ALLOWED_KEYS:
                values[key], layers[key] = value, "this task"
        return Resolved(values, layers, adaptation=adaptive)


def _typed(value: str, default: Any) -> Any:
    if isinstance(default, bool):
        return value.lower() in ("true", "1", "yes")
    return value
