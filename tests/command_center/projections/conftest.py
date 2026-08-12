"""Shared helpers for Command Center projection golden parity tests (P5-4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# Wall-clock fields emitted by the TS oracle with ``new Date().toISOString()``.
# The Python ports inject ``now_ms`` instead, so these fields are compared only
# for shape (both present) and are otherwise excluded from the value comparison.
TIMESTAMP_FIELDS = frozenset({"generatedAt", "cachedAt", "refreshedAt"})

# Per-module documented divergences (the only reasons a result may be ALLOWED
# instead of MATCH). Asserted stable so differences never silently disappear.
ALLOWED_DIFFERENCES: tuple[str, ...] = (
    "wall-clock generatedAt/cachedAt are injectable via now_ms (not oracle-comparable)",
    "aggregation cores take parsed rows, not raw lines; parsing delegates to the P5-3 JSONL base",
    "survey loaders derive currentTick from MAX(agents.tick); missing TS tables degrade to empty",
    "shop history refreshedAt is an explicit input (external fetch is a P5-9 route)",
    "arbitrations compare as ordered cell->entry pairs (Map iteration order)",
)


def load_fixture(name: str) -> dict[str, object]:
    path = FIXTURES / f"{name}.json"
    assert path.exists(), f"missing fixture: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def load_golden(name: str) -> object:
    path = FIXTURES / f"{name}.golden.json"
    assert path.exists(), f"missing golden: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def strip_timestamps(value: object) -> object:
    """Recursively remove wall-clock timestamp fields before comparing."""
    if isinstance(value, dict):
        return {
            key: strip_timestamps(item)
            for key, item in value.items()
            if key not in TIMESTAMP_FIELDS
        }
    if isinstance(value, list):
        return [strip_timestamps(item) for item in value]
    return value


def json_equal(actual: object, expected: object) -> bool:
    """Structural equality with int/float equivalence (mirrors P5-3 parity)."""
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and set(actual) == set(expected)
            and all(json_equal(actual[key], expected[key]) for key in expected)
        )
    if isinstance(expected, list):
        # tuples and lists both serialize to JSON arrays; projections may
        # return either and must compare equal (W44 wave 5 exploration parity)
        return (
            isinstance(actual, (list, tuple))
            and len(actual) == len(expected)
            and all(json_equal(a, b) for a, b in zip(actual, expected, strict=True))
        )
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(actual, bool) and actual == expected
    if expected is None:
        return actual is None
    if isinstance(expected, (int, float)):
        return (
            isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual == expected
        )
    if isinstance(expected, str):
        return isinstance(actual, str) and actual == expected
    return actual == expected


def assert_matches(actual: object, expected: object, label: str) -> None:
    """Assert structural equality; failure is an UNKNOWN parity result."""
    assert json_equal(strip_timestamps(actual), strip_timestamps(expected)), (
        f"UNKNOWN parity result for {label}: "
        f"actual={json.dumps(actual, ensure_ascii=False, sort_keys=True)} "
        f"expected={json.dumps(expected, ensure_ascii=False, sort_keys=True)}"
    )


@pytest.fixture(scope="session")
def ts_oracle_available() -> bool:
    """Whether a live TS oracle can be invoked (used to gate regeneration)."""
    try:
        import shutil

        return shutil.which("node") is not None
    except Exception:  # pragma: no cover - defensive import guard
        return False
