"""Search by meaning over past conversations and your documents (all local).

Texts are turned into embeddings by a local Ollama embedding model (EMBEDDING_MODEL,
default nomic-embed-text: ``ollama pull nomic-embed-text``, 274 MB) and stored in the
SQLite database. A query is embedded the same way and compared (cosine similarity).

* Conversation history (CONVERSATION_MEMORY=true): each finished exchange is saved,
  with passwords/keys/card numbers redacted first; "what did I say about X last week?"
* Documents (DOCUMENT_FOLDERS): .txt .md .csv .pdf .docx and code files are split into
  passages and indexed; "what's the deadline in the tender PDF?" answers from them.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from app.memory.database import Database, utc_now_iso
from app.utils.logging_setup import get_logger, log_event
from app.utils.redaction import redact

_log = get_logger("semantic")
Embedder = Callable[[Sequence[str]], Awaitable[list[list[float]]]]
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".log", ".json", ".py", ".js", ".ts", ".html", ".css", ".java", ".c",
                   ".cpp", ".cs", ".php", ".sql", ".yml", ".yaml", ".xml", ".ini", ".rst"}
DOC_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".docx"}
MAX_FILE_BYTES = 30_000_000
MAX_CHUNKS_PER_FILE = 400
CHUNK_CHARS, CHUNK_OVERLAP = 900, 150


@dataclass(frozen=True)
class Hit:
    kind: str
    source: str
    chunk: int
    text: str
    score: float
    created_at: str


# ------------------------------------------------------------------ keyword matching (hybrid search)

_TOKEN = re.compile(r"[\w][\w.\-]*", re.UNICODE)
_STOP = frozenset({"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "what", "which",
                   "who", "how", "when", "where", "my", "me", "i", "it", "that", "this", "with", "about", "do", "does"})
VECTOR_WEIGHT, KEYWORD_WEIGHT = 0.7, 0.3
KEYWORD_ONLY_MIN = 0.6  # a passage that contains this share of the query's words is a hit even if its meaning score is low


def query_terms(text: str) -> list[str]:
    seen: list[str] = []
    for token in _TOKEN.findall(text.lower()):
        if len(token) > 1 and token not in _STOP and token not in seen:
            seen.append(token)
    return seen


def keyword_score(terms: Sequence[str], text: str) -> float:
    """The share of the query's words found as whole words in ``text`` (0..1). Exact matches (names, numbers,
    error codes) that meaning-based search tends to blur."""
    if not terms:
        return 0.0
    words = set(_TOKEN.findall(text.lower()))
    return sum(1 for t in terms if t in words) / len(terms)


def fuse(vector_score: float, keyword: float) -> float:
    return VECTOR_WEIGHT * vector_score + KEYWORD_WEIGHT * keyword


# ------------------------------------------------------------------ text extraction


def read_docx(path: Path) -> str:
    """Text of a .docx without extra libraries (it's a zip with word/document.xml)."""
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    import html

    return html.unescape(text)


def read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF support needs pypdf: pip install -r requirements.txt") from exc
    reader = PdfReader(str(path))
    # each page starts with a "[p. N]" marker, so answers can say which page they come from (paged_chunks)
    return "\n\n".join(f"[p. {n}]\n{page.extract_text() or ''}" for n, page in enumerate(reader.pages[:500], 1))


_PAGE = re.compile(r"\[p\. (\d{1,4})\]")


def paged_chunks(text: str) -> list[str]:
    """chunks() for text with "[p. N]" page markers: every passage starts with the page it begins on."""
    pieces = chunks(text)
    if not _PAGE.search(text):
        return pieces
    out, pos = [], 0
    flat = re.sub(r"[ \t]+", " ", text)
    for piece in pieces:
        found = flat.find(piece[:60], pos)
        start = found if found >= 0 else pos
        pos = max(pos, start)
        before = list(_PAGE.finditer(flat, 0, start + 1))
        page = before[-1].group(1) if before else "1"
        out.append(piece if piece.startswith("[p. ") else f"[p. {page}] {piece}")
    return out


SENSITIVE_PARTS = {"appdata", "$recycle.bin", ".ssh", ".gnupg"}
SENSITIVE_NAME = re.compile(r"(?:^|[\W_])(?:passwords?|credentials?|secrets?|private[\W_]?key|recovery[\W_]?codes?)"
                            r"(?:[\W_]|$)|^\.env", re.I)


def is_sensitive(path: Path) -> bool:
    """Never indexed even inside a chosen folder: system / app-data folders and files named like passwords or keys."""
    parts = {p.lower() for p in path.parts}
    return bool(parts & SENSITIVE_PARTS) or bool(SENSITIVE_NAME.search(path.stem))


def read_document(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return read_pdf(path)
    if ext == ".docx":
        return read_docx(path)
    return path.read_text(encoding="utf-8", errors="ignore")


def chunks(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Passages of about ``size`` characters, cut at paragraph/sentence ends where possible."""
    text = re.sub(r"[ \t]+", " ", text).strip()
    out: list[str] = []
    start = 0
    while start < len(text) and len(out) < MAX_CHUNKS_PER_FILE:
        end = min(len(text), start + size)
        if end < len(text):
            cut = max(text.rfind("\n\n", start, end), text.rfind(". ", start, end), text.rfind("। ", start, end))
            if cut > start + size // 2:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


# ------------------------------------------------------------------ store


def _vector(values: Iterable[float]) -> np.ndarray:
    v = np.asarray(list(values), dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


class SemanticIndex:
    def __init__(self, database: Database, embed: Embedder, model: str) -> None:
        self.database = database
        self.embed = embed
        self.model = model

    # ---------------------------------------------------------------- writing

    async def _store(self, kind: str, source: str, texts: list[str], mtime: float | None = None) -> int:
        if not texts:
            return 0
        vectors = []
        for start in range(0, len(texts), 32):  # the embedding API takes batches
            vectors.extend(await self.embed(texts[start:start + 32]))
        now = utc_now_iso()
        with self.database.transaction() as conn:
            conn.executemany(
                "INSERT INTO semantic_items (kind, source, chunk, text, embedding, model, mtime, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(kind, source, i, t, _vector(v).tobytes(), self.model, mtime, now)
                 for i, (t, v) in enumerate(zip(texts, vectors))])
        return len(texts)

    async def add_exchange(self, user_text: str, reply_text: str, when: datetime | None = None) -> bool:
        """Save one finished exchange (secrets redacted). Returns False if it wasn't worth saving."""
        user_text, reply_text = redact(user_text.strip()), redact(reply_text.strip())
        if len(user_text) < 4 or not reply_text:
            return False
        stamp = (when or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
        text = f"[{stamp}] You: {user_text[:1500]}\nJarvis: {reply_text[:1500]}"
        await self._store("history", stamp, [text])
        return True

    def recent_history(self, limit: int = 4) -> list[tuple[str, str]]:
        """The last exchanges as (user text, reply) - to continue where you left off after a restart."""
        with self.database.connect() as conn:
            rows = conn.execute("SELECT text FROM semantic_items WHERE kind='history' ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall()
        out = []
        for (text,) in reversed(rows):
            match = re.match(r"^\[[^\]]+\] You: (.*?)\nJarvis: (.*)$", text, re.S)
            if match:
                out.append((match.group(1), match.group(2)))
        return out

    def forget_history(self) -> int:
        with self.database.transaction() as conn:
            return conn.execute("DELETE FROM semantic_items WHERE kind='history'").rowcount

    async def index_folder(self, folder: Path, on_progress: Callable[[str], None] | None = None) -> dict[str, int]:
        """Index new or changed documents in ``folder`` (recursively); unchanged files are skipped."""
        folder = Path(folder)
        if not folder.is_dir():
            raise ValueError(f"The folder {folder} doesn't exist.")
        with self.database.connect() as conn:
            known = {row[0]: row[1] for row in conn.execute(
                "SELECT source, MAX(mtime) FROM semantic_items WHERE kind='document' GROUP BY source")}
        stats = {"indexed": 0, "unchanged": 0, "skipped": 0, "passages": 0, "removed": 0}
        stats["removed"] = self._purge_missing(folder, known)
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in DOC_EXTENSIONS or any(
                    part.startswith(".") or part in ("node_modules", "__pycache__", ".git") for part in path.parts) \
                    or is_sensitive(path.relative_to(folder) if path.is_relative_to(folder) else path):
                continue
            try:
                info = path.stat()
                if info.st_size > MAX_FILE_BYTES:
                    stats["skipped"] += 1
                    continue
                if known.get(str(path)) == info.st_mtime:
                    stats["unchanged"] += 1
                    continue
                passages = paged_chunks(read_document(path))
            except Exception:
                _log.warning("could not read a document", exc_info=True)
                stats["skipped"] += 1
                continue
            with self.database.transaction() as conn:
                conn.execute("DELETE FROM semantic_items WHERE kind='document' AND source=?", (str(path),))
            if on_progress:
                on_progress(path.name)
            stats["passages"] += await self._store("document", str(path), passages, info.st_mtime)
            stats["indexed"] += 1
        log_event(_log, "documents_indexed", **stats)
        return stats

    def _purge_missing(self, folder: Path, known: dict) -> int:
        """Drop the passages of documents under ``folder`` that no longer exist, so answers never cite a deleted file."""
        gone = []
        for source in known:
            try:
                inside = Path(source).resolve().is_relative_to(folder.resolve())
            except (OSError, ValueError):
                continue
            if inside and not Path(source).exists():
                gone.append(source)
        if gone:
            with self.database.transaction() as conn:
                conn.executemany("DELETE FROM semantic_items WHERE kind='document' AND source=?", [(g,) for g in gone])
        return len(gone)

    def document_count(self) -> int:
        with self.database.connect() as conn:
            return conn.execute("SELECT COUNT(DISTINCT source) FROM semantic_items WHERE kind='document'").fetchone()[0]

    # ---------------------------------------------------------------- searching

    async def search(self, query: str, kind: str, limit: int = 5, min_score: float = 0.3) -> list[Hit]:
        with self.database.connect() as conn:
            rows = conn.execute("SELECT kind, source, chunk, text, embedding, created_at FROM semantic_items "
                                "WHERE kind=? AND embedding IS NOT NULL", (kind,)).fetchall()
        if not rows:
            return []
        terms = query_terms(query)
        try:
            query_vector = _vector((await self.embed([query]))[0])
        except Exception:
            if not terms:
                raise
            # no embedding model right now: exact-word search still works, and says so in the log
            _log.warning("embedding unavailable; using keyword search only", exc_info=True)
            query_vector = None
        if query_vector is not None:
            matrix = np.stack([np.frombuffer(r[4], dtype=np.float32) for r in rows])
            if matrix.shape[1] != query_vector.shape[0]:
                raise ValueError("The embedding model changed; re-index your documents.")
            vector_scores = matrix @ query_vector
        else:
            vector_scores = np.zeros(len(rows), dtype=np.float32)
        scored = []
        for i, row in enumerate(rows):
            vec, kw = float(vector_scores[i]), keyword_score(terms, row[3])
            if vec >= min_score or kw >= KEYWORD_ONLY_MIN:
                scored.append((fuse(max(vec, 0.0), kw), i))
        scored.sort(key=lambda pair: -pair[0])
        return [Hit(rows[i][0], rows[i][1], rows[i][2], rows[i][3], score, rows[i][5]) for score, i in scored[:limit]]
