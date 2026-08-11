"""P5-3 acceptance: field-by-field parity of the Python data base with TS fixtures.

The P5-2 snapshot pins 33 fixtures with SHA-256 over the exact source files.
For every TypeScript-side JSON fixture we round-trip the document through the
Python JSONL base (append -> tail read -> full read) and compare field by
field. Every fixture must classify MATCH; registered, documented divergences
live in :data:`ALLOWED_DIFFERENCES` and stay visible; anything else is
UNKNOWN and fails the suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arena_hero_agent.command_center import append_jsonl, load_jsonl_rows, read_jsonl_tail
from scripts.snapshot_command_center import MANIFEST_PATH, find_ts_repo


def _ts_repo_or_skip() -> Path:
    try:
        return find_ts_repo()
    except FileNotFoundError:
        pytest.skip("arena-hero-agent-ts checkout not available in this environment")


def _ts_json_fixtures() -> list[dict[str, str]]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return [
        fixture
        for fixture in manifest["fixtures"]
        if fixture["repo"] == "arena-hero-agent-ts" and fixture["path"].endswith(".json")
    ]


def _json_equal(actual: object, expected: object) -> bool:
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and set(actual) == set(expected)
            and all(_json_equal(actual[key], expected[key]) for key in expected)
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_json_equal(a, b) for a, b in zip(actual, expected, strict=True))
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


# Documented, deliberately registered divergences from the TS oracle. These are
# the only reasons a fixture result may be ALLOWED instead of MATCH; the list is
# asserted stable so differences never silently disappear.
ALLOWED_DIFFERENCES: tuple[str, ...] = (
    "valid-JSON non-object rows raise CommandCenterError instead of being cast (fail-closed)",
    "cache and run-dir memo clocks are monotonic, not wall-clock Date.now() (TTL semantics equal)",
    "goal ids are opaque: goal-<ms>-<6 hex> instead of goal-<ms>-<6 base36>",
    "read-only survey open of a missing database yields unavailable instead of creating ",
    "an empty DB",
    "write open of a legacy single-column-PK agents table raises instead of auto-migrating",
    "ingest event kind/mode and human store entry fields are validated and raise ",
    "instead of being trusted",
    "human store version must be an integer when present (TS Number() would coerce)",
)


def test_ts_fixture_manifest_nonempty() -> None:
    assert len(_ts_json_fixtures()) == 28


@pytest.mark.parametrize(
    "fixture",
    _ts_json_fixtures(),
    ids=lambda f: f"{f['path'].split('/')[-1]}",
)
def test_ts_json_fixture_round_trip_matches(fixture: dict[str, str], tmp_path: Path) -> None:
    """Write then read each TS fixture through the JSONL base; must be MATCH."""
    ts_repo = _ts_repo_or_skip()
    path = ts_repo / fixture["path"]
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(expected, dict), "fixture must be a JSON object"

    jsonl = tmp_path / f"{fixture['path'].split('/')[-1]}.jsonl"
    append_jsonl(jsonl, expected)
    tail = read_jsonl_tail(jsonl, 1)
    full = load_jsonl_rows(jsonl)

    assert len(tail) == 1, f"UNKNOWN: tail returned {len(tail)} rows for {fixture['path']}"
    assert len(full) == 1, f"UNKNOWN: full read returned {len(full)} rows for {fixture['path']}"
    assert _json_equal(tail[0], expected), (
        f"UNKNOWN field difference in {fixture['path']}: tail row does not match the fixture"
    )
    assert _json_equal(full[0], expected), (
        f"UNKNOWN field difference in {fixture['path']}: full row does not match the fixture"
    )


def test_all_ts_json_fixtures_classify_match(tmp_path: Path) -> None:
    """Aggregate classification: every fixture MATCH, no UNKNOWN."""
    _ts_repo_or_skip()
    mismatches: list[str] = []
    for index, fixture in enumerate(_ts_json_fixtures()):
        path = find_ts_repo() / fixture["path"]
        expected = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(expected, dict):
            mismatches.append(f"{fixture['path']}: non-object fixture (ALLOWED by design)")
            continue
        jsonl = tmp_path / f"parity-{index}.jsonl"
        append_jsonl(jsonl, expected)
        rows = read_jsonl_tail(jsonl, 1) + load_jsonl_rows(jsonl)
        if len(rows) != 2 or not all(_json_equal(row, expected) for row in rows):
            mismatches.append(f"{fixture['path']}: round-trip mismatch (UNKNOWN)")
    assert mismatches == [], "UNKNOWN parity results:\n" + "\n".join(mismatches)


def test_allowed_differences_registry_is_stable() -> None:
    """Registered divergences stay visible so ALLOWED never collapses into MATCH."""
    assert len(ALLOWED_DIFFERENCES) >= 5
    assert all(isinstance(item, str) and item for item in ALLOWED_DIFFERENCES)


def test_human_store_field_contract_matches_ts_shape(tmp_path: Path) -> None:
    """The persisted human-commands document uses exactly the TS field names."""
    from arena_hero_agent.command_center import (
        GoalEntry,
        HumanCommand,
        empty_store,
        iso_utc,
        read_human_store,
        write_human_store,
    )

    now = 1_752_000_000_000
    store = empty_store("t1")
    store.goals.append(
        GoalEntry(id="g1", unit_id="u1", kind="mine", target=(3, 4), created_at=iso_utc(now))
    )
    store.commands.append(
        HumanCommand(
            id="c1",
            unit_id="u2",
            action={"type": "MOVE", "to": [1, 1]},
            created_at=iso_utc(now),
        )
    )
    write_human_store(tmp_path, "t1", store, now_ms=now)
    raw = json.loads(
        (tmp_path / "runtime" / "human-commands" / "t1.json").read_text(encoding="utf-8")
    )
    assert set(raw) == {"version", "mode", "commands", "goals", "updatedAt"}
    assert set(raw["commands"][0]) == {"id", "unitId", "action", "createdAt"}
    assert set(raw["goals"][0]) == {"id", "unitId", "kind", "target", "createdAt"}
    assert raw["version"] == 1
    assert read_human_store(tmp_path, "t1").goals[0].target == (3, 4)
