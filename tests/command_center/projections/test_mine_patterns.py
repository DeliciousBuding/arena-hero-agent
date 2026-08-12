"""Mine-pattern projection tests (W44).

``load_mine_patterns`` reads the survey-db resource ledger (``resources`` +
``resource_events`` + ``sync_meta`` watermark) into per-tenant lifecycle
patterns. These tests pin the loader composition on a synthetic fixture and
the empty-root fail-open behavior; Node golden parity is tracked separately
(BLOCKED follow-up).
"""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.projections import load_mine_patterns
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

NOW_MS = 1_752_000_000_000
ISO_AT = "2025-07-08T18:40:00.000Z"


def _load_fixture(name: str) -> dict:
    path = Path(__file__).parent / "fixtures" / "cc_wiring" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_load_mine_patterns_builds_payload(tmp_path: Path) -> None:
    fixture = _load_fixture("mine_patterns_basic")
    materialize_advice_data_root(fixture, tmp_path)

    payload = load_mine_patterns(tmp_path, "all", now_ms=NOW_MS)
    assert payload["generatedAt"] == ISO_AT
    assert payload["cachedAt"] == ISO_AT
    assert payload["tenant"] == "all"
    assert payload["modelCaveat"]

    t1 = payload["tenants"]["t1"]
    assert t1["tenant"] == "t1"
    assert t1["total"] == 2
    assert t1["visible"] == 1  # lastSeenTick 4900 >= 5000-200
    assert t1["stale"] == 1
    assert t1["avgAgeTicks"] == 950  # (1900 + 0) / 2
    assert t1["medianSeenCount"] == 3
    assert t1["harvestSuccessRate"] == 1.0
    assert t1["harvestSucceeded"] == 1
    assert t1["harvestFailed"] == 0
    # fresh (visible) mine ranks before the stale one in topActive
    assert [entry["cell"] for entry in t1["topActive"]] == ["1,50", "2,50"]

    # tenants without data return the TS empty-pattern defaults
    for t in ("t2", "t3", "t4"):
        assert payload["tenants"][t]["total"] == 0
        assert payload["tenants"][t]["topActive"] == []
        assert payload["tenants"][t]["harvestSuccessRate"] is None


def test_load_mine_patterns_single_tenant(tmp_path: Path) -> None:
    fixture = _load_fixture("mine_patterns_basic")
    materialize_advice_data_root(fixture, tmp_path)

    payload = load_mine_patterns(tmp_path, "t1", now_ms=NOW_MS)
    assert payload["tenant"] == "t1"
    assert set(payload["tenants"]) == {"t1"}


def test_load_mine_patterns_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_mine_patterns(tmp_path, "all", now_ms=NOW_MS)
    assert payload["generatedAt"] == ISO_AT
    assert payload["tenant"] == "all"
    for t in ("t1", "t2", "t3", "t4"):
        assert payload["tenants"][t]["total"] == 0
