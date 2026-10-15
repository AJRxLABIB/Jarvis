"""Launcher: ``python run.py`` (use ``python run.py --help`` for options)."""

import sys

if sys.version_info < (3, 11):  # checked before importing the app package
    sys.stderr.write(
        "Jarvis Assistant needs Python 3.11 or newer (3.12 recommended). "
        f"You are running {sys.version.split()[0]}.\n"
    )
    raise SystemExit(1)

from app.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
