"""One health report: self-test, voice benchmark, Bangla answer score, speed and this week's mistakes,
compared with the previous report.

    python run.py --health-report        (or say "health report")

Saved as data\\reports\\health-<date>.md (readable) and data\\reports\\health.jsonl (the numbers, to
compare versions). Nothing leaves the PC.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

KEYS = (("selftest_passed", "Self-test passed", ""), ("selftest_failed", "Self-test failed", ""),
        ("benchmark_accuracy", "Voice commands understood", "%"), ("bangla_score", "Bangla answer score", "%"),
        ("thinking_s", "Typical thinking time", " s"), ("hearing_s", "Typical hearing time", " s"),
        ("ai_on_gpu", "AI model on the GPU", "%"),
        ("not_found", "Things it couldn't find (7 days)", ""), ("bad_answers", "Bad answers (7 days)", ""))
LOWER_IS_BETTER = {"selftest_failed", "thinking_s", "hearing_s", "not_found", "bad_answers"}


def folder(data_dir: Path) -> Path:
    return Path(data_dir) / "reports"


def previous(data_dir: Path) -> dict[str, Any] | None:
    path = folder(data_dir) / "health.jsonl"
    if not path.is_file():
        return None
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    try:
        return json.loads(lines[-1]) if lines else None
    except ValueError:
        return None


def due(data_dir: Path, days: float = 7.0, now: datetime | None = None) -> bool:
    """True when there is no health report yet, or the last one is ``days`` old."""
    last = previous(data_dir)
    try:
        at = datetime.fromisoformat(str((last or {}).get("at")))
    except ValueError:
        return True
    return ((now or datetime.now()) - at).total_seconds() >= days * 86400


def worse(summary: list[str]) -> list[str]:
    return [line for line in summary if "(worse" in line]


def compare(now: dict[str, Any], before: dict[str, Any] | None) -> list[str]:
    """One line per number, with the change since the last report (better / worse)."""
    lines = []
    for key, label, unit in KEYS:
        value = now.get(key)
        if value is None:
            continue
        text = f"{label}: {value:g}{unit}" if isinstance(value, (int, float)) else f"{label}: {value}"
        old = (before or {}).get(key)
        if isinstance(value, (int, float)) and isinstance(old, (int, float)) and old != value:
            better = (value < old) if key in LOWER_IS_BETTER else (value > old)
            text += f"  ({'better' if better else 'worse'} than {old:g}{unit})"
        lines.append(text)
    return lines


def save(data_dir: Path, numbers: dict[str, Any], sections: dict[str, list[str]]) -> tuple[Path, list[str]]:
    from app import __version__

    before = previous(data_dir)
    stamp = datetime.now()
    numbers = {"at": stamp.isoformat(timespec="seconds"), "version": __version__, **numbers}
    summary = compare(numbers, before)
    target = folder(data_dir)
    target.mkdir(parents=True, exist_ok=True)
    lines = [f"# Jarvis health report - {stamp:%d %B %Y %H:%M} (v{__version__})", "", "## Summary", ""]
    lines += [f"- {line}" for line in summary] or ["- (no measurements)"]
    for title, body in sections.items():
        lines += ["", f"## {title}", "", *body]
    path = target / f"health-{stamp:%Y%m%d-%H%M%S}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with open(target / "health.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(numbers, ensure_ascii=False) + "\n")
    return path, summary


def collect(ctx, ai, stt, tts_engine, run_async, out=print) -> tuple[Path, list[str]]:
    """Run every check with the parts the caller already has (the app, or jarvis-cli) and save the report."""
    from statistics import median

    from app import benchmark, eval_bangla
    from app.agent import learning
    from app.ai.base import ChatMessage
    from app.ai.ollama import strip_think
    from app.ai.prompts import build_system_prompt
    from app.selftest import Parts, run_selftest
    from app.tools.applications import known_application
    from app.tools.base import ToolContext
    from app.utils.speed import SpeedLog

    s = ctx.settings
    numbers: dict = {}
    sections: dict[str, list[str]] = {}
    out("1/4 Self-test...")
    results = run_selftest(Parts(s, ai=ai, stt=stt, tts_engine=tts_engine, models_dir=ctx.paths.models_dir,
                                 run_async=run_async), use_microphone=False)
    numbers["selftest_passed"] = sum(r.status == "pass" for r in results)
    numbers["selftest_failed"] = sum(r.status == "fail" for r in results)
    sections["Self-test"] = [f"- {r.line()}" for r in results]
    out("2/4 Voice benchmark...")
    if benchmark.load_items(ctx.paths.data_dir) and stt is not None:
        tool_ctx = ToolContext(s, ctx.paths, ctx.database, ctx.config)
        language = None if s.voice_language == "auto" else s.voice_language
        lines: list[str] = []
        result = benchmark.run(ctx.paths.data_dir, lambda audio: stt.transcribe(audio, language),
                               lambda name: known_application(name, tool_ctx), out=lines.append)
        if result:
            numbers["benchmark_accuracy"] = result["accuracy"]
        sections["Voice benchmark"] = [f"    {line}" for line in lines]
    else:
        sections["Voice benchmark"] = ["No recordings yet: say 'record my benchmark' or run jarvis-cli --record-benchmark."]
    out("3/4 Bangla answers (8 questions)...")
    system = build_system_prompt(assistant_name=s.assistant_name, now=datetime.now(), reply_language="bn", spoken=True)

    async def ask(question: str) -> str:
        reply = await ai.chat([ChatMessage.system(system), ChatMessage.user(question)])
        return strip_think(reply.message.content or "")

    bangla_lines: list[str] = []
    try:  # load the model first: its loading time is not an answer (the last report timed out on question 1)
        run_async(ai.warm_up())
        share = getattr(ai, "gpu_share", None)
        if share is not None:
            numbers["ai_on_gpu"] = round(100 * share)
    except Exception:
        pass
    try:
        scores = run_async(eval_bangla.evaluate(ask, bangla_lines.append, eval_bangla.QUESTIONS[:8]))
        numbers["bangla_score"] = eval_bangla.summary(scores)["score"]
    except Exception as exc:
        bangla_lines.append(f"Could not ask the AI: {exc}")
    sections["Bangla answers"] = [f"    {line}" for line in bangla_lines]
    out("4/4 Speed and mistakes...")
    speed = SpeedLog(ctx.paths.data_dir)
    for key, name in (("ai", "thinking_s"), ("heard", "hearing_s")):
        values = [e[key] for e in speed.entries[-50:] if key in e]
        if values:
            numbers[name] = round(median(values), 2)
    sections["Speed"] = speed.report().splitlines()
    counts = learning.summary(ctx.database)
    numbers["not_found"] = counts.get("not_found", 0)
    numbers["bad_answers"] = counts.get("bad_answer", 0)
    sections["Mistakes (7 days)"] = [f"- {k}: {v}" for k, v in sorted(counts.items())] or ["- none"]
    return save(ctx.paths.data_dir, numbers, sections)
