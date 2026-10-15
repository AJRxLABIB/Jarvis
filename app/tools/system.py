"""System monitoring and process tools (real measurements via psutil)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime
from typing import Any, Literal

import psutil
from pydantic import Field, model_validator

from app.config.files import default_permissions
from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult, clamped_int

_GB = 1024**3
_SAMPLE_SECONDS = 0.5
# Never terminated, whatever the config says.
_ALWAYS_PROTECTED = frozenset({"system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe",
                               "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe", "memory compression"})


def _gb(value: float) -> float:
    return round(value / _GB, 1)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [f"{days} day{'s' * (days != 1)}"] if days else []
    if hours:
        parts.append(f"{hours} hour{'s' * (hours != 1)}")
    parts.append(f"{minutes} minute{'s' * (minutes != 1)}")
    return ", ".join(parts)


# ------------------------------------------------------------------- readings


def cpu_info() -> dict[str, Any]:
    percent = psutil.cpu_percent(interval=_SAMPLE_SECONDS)
    freq = psutil.cpu_freq()
    return {
        "cpu_percent": round(percent),
        "logical_cores": psutil.cpu_count() or 0,
        "physical_cores": psutil.cpu_count(logical=False) or 0,
        "frequency_mhz": round(freq.current) if freq else None,
    }


def memory_info() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    return {"ram_percent": round(vm.percent), "ram_used_gb": _gb(vm.total - vm.available),
            "ram_total_gb": _gb(vm.total), "ram_available_gb": _gb(vm.available)}


def disk_info() -> dict[str, Any]:
    drives = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        drives.append({"drive": part.mountpoint, "used_percent": round(usage.percent),
                       "free_gb": _gb(usage.free), "total_gb": _gb(usage.total)})
    return {"drives": drives}


def battery_info() -> dict[str, Any]:
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery is None:
        return {"battery": None}
    info: dict[str, Any] = {"battery_percent": round(battery.percent), "plugged_in": bool(battery.power_plugged)}
    if not battery.power_plugged and battery.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
        info["time_left"] = _duration(battery.secsleft)
    return info


def network_info() -> dict[str, Any]:
    before = psutil.net_io_counters()
    time.sleep(_SAMPLE_SECONDS)
    after = psutil.net_io_counters()
    factor = 8 / 1_000_000 / _SAMPLE_SECONDS  # bytes per sample -> megabits per second
    return {
        "download_mbps": round((after.bytes_recv - before.bytes_recv) * factor, 2),
        "upload_mbps": round((after.bytes_sent - before.bytes_sent) * factor, 2),
        "received_since_boot_gb": _gb(after.bytes_recv),
        "sent_since_boot_gb": _gb(after.bytes_sent),
    }


def uptime_info() -> dict[str, Any]:
    boot = psutil.boot_time()
    return {"uptime": _duration(time.time() - boot),
            "booted_at": datetime.fromtimestamp(boot).astimezone().strftime("%Y-%m-%d %H:%M")}


def gpu_info() -> dict[str, Any]:
    """NVIDIA GPUs via nvidia-smi (fixed arguments). Other GPUs are reported as unavailable."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"gpu": None, "gpu_note": "GPU statistics are only available for NVIDIA GPUs (nvidia-smi not found)."}
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"gpu": None, "gpu_note": "nvidia-smi did not respond."}
    gpus = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 5:
            name, util, used, total, temp = parts
            gpus.append({"name": name, "utilization_percent": util, "memory_used_mb": used,
                         "memory_total_mb": total, "temperature_c": temp})
    return {"gpus": gpus} if gpus else {"gpu": None, "gpu_note": "nvidia-smi returned no data."}


_ASPECTS = {"cpu": cpu_info, "memory": memory_info, "disk": disk_info, "battery": battery_info,
            "network": network_info, "uptime": uptime_info, "gpu": gpu_info}


def _summary(data: dict[str, Any]) -> str:
    parts = []
    if "cpu_percent" in data:
        parts.append(f"CPU {data['cpu_percent']}%")
    if "ram_percent" in data:
        parts.append(f"RAM {data['ram_used_gb']} of {data['ram_total_gb']} GB ({data['ram_percent']}%)")
    if "battery_percent" in data:
        parts.append(f"battery {data['battery_percent']}%{' charging' if data['plugged_in'] else ''}")
    elif "battery" in data:
        parts.append("no battery detected")
    for drive in data.get("drives", [])[:3]:
        parts.append(f"{drive['drive']} {drive['free_gb']} GB free")
    if "download_mbps" in data:
        parts.append(f"network down {data['download_mbps']} / up {data['upload_mbps']} Mbps")
    if "uptime" in data:
        parts.append(f"up {data['uptime']}")
    return "; ".join(parts) + "." if parts else "No data."


class SystemStatusArgs(ToolArgs):
    aspect: Literal["all", "cpu", "memory", "disk", "battery", "network", "uptime", "gpu"] = Field(
        default="all", description="Which reading. 'all' = CPU, memory, disk, battery and uptime."
    )


def system_status(args: SystemStatusArgs, _: ToolContext) -> ToolResult:
    names = ["cpu", "memory", "disk", "battery", "uptime"] if args.aspect == "all" else [args.aspect]
    data: dict[str, Any] = {}
    for name in names:
        data.update(_ASPECTS[name]())
    if args.aspect == "battery" and "battery_percent" in data:
        state = "charging" if data.get("plugged_in") else "on battery power"
        return ToolResult.success(f"The battery is at {data['battery_percent']}%, {state}.", **data)
    return ToolResult.success(_summary(data) if args.aspect != "gpu" else "GPU readings.", **data)


# ------------------------------------------------------------------ processes


def _process_table(sort_by: str) -> list[dict[str, Any]]:
    """Processes grouped by name (Chrome runs dozens), with CPU sampled over 0.5 s."""
    procs = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            proc.cpu_percent(None)  # prime the counter
            procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(_SAMPLE_SECONDS)
    cores = psutil.cpu_count() or 1
    groups: dict[str, dict[str, Any]] = {}
    for proc in procs:
        try:
            name = (proc.info.get("name") or "").strip()
            if not name or proc.pid == 0 or name.lower() == "system idle process":
                continue
            cpu = proc.cpu_percent(None) / cores
            memory = proc.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        group = groups.setdefault(name.lower(), {"name": name, "processes": 0, "cpu_percent": 0.0, "memory_mb": 0.0})
        group["processes"] += 1
        group["cpu_percent"] += cpu
        group["memory_mb"] += memory / 1024**2
    rows = sorted(groups.values(), key=lambda g: g["cpu_percent" if sort_by == "cpu" else "memory_mb"], reverse=True)
    for row in rows:
        row["cpu_percent"] = round(row["cpu_percent"], 1)
        row["memory_mb"] = round(row["memory_mb"])
    return rows


class ListProcessesArgs(ToolArgs):
    sort_by: Literal["cpu", "memory"] = Field(default="cpu", description="Sort by CPU or memory (RAM) use")
    limit: clamped_int(1, 15, 5, "How many to return")  # type: ignore[valid-type]
    apps_only: bool = Field(default=False, description="Only applications with an open window")


def list_processes(args: ListProcessesArgs, _: ToolContext) -> ToolResult:
    rows = _process_table(args.sort_by)
    note = None
    if args.apps_only:
        if winapi.is_windows():
            titles: dict[str, list[str]] = {}
            for window in winapi.visible_windows():
                titles.setdefault(window.process_name, []).append(window.title[:60])
            rows = [dict(r, windows=titles[r["name"].lower()][:3]) for r in rows if r["name"].lower() in titles]
        else:
            note = "Window information is only available on Windows; showing all processes."
    rows = rows[: args.limit]
    if not rows:
        return ToolResult.success("No matching applications are running.", processes=[])
    key = "cpu_percent" if args.sort_by == "cpu" else "memory_mb"
    unit = "% CPU" if args.sort_by == "cpu" else " MB"
    top = ", ".join(f"{r['name']} ({r[key]}{unit})" for r in rows[:3])
    data: dict[str, Any] = {"processes": rows, "sorted_by": args.sort_by}
    if note:
        data["note"] = note
    return ToolResult.success(f"Top by {args.sort_by}: {top}.", **data)


def _protected_names(ctx: ToolContext) -> set[str]:
    configured = (ctx.config.permissions if ctx.config else default_permissions()).protected_processes
    return set(configured) | _ALWAYS_PROTECTED


class EndProcessArgs(ToolArgs):
    name: str | None = Field(default=None, max_length=80, description="Process name, e.g. 'notepad.exe'")
    pid: int | None = Field(default=None, ge=1, le=2**31, description="Process id, if known")

    @model_validator(mode="after")
    def _one_target(self) -> "EndProcessArgs":
        if (self.name is None) == (self.pid is None):
            raise ValueError("give exactly one of name or pid")
        if self.name is not None:
            name = self.name.strip().lower()
            self.name = name if name.endswith(".exe") or "." in name else f"{name}.exe"
        return self


def _matching_processes(args: EndProcessArgs) -> list[psutil.Process]:
    if args.pid is not None:
        try:
            return [psutil.Process(args.pid)]
        except psutil.NoSuchProcess:
            return []
    found = []
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower() == args.name:
            found.append(proc)
    return found


def _is_protected(proc: psutil.Process, protected: set[str]) -> bool:
    try:
        name = proc.name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True  # cannot identify it -> do not touch it
    own = {os.getpid(), os.getppid()}
    return proc.pid in own or proc.pid <= 4 or name in protected


def end_process(args: EndProcessArgs, ctx: ToolContext) -> ToolResult:
    protected = _protected_names(ctx)
    procs = _matching_processes(args)
    target = args.name or f"process {args.pid}"
    if not procs:
        raise ToolError(f"No running process matches {target}.", "not_running")
    if any(_is_protected(p, protected) for p in procs):
        raise ToolError(f"{target} is a protected system process and will not be ended.", "protected")
    for proc in procs:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            raise ToolError(f"Windows denied access to end {target} (it may need administrator rights).", "access_denied") from None
    gone, alive = psutil.wait_procs(procs, timeout=3)
    if alive:
        return ToolResult.failure("still_running", f"Asked {target} to end, but {len(alive)} process(es) are still running.",
                                  ended=len(gone), still_running=len(alive))
    return ToolResult.success(f"Ended {target} ({len(gone)} process{'es' * (len(gone) != 1)}).", ended=len(gone))


def _end_process_risk(args: EndProcessArgs, ctx: ToolContext) -> RiskLevel:
    """Protected targets become CRITICAL (blocked by default) before anyone is asked."""
    protected = _protected_names(ctx)
    if args.name and args.name in protected:
        return RiskLevel.CRITICAL
    if args.pid is not None:
        try:
            if _is_protected(psutil.Process(args.pid), protected):
                return RiskLevel.CRITICAL
        except psutil.NoSuchProcess:
            pass
    return RiskLevel.DANGEROUS


def _end_process_question(args: EndProcessArgs, _: ToolContext) -> str:
    count = len(_matching_processes(args))
    target = args.name or f"process {args.pid}"
    extra = f" ({count} processes)" if count > 1 else ""
    return f"Force-end {target}{extra}? Unsaved work in it will be lost."


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="system_status",
        description="Measure CPU usage, RAM/memory usage, disk space, battery level, network speed, uptime or GPU. "
                    "Use for any question about the computer's performance or battery.",
        handler=system_status, args_model=SystemStatusArgs, level=RiskLevel.SAFE, category="system", timeout=15,
    ),
    Tool(
        name="list_processes",
        description="List running programs sorted by CPU or memory use. Use for 'what is using the most RAM/CPU' "
                    "or 'show running applications' (apps_only=true).",
        handler=list_processes, args_model=ListProcessesArgs, level=RiskLevel.SAFE, category="system", timeout=15,
    ),
    Tool(
        name="end_process",
        description="Force-end (kill) a running process. Only when the user explicitly asks to kill or force-quit "
                    "something; to close an app normally use close_application.",
        handler=end_process, args_model=EndProcessArgs, level=RiskLevel.DANGEROUS, category="system", timeout=15,
        risk=_end_process_risk, confirmation=_end_process_question,
    ),
)
