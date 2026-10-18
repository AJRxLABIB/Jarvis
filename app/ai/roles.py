"""Model roles: which installed model is best for which job - decided from FACTS only.

Facts used: what Ollama reports a model can do (tools / vision / thinking / embedding), its size against this PC's
memory, how it behaved here (health: failures, latency, tool-call success) and your own benchmark results
(``python run.py --eval-quality``). A model's NAME is never used to judge quality; without a benchmark the
recommendation says so instead of guessing.

Roles: general (AI_MODEL), fast (AI_FAST_MODEL), smart (AI_SMART_MODEL), coding (AI_CODING_MODEL),
vision (VISION_MODEL), embedding (EMBEDDING_MODEL). One model may serve several roles; nothing is required.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.health import HEADROOM

ROLE_SETTINGS = {"general": "AI_MODEL", "fast": "AI_FAST_MODEL", "smart": "AI_SMART_MODEL",
                 "coding": "AI_CODING_MODEL", "vision": "VISION_MODEL", "embedding": "EMBEDDING_MODEL"}
CLOSE_ENOUGH = 0.10        # a smaller model counts as "fast enough" when its benchmark score is within this of the best


@dataclass(frozen=True)
class Pick:
    role: str
    model: str | None
    reason: str


@dataclass(frozen=True)
class ModelFacts:
    name: str
    size_gb: float | None
    fits: bool | None          # None = unknown (size or RAM unknown)
    tools: bool | None
    vision: bool
    thinking: bool
    embedding_only: bool
    score: float | None        # your benchmark (0..1), None = never benchmarked
    safety_failures: int
    failures: int
    calls: int
    avg_seconds: float | None
    tool_ok_rate: float | None


def facts(models, evidence: dict, health: dict, total_ram: int | None) -> list[ModelFacts]:
    out = []
    for m in models:
        size = m.size_bytes
        fits = None if not size or not total_ram else size * HEADROOM <= total_ram
        ev = _evidence_for(m.name, evidence)
        h = _health_for(m.name, health)
        out.append(ModelFacts(m.name, round(size / 1e9, 1) if size else None, fits, m.supports_tools,
                              m.supports_vision, m.supports_thinking, m.is_embedding_only,
                              ev.score if ev else None, ev.safety_failures if ev else 0,
                              int(h.get("failures") or 0), int(h.get("calls") or 0), h.get("avg_seconds"),
                              h.get("tool_call_ok_rate")))
    return out


def _norm(name: str) -> str:
    return name.lower().removesuffix(":latest")


def _evidence_for(name: str, evidence: dict):
    return next((v for k, v in evidence.items() if _norm(k) == _norm(name)), None)


def _health_for(name: str, health: dict) -> dict:
    return next((v for k, v in health.items() if _norm(k) == _norm(name)), {})


def _usable_chat(f: ModelFacts) -> bool:
    return not f.embedding_only and f.tools is not False and f.fits is not False and f.safety_failures == 0


def recommend(all_facts: list[ModelFacts]) -> list[Pick]:
    chat = [f for f in all_facts if _usable_chat(f)]
    scored = [f for f in chat if f.score is not None]
    picks: list[Pick] = []
    if scored:
        best = max(scored, key=lambda f: (f.score, -(f.avg_seconds or 99)))
        picks.append(Pick("general", best.name, f"best benchmark score here ({best.score:.0%}), can use tools"))
        quick = [f for f in scored if f.score >= best.score - CLOSE_ENOUGH and f.size_gb is not None]
        # smaller = less to compute per word (a physical fact); measured reply time breaks ties
        fastest = min(quick, key=lambda f: (f.size_gb or 99, f.avg_seconds or 99)) if quick else best
        if fastest.name != best.name:
            picks.append(Pick("fast", fastest.name, f"within {CLOSE_ENOUGH:.0%} of the best score but smaller / "
                                                    "quicker - for short commands"))
        else:
            picks.append(Pick("fast", None, "no smaller model scores close enough: keep using the general one"))
        bigger = [f for f in scored if f.score > best.score - 0.02 and (f.size_gb or 0) > (best.size_gb or 0)]
        picks.append(Pick("smart", bigger[0].name, "scores as well and is larger - for hard questions") if bigger
                     else Pick("smart", None, "no larger model scores at least as well: keep the general one"))
    elif chat:
        candidate = max(chat, key=lambda f: (f.tools is True, f.size_gb or 0))
        picks.append(Pick("general", None, f"no benchmark yet - run 'python run.py --eval-quality' to compare "
                                           f"{len(chat)} model(s); '{candidate.name}' is installed and can use tools"
                                           if candidate.tools else "no benchmark yet and tool support is unknown"))
        picks.append(Pick("fast", None, "needs a benchmark first"))
        picks.append(Pick("smart", None, "needs a benchmark first"))
    else:
        picks.append(Pick("general", None, "no installed model can chat with tools on this PC (pull one with Ollama)"))
    picks.append(Pick("coding", None, "uses the smart model unless you choose one (no coding benchmark here)"))
    vision = [f for f in all_facts if f.vision and f.fits is not False]
    picks.append(Pick("vision", min(vision, key=lambda f: f.size_gb or 99).name, "Ollama reports vision support")
                 if vision else Pick("vision", None, "no installed model reports vision support"))
    embed = [f for f in all_facts if f.embedding_only]
    picks.append(Pick("embedding", min(embed, key=lambda f: f.size_gb or 99).name, "an embedding model (for "
                      "document search)") if embed else Pick("embedding", None, "none installed (full-text search "
                                                                               "still works)"))
    return picks


def same_model(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and _norm(a) == _norm(b)


def changes(picks: list[Pick], current: dict[str, str]) -> dict[str, str]:
    """{SETTING: model} for the recommended roles that differ from what is set now ('x' and 'x:latest' are equal)."""
    return {ROLE_SETTINGS[p.role]: p.model for p in picks if p.model and not same_model(p.model, current.get(p.role))}


def report(all_facts: list[ModelFacts], picks: list[Pick], current: dict[str, str]) -> str:
    lines = ["Installed models:"]
    for f in all_facts:
        caps = [c for c, on in (("tools", f.tools), ("vision", f.vision), ("thinking", f.thinking),
                                ("embedding", f.embedding_only)) if on]
        size = f"{f.size_gb} GB" if f.size_gb is not None else "size ?"
        fit = "" if f.fits is not False else " - TOO BIG for this PC's memory"
        score = f"benchmark {f.score:.0%}" if f.score is not None else "not benchmarked"
        lines.append(f"  {f.name}: {size}, {', '.join(caps) or 'chat'}, {score}{fit}")
    lines.append("Recommended roles (from the facts above):")
    for p in picks:
        now = current.get(p.role) or "-"
        lines.append(f"  {p.role:9} {p.model or '(keep: ' + now + ')'}  - {p.reason}")
    return "\n".join(lines)
