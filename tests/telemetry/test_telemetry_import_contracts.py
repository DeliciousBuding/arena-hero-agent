"""Import-boundary tests: telemetry must stay free of adapters, frameworks, and
SDK concrete clients (mirrors the domain/ports import contracts)."""

from __future__ import annotations

import ast
from pathlib import Path

TELEMETRY_ROOT = Path(__file__).resolve().parents[2] / "src" / "arena_hero_agent" / "telemetry"
BANNED_ROOTS = {
    "aiohttp",
    "arena_hero",
    "fastapi",
    "httpx",
    "pydantic",
    "requests",
    "socket",
    "sqlalchemy",
    "subprocess",
    "urllib",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_telemetry_has_no_adapter_framework_or_sdk_imports() -> None:
    violations: list[str] = []
    for path in TELEMETRY_ROOT.glob("*.py"):
        for imported in _imports(path):
            root = imported.split(".", maxsplit=1)[0]
            if root in BANNED_ROOTS or imported.startswith("arena_hero_agent.adapters"):
                violations.append(f"{path.name}: {imported}")
    assert violations == []


def test_telemetry_imports_only_siblings_and_stdlib() -> None:
    allowed_roots = {
        "arena_hero_agent.telemetry",
        "__future__",
        "collections",
        "dataclasses",
        "json",
        "math",
        "os",
        "pathlib",
        "re",
        "stat",
        "threading",
        "typing",
    }
    violations: list[str] = []
    for path in TELEMETRY_ROOT.glob("*.py"):
        if path.name == "__init__.py":
            continue
        for imported in _imports(path):
            root = imported.split(".", maxsplit=1)[0]
            if root not in allowed_roots and not imported.startswith("arena_hero_agent.telemetry"):
                violations.append(f"{path.name}: {imported}")
    assert violations == []
