"""Task placement across the user's own devices: which trusted device should do this work, and why the others were ruled out.

Pure scoring over a list of ``Candidate`` devices (built from ``core/device_mesh`` plus local resource readings), so it is tested without a network.

Hard rules first (a device that fails one is EXCLUDED, with the reason, never merely ranked low):
* it must be online and trusted (this PC, or a paired device) - a device found on the network but not paired is never used;
* it must offer every ability the task needs;
* private work stays on devices the task's privacy class allows (``private`` = this PC only; ``personal`` = trusted devices; ``public`` = any);
* it must have enough free memory (RAM, and GPU memory when the task needs the GPU) when those numbers are known;
* battery must not be low on a device that is not charging, when the task is long.
Then soft scoring: more free RAM / GPU headroom, a mains-powered or well-charged device, and the local PC (no transfer) win ties.
Unknown numbers are never invented: they are neither a pass nor a fail and are listed as "unknown".
Placement only RECOMMENDS. Sending work to another device still goes through the existing handoff / phone-command rules, approvals and audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PRIVACY = ("public", "personal", "private")
LOW_BATTERY = 25


@dataclass
class Device:
    key: str
    name: str
    kind: str = "pc"                       # pc | computer | phone
    local: bool = False                    # the machine Jarvis is running on
    online: bool = True
    trusted: bool = True
    abilities: frozenset[str] = frozenset()
    free_ram_mb: int | None = None
    free_vram_mb: int | None = None
    battery: int | None = None
    charging: bool | None = None
    busy: int = 0                          # running tasks


@dataclass
class Task:
    needs: frozenset[str] = frozenset()
    privacy: str = "personal"
    ram_mb: int = 0
    vram_mb: int = 0
    minutes: int = 1


@dataclass
class Choice:
    device: Device
    score: float
    notes: list[str] = field(default_factory=list)


@dataclass
class Placement:
    chosen: Choice | None
    ranked: list[Choice]
    excluded: list[tuple[Device, str]]
    unknowns: list[str]

    def text(self) -> str:
        if self.chosen is None:
            why = "; ".join(f"{d.name}: {r}" for d, r in self.excluded) or "no devices known"
            return f"No suitable device. {why}."
        c = self.chosen
        lines = [f"Best device: {c.device.name} (score {c.score:.0f}) - {'; '.join(c.notes) or 'meets every requirement'}."]
        lines += [f"  also possible: {r.device.name} (score {r.score:.0f})" for r in self.ranked[1:3]]
        lines += [f"  ruled out - {d.name}: {r}" for d, r in self.excluded]
        if self.unknowns:
            lines.append("  not measured: " + ", ".join(self.unknowns))
        lines.append("This is a recommendation; moving work to another device still needs the normal approval.")
        return "\n".join(lines)


def _exclusion(d: Device, t: Task) -> str | None:
    if not d.online:
        return "offline"
    if not d.trusted:
        return "not paired / not trusted"
    if t.privacy == "private" and not d.local:
        return "private work stays on this PC"
    if t.privacy == "personal" and d.kind not in ("pc", "computer", "phone"):
        return "not a device of yours"
    missing = sorted(a for a in t.needs if a.lower() not in {x.lower() for x in d.abilities})
    if missing:
        return "lacks " + ", ".join(missing)
    if t.ram_mb and d.free_ram_mb is not None and d.free_ram_mb < t.ram_mb:
        return f"only {d.free_ram_mb} MB RAM free, needs {t.ram_mb}"
    if t.vram_mb:
        if d.free_vram_mb is None:
            return "no GPU memory reading, task needs the GPU"
        if d.free_vram_mb < t.vram_mb:
            return f"GPU memory {d.free_vram_mb} MB free, needs {t.vram_mb}"
    if t.minutes >= 10 and d.battery is not None and d.battery < LOW_BATTERY and d.charging is not True:
        return f"battery {d.battery}% and not charging, task is long"
    return None


def place(task: Task, devices: list[Device]) -> Placement:
    if task.privacy not in PRIVACY:
        task = Task(task.needs, "private", task.ram_mb, task.vram_mb, task.minutes)   # unknown class = strictest
    ranked: list[Choice] = []
    excluded: list[tuple[Device, str]] = []
    unknowns: set[str] = set()
    for d in devices:
        reason = _exclusion(d, task)
        if reason:
            excluded.append((d, reason))
            continue
        score, notes = 50.0, []
        if d.local:
            score += 15
            notes.append("no transfer needed")
        if d.free_ram_mb is None:
            unknowns.add(f"RAM on {d.name}")
        else:
            score += min(15.0, d.free_ram_mb / 1024)
        if task.vram_mb:
            score += min(10.0, ((d.free_vram_mb or 0) - task.vram_mb) / 512)
        if d.battery is None:
            if d.kind == "phone":
                unknowns.add(f"battery on {d.name}")
        elif d.charging or d.battery >= 60:
            score += 5
        else:
            score -= 5
            notes.append(f"battery {d.battery}%")
        score -= 6 * d.busy
        if d.busy:
            notes.append(f"{d.busy} task(s) already running")
        ranked.append(Choice(d, round(score, 1), notes))
    ranked.sort(key=lambda c: (-c.score, c.device.key))
    return Placement(ranked[0] if ranked else None, ranked, excluded, sorted(unknowns))


def from_mesh(nodes: list, local_ram_mb: int | None = None, local_vram_mb: int | None = None) -> list[Device]:
    """Turn ``device_mesh.Node`` rows into placement devices. Only 'available' abilities count; connection 'local' / 'connected' / 'recent' = online."""
    out = []
    for n in nodes:
        is_local = n.connection == "local" or n.trust == "this device"
        out.append(Device(
            key=n.key, name=n.name, kind=n.kind if n.kind in ("pc", "computer", "phone") else "other", local=is_local,
            online=n.connection in ("local", "connected", "recent"), trusted=n.trust in ("this device", "trusted"),
            abilities=frozenset(name for name, status in n.abilities if status == "available"),
            free_ram_mb=local_ram_mb if is_local else None, free_vram_mb=local_vram_mb if is_local else None,
            battery=n.battery, busy=len(n.running)))
    return out
