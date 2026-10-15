"""Memory 2.0: memory types and contradiction detection - deterministic rules, no model guessing.

Types (``kind``):
* semantic   - stable facts and preferences ("my project folder is D:\\Work", "I use PostgreSQL")
* episodic   - things that happened ("I finished the TMS login on Monday", a completed mission)
* procedural - how you like things done ("when I start work, open VS Code and Spotify")
* goal       - what you're working towards ("I want to learn Rust this year")
Working memory is the current conversation and the context engine's snapshot: never stored.

Contradictions: two memories that name different things of the same kind (MongoDB vs PostgreSQL, VS Code vs
PyCharm) about the same subject. When the new one says it replaced the old one ("migrated to", "switched to",
"no longer use ..."), the old memory is marked *superseded* (kept for history, never used again). Otherwise both
stay and a conflict is opened for you to settle: keep the new, keep the old, or both are true.
"""

from __future__ import annotations

import re

KINDS = ("semantic", "episodic", "procedural", "goal")

_PROCEDURAL = re.compile(r"\b(?:when(?:ever)?\s+i\b|i\s+(?:usually|always|normally|prefer\s+to|like\s+to)\b|"
                         r"my\s+(?:workflow|routine|process)\b|every\s+(?:time|morning|evening|day)\b|"
                         r"the\s+way\s+i\b|step\s+\d)", re.I)
_EPISODIC = re.compile(r"\b(?:yesterday|today\s+i|last\s+(?:week|month|night|year)|i\s+(?:finished|completed|met|"
                       r"started|fixed|released|deployed|submitted|visited|bought)|on\s+(?:monday|tuesday|wednesday|"
                       r"thursday|friday|saturday|sunday)|\d{4}-\d{2}-\d{2})", re.I)
_GOAL = re.compile(r"\b(?:my\s+goal|i\s+(?:want|plan|intend|hope)\s+to|i'?m\s+going\s+to|i\s+will\s+(?:finish|learn|"
                   r"build|complete)|by\s+the\s+end\s+of)\b", re.I)


def classify(text: str) -> str:
    if _GOAL.search(text):
        return "goal"
    if _PROCEDURAL.search(text):
        return "procedural"
    if _EPISODIC.search(text):
        return "episodic"
    return "semantic"


# Things of which a person / project normally uses ONE at a time
GROUPS: dict[str, tuple[str, ...]] = {
    "database": ("mongodb", "mongo", "postgresql", "postgres", "mysql", "mariadb", "sqlite", "firebase", "supabase",
                 "dynamodb", "oracle", "sql server", "mssql", "cassandra"),
    "editor": ("vs code", "vscode", "visual studio code", "pycharm", "intellij", "webstorm", "sublime", "vim",
               "neovim", "cursor", "notepad++", "atom", "zed"),
    "browser": ("chrome", "edge", "firefox", "brave", "opera", "safari", "vivaldi"),
    "frontend framework": ("react", "vue", "angular", "svelte", "solid", "next.js", "nuxt"),
    "backend framework": ("express", "fastify", "django", "flask", "fastapi", "laravel", "spring", "rails", "nestjs"),
    "operating system": ("windows", "linux", "ubuntu", "macos", "fedora", "debian"),
    "cloud": ("aws", "azure", "google cloud", "gcp", "digitalocean", "vercel", "netlify", "heroku"),
    "phone": ("iphone", "android", "pixel", "samsung", "xiaomi", "oneplus"),
}
_ALIASES = {"mongo": "mongodb", "postgres": "postgresql", "vscode": "vs code", "visual studio code": "vs code",
            "mssql": "sql server", "gcp": "google cloud"}
_REPLACED = re.compile(r"\b(?:migrat\w*|switch\w*|mov\w*|chang\w*|replac\w*|upgrad\w*)\s+(?:\w+\s+){0,4}?(?:from\s+"
                       r"(?P<old>[\w .+#-]+?)\s+)?to\b|\bno\s+longer\s+(?:use|using)\b|\binstead\s+of\b|\bnot\s+"
                       r"(?:using|use)\s+\w+\s+(?:any\s*more|anymore)\b|\bnow\s+(?:use|using)\b", re.I)
_WORD = re.compile(r"[a-z0-9][a-z0-9.+#-]*")
_COMMON = {"i", "my", "the", "a", "an", "we", "our", "use", "uses", "using", "used", "for", "with", "to", "from",
           "in", "on", "of", "and", "is", "are", "was", "now", "project", "app", "migrated", "switched", "moved",
           "changed", "no", "longer", "instead", "it", "its", "that", "this", "database", "db", "editor", "browser",
           "anymore", "any", "more", "prefer", "like", "always", "usually", "back", "end", "backend", "frontend"}


def mentions(text: str) -> dict[str, set[str]]:
    """{group: {things named}} in a memory's text."""
    lowered = f" {text.lower()} "
    found: dict[str, set[str]] = {}
    for group, names in GROUPS.items():
        for name in names:
            if re.search(rf"(?<![\w.+#-]){re.escape(name)}(?![\w+#-])", lowered):
                found.setdefault(group, set()).add(_ALIASES.get(name, name))
    return found


def subject_words(text: str) -> set[str]:
    """The words that say what a memory is about (without the technology names and filler)."""
    names = {n for group in GROUPS.values() for n in group for n in n.split()}
    return {w for w in _WORD.findall(text.lower()) if w not in _COMMON and w not in names and len(w) > 2}


def replaced_thing(text: str) -> str | None:
    """'migrated from MongoDB to PostgreSQL' -> 'mongodb'; '' when replacement is said without naming the old."""
    match = _REPLACED.search(text)
    if not match:
        return None
    old = (match.groupdict().get("old") or "").strip().lower()
    return _ALIASES.get(old, old)


_DROPPED = re.compile(r"\b(?:no\s+longer\s+(?:use|using)|not\s+(?:use|using)|instead\s+of|from|stopped\s+using|"
                      r"gave\s+up|got\s+rid\s+of)\s+(?P<thing>[\w .+#-]{2,30})", re.I)


def dropped(text: str) -> set[str]:
    """Things the text says are no longer used ('no longer use Chrome', 'from MongoDB', 'instead of Vue')."""
    out: set[str] = set()
    for match in _DROPPED.finditer(text):
        for group in mentions(match.group("thing")).values():
            out |= group
    return out


def conflict(new_text: str, old_text: str) -> tuple[str, str] | None:
    """(kind, reason) where kind = 'supersedes' or 'contradicts'; None when they're compatible."""
    new_things, old_things = mentions(new_text), mentions(old_text)
    for group in set(new_things) & set(old_things):
        gone = dropped(new_text) & old_things[group]
        new_only = new_things[group] - old_things[group]
        old_only = (old_things[group] - new_things[group]) | gone
        if not new_only or not old_only:
            continue
        new_subject, old_subject = subject_words(new_text), subject_words(old_text)
        # same subject: they share a subject word, or neither names one (both about "me")
        if (new_subject and old_subject and not new_subject & old_subject):
            continue
        replaced = replaced_thing(new_text)
        old_name, new_name = ", ".join(sorted(old_only)), ", ".join(sorted(new_only))
        if gone or (replaced is not None and (not replaced or replaced in old_only or replaced in old_things[group])):
            return "supersedes", f"{group}: {new_name} replaced {old_name}"
        if replaced is not None and new_only:
            return "supersedes", f"{group}: now {new_name} (was {old_name})"
        return "contradicts", f"{group}: {old_name} or {new_name}?"
    return None
