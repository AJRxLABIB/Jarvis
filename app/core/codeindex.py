"""Repository understanding: symbols, imports and "what would this change affect?".

Deterministic and read-only. Python is parsed with the ``ast`` module (exact definitions, imports); JavaScript /
TypeScript / PHP / Java / Go / C# use conservative regular expressions (definitions and imports are found, but
dynamic code isn't). References are whole-word text matches, so they are leads to check, not proof - the result
says so. File contents are data, never instructions.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from app.core.projects import code_files

MAX_FILE_BYTES = 1_000_000
MAX_RESULTS = 40

_DEFS = {
    ".js": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?P<kind>function|class|const|let)\s+(?P<name>[A-Za-z_$][\w$]*)", re.M),
    ".php": re.compile(r"^\s*(?:abstract\s+|final\s+)?(?P<kind>function|class|interface|trait)\s+(?P<name>[A-Za-z_]\w*)", re.M),
    ".java": re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|final\s+|abstract\s+)*(?P<kind>class|interface|enum)\s+(?P<name>[A-Za-z_]\w*)", re.M),
    ".go": re.compile(r"^\s*(?P<kind>func|type)\s+(?:\([^)]*\)\s*)?(?P<name>[A-Za-z_]\w*)", re.M),
    ".cs": re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|static\s+|abstract\s+|sealed\s+)*(?P<kind>class|interface|struct|enum)\s+(?P<name>[A-Za-z_]\w*)", re.M),
}
_DEFS[".jsx"] = _DEFS[".ts"] = _DEFS[".tsx"] = _DEFS[".vue"] = _DEFS[".js"]
_JS_IMPORT = re.compile(r"""(?:import\s+(?:[^'"]*?\s+from\s+)?|require\(\s*)['"](?P<mod>[^'"]+)['"]""")


@dataclass(frozen=True)
class Symbol:
    file: str  # relative to the project root, with forward slashes
    kind: str
    name: str
    line: int


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def file_symbols(path: Path, root: Path, text: str | None = None) -> list[Symbol]:
    text = _read(path) if text is None else text
    if text is None:
        return []
    rel = _rel(path, root)
    suffix = path.suffix.lower()
    if suffix == ".py":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return []
        found: list[Symbol] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                found.append(Symbol(rel, "class", node.name, node.lineno))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(Symbol(rel, "function", node.name, node.lineno))
        return sorted(found, key=lambda s: s.line)
    pattern = _DEFS.get(suffix)
    if pattern is None:
        return []
    return [Symbol(rel, m.group("kind"), m.group("name"), text.count("\n", 0, m.start()) + 1)
            for m in pattern.finditer(text)]


def project_symbols(root: Path, limit_files: int = 3000) -> list[Symbol]:
    out: list[Symbol] = []
    for path in code_files(root, limit_files):
        out.extend(file_symbols(path, root))
    return out


def python_modules(root: Path) -> dict[str, str]:
    """{dotted module name: relative file} for the project's own Python files."""
    modules: dict[str, str] = {}
    for path in code_files(root):
        if path.suffix.lower() != ".py":
            continue
        rel = _rel(path, root)
        dotted = rel[:-3].replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        modules[dotted] = rel
    return modules


def imports_of(path: Path, root: Path, modules: dict[str, str] | None = None, text: str | None = None) -> set[str]:
    """The project files this file imports (relative paths). Third-party and standard-library imports are left out."""
    text = _read(path) if text is None else text
    if text is None:
        return set()
    suffix = path.suffix.lower()
    found: set[str] = set()
    if suffix == ".py":
        modules = modules if modules is not None else python_modules(root)
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return set()
        here = _rel(path, root)[:-3].replace("/", ".")
        package = here.rsplit(".", 1)[0] if "." in here else ""
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:  # a relative import
                    parts = package.split(".") if package else []
                    parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                    base = ".".join([*parts, base]) if base else ".".join(parts)
                names = [base] + [f"{base}.{a.name}" for a in node.names]
            for name in names:
                if name in modules:
                    found.add(modules[name])
        found.discard(_rel(path, root))
        return found
    if suffix in (".js", ".jsx", ".ts", ".tsx", ".vue"):
        for m in _JS_IMPORT.finditer(text):
            module = m.group("mod")
            if not module.startswith("."):
                continue  # a package, not a project file
            target = (path.parent / module).resolve()
            for candidate in (target, *(target.with_suffix(s) for s in (".js", ".jsx", ".ts", ".tsx", ".vue")),
                              *(target / f"index{s}" for s in (".js", ".ts"))):
                try:
                    if candidate.is_file() and candidate.is_relative_to(root.resolve()):
                        found.add(candidate.relative_to(root.resolve()).as_posix())
                        break
                except (OSError, ValueError):
                    continue
    return found


def import_graph(root: Path) -> dict[str, set[str]]:
    """{file: files it imports}."""
    modules = python_modules(root)
    graph: dict[str, set[str]] = {}
    for path in code_files(root):
        deps = imports_of(path, root, modules)
        if deps:
            graph[_rel(path, root)] = deps
    return graph


def dependents(graph: dict[str, set[str]], target: str, depth: int = 3) -> dict[str, int]:
    """Files that import ``target`` directly (1) or through others (up to ``depth``): {file: distance}."""
    reverse: dict[str, set[str]] = {}
    for file, deps in graph.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(file)
    seen: dict[str, int] = {}
    frontier = {target}
    for distance in range(1, depth + 1):
        nxt: set[str] = set()
        for item in frontier:
            for parent in reverse.get(item, ()):
                if parent != target and parent not in seen:
                    seen[parent] = distance
                    nxt.add(parent)
        frontier = nxt
        if not frontier:
            break
    return seen


@dataclass
class Impact:
    target: str
    kind: str  # "symbol" | "file" | "unknown"
    definitions: list[Symbol]
    references: list[str]  # "file:line: text" (whole-word matches outside the definitions)
    dependents: dict[str, int]  # files that import the file (or the file that defines the symbol)
    tests: list[str]  # test files that mention it or import a dependent
    truncated: bool = False


def impact(root: Path, target: str) -> Impact:
    """What would changing ``target`` (a file path inside the project, or a function / class name) touch?"""
    target = target.strip().replace("\\", "/")
    files = list(code_files(root))
    rels = {_rel(p, root): p for p in files}
    graph = import_graph(root)

    if target in rels:
        return Impact(target, "file", file_symbols(rels[target], root), [], dependents(graph, target),
                      _tests_for(rels, graph, target, target.rsplit("/", 1)[-1].split(".")[0]))

    word = re.compile(rf"(?<![\w$]){re.escape(target)}(?![\w$])")
    definitions: list[Symbol] = []
    references: list[str] = []
    mentioning: set[str] = set()
    truncated = False
    for rel, path in rels.items():
        text = _read(path)
        if text is None or target not in text:
            continue
        defs = [s for s in file_symbols(path, root, text) if s.name == target]
        definitions.extend(defs)
        def_lines = {s.line for s in defs}
        for number, line in enumerate(text.splitlines(), 1):
            if number not in def_lines and word.search(line):
                mentioning.add(rel)
                if len(references) < MAX_RESULTS:
                    references.append(f"{rel}:{number}: {line.strip()[:140]}")
                else:
                    truncated = True
    if not definitions and not references:
        return Impact(target, "unknown", [], [], {}, [])
    affected: dict[str, int] = {}
    for d in definitions:
        for file, distance in dependents(graph, d.file).items():
            affected[file] = min(distance, affected.get(file, distance))
    tests = sorted({r for r in mentioning if _is_test(r)} | {f for f in affected if _is_test(f)})
    kind = "symbol" if definitions else "unknown"
    if kind == "unknown":  # mentioned but never defined here: a library call, a string, a generated name
        return Impact(target, "symbol", [], references, {}, tests[:MAX_RESULTS], truncated)
    return Impact(target, kind, definitions, references, affected, tests[:MAX_RESULTS], truncated)


def _is_test(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    return ("/tests/" in f"/{rel.lower()}" or "/test/" in f"/{rel.lower()}" or name.startswith("test_")
            or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts", "test.php", "test.java")))


def _tests_for(rels: dict[str, Path], graph: dict[str, set[str]], target: str, stem: str) -> list[str]:
    affected = dependents(graph, target)
    return sorted({f for f in affected if _is_test(f)} |
                  {r for r in rels if _is_test(r) and stem and stem.lower() in r.lower()})[:MAX_RESULTS]


def describe(found: Impact) -> str:
    if found.kind == "unknown":
        return (f"I can't find '{found.target}' in this project as a file, function or class (it may be named "
                "differently or be generated).")
    parts = []
    if found.kind == "file":
        parts.append(f"{found.target} defines {len(found.definitions)} function(s)/class(es)")
    else:
        where = ", ".join(f"{d.file}:{d.line} ({d.kind})" for d in found.definitions[:4]) or "no definition found"
        parts.append(f"'{found.target}' is defined at {where}")
    if found.references:
        parts.append(f"{len(found.references)}{'+' if found.truncated else ''} reference(s) by name")
    if found.dependents:
        direct = sum(1 for d in found.dependents.values() if d == 1)
        parts.append(f"{len(found.dependents)} file(s) depend on it ({direct} directly)")
    else:
        parts.append("no other project file imports it")
    if found.tests:
        parts.append("related tests: " + ", ".join(found.tests[:5]))
    else:
        parts.append("no related test found - add one before changing it")
    return "; ".join(parts) + ". References are text matches: check them, they aren't proof."
