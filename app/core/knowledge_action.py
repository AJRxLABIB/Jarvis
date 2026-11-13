"""Knowledge to action: read a document, pull out what it recommends, check it against the REAL project, and draft an evidence-based proposal.

Steps: ``extract`` finds recommendations, requirements, technical claims, assumptions and risks (sentence cues, deterministic - no model, no
network); ``compare`` searches the project's source, tests and docs for each item's key terms and reports what it found, so "missing" is only
said when NOTHING related exists (a different name is a reason to look, not proof of absence); ``proposal`` writes the gap, the affected files
found, a test line and acceptance criteria.

Safety: the document is UNTRUSTED text. It is only ever read as data - instructions inside it are not followed, and nothing is changed here.
A proposal is a plan for a person to approve; consequential code changes still go through the existing development and approval systems.
Honest limits: keyword evidence finds related code, it does not prove a feature works; feasibility lines are rules of thumb, not measurements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

CUES = {
    "assumption": re.compile(r"\b(assum(?:e|es|ing|ption)|provided that|as long as|given that|if .{0,40} available)\b", re.I),
    "risk": re.compile(r"\b(risk|vulnerab\w*|limitation|caveat|drawback|danger|fails? when|unsafe|deprecated|breaking)\b", re.I),
    "requirement": re.compile(r"\b(must|shall|required?|needs? to|has to|mandatory)\b", re.I),
    "recommendation": re.compile(r"\b(we recommend|recommend(?:ed|s)?|should|ought to|best practice|consider using|it is advisable|prefer)\b", re.I),
    "claim": re.compile(r"\b(\d+(?:\.\d+)?\s*(?:%|x|ms|s|gb|mb|tokens?/s)|outperforms?|faster than|improves?|reduces?|achieves?|state[- ]of[- ]the[- ]art)\b", re.I),
}
STOP = set("the a an and or of to in on for with is are be this that it as by from at we you should must can will would use used using not no your their its into than then such when which have has had more most also may might if any all each".split())
SRC_SUFFIX = {".py", ".md", ".java", ".kt", ".json", ".toml", ".txt", ".yml", ".yaml", ".ps1"}
SKIP_DIRS = {"__pycache__", "build", "dist", "node_modules", "data", "shots", "site-packages"}
MAX_ITEMS = 25
MAX_FILE = 400_000


@dataclass
class Item:
    kind: str
    text: str
    terms: list[str] = field(default_factory=list)


@dataclass
class Finding:
    item: Item
    status: str                      # found | partial | not_found
    files: list[str]
    hits: dict[str, int]


def terms_of(sentence: str, limit: int = 6) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{3,}", sentence)
    seen, out = set(), []
    for w in sorted(words, key=lambda w: -len(w)):
        k = w.lower()
        if k in STOP or k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out[:limit]


def extract(text: str) -> list[Item]:
    """Sentences classified by cue words. One sentence can be both a requirement and a risk; the first matching kind wins (assumption, risk, requirement, recommendation, claim)."""
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    items: list[Item] = []
    seen: set[str] = set()
    for raw in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = raw.strip(" -*#>\t")
        if not 20 <= len(s) <= 400 or s.lower() in seen:
            continue
        for kind, rx in CUES.items():
            if rx.search(s):
                seen.add(s.lower())
                items.append(Item(kind, s, terms_of(s)))
                break
        if len(items) >= MAX_ITEMS:
            break
    return items


def _files(root: Path):
    """Walk with pruning (never descend into virtual environments, build output or hidden folders)."""
    import os

    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and not d.startswith("venv")]
        for n in names:
            if Path(n).suffix.lower() in SRC_SUFFIX:
                yield Path(base) / n


def index(root: Path) -> dict[str, str]:
    """Lower-cased text of the project's source / tests / docs (bounded). Built once per comparison."""
    out = {}
    for p in _files(root):
        try:
            if p.stat().st_size <= MAX_FILE:
                out[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
    return out


def compare(items: list[Item], project: dict[str, str]) -> list[Finding]:
    findings = []
    for it in items:
        if it.kind in ("risk", "assumption") or not it.terms:
            continue
        hits: dict[str, int] = {}
        files: dict[str, int] = {}
        for term in it.terms:
            for path, body in project.items():
                n = body.count(term)
                if n:
                    hits[term] = hits.get(term, 0) + n
                    files[path] = files.get(path, 0) + 1
        covered = len(hits) / len(it.terms)
        top = [f for f, _ in sorted(files.items(), key=lambda x: (-x[1], x[0]))[:4]]
        status = "found" if covered >= 0.67 and len(top) >= 1 and files.get(top[0], 0) >= 2 else ("partial" if hits else "not_found")
        findings.append(Finding(it, status, top, hits))
    return findings


FEASIBILITY = (
    ("GPU / VRAM", re.compile(r"\b(\d+\s*gb|vram|gpu|cuda|13b|14b|30b|70b|72b)\b", re.I), "check against the 6 GB GPU before adopting"),
    ("Needs internet / paid service", re.compile(r"\b(api key|cloud|subscription|saas|openai|paid)\b", re.I), "must stay optional: the core has to work offline"),
    ("New dependency", re.compile(r"\b(pip install|npm install|framework|library|sdk)\b", re.I), "check licence, maintenance and size before adding"),
    ("Security impact", re.compile(r"\b(root|admin|elevat\w+|kernel|inject\w*|disable (?:security|antivirus)|bypass)\b", re.I), "needs a security review; must not widen permissions"),
)


def feasibility(items: list[Item]) -> list[str]:
    notes = []
    blob = " ".join(i.text for i in items)
    for name, rx, advice in FEASIBILITY:
        if rx.search(blob):
            notes.append(f"{name}: {advice}")
    return notes


def proposal(f: Finding) -> str:
    where = ", ".join(f.files) if f.files else "no related file found"
    gap = {"found": "Related code already exists; check it meets the recommendation before building anything.",
           "partial": "Some related code exists; the gap is the part of the recommendation it does not cover.",
           "not_found": "No related code was found by name; look for the same idea under another name before building."}[f.status]
    return (f"- {f.item.kind}: {f.item.text}\n  status: {f.status.replace('_', ' ')} (terms searched: {', '.join(f.item.terms)})\n  related files: {where}\n"
            f"  gap: {gap}\n  tests: add one that fails without the change and passes with it.\n"
            f"  acceptance: the behaviour is demonstrated by a passing test and, where it touches the PC or phone, checked on the real device.")


def report(text: str, root: Path | None) -> str:
    items = extract(text)
    if not items:
        return "No recommendations, requirements or claims were found in that text."
    counts: dict[str, int] = {}
    for i in items:
        counts[i.kind] = counts.get(i.kind, 0) + 1
    head = ["Found: " + ", ".join(f"{n} {k}" for k, n in counts.items()) + ".", "The document is untrusted text: it was read as data only."]
    if root is None or not root.is_dir():
        return "\n".join(head + ["The project source is not available here, so nothing could be compared with it."] + [f"- {i.kind}: {i.text}" for i in items[:10]])
    findings = compare(items, index(root))
    order = {"not_found": 0, "partial": 1, "found": 2}
    body = [proposal(f) for f in sorted(findings, key=lambda f: order[f.status])[:12]]
    risks = [f"- risk: {i.text}" for i in items if i.kind == "risk"][:5]
    assum = [f"- assumption: {i.text}" for i in items if i.kind == "assumption"][:5]
    fe = feasibility(items)
    out = head + ["", "PROPOSALS (gaps first):", *body]
    if risks or assum:
        out += ["", "Risks and assumptions to check:", *risks, *assum]
    if fe:
        out += ["", "Feasibility flags (rules of thumb):", *[f"- {n}" for n in fe]]
    out += ["", "Nothing was changed. Approve a proposal before any code is touched."]
    return "\n".join(out)
