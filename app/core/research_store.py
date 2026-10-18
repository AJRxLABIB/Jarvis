"""Research projects: a question, the sources read, and the claims each source supports - kept so the research can be
reproduced, re-checked and trusted.

* A **sourced** claim must point at a stored source (the database refuses one that doesn't); an **inference** is your
  or Jarvis's analysis and is always shown separately, labelled as inference - never as a sourced fact.
* The same page is stored once (addresses are normalised: tracking parameters, fragments and a trailing slash are
  ignored), and the same text found under two addresses is flagged as a duplicate.
* Conflicts are only reported where it can be shown: two sources give the same topic with different numbers. Other
  disagreements are not guessed at.
* ``refresh`` compares the pages as they are now with what was stored. A source whose text changed marks its claims
  "needs recheck" - old results stay visible and are clearly separated from the new ones.
* Nothing is fetched here; the tool passes page text in. Page text is untrusted data and only ever used as evidence.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.tools.base import ToolError

_TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_)", re.I)
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalise_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ToolError("A source needs a web address starting with http:// or https://.", "invalid_url")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def content_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode("utf-8")).hexdigest()


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


class ResearchStore:
    def __init__(self, database) -> None:
        self._db = database

    # --------------------------------------------------------------- projects

    def create(self, name: str, question: str, criteria: str = "") -> int:
        name = " ".join(name.split())
        if not name or len(name) > 80 or not question.strip():
            raise ToolError("A research project needs a short name and a question.", "invalid_project")
        try:
            with self._db.transaction() as conn:
                cursor = conn.execute("INSERT INTO research_projects (name, question, criteria, created_at, updated_at) "
                                      "VALUES (?, ?, ?, ?, ?)", (name, question.strip()[:400], criteria.strip()[:400],
                                                                 now_iso(), now_iso()))
                return int(cursor.lastrowid)
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise ToolError(f"There is already a research project called '{name}'.", "exists") from None
            raise

    def project(self, name: str) -> dict[str, Any]:
        wanted = " ".join(name.lower().split())
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM research_projects ORDER BY name").fetchall()
        exact = [r for r in rows if r["name"].lower() == wanted]
        found = exact or [r for r in rows if wanted and wanted in r["name"].lower()]
        if not found:
            raise ToolError(f"There is no research project called '{name}'.", "not_found")
        if len(found) > 1:
            raise ToolError("Several projects match: " + ", ".join(r["name"] for r in found[:5]), "ambiguous")
        return dict(found[0])

    def projects(self) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT p.name, p.question, p.updated_at, (SELECT COUNT(*) FROM research_sources s WHERE s.project_id = p.id) "
                "AS sources FROM research_projects p ORDER BY p.updated_at DESC")]

    def delete(self, name: str) -> None:
        project = self.project(name)
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM research_claims WHERE project_id = ?", (project["id"],))
            conn.execute("DELETE FROM research_sources WHERE project_id = ?", (project["id"],))
            conn.execute("DELETE FROM research_projects WHERE id = ?", (project["id"],))

    def conclude(self, name: str, conclusion: str) -> None:
        project = self.project(name)
        with self._db.transaction() as conn:
            conn.execute("UPDATE research_projects SET conclusion = ?, updated_at = ? WHERE id = ?",
                         (conclusion.strip()[:1500], now_iso(), project["id"]))

    # ---------------------------------------------------------------- sources

    def add_source(self, name: str, url: str, text: str, title: str = "", source_date: str | None = None) -> dict[str, Any]:
        project = self.project(name)
        url = normalise_url(url)
        digest = content_hash(text)
        with self._db.transaction() as conn:
            same_text = conn.execute("SELECT url FROM research_sources WHERE project_id = ? AND content_hash = ? AND url != ?",
                                     (project["id"], digest, url)).fetchone()
            existing = conn.execute("SELECT id FROM research_sources WHERE project_id = ? AND url = ?",
                                    (project["id"], url)).fetchone()
            if existing:
                return {"id": existing["id"], "url": url, "new": False, "duplicate_of": None}
            cursor = conn.execute("INSERT INTO research_sources (project_id, url, title, source_date, fetched_at, "
                                  "content_hash) VALUES (?, ?, ?, ?, ?, ?)",
                                  (project["id"], url, title.strip()[:150], (source_date or None), now_iso(), digest))
            conn.execute("UPDATE research_projects SET updated_at = ? WHERE id = ?", (now_iso(), project["id"]))
        return {"id": int(cursor.lastrowid), "url": url, "new": True, "duplicate_of": same_text["url"] if same_text else None}

    def sources(self, project_id: int) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM research_sources WHERE project_id = ? ORDER BY id",
                                                  (project_id,))]

    # ----------------------------------------------------------------- claims

    def add_claim(self, name: str, topic: str, claim: str, source_id: int | None = None, kind: str = "sourced") -> int:
        project = self.project(name)
        if kind == "sourced":
            with self._db.connect() as conn:
                ok = conn.execute("SELECT 1 FROM research_sources WHERE id = ? AND project_id = ?",
                                  (source_id, project["id"])).fetchone() if source_id else None
            if ok is None:
                raise ToolError("A sourced claim must point at one of this project's sources (give its number). "
                                "Use kind 'inference' for your own analysis.", "no_source")
        elif kind != "inference":
            raise ToolError("A claim is either 'sourced' or 'inference'.", "invalid_kind")
        with self._db.transaction() as conn:
            cursor = conn.execute("INSERT INTO research_claims (project_id, source_id, topic, claim, kind, created_at) "
                                  "VALUES (?, ?, ?, ?, ?, ?)", (project["id"], source_id if kind == "sourced" else None,
                                                                " ".join(topic.lower().split())[:80], claim.strip()[:500],
                                                                kind, now_iso()))
            conn.execute("UPDATE research_projects SET updated_at = ? WHERE id = ?", (now_iso(), project["id"]))
            return int(cursor.lastrowid)

    def claims(self, project_id: int) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM research_claims WHERE project_id = ? ORDER BY topic, id",
                                                  (project_id,))]

    def conflicts(self, project_id: int) -> list[dict[str, Any]]:
        """Same topic, different sources, different numbers: shown as 'possible conflict - check', never resolved."""
        sourced = [c for c in self.claims(project_id) if c["kind"] == "sourced"]
        found = []
        for i, a in enumerate(sourced):
            for b in sourced[i + 1:]:
                if a["topic"] != b["topic"] or a["source_id"] == b["source_id"]:
                    continue
                na, nb = numbers_in(a["claim"]), numbers_in(b["claim"])
                if na and nb and na != nb:
                    found.append({"topic": a["topic"], "claims": [a["id"], b["id"]], "sources": [a["source_id"], b["source_id"]],
                                  "detail": f"source {a['source_id']} says {sorted(na)}, source {b['source_id']} says {sorted(nb)}"})
        return found

    # ---------------------------------------------------------------- refresh

    def refresh(self, name: str, pages: dict[str, str | None]) -> dict[str, list[str]]:
        """``pages``: normalised-or-raw url -> text now (None = could not be read). Returns what changed."""
        project = self.project(name)
        report: dict[str, list[str]] = {"changed": [], "unchanged": [], "unreadable": [], "new_info": []}
        current = {normalise_url(u): t for u, t in pages.items()}
        for source in self.sources(project["id"]):
            text = current.get(source["url"])
            if text is None:
                report["unreadable"].append(source["url"])
                continue
            digest = content_hash(text)
            if digest == source["content_hash"]:
                report["unchanged"].append(source["url"])
                continue
            report["changed"].append(source["url"])
            with self._db.transaction() as conn:
                conn.execute("UPDATE research_sources SET content_hash = ?, fetched_at = ? WHERE id = ?",
                             (digest, now_iso(), source["id"]))
                conn.execute("UPDATE research_claims SET needs_recheck = 1 WHERE source_id = ?", (source["id"],))
        return report

    # ------------------------------------------------------------- monitoring

    def watch(self, name: str, days: int) -> None:
        """Opt in to re-checking this project's saved sources every ``days`` days (0 turns it off)."""
        project = self.project(name)
        with self._db.transaction() as conn:
            conn.execute("UPDATE research_projects SET watch_days = ? WHERE id = ?", (days or None, project["id"]))

    def due(self, now: datetime | None = None) -> list[str]:
        """Names of watched projects whose last check is older than their interval."""
        now = now or datetime.now(timezone.utc)
        with self._db.connect() as conn:
            rows = conn.execute("SELECT name, watch_days, last_checked FROM research_projects "
                                "WHERE watch_days IS NOT NULL").fetchall()
        out = []
        for r in rows:
            try:
                last = datetime.fromisoformat(r["last_checked"]) if r["last_checked"] else None
            except ValueError:
                last = None
            if last is None or now - last >= timedelta(days=r["watch_days"]):
                out.append(r["name"])
        return out

    def mark_checked(self, name: str) -> None:
        project = self.project(name)
        with self._db.transaction() as conn:
            conn.execute("UPDATE research_projects SET last_checked = ? WHERE id = ?", (now_iso(), project["id"]))

    def confirm_claim(self, claim_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE research_claims SET needs_recheck = 0 WHERE id = ?", (claim_id,))

    # ----------------------------------------------------------------- report

    def report(self, name: str) -> str:
        p = self.project(name)
        sources = {s["id"]: s for s in self.sources(p["id"])}
        order = {sid: n for n, sid in enumerate(sources, 1)}
        claims = self.claims(p["id"])
        lines = [f"Research: {p['name']}", f"Question: {p['question']}"]
        if p["criteria"]:
            lines.append(f"Compare by: {p['criteria']}")
        lines.append("Sourced facts:" if any(c["kind"] == "sourced" for c in claims) else "No sourced facts yet.")
        for c in (c for c in claims if c["kind"] == "sourced"):
            tag = " [needs recheck: the source changed]" if c["needs_recheck"] else ""
            lines.append(f"- ({c['topic']}) {c['claim']} [{order[c['source_id']]}]{tag}")
        inferences = [c for c in claims if c["kind"] == "inference"]
        if inferences:
            lines.append("Inference (analysis, not sourced fact):")
            lines += [f"- ({c['topic']}) {c['claim']}" for c in inferences]
        conflicts = self.conflicts(p["id"])
        if conflicts:
            lines.append("Possible conflicts - check these sources:")
            lines += [f"- {c['topic']}: {c['detail']}" for c in conflicts]
        if p["conclusion"]:
            lines.append(f"Conclusion (yours, {p['updated_at'][:10]}): {p['conclusion']}")
        lines.append("Sources:")
        for sid, s in sources.items():
            when = f", dated {s['source_date']}" if s["source_date"] else ", date unknown"
            lines.append(f"[{order[sid]}] {s['title'] or s['url']} - {s['url']} (read {s['fetched_at'][:10]}{when})")
        return "\n".join(lines)
