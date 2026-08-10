from __future__ import annotations

import ast
from pathlib import Path

PORTS_ROOT = Path(__file__).resolve().parents[2] / "src" / "arena_hero_agent" / "ports"
CONCRETE_ROOTS = {"aiohttp", "fastapi", "httpx", "pydantic", "sqlalchemy"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_ports_do_not_import_concrete_adapters_or_frameworks() -> None:
    violations: list[str] = []
    for path in PORTS_ROOT.glob("*.py"):
        for imported in _imports(path):
            root = imported.split(".", maxsplit=1)[0]
            if root in CONCRETE_ROOTS or imported.startswith("arena_hero_agent.adapters"):
                violations.append(f"{path.name}: {imported}")

    assert violations == []


def test_sdk_imports_are_type_checking_only() -> None:
    violations: list[str] = []
    for path in PORTS_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "arena_hero":
                continue
            parent = parents.get(node)
            if not (
                isinstance(parent, ast.If)
                and isinstance(parent.test, ast.Name)
                and parent.test.id == "TYPE_CHECKING"
            ):
                violations.append(f"{path.name}:{node.lineno}")

    assert violations == []
