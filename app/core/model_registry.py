"""Local model registry: one honest row per installed model, joined from what each source actually knows.

Sources: the engine's own list (family, size, quantization, REPORTED capabilities), the model's metadata (licence start, trained context), and the
newest measured speed (model bench). Nothing is guessed: a field no source reported is ``None`` and shown as "not reported" / "not measured".
Capabilities are only what the engine reports (a name is never taken as proof of vision, tools or reasoning). The licence text is shown as the
model states it; whether a use is PERMITTED is for the reader to check - this module does not judge licences. Pure logic; the readers are in
``app/tools/provider_tools.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Entry:
    name: str
    family: str | None = None
    parameters: str | None = None
    quantization: str | None = None
    size_gb: float | None = None
    capabilities: list[str] | None = None            # None = the engine did not report them
    context_length: int | None = None
    license_head: str | None = None
    tokens_per_s: float | None = None
    load_seconds: float | None = None
    gpu_share: float | None = None
    measured_at: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def role(self) -> str:
        if self.capabilities is not None and "embedding" in self.capabilities and "completion" not in self.capabilities:
            return "embedding"
        return "chat"


def _lookup(table: dict[str, Any], name: str) -> dict[str, Any]:
    if name in table:
        return table[name]
    base = name.split(":")[0]
    for key, row in table.items():
        if key == name or (key.split(":")[0] == base and (key.endswith(":latest") or name.endswith(":latest"))):
            return row
    return {}


def build(models: list[Any], speed: dict[str, dict[str, Any]] | None = None, details: dict[str, dict[str, Any]] | None = None) -> list[Entry]:
    speed, details = speed or {}, details or {}
    out = []
    for m in models:
        sp, de = _lookup(speed, m.name), _lookup(details, m.name)
        e = Entry(m.name, m.family, m.parameter_size, m.quantization,
                  round(m.size_bytes / 1e9, 2) if m.size_bytes else None,
                  sorted(m.capabilities) if m.capabilities is not None else None,
                  de.get("context_length"), de.get("license"),
                  sp.get("tokens_per_s"), round(sp["load_ms"] / 1000, 1) if sp.get("load_ms") is not None else None,
                  sp.get("gpu_share"), sp.get("at"))
        if e.capabilities is None:
            e.notes.append("capabilities not reported by the engine: none assumed")
        if sp and e.gpu_share is not None and e.gpu_share < 0.95:
            e.notes.append(f"only {e.gpu_share:.0%} of it fit on the GPU when measured (the rest runs on the CPU, slower)")
        if not sp and e.role == "chat":
            e.notes.append("speed not measured (python run.py --compare-models)")
        if not e.license_head:
            e.notes.append("licence text not reported: check the model's page before relying on it")
        out.append(e)
    return sorted(out, key=lambda e: (e.role != "chat", e.name))


def line(e: Entry) -> str:
    def f(v, fmt="{}", none="not reported"):
        return none if v is None else fmt.format(v)

    caps = "not reported" if e.capabilities is None else (", ".join(e.capabilities) or "none")
    speed = f(e.tokens_per_s, "{:.0f} tokens/s", "not measured") + (f", load {e.load_seconds} s" if e.load_seconds is not None else "")
    head = (f"{e.name} [{e.role}] {f(e.family)} {f(e.parameters, '{}', '')} {f(e.quantization, '{}', '')}".replace("  ", " ").strip()
            + f" - {f(e.size_gb, '{} GB')}, context {f(e.context_length)}")
    return head + f"\n    capabilities: {caps}; speed: {speed}\n    licence: {f(e.license_head)}" + ("".join(f"\n    note: {n}" for n in e.notes))


def report(entries: list[Entry]) -> str:
    if not entries:
        return "No local models found."
    return "\n".join(line(e) for e in entries) + "\nFrom the engine's own metadata and your measurements only. A licence line is what the model states, not legal advice."
