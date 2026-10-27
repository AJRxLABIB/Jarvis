"""Measured model comparison for THIS PC: how fast each installed model really is, how much of it sits on the GPU, and which
one to use for the priority you choose (speed, quality, low resource use, or balanced).

What is measured (from Ollama's own timing fields, not from the wall clock alone):
* load time            - ``load_duration`` of a cold call (the model is unloaded first); "already loaded" is said, not hidden
* prompt speed         - prompt tokens / ``prompt_eval_duration`` (collapses when a model spills out of GPU memory)
* generation speed     - ``eval_count`` / ``eval_duration`` tokens per second, median over the runs
* first-response time  - load + prompt time of a short request (derived from those fields; it is not a streaming measurement)
* GPU share            - the fraction of the loaded model Ollama reports in GPU memory (``/api/ps``): 1.0 = fully on the GPU
* GPU memory peak      - nvidia-smi before / during, when an NVIDIA GPU answers; otherwise "not measured" (never 0)

Quality is NOT measured here: it comes from your ``--eval-quality`` history (``app/quality_eval.py``). A model with no quality
record can be shown but is never recommended. Each failed safety question costs 5 points in the ranking and is always printed
(a hard ban would empty the list: every small local model obeys some hidden instruction; the app's firewall is code).
Numbers from a different machine are not comparable: every record stores the environment it was measured on.

Nothing here downloads, installs or changes a setting: it only reads, measures and recommends.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROFILES = ("balanced", "speed", "quality", "low_resource")
CLOSE_ENOUGH = 0.10           # quality within this of the best counts as "as good" when trading it for speed / size
HISTORY = "speed-history.jsonl"
SAFETY_PENALTY = 0.05         # each failed safety question costs this much quality in the ranking (and is always shown)
SPILL_BELOW = 0.95            # GPU share below this = partly on the CPU (answers several times slower)

PROMPTS: tuple[tuple[str, str], ...] = (
    ("short", "Reply with the single word OK."),
    ("reason", "In two sentences, explain why the sky is blue."),
    ("long", ("The committee reviewed the quarterly logistics summary and noted that the warehouse schedule remained "
              "unchanged for the season. ") * 30 + "\nIn one sentence, what did the committee note?"),
)

Chat = Callable[[str, str], Awaitable[Any]]               # (model, prompt) -> ChatResponse with .stats
Probe = Callable[[str], Awaitable[float | None]]          # (model) -> GPU share or None
Unload = Callable[[str], Awaitable[None]]
GpuReader = Callable[[], dict[str, float] | None]


@dataclass
class Speed:
    model: str
    load_ms: int | None = None
    was_loaded: bool | None = None         # True = no cold load could be measured
    tokens_per_s: float | None = None
    prompt_tokens_per_s: float | None = None
    first_response_s: float | None = None
    gpu_share: float | None = None
    vram_peak_mb: float | None = None
    vram_total_mb: float | None = None
    runs: int = 0
    errors: list[str] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)

    @property
    def spills(self) -> bool | None:
        return None if self.gpu_share is None else self.gpu_share < SPILL_BELOW

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("model", "load_ms", "was_loaded", "tokens_per_s", "prompt_tokens_per_s",
                                              "first_response_s", "gpu_share", "vram_peak_mb", "vram_total_mb", "runs",
                                              "errors", "environment")}


def _rate(tokens: int | None, ms: int | None) -> float | None:
    if not tokens or not ms or ms <= 0:
        return None
    return round(tokens / (ms / 1000), 1)


async def measure(chat: Chat, model: str, *, unload: Unload | None = None, gpu_share: Probe | None = None,
                  read_gpu: GpuReader | None = None, repeats: int = 2, environment: dict[str, Any] | None = None) -> Speed:
    """Measure ``model``. Each failure is recorded; a model that never answers gets no numbers rather than invented ones."""
    out = Speed(model, environment=environment or {})
    if unload is not None:
        try:
            await unload(model)                       # so the first call below pays the real, cold load
        except Exception as exc:
            out.errors.append(f"could not unload first ({type(exc).__name__}); load time may be hidden")
    base = read_gpu() if read_gpu else None
    peaks: list[float] = []
    gen, prompt_speed, first = [], [], []
    for index, (label, text) in enumerate(PROMPTS):
        for attempt in range(repeats if label != "long" else 1):
            try:
                reply = await chat(model, text)
            except Exception as exc:
                out.errors.append(f"{label}: {type(exc).__name__}: {str(exc)[:80]}")
                continue
            stats = getattr(reply, "stats", None)
            out.runs += 1
            if stats is None:
                continue
            if index == 0 and attempt == 0:
                out.load_ms = stats.load_ms
                out.was_loaded = bool(stats.load_ms is not None and stats.load_ms < 200)
            gen_rate = _rate(stats.completion_tokens, stats.eval_ms)
            if gen_rate and (stats.completion_tokens or 0) >= 4:     # a one-token answer says nothing about speed
                gen.append(gen_rate)
            prompt_rate = _rate(stats.prompt_tokens, stats.prompt_ms)
            if prompt_rate and (stats.prompt_tokens or 0) >= 20:
                prompt_speed.append(prompt_rate)
            if label == "short" and stats.prompt_ms is not None:
                first.append(((stats.load_ms or 0) if attempt == 0 else 0) + stats.prompt_ms)
            if read_gpu:
                now = read_gpu()
                if now:
                    peaks.append(now["used_mb"])
    if gen:
        out.tokens_per_s = round(statistics.median(gen), 1)
    if prompt_speed:
        out.prompt_tokens_per_s = round(statistics.median(prompt_speed), 1)
    if first:
        out.first_response_s = round(min(first) / 1000, 2) if out.was_loaded else round(statistics.median(first) / 1000, 2)
    if gpu_share is not None:
        try:
            out.gpu_share = await gpu_share(model)
        except Exception as exc:
            out.errors.append(f"gpu share: {type(exc).__name__}")
    if read_gpu and peaks and base:
        out.vram_peak_mb = max(0.0, max(peaks) - base['used_mb'])      # this model's own rise, not other programs' memory
        out.vram_total_mb = base["total_mb"]
    return out


# ------------------------------------------------------------------ history


def save(data_dir: Path, speed: Speed) -> None:
    from app.quality_eval import folder

    path = folder(data_dir) / HISTORY
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(speed.to_dict() | {"at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False) + "\n")


def load_history(data_dir: Path | None) -> dict[str, dict[str, Any]]:
    """The newest speed record per model."""
    if data_dir is None:
        return {}
    from app.quality_eval import folder

    try:
        lines = (folder(data_dir) / HISTORY).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    found: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            row = json.loads(line)
            found[str(row["model"])] = row
        except (ValueError, KeyError, TypeError):
            continue
    return found


# ------------------------------------------------------------------ choosing


@dataclass
class Row:
    model: str
    size_gb: float | None
    quality: float | None                 # 0..1 from --eval-quality
    safety_failures: int
    tokens_per_s: float | None
    first_response_s: float | None
    gpu_share: float | None
    vram_peak_mb: float | None
    load_ms: int | None

    @property
    def adjusted(self) -> float:
        """Quality for ranking: the benchmark score minus a penalty per failed safety question (never hidden)."""
        return (self.quality or 0.0) - SAFETY_PENALTY * self.safety_failures

    @property
    def spills(self) -> bool:
        return self.gpu_share is not None and self.gpu_share < SPILL_BELOW

    @property
    def measured_speed(self) -> bool:
        return self.tokens_per_s is not None

    @property
    def footprint(self) -> float | None:
        """GPU memory in GB when measured, else the file size as an ESTIMATE of what it needs."""
        if self.vram_peak_mb is not None:
            return self.vram_peak_mb / 1024
        return self.size_gb


def rows(models: list[Any], quality: dict[str, Any], speed: dict[str, dict[str, Any]]) -> list[Row]:
    def look(table: dict[str, Any], name: str) -> Any:
        base = name.removesuffix(":latest")
        for key, value in table.items():
            if key.removesuffix(":latest") == base:
                return value
        return None

    out = []
    for m in models:
        q = look(quality, m.name)
        s = look(speed, m.name) or {}
        score = getattr(q, "score", None)
        if score is not None and score > 1.0:      # --eval-quality stores percent (92.0); everything here uses 0..1
            score = score / 100
        out.append(Row(m.name, round(m.size_bytes / 1e9, 1) if getattr(m, "size_bytes", None) else None,
                       score, getattr(q, "safety_failures", 0) if q else 0,
                       s.get("tokens_per_s"), s.get("first_response_s"), s.get("gpu_share"), s.get("vram_peak_mb"),
                       s.get("load_ms")))
    return out


@dataclass
class Recommendation:
    profile: str
    model: str | None
    reason: str
    unranked: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


def _norm(values: list[float | None], higher_is_better: bool) -> list[float | None]:
    have = [v for v in values if v is not None]
    if not have or max(have) == min(have):
        return [0.5 if v is not None else None for v in values]
    lo, hi = min(have), max(have)
    return [None if v is None else ((v - lo) / (hi - lo) if higher_is_better else (hi - v) / (hi - lo)) for v in values]


def recommend(table: list[Row], profile: str = "balanced", vram_gb: float | None = None) -> Recommendation:
    if profile not in PROFILES:
        profile = "balanced"
    rec = Recommendation(profile, None, "")
    scored = [r for r in table if r.quality is not None]
    rec.unranked = [r.model for r in table if r.quality is None]
    if not scored:
        rec.reason = ("No model has a quality benchmark yet, so nothing can be recommended on evidence. "
                      "Run `python run.py --eval-quality --model <name>` for each model you want considered.")
        return rec
    flagged = [f"{r.model} ({r.safety_failures})" for r in scored if r.safety_failures]
    if flagged:
        rec.caveats.append(f"failed safety questions (each costs {SAFETY_PENALTY:.0%} in the ranking): " + ", ".join(flagged)
                           + ". Jarvis's prompt-injection firewall is code and does not depend on the model, but a model that "
                             "obeys hidden instructions is a weaker second line of defence.")
    pool = scored
    if profile != "quality":
        fits = [r for r in scored if not r.spills]
        if fits and len(fits) < len(scored):
            rec.caveats.append("left out because only part of the model fits in GPU memory (measured, so it runs several times "
                               "slower): " + ", ".join(f"{r.model} ({r.gpu_share:.0%} on GPU)" for r in scored if r.spills))
            pool = fits
    if vram_gb:
        for r in pool:
            fp = r.footprint
            if r.spills:
                rec.caveats.append(f"{r.model}: only {r.gpu_share:.0%} of it is on the GPU (measured) - expect it to be slow")
            elif r.gpu_share is None and fp is not None and fp * 1.15 > vram_gb:
                rec.caveats.append(f"{r.model}: about {fp:.1f} GB (estimate) may not fit your {vram_gb:.0f} GB GPU; "
                                   "run --benchmark-models to measure it")
    best_q = max(r.adjusted for r in pool)
    close = [r for r in pool if r.adjusted >= best_q - CLOSE_ENOUGH]

    def pick(group: list[Row], key: Callable[[Row], Any], why: str) -> None:
        winner = max(group, key=key)
        rec.model = winner.model
        rec.reason = why.format(m=winner)

    if profile == "quality":
        pick(pool, lambda r: (r.adjusted, r.tokens_per_s or 0), "highest benchmark score here ({m.quality:.0%}" + (", after the safety penalty" if any(r.safety_failures for r in pool) else "") + ").")
    elif profile == "speed":
        fast = [r for r in close if r.measured_speed]
        if not fast:
            rec.reason = ("No model near the best quality has a speed measurement yet. Run `python run.py --benchmark-models`.")
            return rec
        pick(fast, lambda r: (r.tokens_per_s, r.adjusted), "fastest measured ({m.tokens_per_s} tokens/s) among models within "
                                                           f"{CLOSE_ENOUGH:.0%} of the best quality.")
    elif profile == "low_resource":
        small = [r for r in close if r.footprint is not None]
        if not small:
            rec.reason = "No model has a known size or GPU-memory measurement."
            return rec
        pick(small, lambda r: (-(r.footprint or 99), r.adjusted),
             "smallest footprint ({m.footprint:.1f} GB, " + ("measured" if any(r.vram_peak_mb for r in small) else "file-size estimate")
             + f") among models within {CLOSE_ENOUGH:.0%} of the best quality.")
    else:
        q = _norm([r.adjusted for r in pool], True)
        s = _norm([r.tokens_per_s for r in pool], True)
        z = _norm([r.footprint for r in pool], False)
        weights = (0.5, 0.3, 0.2)
        best, best_score = None, -1.0
        for r, parts in zip(pool, zip(q, s, z)):
            have = [(w, p) for w, p in zip(weights, parts) if p is not None]
            total = sum(w for w, _ in have)
            score = sum(w * p for w, p in have) / total if total else 0.0
            if score > best_score:
                best, best_score = r, score
        rec.model = best.model if best else None
        missing = [n for n, vals in (("speed", s), ("size", z)) if all(v is None for v in vals)]
        rec.reason = (f"best overall ({best_score:.2f}): quality 50%, speed 30%, size 20%" if best else "") + (
            f"; {', '.join(missing)} not available for any model, so it was left out of the weighting" if missing else "") + "."
    if rec.model and any(rec.model == r.model and not r.measured_speed for r in pool):
        rec.caveats.append(f"{rec.model} has no speed measurement yet (run --benchmark-models).")
    return rec


def table_text(table: list[Row], vram_gb: float | None = None) -> str:
    def f(v: Any, fmt: str = "{}") -> str:
        return "-" if v is None else fmt.format(v)

    lines = [f"{'model':28} {'quality':>8} {'tok/s':>7} {'1st resp':>9} {'on GPU':>7} {'GPU peak':>9} {'size':>6}",
             "-" * 80]
    for r in sorted(table, key=lambda r: (-(r.quality or -1), r.model)):
        lines.append(f"{r.model[:28]:28} {f(None if r.quality is None else round(r.quality * 100), '{}%'):>8} "
                     f"{f(r.tokens_per_s):>7} {f(r.first_response_s, '{}s'):>9} "
                     f"{f(None if r.gpu_share is None else round(r.gpu_share * 100), '{}%'):>7} "
                     f"{f(None if r.vram_peak_mb is None else round(r.vram_peak_mb), '{} MB'):>9} {f(r.size_gb, '{} GB'):>6}")
    lines.append("'-' = not measured (never shown as zero). Quality comes from --eval-quality; speed from --benchmark-models; "
                 "size is the model file, an estimate of what it needs." + (f" GPU: {vram_gb:.0f} GB." if vram_gb else ""))
    return "\n".join(lines)


async def unload_all(provider: Any) -> list[str]:
    """Unload EVERY model Ollama has loaded (e.g. the one the running Jarvis keeps warm), so a measurement gets the whole GPU.
    Returns the names it unloaded. A model that is in GPU memory beside the one measured makes the measured one look like it
    does not fit."""
    try:
        data = await provider._request("GET", "/api/ps")
    except Exception:
        return []
    names = [str(e.get("name") or e.get("model") or "") for e in (data or {}).get("models", []) or []]
    done = []
    for name in filter(None, names):
        try:
            await provider.unload(name)
            done.append(name)
        except Exception:
            pass
    return done
