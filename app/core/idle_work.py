"""Idle-time work: small local housekeeping while you are away - and an honest log of what was actually done.

Runs from the existing upkeep loop (no new thread, no new timer). It starts only when ALL of these hold:
you have not touched keyboard / mouse for ``after_minutes``; the PC is not on battery; the CPU is not busy; the current mode allows background work.
One job at a time, each at most once per its interval, the whole pass capped at ``BUDGET_S`` seconds, and it stops between jobs as soon as you are back.

Jobs (all local, no internet): prune the prediction record, prune old settled promises, refresh the opportunity list for the morning briefing, look for
solved-problem candidates in the action log, count near-duplicate indexed documents for your review, and - with Ollama - one tiny daily speed check of the chat
model, and proposals to merge near-identical memories (the model may word them; you approve each one - nothing is merged unattended). Each run is logged with its real result; a job that failed is logged as
failed. Nothing is ever reported that did not run. Switch off with IDLE_WORK_ENABLED=false.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

BUDGET_S = 20.0
CPU_BUSY = 40.0
LOG_KEY, LAST_KEY, OPP_KEY = "idle_work_log", "idle_work_last", "idle_opportunities"
LOG_KEEP = 40


@dataclass(frozen=True)
class Job:
    name: str
    every_hours: float
    run: Callable[[Any], str]              # returns one line of evidence


def should_run(idle_s: float, after_minutes: float, on_battery: bool | None, cpu: float | None, background_allowed: bool = True) -> tuple[bool, str]:
    if idle_s < after_minutes * 60:
        return False, "you are using the PC"
    if on_battery:
        return False, "on battery"
    if cpu is not None and cpu >= CPU_BUSY:
        return False, f"the PC is busy (CPU {cpu:.0f}%)"
    if not background_allowed:
        return False, "the current mode does not allow background work"
    return True, "idle"


def _prune_predictions(db: Any) -> str:
    from app.core import predictions

    return f"removed {predictions.prune(db)} old prediction(s)"


def _prune_promises(db: Any) -> str:
    from app.core import commitments

    return f"removed {commitments.Ledger(db).prune()} settled promise(s) older than 180 days"


def _opportunities(db: Any) -> str:
    from app.core import opportunities as O
    from app.core.radar import Feedback
    from app.memory.settings_store import SettingsStore

    items, failed = O.collect(db)
    shown, _ = O.rank(items, Feedback(db).blocked, 3)
    SettingsStore(db).set(OPP_KEY, {"at": time.time(), "items": [{"title": o.title, "evidence": o.evidence, "key": o.key} for o in shown]})
    return f"{len(shown)} opportunit{'y' if len(shown) == 1 else 'ies'} ready for the briefing" + (f"; could not check {', '.join(failed)}" if failed else "")


def _experience_candidates(db: Any) -> str:
    from app.core import tech_experience as T

    return f"{len(T.mine(T.load_audit(db)))} solved-problem candidate(s) found in the action log (nothing saved; see 'tech_experience suggest')"


def _memory_duplicates(db: Any) -> str:
    from app.memory.sources import near_duplicates

    pairs = near_duplicates(db)
    return f"{len(pairs)} near-duplicate indexed document pair(s) found for you to review (nothing merged or deleted)"


SPEED_KEY, SPEED_KEEP, SLOW_FACTOR = "idle_model_speed", 30, 0.6
SPEED_PROMPT = "Reply with the single word: ready"


def _ollama_generate(host: str, model: str, timeout: float = 45.0, prompt: str = SPEED_PROMPT, num_predict: int = 24) -> dict[str, Any]:
    import httpx

    r = httpx.post(host.rstrip("/") + "/api/generate", json={"model": model, "prompt": prompt, "stream": False, "think": False, "options": {"num_predict": num_predict, "temperature": 0}},
                   timeout=timeout)
    r.raise_for_status()
    return r.json()


def speed_job(settings: Any, store: Any, generate: Callable[[str, str], dict[str, Any]] = _ollama_generate, clock: Callable[[], float] = time.time) -> Job | None:
    """A daily speed check of the chat model (Ollama only): one tiny local request, tokens per second kept as a history. A reading well below your own
    median is reported as a slowdown (typical cause: the model fell back to the CPU). Local request to your own Ollama; nothing leaves the PC."""
    if str(getattr(settings, "ai_provider", "ollama") or "ollama").lower() != "ollama" or not getattr(settings, "ai_model", ""):
        return None

    def run(_db: Any) -> str:
        data = generate(str(settings.ollama_host), str(settings.ai_model))
        count, dur = data.get("eval_count"), data.get("eval_duration")
        if not count or not dur:
            return "the model answered but gave no timing, so no speed was recorded"
        tps = round(float(count) / (float(dur) / 1e9), 1)
        hist = [h for h in (store.get(SPEED_KEY, []) or []) if h.get("model") == settings.ai_model]
        past = sorted(h["tps"] for h in hist)
        note = ""
        if len(past) >= 5:
            median = past[len(past) // 2]
            if tps < median * SLOW_FACTOR:
                note = f" - SLOWER than your usual {median} tokens/s (often the model running on the CPU instead of the GPU)"
        store.set(SPEED_KEY, ((store.get(SPEED_KEY, []) or []) + [{"at": clock(), "model": settings.ai_model, "tps": tps}])[-SPEED_KEEP:])
        return f"{settings.ai_model}: {tps} tokens/s" + note

    return Job("model speed check", 24, run)


def merge_job(settings: Any, store: Any, generate: Callable[[str], str] | None = None) -> Job:
    """Every two days: PROPOSE up to 3 memory merges for your review (app/core/memory_consolidation.py). With Ollama the wording comes from the local model
    and is checked for invented facts; otherwise the rule "keep the longer one" is proposed. Nothing is merged here."""
    if generate is None and str(getattr(settings, "ai_provider", "ollama") or "ollama").lower() == "ollama" and getattr(settings, "ai_model", ""):
        def generate(prompt: str) -> str:
            return str(_ollama_generate(str(settings.ollama_host), str(settings.ai_model), 60.0, prompt, 80).get("response", ""))

    def run(db: Any) -> str:
        from app.core import memory_consolidation as MC
        from app.memory.manager import MemoryManager

        props = MC.Proposals(store)
        added = MC.scan(MemoryManager(db), props, generate)
        return f"{added} new merge proposal(s) for your review; {len(props.open())} open (nothing merged)"

    return Job("propose memory merges", 48, run)


JOBS: tuple[Job, ...] = (Job("prune predictions", 24, _prune_predictions), Job("prune promises", 24, _prune_promises),
                         Job("refresh opportunities", 6, _opportunities), Job("find solved problems", 12, _experience_candidates),
                         Job("find duplicate documents", 24, _memory_duplicates))


class IdleWorker:
    def __init__(self, db: Any, store: Any, jobs: tuple[Job, ...] = JOBS, clock: Callable[[], float] = time.time, budget_s: float = BUDGET_S,
                 still_idle: Callable[[], bool] = lambda: True, timer: Callable[[], float] = time.monotonic) -> None:
        self.db, self.store, self.jobs, self.clock, self.budget_s, self.still_idle, self.timer = db, store, jobs, clock, budget_s, still_idle, timer

    def due(self) -> list[Job]:
        last = self.store.get(LAST_KEY, {}) or {}
        now = self.clock()
        return [j for j in self.jobs if now - float(last.get(j.name, 0)) >= j.every_hours * 3600]

    def run_due(self) -> list[dict[str, Any]]:
        done: list[dict[str, Any]] = []
        started = self.timer()
        for job in self.due():
            if self.timer() - started >= self.budget_s or not self.still_idle():
                break                                             # out of budget, or you came back: the rest waits
            t0 = self.timer()
            try:
                entry = {"job": job.name, "ok": True, "result": str(job.run(self.db))[:200]}
            except Exception as exc:
                entry = {"job": job.name, "ok": False, "result": f"failed: {exc}"[:200]}
            entry.update(at=self.clock(), ms=int((self.timer() - t0) * 1000))
            done.append(entry)
            last = self.store.get(LAST_KEY, {}) or {}
            last[job.name] = self.clock()                         # a failed job also waits for its interval (no hot retry loop)
            self.store.set(LAST_KEY, last)
        if done:
            self.store.set(LOG_KEY, ((self.store.get(LOG_KEY, []) or []) + done)[-LOG_KEEP:])
        return done


def describe(log: list[dict[str, Any]], now: float, enabled: bool = True) -> str:
    if not enabled:
        return "Idle work is switched off (IDLE_WORK_ENABLED=false)."
    if not log:
        return "No idle work has run yet. It starts after you have been away, on mains power, with a quiet PC."
    lines = [f"{time.strftime('%d %b %H:%M', time.localtime(e['at']))} - {e['job']}: {'ok' if e['ok'] else 'FAILED'} - {e['result']} ({e['ms']} ms)" for e in log[-12:]]
    return "Idle work actually run (newest last):\n" + "\n".join(lines)


def briefing_line(store: Any, now: float, max_age_h: float = 18.0) -> str:
    """One line for the morning briefing from the stored opportunity list; empty when there is none or it is old."""
    data = store.get(OPP_KEY, None)
    if not data or now - float(data.get("at", 0)) > max_age_h * 3600 or not data.get("items"):
        return ""
    first = data["items"][0]
    more = len(data["items"]) - 1
    return f"While you were away I found {len(data['items'])} thing{'s' if more else ''} worth a look: {first['title']} ({first['evidence'][:80]})" + (f" and {more} more." if more else ".")
