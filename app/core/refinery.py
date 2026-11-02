"""Personal data refinery: an incremental, local pipeline from your documents to source-linked FACTS you can review.

For each authorized folder it detects new / changed / removed files (content hash), reads them with the existing readers (the same ones the
document index uses; sensitive and excluded files are skipped), and extracts with plain rules - no model, nothing leaves the PC:
deadlines, tasks, decisions and "Label: value" facts. Every fact keeps its source file, line, the file's version (hash) and extraction
time, and gets a confidence. Rules, not guesses: an explicit marker ("TODO:", "Deadline:", "Decision:") is 0.9, a looser sentence 0.6.

Trust: nothing is trusted automatically. A fact is "extracted" (source-backed) until YOU confirm it; below 0.85 it also needs review.
Only confirmed facts can be copied into memory (provenance "suggestion"). A changed file marks facts that vanished from it as stale; a removed
file marks all its facts stale. Two sources giving different values for the same label are reported as a conflict, never merged silently.
Facts live in the settings store (no new database), capped at 2000. The semantic document index stays the one retrieval path.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

FACTS_KEY, FILES_KEY = "refinery.facts", "refinery.files"
MAX_FACTS = 2000
MAX_FILES = 3000
MAX_BYTES = 2_000_000
EXTENSIONS = {".txt", ".md", ".rst", ".csv", ".docx", ".pdf", ".html", ".htm", ".ics", ".json"}
TRUST_AT = 0.85
_DATE = (r"(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}"
         r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})")
_DEADLINE = re.compile(r"\b(deadline|due(?:\s+date)?|submit(?:ted)?\s+by|by)\b[^\n]{0,40}?" + _DATE, re.I)
_TASK = re.compile(r"^\s*(?:[-*]\s*\[\s\]\s*|todo\s*[:\-]\s*|action(?:\s+item)?\s*[:\-]\s*|task\s*[:\-]\s*)(?P<t>.{4,160})", re.I)
_DECISION = re.compile(r"^\s*(?:decision\s*[:\-]\s*|(?:we|i)\s+(?:have\s+)?decided\s+(?:to|that)\s+)(?P<t>.{4,200})", re.I)
_KV = re.compile(r"^\s*(?P<k>[A-Za-z][A-Za-z ]{2,30}?)\s*:\s*(?P<v>[^\n:]{1,80})$")
_LABELS = {"budget", "owner", "client", "price", "cost", "total", "venue", "location", "deadline", "manager", "contact", "version", "rate", "salary"}


@dataclass
class Fact:
    id: str
    kind: str                # deadline | task | decision | value
    text: str
    key: str                 # what it is about (for conflict detection)
    value: str
    source: str
    line: int
    version: str             # sha256 (first 12) of the file it came from
    extracted_at: str
    confidence: float
    status: str = "extracted"        # extracted | confirmed | rejected | stale
    stale_reason: str = ""

    @property
    def needs_review(self) -> bool:
        return self.status == "extracted" and self.confidence < TRUST_AT


def parse_date(text: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(re.sub(r"\s+", " ", text.strip().replace(".", "") if fmt.startswith("%d %b") or "%b" in fmt else text.strip()), fmt).date()
        except ValueError:
            continue
    return None


def _fid(kind: str, text: str, source: str) -> str:
    return hashlib.sha256(f"{kind}|{' '.join(text.lower().split())}|{source}".encode()).hexdigest()[:14]


def extract(text: str, source: str, version: str, now: datetime | None = None) -> list[Fact]:
    """Facts found in one document's text (pure). A line yields at most one fact."""
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    out: list[Fact] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or len(line) > 400:
            continue
        fact = None
        m = _DECISION.match(line)
        if m:
            fact = ("decision", m.group("t").strip(" ."), "decision", m.group("t").strip(" ."), 0.9 if line.lower().startswith("decision") else 0.7)
        if fact is None:
            m = _TASK.match(line)
            if m:
                explicit = bool(re.match(r"^\s*(?:[-*]\s*\[\s\]|todo|action|task)", line, re.I))
                fact = ("task", m.group("t").strip(" ."), "task", m.group("t").strip(" ."), 0.9 if explicit else 0.6)
        if fact is None:
            m = _DEADLINE.search(line)
            d = parse_date(m.group(2)) if m else None
            if m and d:
                explicit = m.group(1).lower().startswith(("deadline", "due", "submit"))
                fact = ("deadline", line[:160], "deadline:" + re.sub(r"\W+", " ", line[: m.start()].lower()).strip()[:40], d.isoformat(), 0.9 if explicit else 0.6)
        if fact is None:
            m = _KV.match(line)
            if m and m.group("k").strip().lower() in _LABELS:
                fact = ("value", line[:160], m.group("k").strip().lower(), m.group("v").strip(), 0.9)
        if fact:
            kind, body, key, value, conf = fact
            out.append(Fact(_fid(kind, body, source), kind, body, key, value, source, n, version, stamp, conf))
    return out


# ---------------------------------------------------------------------------------------------------- the pipeline
def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class RunReport:
    scanned: int = 0
    new: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    skipped: int = 0
    facts_added: int = 0
    facts_stale: int = 0
    errors: list[str] = None

    def describe(self) -> str:
        return (f"Scanned {self.scanned} file(s): {self.new} new, {self.changed} changed, {self.removed} removed, {self.unchanged} unchanged, "
                f"{self.skipped} skipped. Facts: {self.facts_added} added, {self.facts_stale} marked stale."
                + (f" Problems: {'; '.join(self.errors[:3])}." if self.errors else ""))


def run(store: Any, folders: list[Path], *, reader: Callable[[Path], str], sensitive: Callable[[Path], bool] = lambda p: False,
        excluded: Callable[[Path], bool] = lambda p: False, now: datetime | None = None, limit_files: int = MAX_FILES) -> RunReport:
    """One incremental pass. Only new or changed files are read; nothing is deleted from disk; a failure on one file never stops the rest."""
    report = RunReport(errors=[])
    files_state: dict[str, dict[str, Any]] = dict(store.get(FILES_KEY, {}) or {})
    facts: dict[str, dict[str, Any]] = {f["id"]: f for f in (store.get(FACTS_KEY, []) or [])}
    seen: set[str] = set()
    for folder in folders:
        if not Path(folder).is_dir():
            report.errors.append(f"{folder} is not a folder")
            continue
        for path in sorted(Path(folder).rglob("*")):
            if report.scanned >= limit_files:
                break
            if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
                continue
            if sensitive(path) or excluded(path):
                report.skipped += 1
                continue
            try:
                if path.stat().st_size > MAX_BYTES:
                    report.skipped += 1
                    continue
                digest = _hash(path)
            except OSError as exc:
                report.errors.append(f"{path.name}: {exc.strerror or exc}")
                continue
            key = str(path)
            seen.add(key)
            report.scanned += 1
            previous = files_state.get(key)
            if previous and previous["sha"] == digest:
                report.unchanged += 1
                continue
            try:
                found = extract(reader(path), key, digest[:12], now)
            except Exception as exc:
                report.errors.append(f"{path.name}: unreadable ({type(exc).__name__})")
                continue
            report.new += previous is None
            report.changed += previous is not None
            ids = set()
            for fact in found:
                ids.add(fact.id)
                if fact.id in facts:                                    # same fact again: keep status, refresh version, un-stale
                    old = facts[fact.id]
                    old.update(version=fact.version, line=fact.line, stale_reason="")
                    if old["status"] == "stale":
                        old["status"] = "extracted"
                else:
                    facts[fact.id] = asdict(fact)
                    report.facts_added += 1
            for f in facts.values():                                   # facts this file no longer says
                if f["source"] == key and f["id"] not in ids and f["status"] in ("extracted", "confirmed"):
                    f["status"], f["stale_reason"] = "stale", "no longer in the file (it changed)"
                    report.facts_stale += 1
            files_state[key] = {"sha": digest, "version": (previous["version"] + 1) if previous else 1, "at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")}
    roots = [str(Path(f)) for f in folders]
    for key in list(files_state):
        if key not in seen and any(key.startswith(r) for r in roots):
            report.removed += 1
            del files_state[key]
            for f in facts.values():
                if f["source"] == key and f["status"] in ("extracted", "confirmed"):
                    f["status"], f["stale_reason"] = "stale", "the source file was removed"
                    report.facts_stale += 1
    store.set(FILES_KEY, dict(list(files_state.items())[-MAX_FILES:]))
    store.set(FACTS_KEY, sorted(facts.values(), key=lambda f: f["extracted_at"])[-MAX_FACTS:])
    return report


# ---------------------------------------------------------------------------------------------------- review, conflicts, promotion
def all_facts(store: Any) -> list[Fact]:
    return [Fact(**f) for f in (store.get(FACTS_KEY, []) or [])]


def conflicts(facts: list[Fact]) -> list[tuple[Fact, Fact]]:
    """Same label from DIFFERENT sources with different values (live facts only). Reported, never merged."""
    live = [f for f in facts if f.status in ("extracted", "confirmed") and f.kind in ("value", "deadline")]
    out = []
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            if a.key == b.key and (a.kind != "deadline" or len(a.key) > 14) and a.source != b.source and a.value.strip().lower() != b.value.strip().lower():
                out.append((a, b))
    return out


def review_queue(facts: list[Fact], today: date | None = None) -> list[tuple[Fact, str]]:
    """What needs your eyes: low confidence, conflicts, and anything consequential (a deadline within 14 days)."""
    today = today or date.today()
    out: list[tuple[Fact, str]] = []
    seen: set[str] = set()
    for a, b in conflicts(facts):
        for f, other in ((a, b), (b, a)):
            if f.id not in seen:
                seen.add(f.id)
                out.append((f, f"conflicts with '{other.value}' in {Path(other.source).name}"))
    for f in facts:
        if f.id in seen or f.status != "extracted":
            continue
        if f.kind == "deadline":
            try:
                days = (date.fromisoformat(f.value) - today).days
            except ValueError:
                days = None
            if days is not None and -1 <= days <= 14:
                seen.add(f.id)
                out.append((f, f"deadline in {days} day(s) - confirm it so it is not missed" if days >= 0 else "deadline was yesterday"))
                continue
        if f.needs_review:
            seen.add(f.id)
            out.append((f, f"low confidence ({f.confidence:.1f}): a loose sentence, not an explicit marker"))
    return out


def set_status(store: Any, fact_id: str, status: str) -> Fact:
    facts = store.get(FACTS_KEY, []) or []
    for f in facts:
        if f["id"].startswith(fact_id):
            if status not in ("confirmed", "rejected"):
                raise ValueError("status must be confirmed or rejected")
            f["status"] = status
            store.set(FACTS_KEY, facts)
            return Fact(**f)
    raise KeyError(fact_id)


def promote(store: Any, memory: Any, fact_id: str) -> str:
    """Copy a CONFIRMED fact into memory with its source in the text. Unconfirmed facts are refused."""
    fact = next((f for f in all_facts(store) if f.id.startswith(fact_id)), None)
    if fact is None:
        raise KeyError(fact_id)
    if fact.status != "confirmed":
        raise ValueError("Confirm the fact first: only facts you confirmed are copied into memory.")
    text = f"{fact.text} (from {Path(fact.source).name}, line {fact.line}; confirmed by you)"
    memory.remember(text, category="documents", provenance="suggestion", kind="fact" if fact.kind == "value" else None)
    return text


def describe_fact(f: Fact) -> str:
    return (f"[{f.id[:6]}] {f.kind}: {f.text}  - {Path(f.source).name} line {f.line}, v{f.version[:6]}, confidence {f.confidence:.1f}, {f.status}"
            + (f" ({f.stale_reason})" if f.stale_reason else ""))
