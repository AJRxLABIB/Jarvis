"""The first-run setup is done ONCE: remembered in the data folder as well as in .env.

Before, the only record was SETUP_DONE=true in .env. When .env was replaced (a fresh copy of .env.example, an
older copy, a re-extracted project folder), Jarvis forgot it had been set up and showed the wizard again at every
start. Now finishing - or cancelling - the wizard also writes data/setup_state.json with the (non-secret) answers.
At start-up, if .env says "not set up" but that record exists, SETUP_DONE and the remembered answers
are written back, and the wizard is not shown. No secret (token, password, key) is ever stored in the record.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

FILE_NAME = "setup_state.json"
# what the wizard writes (never a secret)
KEEP = ("ASSISTANT_NAME", "USER_ADDRESS", "USER_NAME", "HOME_LOCATION", "VOICE_LANGUAGE", "TTS_ENGINE", "HANDS_FREE",
        "CONVERSATION_MEMORY", "SPEAKER_VERIFICATION", "PRESENCE_DETECTION", "START_WITH_WINDOWS", "MINI_ORB",
        "DOCUMENT_FOLDERS", "AI_MODEL", "VISION_MODEL")


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE_NAME


def remember(data_dir: Path, answers: Mapping[str, str] | None = None, *, finished: bool = True) -> Path:
    """Record that setup happened (``finished`` False = the wizard was closed without finishing)."""
    path = _path(data_dir)
    old = read(data_dir)
    values = dict(old.get("answers", {}))
    values.update({k: str(v) for k, v in (answers or {}).items() if k in KEEP})
    record = {"done": True, "finished": finished or bool(old.get("finished")), "answers": values,
              "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def read(data_dir: Path) -> dict:
    try:
        data = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_done(data_dir: Path) -> bool:
    return bool(read(data_dir).get("done"))


def repairs(file_values: Mapping[str, str], data_dir: Path) -> dict[str, str]:
    """What to write back to a .env that forgot the setup (SETUP_DONE missing/false although setup was done): SETUP_DONE
    plus the remembered answers - such a file is a reset copy, so its values are defaults, not your choices.
    Empty when there is nothing to repair."""
    record = read(data_dir)
    if not record.get("done") or str(file_values.get("SETUP_DONE", "")).strip().lower() in ("true", "1", "yes"):
        return {}
    out = {"SETUP_DONE": "true"}
    for key, value in record.get("answers", {}).items():
        if key in KEEP and (value or not str(file_values.get(key, "")).strip()):
            out[key] = value
    return out
