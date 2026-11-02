"""Check results and the saved validation ledger."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PASS, FAIL, WARN, SKIP, PENDING = "pass", "fail", "warn", "skip", "pending"
LABELS = {PASS: "[PASS]", FAIL: "[FAIL]", WARN: "[WARN]", SKIP: "[SKIP]", PENDING: "[PENDING]"}


@dataclass
class Result:
    name: str
    status: str
    detail: str
    ms: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        return f"{LABELS[self.status]} {self.name}: {self.detail}" + (f"  ({self.ms} ms)" if self.ms else "")


def timed(name: str, fn: Callable[[], tuple[str, str] | tuple[str, str, dict[str, Any]]]) -> Result:
    """Run one check; any exception becomes a FAIL line (a crashed check is never a pass)."""
    started = time.perf_counter()
    try:
        out = fn()
        status, detail = out[0], out[1]
        evidence = out[2] if len(out) > 2 else {}
    except Exception as exc:
        status, detail, evidence = FAIL, f"{type(exc).__name__}: {str(exc)[:200]}", {}
    return Result(name, status, detail, int((time.perf_counter() - started) * 1000), evidence)


@dataclass
class Report:
    area: str
    results: list[Result] = field(default_factory=list)
    started: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    environment: dict[str, Any] = field(default_factory=dict)

    def add(self, result: Result) -> Result:
        self.results.append(result)
        return result

    def counts(self) -> dict[str, int]:
        return {s: sum(r.status == s for r in self.results) for s in (PASS, FAIL, WARN, SKIP, PENDING)}

    @property
    def verdict(self) -> str:
        c = self.counts()
        if c[FAIL]:
            return "FAILED"
        if c[PENDING] or c[SKIP] or not self.results:
            return "INCOMPLETE"       # something could not be checked: never "validated"
        return "PASSED WITH WARNINGS" if c[WARN] else "PASSED"

    def describe(self) -> str:
        c = self.counts()
        head = f"{self.area}: {self.verdict} - " + ", ".join(f"{n} {s}" for s, n in c.items() if n)
        return "\n".join([*[r.line() for r in self.results], "", head])

    def to_dict(self) -> dict[str, Any]:
        return {"area": self.area, "started": self.started, "verdict": self.verdict, "counts": self.counts(),
                "environment": self.environment, "results": [r.__dict__ for r in self.results]}


def folder(data_dir: Path) -> Path:
    path = Path(data_dir) / "validation"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save(data_dir: Path, report: Report) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = folder(data_dir) / f"{report.area}-{stamp}.json"
    path.write_text(json.dumps(report.to_dict(), indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def last_reports(data_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(folder(data_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out[data["area"]] = data
        except (ValueError, KeyError, OSError):
            continue
    return out


def status_text(data_dir: Path, areas: tuple[str, ...] = ("windows", "voice", "phone", "gesture")) -> str:
    saved = last_reports(data_dir)
    lines = []
    for area in areas:
        r = saved.get(area)
        if r is None:
            lines.append(f"{area}: NEVER RUN on this PC (nothing here has been validated on hardware).")
        else:
            c = r["counts"]
            lines.append(f"{area}: {r['verdict']} at {r['started'][:16].replace('T', ' ')} UTC - "
                         + ", ".join(f"{n} {s}" for s, n in c.items() if n))
    return "\n".join(lines)
