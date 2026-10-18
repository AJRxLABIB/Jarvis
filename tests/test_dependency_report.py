from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dependency_report", ROOT / "packaging" / "dependency_report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

ITEMS = [{"name": "Alpha_Pkg", "version": "1.2.3", "license": "MIT"}, {"name": "beta", "version": "0.1", "license": ""}]


def test_cyclonedx_shape_and_purl() -> None:
    bom = report.cyclonedx(ITEMS, "9.9.9")
    assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.5"
    assert bom["metadata"]["component"]["version"] == "9.9.9"
    first, second = bom["components"]
    assert first["purl"] == "pkg:pypi/alpha-pkg@1.2.3" and first["licenses"][0]["license"]["name"] == "MIT"
    assert "licenses" not in second                      # an undeclared licence is left out, never invented


def test_licence_table_lists_the_undeclared() -> None:
    text = report.licence_table(ITEMS)
    assert "| Alpha_Pkg | 1.2.3 | MIT |" in text and "NOT DECLARED" in text
    assert "without a declared licence: beta" in text


def test_real_inventory_runs_and_includes_pydantic() -> None:
    names = {i["name"].lower() for i in report.inventory()}
    assert "pydantic" in names and all(i["version"] for i in report.inventory())


def test_main_writes_the_three_files_and_flags_missing_licences(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(report, "inventory", lambda: ITEMS)
    assert report.main(["x", str(tmp_path)]) == 1        # beta declares no licence
    assert json.loads((tmp_path / "sbom.cdx.json").read_text())["components"]
    assert (tmp_path / "requirements.lock").read_text() == "Alpha_Pkg==1.2.3\nbeta==0.1\n"
    assert (tmp_path / "licenses.md").exists()
    monkeypatch.setattr(report, "inventory", lambda: ITEMS[:1])
    assert report.main(["x", str(tmp_path)]) == 0
