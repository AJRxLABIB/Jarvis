"""Crash reports that write themselves.

While the app runs, a "running" marker sits in the data folder, and hard crashes (native
faults) are traced to logs\\crash.log (faulthandler). A clean exit removes the marker. If the
marker is still there at the next start, the last run did not end normally: a redacted
report (the diagnostics bundle plus crash.log) is saved to data\\crash-reports, and the app
says where it is. Nothing is sent anywhere.
"""

from __future__ import annotations

import faulthandler
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from app.utils.logging_setup import get_logger, log_event

if TYPE_CHECKING:
    from app.main import AppContext

_log = get_logger("crash")
MARKER = "running.marker"
KEEP_REPORTS = 10
_fault_file: TextIO | None = None


def start(ctx: "AppContext") -> Path | None:
    """Call at start-up. Returns the path of a new crash report if the last run crashed."""
    global _fault_file
    data = ctx.paths.data_dir
    marker = data / MARKER
    report = None
    from app import __version__

    if marker.exists():
        try:
            started, _, version = marker.read_text(encoding="utf-8", errors="ignore").strip().partition(" ")
            if version != __version__ and not _fault_logged(Path(ctx.log_file).parent / "crash.log"):
                # the installer closed the old version to put the new one in place: not a crash
                log_event(_log, "previous_run_closed_by_update", version=version or "unknown")
            else:
                report = _write_report(ctx, started)
        except Exception:
            _log.warning("could not write the crash report", exc_info=True)
    data.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{datetime.now().isoformat(timespec='seconds')} {__version__}", encoding="utf-8")
    try:
        crash_log = Path(ctx.log_file).parent / "crash.log"
        _fault_file = crash_log.open("a", encoding="utf-8")
        _fault_file.write(f"\n--- started {datetime.now().isoformat(timespec='seconds')}\n")
        _fault_file.flush()
        faulthandler.enable(_fault_file, all_threads=True)
    except OSError:
        _log.warning("crash tracing unavailable", exc_info=True)
    return report


FAULT_SIGNS = ("Fatal Python error", "Windows fatal exception", "Current thread", "Thread 0x")


def _fault_logged(crash_log: Path) -> bool:
    """Did the last run leave a native crash trace (faulthandler output after its "--- started" line)?"""
    try:
        text = crash_log.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    last = text.rsplit("--- started", 1)[-1]
    return any(sign in last for sign in FAULT_SIGNS)


def stop(ctx: "AppContext") -> None:
    """Call on a clean exit."""
    global _fault_file
    try:
        (ctx.paths.data_dir / MARKER).unlink(missing_ok=True)
    except OSError:
        pass
    if _fault_file is not None:
        try:
            faulthandler.disable()
            _fault_file.close()
        except Exception:
            pass
        _fault_file = None


def _write_report(ctx: "AppContext", started: str) -> Path:
    import zipfile

    from app.utils.diagnostics import export_diagnostics
    from app.utils.redaction import redact

    folder = ctx.paths.data_dir / "crash-reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = export_diagnostics(ctx, folder / f"crash-{datetime.now():%Y%m%d-%H%M%S}.zip")
    crash_log = Path(ctx.log_file).parent / "crash.log"
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("previous_run.txt", f"The previous run started {started} and did not exit normally.\n")
        if crash_log.is_file():
            archive.writestr("crash.log", redact(crash_log.read_text(encoding="utf-8", errors="ignore")[-200_000:]))
    for old in sorted(folder.glob("crash-*.zip"))[:-KEEP_REPORTS]:
        old.unlink(missing_ok=True)
    log_event(_log, "crash_report_written", path=str(path))
    return path
