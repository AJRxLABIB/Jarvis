"""Nicknames you give by voice: "call Visual Studio Code 'code'", "'my university' means diu.edu.bd".

Saved in data\\aliases.json. Saying the nickname alone (or "open <nickname>") opens what it points
to: an app, a website, a Settings page or one of your workflows.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

FILE = "aliases.json"
KINDS = ("app", "site", "page", "workflow")


def _key(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s.&ঀ-৿-]", " ", (name or "").lower()).split())


class AliasStore:
    def __init__(self, data_dir: Path | None) -> None:
        self.path = Path(data_dir) / FILE if data_dir is not None else None
        self._items: dict[str, dict] | None = None

    def items(self) -> dict[str, dict]:
        if self._items is None:
            self._items = {}
            if self.path is not None and self.path.is_file():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    self._items = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("kind") in KINDS}
                except (OSError, ValueError):
                    pass
        return self._items

    def _save(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.items(), ensure_ascii=False, indent=1), encoding="utf-8")

    def set(self, name: str, kind: str, target: str) -> str:
        key = _key(name)
        if not key or kind not in KINDS or not target.strip():
            raise ValueError("a nickname needs a name, a kind and a target")
        self.items()[key] = {"kind": kind, "target": target.strip()}
        self._save()
        return key

    def get(self, name: str) -> dict | None:
        return self.items().get(_key(name))

    def remove(self, name: str) -> bool:
        removed = self.items().pop(_key(name), None) is not None
        self._save()
        return removed


def call_for(alias: dict):
    """The direct command a nickname stands for."""
    from app.ai.base import ToolCall

    kind, target = alias["kind"], alias["target"]
    if kind == "app":
        return ToolCall("open_application", {"name": target})
    if kind == "site":
        return ToolCall("open_website", {"site": target})
    if kind == "page":
        return ToolCall("open_settings_page", {"page": target})
    return ToolCall("run_workflow", {"name": target})
