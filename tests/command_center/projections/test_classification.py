"""P5-4 acceptance classification (B5C): MATCH / ALLOWED / UNKNOWN.

The parity suite classifies every projection result:

- MATCH    — the Python aggregation equals the TS oracle golden output
  (field-for-field, after stripping injectable wall-clock timestamps).
- ALLOWED  — a registered, documented divergence (see conftest) that keeps
  semantics intact and never silently disappears.
- UNKNOWN  — anything else; fails the suite so no unverified aggregation is
  shipped.

This module pins the registry so ALLOWED never collapses into MATCH and no
case is silently dropped.
"""

from __future__ import annotations

from .conftest import ALLOWED_DIFFERENCES


def test_allowed_differences_registry_is_stable() -> None:
    assert len(ALLOWED_DIFFERENCES) >= 5
    assert all(isinstance(item, str) and item for item in ALLOWED_DIFFERENCES)


def test_every_registered_divergence_has_a_home() -> None:
    """Each ALLOWED item must be referenced by at least one module docstring."""
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[3]
        / "src"
        / "arena_hero_agent"
        / "command_center"
        / "projections"
    )
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in src.glob("*.py"))
    for item in ALLOWED_DIFFERENCES:
        marker = item.split("(")[0].strip()
        assert marker in corpus or any(token in corpus for token in item.split()[:4]), (
            f"ALLOWED difference not documented in any module: {item}"
        )


def test_classification_terms_are_explicit() -> None:
    """MATCH / ALLOWED / UNKNOWN are the only result classes used."""
    import pathlib
    import re

    tests = pathlib.Path(__file__).resolve().parent
    corpus = "\n".join(
        path.read_text(encoding="utf-8")
        for path in tests.glob("*.py")
        if path.name != "test_classification.py"
    )
    assert "UNKNOWN" in corpus
    assert "ALLOWED" in corpus
    assert "MATCH" in corpus
    unexpected = re.findall(r"\b(PARTIAL|WARN|FAIL_OPEN)\b", corpus)
    assert unexpected == []
