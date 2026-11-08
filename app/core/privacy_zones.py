"""Privacy zones: while a sensitive program is in front, Jarvis restricts what it captures, remembers and lets others control.

The existing screen-capture block (``SCREEN_BLOCKED_WINDOWS``) only stops screenshots. A zone is wider and is named, listable and switchable:

  restrict kinds:  capture   screenshots, screen reading, screen watch
                   memory    saving the conversation / habits / suggestions from what is happening
                   clipboard clipboard history
                   remote    a paired phone viewing or controlling the screen, mouse or keyboard
                   index     adding what is on screen / in that app to searchable knowledge

Built-in zones: password managers, banking and payments, private browsing, meetings. You can switch any built-in zone off and add your own (a title
word or a program name, and which kinds to restrict). A zone only ever RESTRICTS Jarvis - it never grants anything and cannot be used to widen
permissions; urgent safety features (emergency stop) are unaffected. Decisions are made from the foreground program and window title, cached for 2 seconds.
If the foreground cannot be read the answer is "not in a zone" for the built-in title rules but the existing screen block still applies; that gap is
stated in ``status``. The Windows foreground reader is the same one as the meeting detector's (unverified on hardware).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

KEY_OFF = "privacy_zones_off"
KEY_CUSTOM = "privacy_zones_custom"
KINDS = ("capture", "memory", "clipboard", "remote", "index")
MAX_CUSTOM = 20
_WORD = re.compile(r"^[\w .:+&-]{2,40}$", re.UNICODE)


@dataclass(frozen=True)
class Zone:
    key: str
    label: str
    processes: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()
    restrict: frozenset[str] = field(default_factory=lambda: frozenset(KINDS))
    builtin: bool = True
    meeting: bool = False


BUILTIN: tuple[Zone, ...] = (
    Zone("passwords", "Password managers", ("keepass", "keepassxc", "bitwarden", "1password", "lastpass", "dashlane", "nordpass", "protonpass"),
         ("password manager", "vault", "keepass", "bitwarden", "1password", "lastpass")),
    Zone("banking", "Banking and payments", (), ("bank", "banking", "paypal", "bkash", "nagad", "rocket", "payoneer", "wise.com", "net banking",
                                               "internet banking", "online banking", "checkout", "payment"), frozenset({"capture", "memory", "clipboard", "remote", "index"})),
    Zone("private_browsing", "Private browsing", (), ("inprivate", "private browsing", "incognito", "private window"), frozenset({"capture", "memory", "index"})),
    Zone("meetings", "Meetings", (), (), frozenset({"capture", "memory", "remote", "index"}), meeting=True),
)


@dataclass
class Config:
    off: set[str] = field(default_factory=set)
    custom: list[Zone] = field(default_factory=list)

    def zones(self) -> list[Zone]:
        return [z for z in BUILTIN if z.key not in self.off] + list(self.custom)


def load(kv: Any) -> Config:
    off = set(kv.get(KEY_OFF, []) or [])
    custom = []
    for raw in kv.get(KEY_CUSTOM, []) or []:
        try:
            custom.append(Zone(f"custom:{raw['name']}", raw["name"], tuple(raw.get("processes", [])), tuple(raw.get("titles", [])),
                               frozenset(k for k in raw.get("restrict", KINDS) if k in KINDS) or frozenset(KINDS), builtin=False))
        except (KeyError, TypeError):
            continue
    return Config(off, custom)


@dataclass(frozen=True)
class Facts:
    process: str = ""
    title: str = ""
    in_meeting: bool = False
    known: bool = True                 # False = the foreground could not be read


@dataclass(frozen=True)
class Hit:
    zone: Zone
    why: str


def active(cfg: Config, f: Facts) -> list[Hit]:
    proc, title = f.process.lower().removesuffix(".exe"), f.title.lower()
    hits = []
    for z in cfg.zones():
        if z.meeting:
            if f.in_meeting:
                hits.append(Hit(z, "a meeting is in progress"))
            continue
        p = next((x for x in z.processes if x and x in proc), None)
        t = next((x for x in z.titles if x and x in title), None)
        if p or t:
            hits.append(Hit(z, f"program '{f.process}'" if p else f"window title contains '{t}'"))
    return hits


def blocked(cfg: Config, f: Facts, kind: str) -> str | None:
    """The reason this kind of thing is restricted right now, or None."""
    for h in active(cfg, f):
        if kind in h.zone.restrict:
            return f"{h.zone.label} zone is active ({h.why})"
    return None


# ---- editing ----------------------------------------------------------------------------------------------------------------------------

def add_custom(kv: Any, name: str, titles: list[str], processes: list[str], restrict: list[str]) -> str:
    name = (name or "").strip()
    if not _WORD.match(name):
        raise ValueError("Give the zone a short name (letters, digits, spaces).")
    clean_t = [t.strip().lower() for t in titles if t.strip()][:10]
    clean_p = [p.strip().lower().removesuffix(".exe") for p in processes if p.strip()][:10]
    if not (clean_t or clean_p):
        raise ValueError("Name at least one window-title word or program for the zone.")
    for w in clean_t + clean_p:
        if not _WORD.match(w) or len(w) < 3:
            raise ValueError(f"'{w}' is too short or has odd characters; use at least 3 normal characters.")
    kinds = [k for k in restrict if k in KINDS] or list(KINDS)
    items = [z for z in (kv.get(KEY_CUSTOM, []) or []) if z.get("name", "").lower() != name.lower()]
    if len(items) >= MAX_CUSTOM:
        raise ValueError(f"At most {MAX_CUSTOM} custom zones.")
    items.append({"name": name, "titles": clean_t, "processes": clean_p, "restrict": kinds})
    kv.set(KEY_CUSTOM, items)
    return f"Zone '{name}' added: restricts {', '.join(kinds)} when {', '.join(clean_t + clean_p)} is in front."


def remove_custom(kv: Any, name: str) -> bool:
    items = kv.get(KEY_CUSTOM, []) or []
    keep = [z for z in items if z.get("name", "").lower() != name.strip().lower()]
    kv.set(KEY_CUSTOM, keep)
    return len(keep) != len(items)


def set_builtin(kv: Any, key: str, on: bool) -> str:
    if key not in {z.key for z in BUILTIN}:
        raise ValueError(f"Not a built-in zone. Built-in: {', '.join(z.key for z in BUILTIN)}.")
    off = set(kv.get(KEY_OFF, []) or [])
    (off.discard if on else off.add)(key)
    kv.set(KEY_OFF, sorted(off))
    return f"{key} zone is {'on' if on else 'OFF - Jarvis will no longer restrict anything for it'}."


def describe(cfg: Config, f: Facts | None = None) -> str:
    lines = []
    for z in BUILTIN:
        lines.append(f"{'on ' if z.key not in cfg.off else 'OFF'} {z.label} [{z.key}]: restricts {', '.join(sorted(z.restrict))}")
    lines += [f"on  {z.label} (yours): restricts {', '.join(sorted(z.restrict))}" for z in cfg.custom]
    if f is not None:
        now = active(cfg, f)
        lines.append("In a zone now: " + ("; ".join(f"{h.zone.label} ({h.why})" for h in now) if now else "no") +
                     ("" if f.known else " (the foreground window could not be read, so title rules cannot be checked)"))
    return "\n".join(lines)


# ---- the running check used by the rest of Jarvis ---------------------------------------------------------------------------------------

_cfg = Config()
_cache: tuple[float, Facts] | None = None
FACTS: Callable[[], Facts] | None = None              # tests replace this; the default reads the PC


def configure(kv: Any) -> None:
    """Load the stored zones (called at start-up and after every change)."""
    global _cfg
    _cfg = load(kv)


def current_facts(max_age: float = 2.0) -> Facts:
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < max_age:
        return _cache[1]
    if FACTS is not None:
        facts = FACTS()
    else:
        try:
            from app.core import meeting

            raw = meeting.real_facts()
            facts = Facts(raw.process, raw.title, meeting.detect(raw).in_meeting, known=bool(raw.process or raw.title))
        except Exception:
            facts = Facts(known=False)
    _cache = (now, facts)
    return facts


def reset_cache() -> None:
    global _cache
    _cache = None


def restriction(kind: str) -> str | None:
    """Why ``kind`` is restricted right now (text), or None. Never raises."""
    try:
        return blocked(_cfg, current_facts(), kind)
    except Exception:
        return None


def allows(kind: str) -> bool:
    return restriction(kind) is None
