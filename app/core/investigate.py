"""Diagnostic investigation: from a symptom to ranked, evidence-backed explanations - and a check afterwards.

Reuses what exists (``core/troubleshoot`` findings + repairs, ``core/perf`` snapshot, the event log, Jarvis's own log file, Ollama reachability) and adds the
missing method:

1. the symptom is mapped to domains (model, voice, memory/GPU, disk, phone, network, browser, database, mission, plugin);
2. evidence is collected ONLY for those domains (the minimum needed), each item with a source, a time and whether it names a cause directly;
3. hypotheses are matched from a fixed rule table, never invented: each one lists the evidence that supports it;
4. each gets one of four honest classes - CONFIRMED (a direct evidence item states it, e.g. an error line or a failed check), STRONGLY_SUPPORTED (two or more
   independent sources point at it), POSSIBLE (one indirect hint), UNKNOWN (nothing matches: "no cause found" is a valid result);
5. next steps are TEXT (safe checks first); anything that changes something goes through the existing repair path and its approval;
6. ``recheck`` runs the same collectors again and says whether the supporting evidence is gone, unchanged or cannot be read - verification, not hope.
Pure ``investigate`` over injected collectors, so it is tested with made-up evidence. Tool: ``investigate``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

CONFIRMED, STRONG, POSSIBLE, UNKNOWN = "CONFIRMED", "STRONGLY_SUPPORTED", "POSSIBLE", "UNKNOWN"
_RANK = {CONFIRMED: 3, STRONG: 2, POSSIBLE: 1, UNKNOWN: 0}
LEDGER_KEY = "investigations"

DOMAIN_WORDS = {
    "model": ("ollama", "model", "answer", "slow", "reply", "response", "load", "thinking", "context"),
    "memory": ("memory", "vram", "gpu", "out of memory", "oom", "lag", "freez", "slow", "ram"),
    "voice": ("voice", "mic", "microphone", "speech", "listen", "hear", "audio", "speaker", "wake"),
    "disk": ("disk", "storage", "space", "full", "drive"),
    "phone": ("phone", "android", "paired", "mobile", "remote", "pair"),
    "network": ("network", "wifi", "internet", "offline", "connect", "online"),
    "browser": ("browser", "playwright", "chrome", "edge", "website", "page"),
    "database": ("database", "sqlite", "locked", "migration", "corrupt"),
    "mission": ("mission", "task", "workflow", "automation", "stuck", "failed"),
    "plugin": ("plugin", "extension"),
}


@dataclass
class Evidence:
    domain: str
    source: str                      # log | finding | resource | event | check
    text: str
    at: str = ""
    direct: bool = False             # the item itself states a failure / cause (an error line, a failed check)
    key: str = ""                    # what a hypothesis matches on


@dataclass
class Hypothesis:
    id: str
    cause: str
    confidence: str
    support: list[Evidence] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    repair: str = ""                 # id in core/troubleshoot.REPAIRS, run only with approval


@dataclass
class Investigation:
    symptom: str
    domains: list[str]
    evidence: list[Evidence]
    hypotheses: list[Hypothesis]
    gaps: list[str]
    at: str

    def text(self) -> str:
        out = [f"Symptom: {self.symptom}", "Looked at: " + (", ".join(self.domains) or "nothing specific")]
        if self.gaps:
            out.append("Could not read: " + "; ".join(self.gaps))
        if not self.hypotheses or all(h.confidence == UNKNOWN for h in self.hypotheses):
            out.append("No cause was found in the evidence I could read. That is not 'nothing is wrong': say what you see and I can look at something else.")
        for h in self.hypotheses:
            out.append(f"[{h.confidence}] {h.cause}")
            out += [f"    evidence ({e.source}): {e.text[:160]}" for e in h.support[:3]]
            out += [f"    next: {s}" for s in h.steps[:2]]
            if h.repair:
                out.append(f"    I can fix this after you approve it ('{h.repair}').")
        return "\n".join(out)


# (id, domain, cause, keys that count as hints, direct patterns in log / check text, safe steps, repair id)
RULES: list[tuple[str, str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...], str]] = [
    ("ollama_down", "model", "Ollama is not reachable, so no local model can answer", ("ollama_unreachable",), (r"connection refused|connecterror|ollama.*(unreachable|not running)",),
     ("check that Ollama is running (system tray) or start it", "open http://127.0.0.1:11434 in a browser"), ""),
    ("model_missing", "model", "The configured model is not installed in Ollama", ("model_missing",), (r"model .* not found|pull the model",),
     ("run 'ollama list' to see installed models", "pull the model or choose another in Settings"), ""),
    ("gpu_oom", "memory", "GPU memory ran out while loading or running a model", ("vram_low",), (r"out of memory|cuda error|cudamalloc|oom",),
     ("check which models are loaded ('what models are loaded')", "unload one or choose a smaller model"), ""),
    ("ram_low", "memory", "System memory is nearly used up", ("ram_low",), (r"memoryerror|cannot allocate memory",),
     ("close programs that use a lot of memory", "check the process list in the health view"), ""),
    ("disk_full", "disk", "The drive Jarvis stores data on is almost full", ("disk_low",), (r"no space left|disk full|errno 28",),
     ("free space on the data drive", "review what is kept ('data kept')"), ""),
    ("mic_missing", "voice", "No microphone input device was found", ("no_input_device",), (r"no (default )?input device|portaudio|invalid input device",),
     ("check the microphone is connected and allowed for desktop apps in Windows privacy settings", "pick the device in Settings"), ""),
    ("stt_missing", "voice", "A speech recognition package or model is missing", ("package_faster_whisper", "package_sounddevice"), (r"faster.whisper|no module named",),
     ("install the requirements into the project's .venv",), ""),
    ("phone_silent", "phone", "A paired phone has not checked in", ("phone_silent",), (r"phone.*(timeout|unreachable)|unauthorized device",),
     ("check the phone is on the same network and the Jarvis app is running", "re-pair if the trust was revoked"), ""),
    ("db_locked", "database", "The database is locked or damaged", ("db_error",), (r"database is locked|database disk image is malformed|sqlite3\.(operational|database)error",),
     ("close other copies of Jarvis", "restore the latest backup if the file is damaged"), ""),
    ("browser_missing", "browser", "The automation browser is not installed", ("package_playwright",), (r"executable doesn't exist|playwright install|browsertype\.launch",),
     ("run 'playwright install chromium' in the .venv",), ""),
    ("mission_failed", "mission", "A mission step failed", ("mission_failed",), (r"mission .* failed|step .* failed",),
     ("open the task replay for the mission to see the first failed step",), ""),
    ("plugin_failed", "plugin", "A plugin failed to load or crashed", ("plugin_failed",), (r"plugin .* failed",),
     ("open Menu > Plugins; a changed plugin must be trusted again after you check it",), ""),
]


def domains_for(symptom: str) -> list[str]:
    low = (symptom or "").lower()
    hit = [d for d, words in DOMAIN_WORDS.items() if any(w in low for w in words)]
    return hit or ["model", "memory", "disk"]               # nothing recognisable: look at the common causes, and say so


Collector = Callable[[list[str]], tuple[list[Evidence], list[str]]]


def investigate(symptom: str, collectors: list[Collector], now: datetime | None = None) -> Investigation:
    domains = domains_for(symptom)
    evidence: list[Evidence] = []
    gaps: list[str] = []
    for collect in collectors:
        try:
            got, missing = collect(domains)
        except Exception as exc:                                       # a broken collector is a gap, never a guess
            got, missing = [], [f"{getattr(collect, '__name__', 'a collector')} failed ({type(exc).__name__})"]
        evidence += got
        gaps += missing
    evidence.sort(key=lambda e: e.at or "")
    hyps = []
    for rid, dom, cause, keys, patterns, steps, repair in RULES:
        if dom not in domains:
            continue
        support = []
        for e in evidence:
            if e.key in keys or (e.direct and any(re.search(p, e.text, re.I) for p in patterns)):
                support.append(e)
        if not support:
            continue
        sources = {e.source for e in support}
        direct = any(e.direct for e in support)
        conf = CONFIRMED if direct and len(sources) >= 1 and any(e.source in ("log", "check", "finding") and e.direct for e in support) \
            else STRONG if len(sources) >= 2 else POSSIBLE
        hyps.append(Hypothesis(rid, cause, conf, support, list(steps), repair))
    hyps.sort(key=lambda h: (-_RANK[h.confidence], -len(h.support)))
    if not hyps:
        hyps = [Hypothesis("unknown", "No known cause matches the evidence", UNKNOWN)]
    return Investigation(symptom[:200], domains, evidence, hyps, gaps, (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"))


def recheck(previous: Investigation, collectors: list[Collector]) -> dict[str, str]:
    """After a fix: has the evidence that supported each hypothesis gone? -> {hypothesis id: resolved | unchanged | cannot_tell}."""
    now = investigate(previous.symptom, collectors)
    present = {h.id for h in now.hypotheses if h.confidence != UNKNOWN}
    result = {}
    for h in previous.hypotheses:
        if h.confidence == UNKNOWN:
            continue
        result[h.id] = "unchanged" if h.id in present else ("cannot_tell" if now.gaps else "resolved")
    return result


# ---- real collectors (used by the tool; tests pass fakes) ---------------------------------------------------------------------------------

ERROR_LINE = re.compile(r"\b(error|exception|traceback|failed|refused|critical)\b", re.I)


def log_collector(logs_dir: Any, max_lines: int = 400, max_items: int = 12) -> Collector:
    def collect(domains: list[str]) -> tuple[list[Evidence], list[str]]:
        from pathlib import Path

        files = sorted(Path(logs_dir).glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True) if logs_dir and Path(logs_dir).is_dir() else []
        if not files:
            return [], ["no log file"]
        lines = files[0].read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
        items = []
        for ln in lines:
            if ERROR_LINE.search(ln):
                stamp = ln[:19] if re.match(r"\d{4}-\d\d-\d\d", ln) else ""
                items.append(Evidence("*", "log", re.sub(r"(token|password|key)=\S+", r"\1=[hidden]", ln, flags=re.I)[:240], stamp, direct=True))
        return items[-max_items:], []
    return collect


def resource_collector(snapshot: Callable[[], Any], gpu: Callable[[], Any]) -> Collector:
    def collect(domains: list[str]) -> tuple[list[Evidence], list[str]]:
        out, gaps = [], []
        snap = snapshot()
        if "memory" in domains or "model" in domains:
            if snap.ram_free_gb is not None and (snap.ram_free_gb < 1.0 or (snap.ram_total_gb and snap.ram_free_gb / snap.ram_total_gb < 0.08)):
                out.append(Evidence("memory", "resource", f"only {snap.ram_free_gb:.1f} GB of RAM free", key="ram_low"))
            g = gpu()
            if g.total_gb is None:
                gaps.append("GPU memory (no NVIDIA reading)")
            elif g.free_gb is not None and g.free_gb < 0.8:
                out.append(Evidence("memory", "resource", f"only {g.free_gb:.1f} of {g.total_gb:.1f} GB GPU memory free", key="vram_low"))
        if "disk" in domains and snap.disk_free_gb is not None and snap.disk_free_gb < 3.0:
            out.append(Evidence("disk", "resource", f"only {snap.disk_free_gb:.1f} GB free on the data drive", key="disk_low"))
        return out, gaps
    return collect


def finding_collector(findings: Callable[[], list[Any]]) -> Collector:
    def collect(domains: list[str]) -> tuple[list[Evidence], list[str]]:
        out = []
        for f in findings():
            key = {"ollama_unreachable": "ollama_unreachable", "model_missing": "model_missing", "no_input_device": "no_input_device"}.get(f.id, f.id)
            out.append(Evidence("*", "finding", f"{f.problem}: {f.evidence}", key=key, direct=f.severity == "problem"))
        return out, []
    return collect


def event_collector(events: Callable[[], list[Any]]) -> Collector:
    def collect(domains: list[str]) -> tuple[list[Evidence], list[str]]:
        out = []
        for e in events():
            if e.name in ("MissionPaused", "PcProblem", "BuildFailed"):
                key = "mission_failed" if e.name == "MissionPaused" else ""
                out.append(Evidence("*", "event", f"{e.name}: {str(e.evidence)[:120]}", e.ts, key=key))
        return out, []
    return collect


def record(store: Any, inv: Investigation) -> None:
    items = list(store.get(LEDGER_KEY, []) or [])
    items.append({"at": inv.at, "symptom": inv.symptom, "domains": inv.domains, "top": [(h.id, h.confidence) for h in inv.hypotheses[:3]]})
    store.set(LEDGER_KEY, items[-20:])
