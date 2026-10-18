"""Dependency inventory for a release: a CycloneDX 1.5 SBOM, a licence table and a pinned list of what was installed.

    python packaging/dependency_report.py [OUT_DIR]        (default: dist/)

Uses only the standard library and reads the packages installed in the CURRENT environment, so run it in the same
environment the release is built from (CI does). It writes:

* ``sbom.cdx.json``     CycloneDX 1.5 components (name, version, purl, declared licence)
* ``licenses.md``       one line per package; packages whose licence is not declared are listed separately
* ``requirements.lock`` ``name==version`` for everything installed (what this exact build used)

Policy (exit code): 0 when every package declares a licence; 1 when a package declares none (the list is printed). A
missing licence is a documentation problem to resolve, not proof of a legal problem. This script does NOT scan for
vulnerabilities - ``pip-audit`` does that in CI, and the SBOM here lets another scanner do it too.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path


def licence_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression
    raw = (meta.get("License") or "").strip()
    if raw and len(raw) < 80 and "\n" not in raw and raw.upper() != "UNKNOWN":
        return raw
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            return classifier.split("::")[-1].strip()
    return ""


def inventory() -> list[dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if not name:
            continue
        seen[name.lower().replace("_", "-")] = {"name": name, "version": dist.version, "license": licence_of(dist)}
    return sorted(seen.values(), key=lambda d: d["name"].lower())


def purl(item: dict[str, str]) -> str:
    return f"pkg:pypi/{item['name'].lower().replace('_', '-')}@{item['version']}"


def cyclonedx(items: list[dict[str, str]], app_version: str, now: datetime | None = None) -> dict:
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "metadata": {"timestamp": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
                     "component": {"type": "application", "name": "jarvis-assistant", "version": app_version}},
        "components": [{"type": "library", "name": i["name"], "version": i["version"], "purl": purl(i),
                        **({"licenses": [{"license": {"name": i["license"]}}]} if i["license"] else {})} for i in items],
    }


def licence_table(items: list[dict[str, str]]) -> str:
    lines = ["# Third-party licences", "", "| Package | Version | Licence |", "|---|---|---|"]
    lines += [f"| {i['name']} | {i['version']} | {i['license'] or 'NOT DECLARED'} |" for i in items]
    missing = [i["name"] for i in items if not i["license"]]
    lines += ["", f"Packages without a declared licence: {', '.join(missing) if missing else 'none'}."]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else Path("dist")
    out.mkdir(parents=True, exist_ok=True)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app import __version__ as app_version
    except Exception:
        app_version = "unknown"
    items = inventory()
    (out / "sbom.cdx.json").write_text(json.dumps(cyclonedx(items, app_version), indent=2), encoding="utf-8")
    (out / "licenses.md").write_text(licence_table(items), encoding="utf-8")
    (out / "requirements.lock").write_text("".join(f"{i['name']}=={i['version']}\n" for i in items), encoding="utf-8")
    missing = [i["name"] for i in items if not i["license"]]
    print(f"{len(items)} packages inventoried into {out}.")
    if missing:
        print("No declared licence: " + ", ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
