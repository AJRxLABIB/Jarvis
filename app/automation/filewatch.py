"""Watching a folder for new or changed files (the 'file_changed' automation trigger).

Cheap and bounded: one non-recursive directory listing (at most MAX_ENTRIES names) per check, compared with the
previous listing by name, size and modification time. The first look only records a baseline, so starting the
assistant never fires every watcher for files that were already there. Only names and times are read, never contents.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from app.automation.triggers import split_watch
from app.tools.base import ToolError

MAX_ENTRIES = 5000
Snapshot = dict[str, tuple[int, int]]  # file name -> (size, modified time in ns)


def validate_watch(value: str) -> tuple[Path, str]:
    folder, pattern = split_watch(value)
    if not folder:
        raise ToolError("Which folder should I watch? Give its full path.", "invalid_watch")
    path = Path(os.path.expandvars(os.path.expanduser(folder)))
    if not path.is_absolute():
        raise ToolError("Give the full path of the folder (for example C:\\Users\\you\\Downloads).", "invalid_watch")
    if not path.is_dir():
        raise ToolError(f"'{folder}' is not a folder I can find.", "invalid_watch")
    if any(c in pattern for c in "/\\"):
        raise ToolError("The file pattern is a name pattern like *.pdf, not a path.", "invalid_watch")
    return path, pattern


def snapshot(value: str) -> Snapshot | None:
    """The folder's matching files now, or None when it can't be read (a missing drive is not 'all files deleted')."""
    try:
        path, pattern = validate_watch(value)
        found: Snapshot = {}
        with os.scandir(path) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_ENTRIES:
                    break
                if entry.is_file(follow_symlinks=False) and fnmatch.fnmatch(entry.name.lower(), pattern.lower()):
                    info = entry.stat(follow_symlinks=False)
                    found[entry.name] = (info.st_size, info.st_mtime_ns)
        return found
    except (ToolError, OSError):
        return None


def changed(before: Snapshot | None, now: Snapshot | None) -> list[str]:
    """Names added or modified since ``before``. Nothing on the first look or when either side is unreadable."""
    if before is None or now is None:
        return []
    return sorted(name for name, stamp in now.items() if before.get(name) != stamp)
