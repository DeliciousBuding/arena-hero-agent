"""Shop-history projection wiring tests (W44)."""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.projections import load_shop_history
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cc_wiring"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_load_shop_history_fixture_builds_trends(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("shop_history_basic"), tmp_path)
    payload = load_shop_history(root)
    assert payload["snapshots"] == 2
    assert payload["lastSnapshotAt"] == "2026-08-08T01:00:00Z"
    by_id = {trend["id"]: trend for trend in payload["trends"]}
    assert set(by_id) == {"p1", "p2"}
    assert by_id["p1"]["currentCost"] == 120
    assert by_id["p1"]["costDelta"] == 20
    assert by_id["p1"]["stockDelta"] == -2
    assert by_id["p1"]["snapshots"] == 2
    assert by_id["p2"]["costDelta"] is None


def test_load_shop_history_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_shop_history(tmp_path)
    assert payload["snapshots"] == 0
    assert payload["productCount"] == 0
    assert payload["trends"] == []
    assert payload["lastSnapshotAt"] is None
