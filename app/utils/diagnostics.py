"""One-click diagnostics bundle for bug reports (Menu -> Export diagnostics).

The zip holds the version, the --doctor checks, the settings and the recent logs. Values of
settings whose names look secret (keys, tokens, passwords) are removed, and every text is run
through the same redaction as the logs (card numbers, keys, tokens). Conversations,
memories, documents, voice prints and the database are never included.
"""

from __future__ import annotations

import io
import platform
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app import __version__
from app.config.migrate import read_env
from app.utils.redaction import is_sensitive_key, redact

if TYPE_CHECKING:
    from app.main import AppContext

MAX_LOG_BYTES = 2_000_000


def safe_settings(env_text: str) -> str:
    lines = []
    for key, value in read_env(env_text).items():
        lines.append(f"{key}=" + ("<removed>" if is_sensitive_key(key) and value else redact(value)))
    return "\n".join(lines) + "\n"


def export_diagnostics(ctx: "AppContext", destination: str | Path) -> Path:
    from app.main import collect_checks, print_checks

    destination = Path(destination)
    if destination.suffix.lower() != ".zip":
        destination = destination.with_suffix(".zip")
    report = io.StringIO()
    try:
        print_checks(collect_checks(ctx), report)
    except Exception as exc:  # the bundle is still useful without it
        report.write(f"(checks failed: {exc})\n")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("version.txt", f"Jarvis Assistant {__version__}\nPython {platform.python_version()}\n"
                                        f"{platform.platform()}\nfrozen={getattr(sys, 'frozen', False)}\n"
                                        f"created {datetime.now().isoformat(timespec='seconds')}\n")
        archive.writestr("doctor.txt", redact(report.getvalue()))
        env_file = ctx.paths.env_file
        if env_file.is_file():
            archive.writestr("settings.env", safe_settings(env_file.read_text(encoding="utf-8", errors="ignore")))
        logs = sorted(Path(ctx.log_file).parent.glob(Path(ctx.log_file).name + "*"))[:4]
        for log in logs:
            data = log.read_bytes()[-MAX_LOG_BYTES:].decode("utf-8", "ignore")
            archive.writestr(f"logs/{log.name}", redact(data))
    return destination
