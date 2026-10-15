"""Long-term memory: facts the user explicitly asked the assistant to remember.

Rules:
* Only explicit requests are stored (the tools say so; nothing is saved silently).
* Secrets are refused: passwords, PINs, keys, tokens, card/account/ID numbers,
  and long random-looking strings.
* A fact with a topic replaces the previous fact on the same topic
  ("my project folder is X" twice keeps only the newest).
* Everything is local SQLite; the user can list, search, delete and clear it.

Memory firewall (schema 11): every memory records where it came from (``provenance``: you said it, you
accepted a suggestion, you typed it in the Memory window, or you confirmed it after Jarvis had read outside
text), the request it came from (``run_id``), how private it is (``private`` memories are never put into the
AI's prompt; they're only read back when you ask), an optional expiry ("remember for today..."), whether it is
switched off, and every earlier version. "Why do you remember that?" answers from these fields.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from app.memory.database import Database, utc_now_iso
from app.tools.base import ToolError
from app.utils.redaction import redact

MAX_FACT_CHARS = 500
MAX_PROMPT_MEMORIES = 20
MAX_PROMPT_CHARS = 1500

_SENSITIVE = re.compile(
    r"\b(pass\s?words?|passwd|passcodes?|pins?(\s+codes?)?|api[\s_-]?keys?|secret\s+keys?|access\s+tokens?|"
    r"tokens?|private\s+keys?|seed\s+phrases?|recovery\s+(phrases?|codes?)|credit\s+cards?|debit\s+cards?|"
    r"card\s+numbers?|cvv|cvc|bank\s+accounts?|account\s+numbers?|iban|routing\s+numbers?|ssn|"
    r"social\s+security|national\s+id|nid\s+numbers?|passport\s+numbers?|otp|2fa|one[\s-]time\s+codes?|"
    r"login\s+details|credentials?)\b",
    re.I,
)
_RANDOM_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_\-+/=]*\d)(?=[A-Za-z0-9_\-+/=]*[A-Za-z])[A-Za-z0-9_\-+/=]{24,}\b")
_LONG_NUMBER = re.compile(r"(?<!\d)(?:\d[ \-]?){9,}\d(?!\d)")
_FACT = re.compile(r"^(?:that\s+)?(?:my|the|our)\s+(?P<subject>[a-z][a-z0-9 '\-]{1,40}?)\s+(?:is|are|=|:)\s+(?P<value>.+)$", re.I)
_STOP = {"the", "a", "an", "my", "our"}


def topic_key(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP]
    return "_".join(words)[:40]


_PATH = re.compile(r"(?:\b[A-Za-z]:)?[\\/][^\s\"']*")


def check_not_secret(text: str) -> None:
    # File paths often contain GUIDs, hashes or long numbers; they are not secrets.
    without_paths = _PATH.sub(" ", text)
    if (_SENSITIVE.search(text) or redact(text) != text or _RANDOM_TOKEN.search(without_paths)
            or _LONG_NUMBER.search(without_paths)):
        raise ToolError(
            "I don't store passwords, PINs, keys, tokens, or card, account or ID numbers. "
            "Please use a password manager for those.",
            "sensitive",
        )


PROVENANCE = {
    "user": "you asked me to remember it",
    "suggestion": "you accepted my suggestion",
    "typed": "you typed it in the Memory window",
    "confirmed_after_untrusted": "you confirmed it after I had read outside text (a page, email or document)",
    "event": "I recorded it when it happened (you didn't say it; it's what I saw)",
}
STATED_PROVENANCE = frozenset({"user", "typed", "suggestion", "confirmed_after_untrusted"})
_VISIBLE = "disabled = 0 AND superseded_by IS NULL AND (expires_at IS NULL OR expires_at > ?)"


@dataclass(frozen=True)
class Memory:
    id: int
    content: str
    topic: str | None
    value: str | None
    category: str
    updated_at: str
    provenance: str = "user"
    sensitivity: str = "normal"
    expires_at: str | None = None
    revision: int = 1
    disabled: bool = False
    created_at: str = ""
    run_id: str | None = None
    kind: str = "semantic"
    confidence: float = 1.0
    importance: int = 2
    stated: bool = True
    tags: str = ""
    superseded_by: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Memory":
        keys = row.keys()

        def get(name, default=None):
            return row[name] if name in keys else default

        return cls(row["id"], row["content"], row["subject_key"], row["value"], row["category"], row["updated_at"],
                   get("provenance", "user"), get("sensitivity", "normal"), get("expires_at"), get("revision", 1),
                   bool(get("disabled", 0)), get("created_at", ""), get("run_id"), get("kind", "semantic") or "semantic",
                   float(get("confidence", 1.0) or 0), int(get("importance", 2) or 2), bool(get("stated", 1)),
                   get("tags", "") or "", get("superseded_by"))

    def to_dict(self) -> dict:
        data = {"id": self.id, "fact": self.content, "topic": self.topic, "saved": self.updated_at[:10],
                "kind": self.kind}
        if not self.stated:
            data["recorded_by_jarvis"] = True
        if self.superseded_by:
            data["replaced_by"] = self.superseded_by
        if self.sensitivity == "private":
            data["private"] = True
        if self.expires_at:
            data["expires"] = self.expires_at[:16]
        return data


def parse_fact(text: str) -> tuple[str | None, str | None]:
    """'my project folder is D:\\\\Work' -> ('project_folder', 'D:\\\\Work')."""
    match = _FACT.match(text.strip().rstrip("."))
    if not match:
        return None, None
    return topic_key(match.group("subject")), match.group("value").strip().strip("\"'")


class MemoryManager:
    def __init__(self, database: Database) -> None:
        self._db = database
        self.last_touched: int | None = None  # for "forget that" within a session
        self.last_conflicts: list[dict] = []  # what the last remember() found (superseded / possible contradictions)

    # ------------------------------------------------------------------ write

    def remember(self, fact: str, topic: str | None = None, category: str = "general", *, provenance: str = "user",
                 private: bool = False, expires_at: str | None = None, kind: str | None = None,
                 importance: int = 2, tags: str = "") -> tuple[Memory, bool]:
        """Store a fact. Returns (memory, replaced_existing). The old text of a replaced fact is kept as a revision.
        New facts are checked against the others (app/memory/memory2.py): ``last_conflicts`` says what it found."""
        from app.memory.memory2 import KINDS, classify
        from app.core.runs import current

        if provenance not in PROVENANCE:
            raise ToolError("Unknown memory source.", "invalid")
        fact = " ".join(fact.split()).strip()
        if not fact:
            raise ToolError("What should I remember?", "empty")
        if len(fact) > MAX_FACT_CHARS:
            raise ToolError(f"That is too long to remember (max {MAX_FACT_CHARS} characters).", "too_long")
        check_not_secret(fact)
        parsed_topic, value = parse_fact(fact)
        key = topic_key(topic) if topic else parsed_topic
        now = utc_now_iso()
        run = current()
        run_id = run.id if run is not None else None
        sensitivity = "private" if private else "normal"
        kind = kind if kind in KINDS else classify(fact)
        stated = provenance in STATED_PROVENANCE
        confidence = 1.0 if provenance in ("user", "typed") else 0.9 if stated else 0.8
        importance = max(1, min(3, int(importance)))
        self.last_conflicts: list[dict] = []
        with self._db.transaction() as conn:
            existing = None
            if key:
                existing = conn.execute("SELECT * FROM memories WHERE subject_key = ?", (key,)).fetchone()
            if existing is None:
                existing = conn.execute("SELECT * FROM memories WHERE lower(content) = lower(?)", (fact,)).fetchone()
            if existing is not None:
                conn.execute("INSERT INTO memory_revisions (memory_id, revision, content, provenance, changed_at) "
                             "VALUES (?, ?, ?, ?, ?)", (existing["id"], existing["revision"], existing["content"],
                                                        existing["provenance"], now))
                conn.execute(
                    "UPDATE memories SET content = ?, subject_key = ?, value = ?, category = ?, updated_at = ?, "
                    "provenance = ?, run_id = ?, sensitivity = ?, expires_at = ?, revision = revision + 1, disabled = 0, "
                    "kind = ?, confidence = ?, importance = ?, stated = ?, tags = ?, superseded_by = NULL WHERE id = ?",
                    (fact, key, value, category, now, provenance, run_id, sensitivity, expires_at, kind, confidence,
                     importance, int(stated), tags[:200], existing["id"]),
                )
                memory_id, replaced = existing["id"], True
            else:
                cursor = conn.execute(
                    "INSERT INTO memories (content, category, subject_key, value, source, created_at, updated_at, "
                    "provenance, run_id, sensitivity, expires_at, kind, confidence, importance, stated, tags) "
                    "VALUES (?, ?, ?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (fact, category, key, value, now, now, provenance, run_id, sensitivity, expires_at, kind,
                     confidence, importance, int(stated), tags[:200]),
                )
                memory_id, replaced = int(cursor.lastrowid), False
        self.last_touched = memory_id
        self.last_conflicts = self._check_conflicts(memory_id)
        return self.get(memory_id), replaced  # type: ignore[return-value]

    def record_event(self, text: str, *, importance: int = 2) -> Memory:
        """Episodic memory Jarvis writes itself (a mission finished): marked as recorded, not stated by you."""
        memory, _ = self.remember(text, category="event", provenance="event", kind="episodic", importance=importance)
        return memory

    # ------------------------------------------------------------------ contradictions

    def _check_conflicts(self, memory_id: int) -> list[dict]:
        from app.memory.memory2 import conflict

        new = self.get(memory_id)
        if new is None:
            return []
        found = []
        now = utc_now_iso()
        for old in self.all(500):
            if old.id == new.id:
                continue
            verdict = conflict(new.content, old.content)
            if verdict is None:
                continue
            relation, reason = verdict
            with self._db.transaction() as conn:
                conn.execute("INSERT OR IGNORE INTO memory_links (from_id, to_id, relation, created_at) VALUES "
                             "(?, ?, ?, ?)", (new.id, old.id, relation, now))
                if relation == "supersedes":
                    conn.execute("UPDATE memories SET superseded_by = ? WHERE id = ?", (new.id, old.id))
                else:
                    conn.execute("INSERT INTO memory_conflicts (new_id, old_id, reason, created_at) VALUES "
                                 "(?, ?, ?, ?)", (new.id, old.id, reason, now))
            found.append({"relation": relation, "old_id": old.id, "old": old.content, "reason": reason})
        return found

    def conflicts(self, status: str = "open") -> list[dict]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT c.id, c.new_id, c.old_id, c.reason, c.status, c.created_at, n.content AS new, "
                                "o.content AS old FROM memory_conflicts c JOIN memories n ON n.id = c.new_id JOIN "
                                "memories o ON o.id = c.old_id WHERE c.status = ? ORDER BY c.id DESC",
                                (status,)).fetchall()
        return [dict(r) for r in rows]

    def resolve_conflict(self, conflict_id: int, keep: str) -> dict:
        """keep = 'new' (the old one is superseded), 'old' (the new one is superseded) or 'both' (both true)."""
        if keep not in ("new", "old", "both"):
            raise ToolError("Keep which one: new, old or both?", "invalid")
        with self._db.transaction() as conn:
            row = conn.execute("SELECT new_id, old_id, status FROM memory_conflicts WHERE id = ?",
                               (conflict_id,)).fetchone()
            if row is None or row["status"] != "open":
                raise ToolError(f"There's no open conflict #{conflict_id}.", "not_found")
            if keep == "new":
                conn.execute("UPDATE memories SET superseded_by = ? WHERE id = ?", (row["new_id"], row["old_id"]))
            elif keep == "old":
                conn.execute("UPDATE memories SET superseded_by = ? WHERE id = ?", (row["old_id"], row["new_id"]))
            conn.execute("UPDATE memory_conflicts SET status = ? WHERE id = ?", (f"kept_{keep}", conflict_id))
        return {"id": conflict_id, "kept": keep}

    def links(self, memory: Memory) -> list[dict]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT l.from_id, l.to_id, l.relation, m.content FROM memory_links l JOIN memories m "
                                "ON m.id = CASE WHEN l.from_id = ? THEN l.to_id ELSE l.from_id END WHERE l.from_id = ? "
                                "OR l.to_id = ?", (memory.id, memory.id, memory.id)).fetchall()
        return [dict(r) for r in rows]

    def forget(self, memory_id: int) -> Memory:
        memory = self.get(memory_id)
        if memory is None:
            raise ToolError(f"There is no memory number {memory_id}.", "not_found")
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.execute("DELETE FROM memory_revisions WHERE memory_id = ?", (memory_id,))  # forgotten means gone
            conn.execute("DELETE FROM memory_links WHERE from_id = ? OR to_id = ?", (memory_id, memory_id))
            conn.execute("DELETE FROM memory_conflicts WHERE new_id = ? OR old_id = ?", (memory_id, memory_id))
            conn.execute("UPDATE memories SET superseded_by = NULL WHERE superseded_by = ?", (memory_id,))
        if self.last_touched == memory_id:
            self.last_touched = None
        return memory

    def clear(self) -> int:
        with self._db.transaction() as conn:
            count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            conn.execute("DELETE FROM memories")
            conn.execute("DELETE FROM memory_revisions")
            conn.execute("DELETE FROM memory_links")
            conn.execute("DELETE FROM memory_conflicts")
        self.last_touched = None
        return int(count)

    # ------------------------------------------------------------------- read

    def get(self, memory_id: int) -> Memory | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return Memory.from_row(row) if row else None

    def by_topic(self, topic: str) -> Memory | None:
        with self._db.connect() as conn:
            row = conn.execute(f"SELECT * FROM memories WHERE subject_key = ? AND {_VISIBLE}",
                               (topic_key(topic), utc_now_iso())).fetchone()
        return Memory.from_row(row) if row else None

    def all(self, limit: int = 200, *, include_hidden: bool = False) -> list[Memory]:
        """Active memories (not switched off, not expired); ``include_hidden`` for the Memory window."""
        where = "" if include_hidden else f"WHERE {_VISIBLE}"
        params = () if include_hidden else (utc_now_iso(),)
        with self._db.connect() as conn:
            rows = conn.execute(f"SELECT * FROM memories {where} ORDER BY updated_at DESC, id DESC LIMIT ?",
                                (*params, limit)).fetchall()
        return [Memory.from_row(r) for r in rows]

    # ------------------------------------------------------------------ the firewall controls

    def set_disabled(self, memory: Memory, disabled: bool) -> Memory:
        with self._db.transaction() as conn:
            conn.execute("UPDATE memories SET disabled = ? WHERE id = ?", (int(disabled), memory.id))
        return self.get(memory.id)  # type: ignore[return-value]

    def set_expiry(self, memory: Memory, expires_at: str | None) -> Memory:
        with self._db.transaction() as conn:
            conn.execute("UPDATE memories SET expires_at = ? WHERE id = ?", (expires_at, memory.id))
        return self.get(memory.id)  # type: ignore[return-value]

    def set_private(self, memory: Memory, private: bool) -> Memory:
        with self._db.transaction() as conn:
            conn.execute("UPDATE memories SET sensitivity = ? WHERE id = ?", ("private" if private else "normal", memory.id))
        return self.get(memory.id)  # type: ignore[return-value]

    def history(self, memory: Memory) -> list[dict]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT revision, content, provenance, changed_at FROM memory_revisions WHERE memory_id = ? "
                                "ORDER BY revision DESC", (memory.id,)).fetchall()
        return [dict(r) for r in rows]

    def purge_expired(self) -> int:
        with self._db.transaction() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?",
                                              (utc_now_iso(),))]
            for memory_id in ids:
                conn.execute("DELETE FROM memory_revisions WHERE memory_id = ?", (memory_id,))
                conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return len(ids)

    def explain(self, memory: Memory) -> str:
        """Why Jarvis remembers this, and where it came from - from the stored fields only."""
        parts = [f"I remember \"{memory.content}\" because {PROVENANCE.get(memory.provenance, memory.provenance)}"]
        when = (memory.created_at or memory.updated_at)[:10]
        if when:
            parts[0] += f", first on {when}"
        if memory.run_id:
            with self._db.connect() as conn:
                row = conn.execute("SELECT source FROM agent_runs WHERE id = ?", (memory.run_id,)).fetchone()
            if row is not None:
                parts[0] += {"voice": " (by voice)", "text": " (typed)", "phone": " (from your phone)"}.get(row[0], "")
        parts[0] += "."
        if memory.revision > 1:
            previous = self.history(memory)
            parts.append(f"It changed {memory.revision - 1} time(s)" +
                         (f"; before it was \"{previous[0]['content']}\"." if previous else "."))
        kinds = {"semantic": "a fact", "episodic": "something that happened", "procedural": "how you like things "
                 "done", "goal": "a goal of yours"}
        parts.append(f"It's {kinds.get(memory.kind, memory.kind)}; "
                     + ("you said it (confidence " if memory.stated else "I recorded it, you didn't say it (confidence ")
                     + f"{memory.confidence:.0%}), importance {memory.importance}/3.")
        if memory.superseded_by:
            newer = self.get(memory.superseded_by)
            parts.append("It's out of date: replaced by " + (f"\"{newer.content}\"" if newer else "a newer memory")
                         + ", so I don't use it.")
        for link in self.links(memory):
            if link["relation"] == "supersedes" and link["from_id"] == memory.id:
                parts.append(f"It replaced \"{link['content']}\".")
        for item in self.conflicts():
            if memory.id in (item["new_id"], item["old_id"]):
                other = item["old"] if item["new_id"] == memory.id else item["new"]
                parts.append(f"It may contradict \"{other}\" (conflict #{item['id']}, not settled yet).")
        if memory.sensitivity == "private":
            parts.append("It's private: I don't give it to the AI model, I only read it back when you ask.")
        if memory.expires_at:
            parts.append(f"It expires {memory.expires_at[:16].replace('T', ' ')} (UTC).")
        if memory.disabled:
            parts.append("It's switched off: I don't use it.")
        return " ".join(parts)

    def search(self, query: str, limit: int = 20, *, include_hidden: bool = False) -> list[Memory]:
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _STOP and len(w) > 1]
        if not words:
            return self.all(limit, include_hidden=include_hidden)
        memories = self.all(500, include_hidden=include_hidden)
        scored = []
        for memory in memories:
            haystack = f"{memory.content} {memory.topic or ''}".lower().replace("_", " ")
            hits = sum(1 for w in words if w in haystack)
            if hits:
                scored.append((hits, memory))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [m for _, m in scored[:limit]]

    def find_one(self, description: str, *, include_hidden: bool = False) -> Memory:
        """The single memory a description like 'my project folder' refers to."""
        text = description.strip().lower()
        if text in ("that", "it", "this", "the last one", "last one"):
            if self.last_touched is None or self.get(self.last_touched) is None:
                raise ToolError("I'm not sure which memory you mean. Say what it was about.", "ambiguous")
            return self.get(self.last_touched)  # type: ignore[return-value]
        topic = self.by_topic(description)
        if topic is not None:
            return topic
        matches = self.search(description, limit=5, include_hidden=include_hidden)
        if not matches:
            raise ToolError(f"I don't have anything saved about '{description}'.", "not_found")
        if len(matches) > 1:
            best, second = matches[0], matches[1]
            best_hits = self._hits(description, best)
            if best_hits == self._hits(description, second):
                options = "; ".join(f"#{m.id}: {m.content}" for m in matches[:3])
                raise ToolError(f"That could be several memories ({options}). Which one?", "ambiguous")
        return matches[0]

    def _hits(self, query: str, memory: Memory) -> int:
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _STOP and len(w) > 1]
        haystack = f"{memory.content} {memory.topic or ''}".lower().replace("_", " ")
        return sum(1 for w in words if w in haystack)

    def prompt_section(self) -> str:
        """Saved facts for the system prompt (newest first, size-limited)."""
        lines, used = [], 0
        usable = [m for m in self.all(MAX_PROMPT_MEMORIES * 3) if m.sensitivity != "private"]
        usable.sort(key=lambda m: -m.importance)  # stable: newest first within the same importance
        for memory in usable[:MAX_PROMPT_MEMORIES]:
            tag = {"procedural": " (how they like it done)", "episodic": " (happened)", "goal": " (goal)"}.get(
                memory.kind, "")
            line = f"- {memory.content}{tag}"
            if used + len(line) > MAX_PROMPT_CHARS:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)
