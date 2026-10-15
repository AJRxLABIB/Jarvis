"""Applying a tested proposal to the real source, and rolling it back.

Before anything is written the files are backed up (data/evolution/backups/<id>/, with a manifest). After
writing, every file is read back and compared (sha256): "applied" is only claimed when the disk says so. A
rollback restores the backup only while the files are still exactly what was applied - a later edit (by you or
another proposal) is never overwritten silently.

Version channels (data/evolution/channels.json): stable = the source as it is now, previous = what the last
apply replaced (its backup), candidate = the proposal being tested.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.evolution.lab import write_changes
from app.evolution.proposals import Proposal, apply_edits, sha
from app.tools.base import ToolError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_channels(folder: Path) -> dict[str, Any]:
    try:
        return json.loads((folder / "channels.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"stable": {"applied": []}, "previous": None, "candidate": None}


def save_channels(folder: Path, channels: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / "channels.json.tmp"
    temp.write_text(json.dumps(channels, indent=2), encoding="utf-8")
    temp.replace(folder / "channels.json")


def apply(proposal: Proposal, source: Path, folder: Path) -> dict[str, Any]:
    """Back up, write, verify. Returns {"backup": dir, "files": {path: sha}}."""
    changes = apply_edits(source, proposal.edits)
    backup = folder / "backups" / str(proposal.id)
    backup.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"proposal": proposal.id, "at": _now(), "files": {}}
    for path, (old, _new) in changes.items():
        if old is None:
            manifest["files"][path] = None  # a new file: rollback removes it
        else:
            saved = backup / "files" / path
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(old.encode("utf-8"))
            manifest["files"][path] = sha(old)
    written = {path: sha(new) for path, (_old, new) in changes.items()}
    manifest["applied"] = written
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    write_changes(source, changes)
    wrong = [p for p, digest in written.items() if sha((source / p).read_bytes()) != digest]
    if wrong:
        restore(backup, source, check=False)
        raise ToolError(f"Writing {', '.join(wrong)} didn't come out right, so I put the originals back.",
                        "not_verified")
    channels = load_channels(folder)
    channels["previous"] = {"proposal": proposal.id, "backup": str(backup), "at": manifest["at"]}
    channels["stable"]["applied"] = [*channels["stable"].get("applied", []), proposal.id][-50:]
    channels["candidate"] = None
    save_channels(folder, channels)
    return {"backup": str(backup), "files": written}


def restore(backup: Path, source: Path, *, check: bool = True) -> list[str]:
    """Put the backed-up originals back (and remove files the proposal created). Returns the paths restored."""
    try:
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError("The backup for this change is missing, so it can't be rolled back.", "no_backup") from exc
    applied: dict[str, str] = manifest.get("applied", {})
    if check:
        changed = [p for p, digest in applied.items()
                   if not (source / p).is_file() or sha((source / p).read_bytes()) != digest]
        if changed:
            raise ToolError(f"{', '.join(changed)} changed after this was applied, so I won't overwrite it. "
                            "Roll back the later change first, or restore it by hand.", "changed_since")
    restored = []
    for path, original in manifest.get("files", {}).items():
        target = source / path
        if original is None:
            if target.is_file():
                target.unlink()
        else:
            data = (backup / "files" / path).read_bytes()
            if sha(data) != original:
                raise ToolError(f"The backup of {path} is damaged; nothing more was restored.", "bad_backup")
            target.write_bytes(data)
        restored.append(path)
    return restored
