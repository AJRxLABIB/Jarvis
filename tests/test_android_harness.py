"""The JVM test harness must list every source that a listed source depends on (no JDK needed to check this)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "android" / "JarvisMobile"
SRC = ROOT / "src" / "com" / "jarvis" / "mobile"


def _listed() -> set[str]:
    text = (ROOT / "test" / "run.sh").read_text(encoding="utf-8")
    return set(re.findall(r"\$S/(\w+)\.java", text))


def test_run_sh_includes_every_class_its_sources_use():
    stubbed = {p.stem for p in (ROOT / "test" / "stubs" / "com").rglob("*.java")}
    listed = _listed() | stubbed          # run.sh compiles the stand-ins in test/stubs/com as well
    classes = {p.stem for p in SRC.glob("*.java")}
    missing: dict[str, set[str]] = {}
    for name in _listed():
        body = (SRC / f"{name}.java").read_text(encoding="utf-8")
        body = re.sub(r"//.*|/\*.*?\*/", "", body, flags=re.S)
        uses = {c for c in classes if c != name and re.search(rf"\b{c}\b", body)}
        gap = uses - listed
        if gap:
            missing[name] = gap
    assert not missing, f"test/run.sh would not compile; add these sources: {missing}"


def test_compile_all_covers_the_whole_source_tree():
    text = (ROOT / "test" / "compile_all.sh").read_text(encoding="utf-8")
    assert "find src -name '*.java'" in text
