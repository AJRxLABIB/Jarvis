"""Bounded parallel execution of independent jobs with dependencies.

Used where parallelism is safe and worth it (several read-only lookups at once). Rules:

* At most ``limit`` jobs run at the same time (a semaphore), so a plan can't start an unbounded number of
  tool calls, subprocesses or model loads.
* A job starts only after everything it depends on has finished successfully. If a dependency fails, is
  skipped or never ran, the job is **skipped** with a reason - it never runs on missing input.
* Each job has its own timeout; the whole batch has a deadline. Jobs that never started are ``not_run``.
* Cancelling the batch cancels every running job and marks the rest ``not_run`` (no orphan work).
* Results come back in the order the jobs were given, whatever order they finished in.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

DONE, FAILED, SKIPPED, NOT_RUN, TIMEOUT = "done", "failed", "skipped", "not_run", "timeout"


@dataclass
class Job:
    id: str
    run: Callable[[], Awaitable[Any]]
    depends_on: tuple[str, ...] = ()
    timeout: float | None = None


@dataclass
class JobResult:
    id: str
    state: str = NOT_RUN
    value: Any = None
    error: str = ""
    seconds: float = 0.0


@dataclass
class BatchResult:
    results: list[JobResult] = field(default_factory=list)
    peak_parallel: int = 0

    def by_id(self) -> dict[str, JobResult]:
        return {r.id: r for r in self.results}

    @property
    def ok(self) -> bool:
        return all(r.state == DONE for r in self.results)


def validate(jobs: list[Job]) -> None:
    """Unique ids, known dependencies, no cycles."""
    ids = [j.id for j in jobs]
    if len(set(ids)) != len(ids):
        raise ValueError("job ids must be unique")
    known = set(ids)
    for job in jobs:
        missing = [d for d in job.depends_on if d not in known]
        if missing:
            raise ValueError(f"job {job.id} depends on unknown job {missing[0]}")
    state: dict[str, int] = {}
    graph = {j.id: j.depends_on for j in jobs}

    def visit(node: str) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            raise ValueError(f"jobs depend on each other in a loop (at {node})")
        state[node] = 1
        for dep in graph[node]:
            visit(dep)
        state[node] = 2

    for job in jobs:
        visit(job.id)


async def run_jobs(jobs: list[Job], *, limit: int = 3, deadline_seconds: float | None = None) -> BatchResult:
    """Run ``jobs`` with at most ``limit`` at a time, respecting dependencies. Never raises for a job's own failure."""
    validate(jobs)
    limit = max(1, limit)
    results = {j.id: JobResult(j.id) for j in jobs}
    semaphore = asyncio.Semaphore(limit)
    finished: dict[str, asyncio.Event] = {j.id: asyncio.Event() for j in jobs}
    running = 0
    batch = BatchResult([results[j.id] for j in jobs])
    started = time.monotonic()

    async def one(job: Job) -> None:
        nonlocal running
        result = results[job.id]
        try:
            for dep in job.depends_on:
                await finished[dep].wait()
            blocked = [d for d in job.depends_on if results[d].state != DONE]
            if blocked:
                result.state, result.error = SKIPPED, f"needs {blocked[0]}, which {results[blocked[0]].state.replace('_', ' ')}"
                return
            async with semaphore:
                if deadline_seconds is not None and time.monotonic() - started >= deadline_seconds:
                    result.state, result.error = NOT_RUN, "the time budget ran out before this job started"
                    return
                running += 1
                batch.peak_parallel = max(batch.peak_parallel, running)
                begin = time.monotonic()
                try:
                    remaining = None if deadline_seconds is None else max(0.0, deadline_seconds - (begin - started))
                    limit_s = min(x for x in (job.timeout, remaining) if x is not None) \
                        if (job.timeout is not None or remaining is not None) else None
                    result.value = await asyncio.wait_for(job.run(), limit_s)
                    result.state = DONE
                except asyncio.TimeoutError:
                    result.state, result.error = TIMEOUT, "took too long and was stopped"
                except asyncio.CancelledError:
                    result.state, result.error = NOT_RUN, "cancelled"
                    raise
                except Exception as exc:  # a job's own failure is its result, not the batch's
                    result.state, result.error = FAILED, f"{exc.__class__.__name__}: {exc}"
                finally:
                    running -= 1
                    result.seconds = round(time.monotonic() - begin, 3)
        finally:
            finished[job.id].set()

    tasks = [asyncio.ensure_future(one(j)) for j in jobs]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for r in results.values():
            if r.state == NOT_RUN and not r.error:
                r.error = "cancelled"
        raise
    return batch
