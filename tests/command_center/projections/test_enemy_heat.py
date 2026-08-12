"""Enemy-heat projection wiring tests (W44)."""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.projections import load_enemy_heat
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

NOW_MS = 1_752_000_000_000
FIXTURES = Path(__file__).parent / "fixtures" / "cc_wiring"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_load_enemy_heat_fixture_builds_buckets(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("enemy_heat_basic"), tmp_path)
    payload = load_enemy_heat(root, "t1", now_ms=NOW_MS)
    assert payload["tenant"] == "t1"
    assert payload["currentTick"] == 5000
    by_bucket = {(b["bx"], b["by"]): b for b in payload["buckets"]}
    assert by_bucket[(0, 0)]["count"] == 2
    assert by_bucket[(0, 0)]["combatCount"] == 2
    assert by_bucket[(0, 0)]["lastTick"] == 4950
    assert by_bucket[(1, 1)]["workerCount"] == 1
    assert payload["summary"] == {
        "totalSightings": 3,
        "distinctCells": 2,
        "combatSightings": 2,
        "workerSightings": 1,
        "tenants": 1,
    }


def test_load_enemy_heat_all_merges_tenants(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("enemy_heat_basic"), tmp_path)
    payload = load_enemy_heat(root, "all", now_ms=NOW_MS)
    assert payload["summary"]["tenants"] == 4
    assert payload["summary"]["totalSightings"] == 3


def test_load_enemy_heat_respects_window(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("enemy_heat_basic"), tmp_path)
    payload = load_enemy_heat(root, "t1", now_ms=NOW_MS, recent_window_ticks=100)
    assert payload["summary"]["totalSightings"] == 1


def test_load_enemy_heat_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_enemy_heat(tmp_path, "all", now_ms=NOW_MS)
    assert payload["buckets"] == []
    assert payload["fullBuckets"] == []
    assert payload["summary"]["totalSightings"] == 0
    assert payload["currentTick"] == 0
