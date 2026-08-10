from __future__ import annotations

import ast
from pathlib import Path

DOMAIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "arena_hero_agent" / "domain"
BANNED_ROOTS = {
    "aiohttp",
    "arena_hero",
    "fastapi",
    "httpx",
    "io",
    "os",
    "pathlib",
    "pydantic",
    "socket",
    "sqlite3",
    "subprocess",
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


def test_domain_has_no_io_framework_sdk_or_adapter_imports() -> None:
    violations: list[str] = []
    for path in DOMAIN_ROOT.glob("*.py"):
        for imported in _imports(path):
            root = imported.split(".", maxsplit=1)[0]
            if root in BANNED_ROOTS or imported.startswith("arena_hero_agent.adapters"):
                violations.append(f"{path.name}: {imported}")

    assert violations == []
