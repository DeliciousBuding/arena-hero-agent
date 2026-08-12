"""Audit-family projection wiring tests (W44).

Pins the loaders behind the wired /api/audit/* endpoints against synthetic
cc_wiring fixtures (telemetry tails, survey DBs, four-source trail) and the
empty-root fail-open behavior. Node golden parity stays a BLOCKED follow-up.
"""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.projections import (
    load_audit_trail,
    load_decision_audit,
    load_decision_trend,
    load_human_conflict,
    load_mine_utilization,
    load_mine_utilization_trend,
    load_mining_effectiveness,
    load_worker_liveness_audit,
)
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

NOW_MS = 1_752_000_000_000
FIXTURES = Path(__file__).parent / "fixtures" / "cc_wiring"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_load_decision_audit_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("audit_decisions_basic"), tmp_path)
    payload = load_decision_audit(root, "t1", window=100)
    assert payload["tenant"] == "t1"
    assert payload["decision"]["records"] == 2
    assert payload["decision"]["stallTicks"] == 1
    assert payload["decision"]["planChurn"] == {"unique": 2, "records": 2, "rate": 1.0}
    assert payload["outcome"]["coreDeltaSum"] == 3
    assert payload["outcome"]["humanApplied"] == 1
    assert payload["outcome"]["humanRejected"] == 1
    assert set(load_decision_audit(root, "all", window=100)) == {"t1", "t2", "t3", "t4"}


def test_load_decision_audit_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_decision_audit(tmp_path, "t1", window=100)
    assert payload["decision"]["records"] == 0
    assert payload["outcome"]["records"] == 0


def test_load_decision_trend_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("audit_decisions_basic"), tmp_path)
    payload = load_decision_trend(root, "t1", window=50, steps=3)
    assert payload["steps"] == 3
    assert len(payload["trend"]) == 3
    assert payload["trend"][-1]["coreDelta"] == 3


def test_load_decision_trend_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_decision_trend(tmp_path, "t1", window=50, steps=3)
    assert len(payload["trend"]) == 3
    assert all(step["window"] == 0 for step in payload["trend"])


def test_load_human_conflict_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("audit_decisions_basic"), tmp_path)
    payload = load_human_conflict(root, "t1", window=100)
    assert payload["applied"] == 1
    assert payload["rejected"] == 1
    assert payload["rejectedRate"] == 0.5
    assert payload["topRejectedReasons"][0]["reason"] == "Core is already moving"
    assert payload["commandKinds"] == {"goal": 1, "delete": 1}
    assert set(load_human_conflict(root, "all", window=100)) == {"t1", "t2", "t3", "t4"}


def test_load_human_conflict_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_human_conflict(tmp_path, "t1", window=100)
    assert payload["applied"] == 0
    assert payload["rejected"] == 0


def test_load_mine_utilization_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("mines_basic"), tmp_path)
    payload = load_mine_utilization(root, "all")
    tenant = payload["tenants"]["t1"]
    assert tenant["total"] == 3
    assert tenant["harvested"] == 1
    assert tenant["neverHarvested"] == 2
    assert tenant["visibleNever"] == 1
    assert tenant["staleNever"] == 1
    assert tenant["medianTimeToFirstHarvest"] == 1000
    assert [c["cell"] for c in tenant["candidates"]] == ["7,5"]


def test_load_mine_utilization_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_mine_utilization(tmp_path, "all")
    for t in ("t1", "t2", "t3", "t4"):
        assert payload["tenants"][t]["total"] == 0


def test_load_mine_utilization_trend_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("mines_basic"), tmp_path)
    payload = load_mine_utilization_trend(root, "t1", window=2000, steps=6)
    assert payload["steps"] == 6
    assert len(payload["trend"]) == 6
    assert payload["currentTick"] == 5000


def test_load_mine_utilization_trend_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_mine_utilization_trend(tmp_path, "t1", window=2000, steps=6)
    assert payload["trend"] == []


def test_load_mining_effectiveness_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("mines_basic"), tmp_path)
    payload = load_mining_effectiveness(root, now_ms=NOW_MS)
    assert payload["global"]["assigned"] == 1
    assert payload["global"]["open"] == 1
    assert payload["perTenant"]["t1"]["assigned"] == 1
    assert payload["currentTick"] == 5000


def test_load_mining_effectiveness_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_mining_effectiveness(tmp_path, now_ms=NOW_MS)
    assert payload["global"] == {
        "assigned": 0,
        "harvested": 0,
        "harvestedByOther": 0,
        "open": 0,
        "stale": 0,
        "effectiveRate": None,
        "progressRate": None,
    }
    assert payload["items"] == []


def test_load_audit_trail_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("trail_basic"), tmp_path)
    payload = load_audit_trail(root, limit=10)
    assert [entry["source"] for entry in payload["entries"]] == [
        "supervisor",
        "arbitration",
        "command",
        "human",
    ]
    assert payload["counts"] == {"human": 1, "command": 1, "arbitration": 1, "supervisor": 1}
    filtered = load_audit_trail(root, source="human", limit=10)
    assert all(entry["source"] == "human" for entry in filtered["entries"])


def test_load_audit_trail_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_audit_trail(tmp_path, limit=10)
    assert payload["entries"] == []
    assert payload["counts"] == {"human": 0, "command": 0, "arbitration": 0, "supervisor": 0}


def test_load_worker_liveness_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("workers_basic"), tmp_path)
    payload = load_worker_liveness_audit(root, "all", window=4000)
    assert payload["totals"]["eventCount"] == 2
    assert payload["totals"]["affectedWorkers"] == 1
    assert payload["totals"]["repeatedWorkers"] == 1
    assert payload["tenants"][0]["latestByWorker"][0]["status"] == "repeated"


def test_load_worker_liveness_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_worker_liveness_audit(tmp_path, "all", window=4000)
    assert payload["totals"]["eventCount"] == 0
    assert len(payload["tenants"]) == 4
    assert all(tenant["eventCount"] == 0 for tenant in payload["tenants"])
