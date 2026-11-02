"""One per-request context for the whole assistant: what Jarvis knows right now, where each piece came from, how sure it is, and what it does NOT know.

Before this module, six systems each knew a part (the context engine: what you're doing; the world model: projects/goals/people; memory; missions;
the device registry; the performance plan) and nothing merged them. ``build`` merges them for ONE request, with:

* an evidence label on every item - verified (measured / read just now), retrieved (stored, confirmed by you), inferred (a guess from signals or an
  unconfirmed suggestion), predicted (a forecast), unknown (looked for, not available);
* a source, a confidence, and freshness (age against a lifetime; stale items are flagged, very old ones dropped);
* relevance selection under a total and a per-section character budget (the whole personal database is never loaded);
* a short cache so a burst of related requests does not re-read everything;
* ``clarify``: when the request points at something ("that project", "oita", "continue where I left off") and nothing in the context can resolve it
  with enough confidence, ONE focused question instead of a guess.

Pure logic: the readers of the real systems are in ``gather`` (each wrapped, so one failing source never breaks a request). Hardware-free; tested.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

VERIFIED, RETRIEVED, INFERRED, PREDICTED, UNKNOWN = "verified", "retrieved", "inferred", "predicted", "unknown"
EVIDENCE = (VERIFIED, RETRIEVED, INFERRED, PREDICTED, UNKNOWN)

# section -> (budget in characters, base weight, always included even when the request does not mention it)
SECTIONS: dict[str, tuple[int, float, bool]] = {
    "permissions": (260, 3.0, True),       # emergency switches, mode, autonomy: always known, always short
    "now": (320, 2.0, True),               # what you are doing right now
    "approval": (160, 3.0, True),          # something waiting for you
    "mission": (320, 1.5, False),
    "project": (300, 1.0, False),
    "device": (320, 1.0, False),
    "resource": (200, 0.8, False),
    "memory": (500, 1.0, False),
    "preference": (240, 0.8, False),
    "goal": (240, 1.0, False),
    "forecast": (200, 0.5, False),
}
TOTAL_BUDGET = 1800
STALE_AFTER = 1.0          # x lifetime: flagged "may be out of date"
DROP_AFTER = 4.0           # x lifetime: not worth showing
CACHE_SECONDS = 20.0
_WORD = re.compile(r"\w{2,}", re.U)
_GENERIC = frozenset("project projects file files document device devices phone laptop task mission that those same previous last open start run".split())
_NOISE = frozenset("the and for with that this what how can you please jarvis amar ar ta ki ke koro kore dao den hobe achhe ase".split())


@dataclass
class Item:
    section: str
    text: str
    evidence: str = VERIFIED
    source: str = ""
    confidence: float = 1.0
    observed_at: float | None = None     # epoch seconds; None = timeless
    lifetime_s: float | None = None      # how long it stays trustworthy; None = does not age
    private: bool = False                # only shown when the request names it

    def age(self, now: float) -> float | None:
        return None if self.observed_at is None else max(0.0, now - self.observed_at)

    def freshness(self, now: float) -> str:
        age = self.age(now)
        if age is None or not self.lifetime_s:
            return "fresh"
        ratio = age / self.lifetime_s
        return "fresh" if ratio <= STALE_AFTER else ("stale" if ratio <= DROP_AFTER else "expired")

    def line(self, now: float) -> str:
        stale = " - may be out of date" if self.freshness(now) == "stale" else ""
        return f"[{self.evidence}] {self.text} ({self.source}{stale})"


@dataclass
class RequestContext:
    request: str
    items: list[Item] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)       # things looked for and not available (with the reason)
    dropped: int = 0
    built_at: float = 0.0

    def of(self, section: str) -> list[Item]:
        return [i for i in self.items if i.section == section]

    def render(self) -> str:
        """The block for the model's prompt: grouped, labelled, short. Empty when nothing is known."""
        if not self.items and not self.unknown:
            return ""
        out = ["What I know right now (evidence in brackets; anything not listed is unknown, do not guess it):"]
        for section in SECTIONS:
            for item in self.of(section):
                out.append("- " + item.line(self.built_at))
        if self.unknown:
            out.append("- not available: " + "; ".join(self.unknown[:5]))
        return "\n".join(out)

    def to_dict(self) -> dict[str, Any]:
        return {"request": self.request, "built_at": self.built_at, "dropped": self.dropped, "unknown": self.unknown,
                "items": [{"section": i.section, "text": i.text, "evidence": i.evidence, "source": i.source, "confidence": round(i.confidence, 2),
                           "freshness": i.freshness(self.built_at), "age_s": None if i.age(self.built_at) is None else round(i.age(self.built_at), 1)}
                          for i in self.items]}

    @property
    def confidence(self) -> float:
        """Average confidence of what was included (0 when nothing was)."""
        return round(sum(i.confidence for i in self.items) / len(self.items), 2) if self.items else 0.0


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _NOISE}


def relevance(request_words: set[str], item: Item) -> float:
    if not request_words:
        return 0.0
    hits = len(request_words & _words(item.text))
    prefix = sum(1 for w in request_words if len(w) >= 4 and any(t.startswith(w[:4]) for t in _words(item.text))) if not hits else 0
    return hits * 2.0 + prefix * 0.8


def build(request: str, items: list[Item], *, unknown: list[str] | None = None, now: float | None = None,
          budget: int = TOTAL_BUDGET) -> RequestContext:
    """Choose what matters for ``request`` from ``items`` under the section budgets. Deterministic."""
    now = time.time() if now is None else now
    words = _words(request)
    scored: list[tuple[float, Item]] = []
    dropped = 0
    for item in items:
        if item.evidence not in EVIDENCE or item.freshness(now) == "expired":
            dropped += 1
            continue
        _, base, always = SECTIONS.get(item.section, (200, 0.5, False))
        rel = relevance(words, item)
        if item.private and not rel:
            dropped += 1
            continue
        if not always and not rel and item.section not in ("mission",):
            dropped += 1                    # not about this request
            continue
        stale = 0.6 if item.freshness(now) == "stale" else 1.0
        scored.append(((base + rel) * max(item.confidence, 0.05) * stale, item))
    scored.sort(key=lambda p: -p[0])
    chosen: list[Item] = []
    used_total, used_section = 0, {}
    for _, item in scored:
        cap = SECTIONS.get(item.section, (200, 0.5, False))[0]
        size = len(item.line(now)) + 3
        if used_section.get(item.section, 0) + size > cap or used_total + size > budget:
            dropped += 1
            continue
        used_section[item.section] = used_section.get(item.section, 0) + size
        used_total += size
        chosen.append(item)
    return RequestContext(request=request, items=chosen, unknown=list(unknown or []), dropped=dropped, built_at=now)


# ------------------------------------------------------------------ "ask instead of guessing"

# A phrase in the request that points at something in the user's world, and the section that must resolve it.
_REFERENCES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\b(that|the same|my|this|oi|oita|ei|amar|oi\s+(?:ta|tai)?)\s*(?:laptop e\s+)?(?:project|proj)\b|\bproject\s*(?:ta|tai|ti)\b|"
                r"\b(my|the)\s+project\b|\bprojekt\b", re.I | re.U), "project", "Which project do you mean?"),
    (re.compile(r"\b(?:continue|resume|carry on)\s+(?:with\s+)?(?:my|the|that|our)?\s*(?:previous|last|unfinished|earlier)?\s*(?:task|work|mission|job)\b|"
                r"\bwhere i left off\b|\b(?:previous|last) task\b|\bager kaj\b|\bcontinue (?:my )?previous\b", re.I),
     "mission", "Which task should I continue? I don't see an unfinished one."),
    (re.compile(r"\b(my phone|the phone|amar phone|mobile e|phone e|send (?:it )?to (?:my )?phone)\b", re.I), "device", "Which device do you mean?"),
)


def clarify(request: str, ctx: RequestContext, *, min_confidence: float = 0.6) -> str | None:
    """A single focused question when the request points at something the context cannot resolve (None = go ahead).

    Rules: a reference needs at least one item of the matching section that is confident enough (>= ``min_confidence``) and not stale; when two
    equally relevant, equally confident items compete and the request names neither, ask which. A request that names the thing itself
    (its words overlap an item) is never questioned."""
    text = request or ""
    words = _words(text)
    for pattern, section, question in _REFERENCES:
        if not pattern.search(text):
            continue
        candidates = [i for i in ctx.of(section) if i.confidence >= min_confidence and i.evidence != UNKNOWN and i.freshness(ctx.built_at) != "stale"]
        if not candidates:
            return question
        specific = words - _GENERIC                           # "project" itself does not name WHICH project
        named = [i for i in candidates if relevance(specific, i) >= 2.0]
        if named:
            continue
        if len(candidates) > 1:
            top = sorted(candidates, key=lambda i: -i.confidence)
            if abs(top[0].confidence - top[1].confidence) < 0.1 and top[0].section == "project":
                names = ", ".join(i.text.split(" (")[0] for i in top[:3])
                return f"{question} I see {names}."
    return None


# ------------------------------------------------------------------ cache

_CACHE: dict[str, tuple[float, RequestContext]] = {}


def cached(request: str, version: str, make: Callable[[], RequestContext], *, now: float | None = None) -> RequestContext:
    """Re-use a context built for the same request (and the same state ``version``) in the last few seconds."""
    now = time.time() if now is None else now
    key = f"{version}|{' '.join(sorted(_words(request)))}"
    hit = _CACHE.get(key)
    if hit is not None and now - hit[0] <= CACHE_SECONDS:
        return hit[1]
    value = make()
    _CACHE[key] = (now, value)
    for old in [k for k, (t, _) in _CACHE.items() if now - t > CACHE_SECONDS * 4]:
        _CACHE.pop(old, None)
    return value


# ------------------------------------------------------------------ the readers of the real systems


def gather(ctx: Any, request: str, *, now: float | None = None) -> tuple[list[Item], list[str]]:
    """Read the real systems into items. Each source is isolated: a failure becomes an "unknown" line, never an exception."""
    now = time.time() if now is None else now
    items: list[Item] = []
    unknown: list[str] = []

    def guard(name: str, reader: Callable[[], None]) -> None:
        try:
            reader()
        except Exception as exc:
            unknown.append(f"{name} ({type(exc).__name__})")

    def read_context() -> None:
        from app.core import context_engine

        snap = context_engine.ENGINE.snapshot(ctx)
        for name, fact in snap.facts.items():
            if fact.certainty == "unknown" or fact.value in (None, "", [], {}):
                continue
            section = {"pending_approval": "approval", "current_goal": "goal", "current_mission": "mission", "project": "project",
                       "workspace": "project", "jarvis_mode": "permissions", "emergency": "permissions", "device_state": "resource"}.get(name, "now")
            evidence = VERIFIED if fact.certainty == "known" else INFERRED
            items.append(Item(section, f"{name.replace('_', ' ')}: {fact.value}", evidence, fact.source or "context engine",
                              1.0 if fact.certainty == "known" else 0.6, snap.timestamp, 120.0))

    def read_world() -> None:
        from app.core.world import collect

        for e in collect(ctx):
            section = {"project": "project", "goal": "goal", "mission": "mission", "fact": "memory", "person": "memory"}.get(e.kind)
            if section is None:
                continue
            items.append(Item(section, f"{e.kind} {e.line()}", RETRIEVED, e.source or "world model", e.confidence, None, None, private=e.private))

    def read_memory() -> None:
        memory = ctx.get_memory()
        for m in memory.search(request, limit=8):
            if getattr(m, "disabled", False) or getattr(m, "sensitivity", "normal") != "normal":
                continue
            confirmed = getattr(m, "provenance", "user") in ("user", "confirmed", "told")
            items.append(Item("preference" if getattr(m, "category", "") == "preference" else "memory", m.content[:160],
                              RETRIEVED if confirmed else INFERRED, f"memory #{m.id} ({getattr(m, 'provenance', 'user')})",
                              float(getattr(m, "confidence", 1.0)) * (1.0 if confirmed else 0.7), None, None))

    def read_missions() -> None:
        from app.core.missions import MissionStore

        store = MissionStore(ctx.database)
        for mission in store.recent(5, states=("planned", "running", "paused", "verifying", "waiting")):
            items.append(Item("mission", f"unfinished mission '{mission.title}' ({mission.progress()} steps)", VERIFIED, "mission store", 1.0, now, 3600.0))

    def read_devices() -> None:
        from app.core.devices import DeviceRegistry

        for d in DeviceRegistry(ctx.database).all():
            items.append(Item("device", d.describe(), VERIFIED if d.is_self else RETRIEVED, "device registry", 1.0 if d.is_self else 0.8, None, None))

    def read_resources() -> None:
        import psutil

        items.append(Item("resource", f"CPU {psutil.cpu_percent(interval=None):.0f}%, memory {psutil.virtual_memory().percent:.0f}%", VERIFIED,
                          "psutil", 1.0, now, 60.0))

    def read_permissions() -> None:
        from app.core import emergency

        active = emergency.STATE.active()
        items.append(Item("permissions", ("emergency switches ON: " + ", ".join(active)) if active else "no emergency switch is on", VERIFIED,
                          "emergency state", 1.0, now, 30.0))

    for name, reader in (("context engine", read_context), ("world model", read_world), ("memory", read_memory), ("missions", read_missions),
                         ("devices", read_devices), ("resources", read_resources), ("permissions", read_permissions)):
        if name in ("memory", "missions", "devices") and getattr(ctx, "database", None) is None:
            unknown.append(f"{name} (no database)")
            continue
        guard(name, reader)
    return items, unknown


def for_request(ctx: Any, request: str, *, now: float | None = None, use_cache: bool = True) -> RequestContext:
    """The unified context for ``request`` from the live system."""
    def make() -> RequestContext:
        items, unknown = gather(ctx, request, now=now)
        return build(request, items, unknown=unknown, now=now)

    if not use_cache:
        return make()
    return cached(request, "live", make, now=now)
