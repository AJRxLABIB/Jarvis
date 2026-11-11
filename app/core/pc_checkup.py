"""A whole-PC check-up: protection, updates, disks, battery wear, start-up load, free space - each as ok / attention / problem / unknown, with the evidence.

``pc_health`` already watches CPU, memory and disk over time and the guardian watches for changes; this is the part neither does: is the machine PROTECTED and
are its PARTS healthy. Everything is read-only - it changes no setting, starts no scan, removes nothing.

Readings come from Windows' own PowerShell cmdlets, run as fixed scripts (no user text is ever put into a command) that print JSON:
Get-MpComputerStatus (Defender), Get-NetFirewallProfile, Get-PhysicalDisk (+ Get-StorageReliabilityCounter), the WMI battery classes (design vs full-charge
capacity), the pending-restart registry keys, Get-HotFix, Win32_StartupCommand, Get-Volume. A reading that needs administrator rights or is not present on this
PC (no battery, third-party antivirus) is reported as UNKNOWN with the reason - never as "ok".

The thresholds are stated next to each check. They are rules of thumb, not vendor diagnostics: a disk can fail with a clean report, and battery "wear" is the
controller's own estimate.

Honest status: parsers and rules are tested with recorded-shape JSON; the scripts have NOT been run on Windows here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable

OK, ATTENTION, PROBLEM, UNKNOWN = "ok", "attention", "problem", "unknown"
TIMEOUT_S = 25
SCRIPTS: dict[str, str] = {
    "defender": "Get-MpComputerStatus | Select-Object AMServiceEnabled,AntivirusEnabled,RealTimeProtectionEnabled,AntivirusSignatureAge,QuickScanAge,FullScanAge | ConvertTo-Json -Compress",
    "firewall": "Get-NetFirewallProfile | Select-Object Name,Enabled | ConvertTo-Json -Compress",
    "disks": "Get-PhysicalDisk | ForEach-Object { $r = $_ | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue; [pscustomobject]@{Name=$_.FriendlyName;Media=[string]$_.MediaType;"
             "Health=[string]$_.HealthStatus;SizeGB=[math]::Round($_.Size/1GB);Wear=$r.Wear;TempC=$r.Temperature;ReadErrors=$r.ReadErrorsUncorrected} } | ConvertTo-Json -Compress",
    "battery": "$d=(Get-CimInstance -Namespace root\\wmi -ClassName BatteryStaticData -ErrorAction SilentlyContinue | Select-Object -First 1).DesignedCapacity; "
               "$f=(Get-CimInstance -Namespace root\\wmi -ClassName BatteryFullChargedCapacity -ErrorAction SilentlyContinue | Select-Object -First 1).FullChargedCapacity; "
               "$c=(Get-CimInstance -Namespace root\\wmi -ClassName BatteryCycleCount -ErrorAction SilentlyContinue | Select-Object -First 1).CycleCount; "
               "[pscustomobject]@{Design=$d;Full=$f;Cycles=$c} | ConvertTo-Json -Compress",
    "restart": "[pscustomobject]@{CBS=(Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Component Based Servicing\\RebootPending');"
               "WU=(Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired')} | ConvertTo-Json -Compress",
    "updates": "$h=Get-HotFix | Where-Object InstalledOn | Sort-Object InstalledOn -Descending | Select-Object -First 1; "
               "[pscustomobject]@{Last=if($h){$h.InstalledOn.ToString('yyyy-MM-dd')}else{''};Days=if($h){[int]((Get-Date)-$h.InstalledOn).TotalDays}else{$null}} | ConvertTo-Json -Compress",
    "startup": "@(Get-CimInstance Win32_StartupCommand | Select-Object Name,Location) | ConvertTo-Json -Compress",
    "volumes": "@(Get-Volume | Where-Object { $_.DriveLetter -and $_.DriveType -eq 'Fixed' } | Select-Object DriveLetter,@{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}},"
               "@{n='FreeGB';e={[math]::Round($_.SizeRemaining/1GB,1)}}) | ConvertTo-Json -Compress",
}
TITLES = {"defender": "Virus protection", "firewall": "Firewall", "disks": "Disk health", "battery": "Battery wear", "restart": "Restart pending",
          "updates": "Windows updates", "startup": "Start-up programs", "volumes": "Free space"}


@dataclass(frozen=True)
class Check:
    key: str
    status: str
    summary: str
    advice: str = ""

    @property
    def title(self) -> str:
        return TITLES.get(self.key, self.key)


Runner = Callable[[str], str]


# Not part of the check-up: read-only event queries for app/core/file_attribution.py. Events only exist when YOU switched recording on; reading Security needs admin.
EVENT_SCRIPTS: dict[str, str] = {
    "file_audit_events": "(Get-WinEvent -FilterHashtable @{LogName='Security';Id=4663} -MaxEvents 400 -ErrorAction Stop | ForEach-Object { $_.ToXml() }) -join ''",
    "sysmon_file_events": "(Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Sysmon/Operational';Id=11,23,26} -MaxEvents 400 -ErrorAction Stop | ForEach-Object { $_.ToXml() }) -join ''",
}


def run_powershell(script: str) -> str:
    """Runs one of the FIXED scripts above and returns its text. Raises RuntimeError with a short reason."""
    if sys.platform != "win32":
        raise RuntimeError("only available on Windows")
    if script not in SCRIPTS.values() and script not in EVENT_SCRIPTS.values():
        raise RuntimeError("not one of the built-in read-only scripts")
    args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT_S, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError("Windows took too long to answer") from None
    except OSError as exc:
        raise RuntimeError(f"PowerShell could not be started ({exc.__class__.__name__})") from None
    if done.returncode != 0 and not done.stdout.strip():
        raise RuntimeError((done.stderr or "no answer").strip().splitlines()[0][:160])
    return done.stdout


def _load(text: str) -> Any:
    text = (text or "").strip().lstrip("﻿")
    if not text:
        return None
    return json.loads(text)


def _rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        return [data]
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _n(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ---- one rule per check (pure: parsed JSON -> Check) ---------------------------------------------------------------------------------

def check_defender(d: Any) -> Check:
    if not isinstance(d, dict):
        return Check("defender", UNKNOWN, "Microsoft Defender did not answer (another antivirus may be in charge, or the service is off).",
                     "Open Windows Security > Virus & threat protection to see which antivirus is active.")
    if d.get("AntivirusEnabled") is False or d.get("AMServiceEnabled") is False:
        return Check("defender", ATTENTION, "Microsoft Defender antivirus is not the active protection.", "If no other antivirus is installed, switch Defender on in Windows Security.")
    if d.get("RealTimeProtectionEnabled") is False:
        return Check("defender", PROBLEM, "Real-time protection is OFF.", "Switch it on: Windows Security > Virus & threat protection > Manage settings.")
    age, scan = _n(d.get("AntivirusSignatureAge")), _n(d.get("QuickScanAge"))
    if age is not None and age > 7:                               # definitions older than a week
        return Check("defender", ATTENTION, f"Protection is on, but virus definitions are {age:.0f} days old.", "Run Windows Update (definitions normally update daily).")
    if scan is not None and scan > 30 and scan < 4_000_000:       # 4294967295 = never scanned
        return Check("defender", ATTENTION, f"Protection is on; the last quick scan was {scan:.0f} days ago.", "Run a quick scan from Windows Security.")
    return Check("defender", OK, "Real-time protection is on" + (f", definitions {age:.0f} day(s) old." if age is not None else "."))


def check_firewall(d: Any) -> Check:
    rows = _rows(d)
    if not rows:
        return Check("firewall", UNKNOWN, "The firewall state could not be read.")
    off = [str(r.get("Name")) for r in rows if r.get("Enabled") in (False, 0)]
    if off:
        return Check("firewall", PROBLEM, "Windows Firewall is OFF for: " + ", ".join(off) + ".", "Switch it on in Windows Security > Firewall & network protection (unless another firewall replaces it).")
    return Check("firewall", OK, f"On for all {len(rows)} network profiles.")


def check_disks(d: Any) -> Check:
    rows = _rows(d)
    if not rows:
        return Check("disks", UNKNOWN, "The disks did not report their health.")
    bad, warn, notes = [], [], []
    for r in rows:
        name = str(r.get("Name") or "disk")[:40]
        health, wear, temp, errors = str(r.get("Health") or ""), _n(r.get("Wear")), _n(r.get("TempC")), _n(r.get("ReadErrors"))
        if health and health.lower() != "healthy":
            bad.append(f"{name} reports '{health}'")
        elif errors:
            bad.append(f"{name} has {errors:.0f} uncorrected read errors")
        elif wear is not None and wear >= 80:                     # SSD life used, by the drive's own counter
            warn.append(f"{name} has used {wear:.0f}% of its rated life")
        elif temp is not None and temp >= 65:                     # sustained 65 C+ shortens SSD life
            warn.append(f"{name} is at {temp:.0f}°C")
        else:
            notes.append(f"{name} healthy" + (f" ({wear:.0f}% life used)" if wear is not None else ""))
    if bad:
        return Check("disks", PROBLEM, "; ".join(bad) + ".", "Back up your files now, then check the drive with the maker's tool.")
    if warn:
        return Check("disks", ATTENTION, "; ".join(warn) + ".", "Keep a current backup; plan a replacement if the wear is high.")
    detail = "" if any("life used" in n for n in notes) else " Wear and temperature need administrator rights to read."
    return Check("disks", OK, "; ".join(notes) + "." + detail)


def check_battery(d: Any) -> Check:
    design, full, cycles = (_n(d.get(k)) for k in ("Design", "Full", "Cycles")) if isinstance(d, dict) else (None, None, None)
    if not design or not full:
        return Check("battery", UNKNOWN, "No battery capacity data (a desktop PC, or the battery does not report it).")
    health = min(100.0, 100.0 * full / design)
    text = f"Holds {health:.0f}% of its original capacity" + (f" after {cycles:.0f} charge cycles." if cycles else ".")
    if health < 60:
        return Check("battery", PROBLEM, text, "The battery is worn: expect short run time; consider replacing it.")
    if health < 80:                                               # below 80% is the usual "consumed" mark
        return Check("battery", ATTENTION, text, "Noticeably worn. Keeping the charge between about 20% and 80% slows further wear.")
    return Check("battery", OK, text)


def check_restart(d: Any) -> Check:
    if not isinstance(d, dict):
        return Check("restart", UNKNOWN, "Could not tell whether a restart is pending.")
    if d.get("CBS") or d.get("WU"):
        return Check("restart", ATTENTION, "Windows is waiting for a restart to finish installing updates.", "Restart when convenient; updates are not fully applied until then.")
    return Check("restart", OK, "No restart is pending.")


def check_updates(d: Any) -> Check:
    days = _n(d.get("Days")) if isinstance(d, dict) else None
    if days is None:
        return Check("updates", UNKNOWN, "The date of the last installed update could not be read.")
    if days > 60:
        return Check("updates", PROBLEM, f"No Windows update has been installed for {days:.0f} days (last: {d.get('Last')}).", "Open Settings > Windows Update and install what is waiting.")
    if days > 35:                                                 # Microsoft ships security updates monthly
        return Check("updates", ATTENTION, f"The last update was installed {days:.0f} days ago ({d.get('Last')}).", "Check Windows Update: a monthly security update is probably waiting.")
    return Check("updates", OK, f"Last update installed {days:.0f} day(s) ago.")


def check_startup(d: Any) -> Check:
    rows = _rows(d)
    if d is None:
        return Check("startup", UNKNOWN, "The start-up list could not be read.")
    names = sorted({str(r.get("Name") or "?")[:30] for r in rows})
    if len(names) > 15:
        return Check("startup", ATTENTION, f"{len(names)} programs start with Windows (e.g. {', '.join(names[:5])}).", "Many start-up programs slow sign-in: switch off the ones you do not need in Task Manager > Startup apps.")
    return Check("startup", OK, f"{len(names)} program(s) start with Windows.")


def check_volumes(d: Any) -> Check:
    rows = _rows(d)
    if not rows:
        return Check("volumes", UNKNOWN, "The drives did not report their free space.")
    low, tight, parts = [], [], []
    for r in rows:
        size, free = _n(r.get("SizeGB")), _n(r.get("FreeGB"))
        if not size or free is None:
            continue
        share = 100.0 * free / size
        parts.append(f"{r.get('DriveLetter')}: {free:.0f} GB free ({share:.0f}%)")
        if share < 5 or free < 5:
            low.append(str(r.get("DriveLetter")))
        elif share < 15:                                          # Windows and SSDs slow down below roughly 10-15% free
            tight.append(str(r.get("DriveLetter")))
    if low:
        return Check("volumes", PROBLEM, "; ".join(parts) + ".", f"Drive {', '.join(low)} is almost full: free space now (Storage settings, or ask me to find large and duplicate files).")
    if tight:
        return Check("volumes", ATTENTION, "; ".join(parts) + ".", f"Drive {', '.join(tight)} is getting full.")
    return Check("volumes", OK, "; ".join(parts) + "." if parts else "Free space looks fine.")


RULES: dict[str, Callable[[Any], Check]] = {"defender": check_defender, "firewall": check_firewall, "disks": check_disks, "battery": check_battery,
                                            "restart": check_restart, "updates": check_updates, "startup": check_startup, "volumes": check_volumes}


def run(runner: Runner = run_powershell, only: tuple[str, ...] = ()) -> list[Check]:
    """Every check. A script that fails or prints something unreadable gives UNKNOWN with the reason - it never stops the others."""
    out = []
    for key, rule in RULES.items():
        if only and key not in only:
            continue
        try:
            out.append(rule(_load(runner(SCRIPTS[key]))))
        except (RuntimeError, ValueError) as exc:
            out.append(Check(key, UNKNOWN, f"Could not be read ({str(exc)[:120]})."))
    return out


def describe(checks: list[Check]) -> str:
    order = {PROBLEM: 0, ATTENTION: 1, UNKNOWN: 2, OK: 3}
    mark = {PROBLEM: "PROBLEM", ATTENTION: "attention", UNKNOWN: "unknown", OK: "ok"}
    counts = {s: sum(c.status == s for c in checks) for s in order}
    head = ("Your PC needs attention: " if counts[PROBLEM] else "Mostly fine: " if counts[ATTENTION] else "Your PC looks healthy: ") + \
        ", ".join(f"{n} {mark[s]}" for s, n in counts.items() if n) + "."
    lines = [head]
    for c in sorted(checks, key=lambda c: order[c.status]):
        lines.append(f"  [{mark[c.status]}] {c.title}: {c.summary}" + (f" -> {c.advice}" if c.advice else ""))
    lines.append("Read-only: nothing was changed. 'unknown' means Windows did not give the reading (often it needs administrator rights), not that it is fine.")
    return "\n".join(lines)
