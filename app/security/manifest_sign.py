"""Signatures for plugin manifests: a manifest is only believed if it was written by THIS installation.

Before this, a manifest pinned the plugin file's sha256 - but anything able to edit the plugin could rewrite the manifest too. Now the manifest also
carries an HMAC-SHA256 over its meaningful fields (name, version, file hash, tools with their risk levels, declared capabilities), made with a random
key that lives beside the plugins folder (``plugin_signing.key``, protected with Windows DPAPI where available, so another user or another PC cannot
produce it). Editing the plugin, the tool list, a risk level or the hash without that key makes the signature wrong and the plugin is not loaded.

Limits, stated plainly: this proves "unchanged since you trusted it on this PC", not who wrote the plugin; and code already running as you with access
to your profile can read the key. It is tamper-evidence against stray edits, synced folders and other accounts - not a defence against malware running as you.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

KEY_FILE = "plugin_signing.key"
FIELDS = ("name", "version", "sha256", "tools", "capabilities")


def key_path(folder: Path) -> Path:
    return Path(folder).parent / KEY_FILE


def has_key(folder: Path) -> bool:
    return key_path(folder).is_file()


def _load_key(folder: Path, create: bool) -> bytes | None:
    from app.security import protect

    path = key_path(folder)
    if path.is_file():
        try:
            return bytes.fromhex(protect.reveal_text(path.read_text(encoding="utf-8").strip()))
        except (OSError, ValueError):
            return None                                            # unreadable key (other user / other PC / damaged): nothing verifies
    if not create:
        return None
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(protect.protect_text(key.hex()), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _payload(manifest: dict[str, Any]) -> bytes:
    return json.dumps({k: manifest.get(k) for k in FIELDS}, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sign(folder: Path, manifest: dict[str, Any]) -> str:
    key = _load_key(folder, create=True)
    if key is None:
        raise PermissionError("the plugin signing key could not be read, so nothing can be trusted on this PC until it is restored or deleted")
    return hmac.new(key, _payload(manifest), hashlib.sha256).hexdigest()


def verify(folder: Path, manifest: dict[str, Any]) -> tuple[bool, str]:
    """(valid, why not)."""
    sig = manifest.get("sig")
    if not isinstance(sig, str) or not sig:
        return False, "the manifest is not signed; trust the plugin again"
    key = _load_key(folder, create=False)
    if key is None:
        return False, "the signing key of this installation is missing or unreadable; trust the plugin again"
    if not hmac.compare_digest(sig, hmac.new(key, _payload(manifest), hashlib.sha256).hexdigest()):
        return False, "the manifest was changed after you trusted the plugin (its signature does not match)"
    return True, ""
