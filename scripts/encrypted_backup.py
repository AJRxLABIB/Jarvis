"""Encrypted database backup to a USB stick / drive, and opening one again. See app/memory/encrypted_backup.py."""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.memory import encrypted_backup  # noqa: E402
from app.memory.database import StorageError  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("save", "open"):
        print(__doc__)
        return 2
    try:
        if argv[0] == "save":
            from app.utils.paths import AppPaths

            paths = AppPaths.build(ROOT)
            password = getpass.getpass("Password for the backup: ")
            if getpass.getpass("Again: ") != password:
                print("The passwords differ.")
                return 1
            print("Saved:", encrypted_backup.save(paths.db_path, Path(argv[1]), password))
        else:
            if len(argv) < 3:
                print("Usage: open <file.jbk> <output.db>")
                return 2
            print("Opened to:", encrypted_backup.open_to(Path(argv[1]), Path(argv[2]), getpass.getpass("Password: ")))
    except StorageError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
