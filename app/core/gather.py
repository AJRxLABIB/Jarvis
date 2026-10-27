"""Active information gathering: observe only what is worth observing, then stop.

An ``Investigation`` has a problem, a few candidate causes (hypotheses) and a set of cheap, read-only probes. The loop:

1. pick the unrun probe that can settle the MOST still-open hypotheses per unit of cost (ties: the cheaper, then by name);
2. run it (a probe that cannot answer returns ``None`` - that is recorded as "couldn't observe", not guessed);
3. re-judge every hypothesis from ALL findings so far: supported, ruled out, or still open;
4. stop when a cause is supported by direct observation (unless told to continue), when nothing is open, when the budget (probe count / seconds) is spent, or when no remaining probe bears on an
   open hypothesis. A probe never runs twice, and a probe marked private only runs when the caller allowed it.

The report always says what was observed, what each cause now looks like, and what evidence is still missing - it never
turns "open" into a conclusion. No model is involved; the probes are ordinary local reads.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

SUPPORTED, RULED_OUT, OPEN = "supported", "ruled out", "open"


@dataclass(frozen=True)
class Probe:
    name: str
    run: Callable[[], Any | Awaitable[Any]]
    cost: int = 1                       # 1 = cheap local read ... 3 = slow or heavy
    private: bool = False               # touches something personal: only with explicit permission


@dataclass(frozen=True)
class Hypothesis:
    key: str
    text: str
    needs: tuple[str, ...]              # probe names whose findings can settle it
    check: Callable[[dict[str, Any]], str | None]     # findings -> "supported" / "ruled out" / None (cannot tell yet)
    next_step: str = ""                 # what to do if it is the cause


@dataclass(frozen=True)
class Budget:
    max_probes: int = 5
    max_seconds: float = 15.0


@dataclass
class Step:
    probe: str
    summary: str
    ok: bool


@dataclass
class Report:
    problem: str
    steps: list[Step] = field(default_factory=list)
    status: dict[str, str] = field(default_factory=dict)
    hypotheses: dict[str, Hypothesis] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    stopped: str = ""

    @property
    def supported(self) -> list[Hypothesis]:
        return [self.hypotheses[k] for k, s in self.status.items() if s == SUPPORTED]

    def describe(self) -> str:
        lines = [f"Problem: {self.problem}"]
        for s in self.steps:
            lines.append(f"  looked at {s.probe}: {s.summary}" if s.ok else f"  could not look at {s.probe}: {s.summary}")
        for key, state in self.status.items():
            lines.append(f"  [{state}] {self.hypotheses[key].text}")
        if self.supported:
            lines.append("Most likely: " + "; ".join(h.text for h in self.supported)
                         + ". Next: " + " ".join(h.next_step for h in self.supported if h.next_step))
        elif all(s == RULED_OUT for s in self.status.values()):
            lines.append("None of the causes I can check match. More information is needed from you or from a deeper check.")
        else:
            lines.append("Not settled yet.")
        if self.missing:
            lines.append("Still missing: " + "; ".join(self.missing))
        lines.append(f"(stopped: {self.stopped})")
        return "\n".join(lines)


def _judge(hypotheses: list[Hypothesis], findings: dict[str, Any]) -> dict[str, str]:
    out = {}
    for h in hypotheses:
        try:
            verdict = h.check(findings)
        except Exception:
            verdict = None
        out[h.key] = verdict if verdict in (SUPPORTED, RULED_OUT) else OPEN
    return out


def next_probe(hypotheses: list[Hypothesis], status: dict[str, str], probes: dict[str, Probe], done: set[str],
               allow_private: bool = False) -> Probe | None:
    """The unrun probe worth the most: open hypotheses it feeds, per unit of cost. None when nothing would help."""
    best, best_key = None, None
    for probe in probes.values():
        if probe.name in done or (probe.private and not allow_private):
            continue
        value = sum(1 for h in hypotheses if status.get(h.key) == OPEN and probe.name in h.needs)
        if value == 0:
            continue
        key = (-value / probe.cost, probe.cost, probe.name)
        if best_key is None or key < best_key:
            best, best_key = probe, key
    return best


async def investigate(problem: str, hypotheses: list[Hypothesis], probes: list[Probe], budget: Budget | None = None,
                      allow_private: bool = False, clock: Callable[[], float] = time.monotonic,
                      stop_at_first: bool = True) -> Report:
    budget = budget or Budget()
    by_name = {p.name: p for p in probes}
    report = Report(problem, hypotheses={h.key: h for h in hypotheses})
    findings: dict[str, Any] = {}
    done: set[str] = set()
    started = clock()
    report.status = _judge(hypotheses, findings)
    while True:
        if OPEN not in report.status.values():
            report.stopped = "every cause is settled"
            break
        if stop_at_first and SUPPORTED in report.status.values():
            report.stopped = "a cause is supported by direct observation (the others were not all checked)"
            break
        if len(done) >= budget.max_probes:
            report.stopped = f"budget reached ({budget.max_probes} observations)"
            break
        if clock() - started >= budget.max_seconds:
            report.stopped = f"time budget reached ({budget.max_seconds:g} s)"
            break
        probe = next_probe(hypotheses, report.status, by_name, done, allow_private)
        if probe is None:
            report.stopped = "nothing else I can observe would help"
            break
        done.add(probe.name)
        try:
            result = probe.run()
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=max(1.0, budget.max_seconds - (clock() - started)))
        except Exception as error:
            result = None
            report.steps.append(Step(probe.name, f"{type(error).__name__}", False))
        else:
            report.steps.append(Step(probe.name, _short(result) if result is not None else "no answer", result is not None))
        if result is not None:
            findings[probe.name] = result
        report.status = _judge(hypotheses, findings)
    for probe in probes:
        if probe.name not in done and any(report.status.get(h.key) == OPEN and probe.name in h.needs for h in hypotheses):
            report.missing.append(probe.name + (" (private: not allowed)" if probe.private and not allow_private else ""))
    for step in report.steps:
        if not step.ok:
            report.missing.append(f"{step.probe} could not be read")
    report.missing = list(dict.fromkeys(report.missing))
    return report


def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 160 else text[:157] + "..."


# ------------------------------------------------------------------ the built-in investigation: a model that will not load


def model_failure(ctx: Any, model: str | None = None) -> tuple[list[Hypothesis], list[Probe]]:
    """Why does the local model not answer? Causes, and the read-only probes that tell them apart."""
    settings = ctx.settings
    wanted = (model or getattr(settings, "ai_model", "") or "").strip()

    async def runtime() -> dict[str, Any] | None:
        if ctx.ai is None:
            return None
        try:
            models = await ctx.ai.models()
        except Exception:
            return {"reachable": False, "installed": [], "sizes": {}}
        return {"reachable": True, "installed": [m.name for m in models],
                "sizes": {m.name.lower(): getattr(m, "size_bytes", None) for m in models}}

    def memory() -> dict[str, Any] | None:
        try:
            import psutil

            return {"free_gb": round(psutil.virtual_memory().available / 1e9, 1)}
        except Exception:
            return None

    def health() -> dict[str, Any] | None:
        h = getattr(ctx.ai, "health", None)
        if h is None:
            return None
        info = h.summary().get(wanted) or h.summary().get(wanted.lower()) or {}
        return {"failures": int(info.get("failures") or 0), "last_error": info.get("last_error") or "",
                "skipped_for": info.get("skipped_for") or 0}

    def installed(f: dict[str, Any]) -> bool | None:
        r = f.get("runtime")
        if not r or not r["reachable"]:
            return None
        names = {n.lower() for n in r["installed"]}
        return wanted.lower() in names or f"{wanted.lower()}:latest" in names

    def runtime_check(f):
        r = f.get("runtime")
        return None if r is None else (RULED_OUT if r["reachable"] else SUPPORTED)

    def missing_check(f):
        have = installed(f)
        return None if have is None else (RULED_OUT if have else SUPPORTED)

    def memory_check(f):
        mem, r = f.get("memory"), f.get("runtime")
        if not mem or not r or not r["reachable"]:
            return None
        size = (r["sizes"].get(wanted.lower()) or r["sizes"].get(f"{wanted.lower()}:latest"))
        if not size:
            return None
        return SUPPORTED if mem["free_gb"] * 1e9 < size * 1.25 else RULED_OUT

    def error_check(f):
        h = f.get("health")
        if h is None:
            return None
        return SUPPORTED if h["failures"] and h["last_error"] else RULED_OUT

    hyps = [
        Hypothesis("runtime_down", "Ollama isn't running or isn't reachable", ("runtime",), runtime_check,
                   "start Ollama (open the Ollama app, or run `ollama serve`)."),
        Hypothesis("model_missing", f"the model '{wanted}' isn't installed", ("runtime",), missing_check,
                   "run .\\scripts\\setup_models.ps1, or pick an installed model in Settings > AI."),
        Hypothesis("memory_pressure", "there isn't enough free memory for the model", ("runtime", "memory"), memory_check,
                   "close heavy programs or choose a smaller model."),
        Hypothesis("recent_errors", "the model has been failing with a recorded error", ("health",), error_check,
                   "read the last error below and fix that first."),
    ]
    probes = [Probe("runtime", runtime, 1), Probe("memory", memory, 1), Probe("health", health, 1)]
    return hyps, probes
