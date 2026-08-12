"""Alignment audit projection tests (W44 wave 5).

Pins the pure ``aggregate_alignment`` grade/reason semantics (synthetic inputs,
no live data) and the ``load_alignment_audit`` loader against a materialized
Command Center data root, plus the wired ``/api/audit/alignment`` route.
"""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.api import ApiRequest, CommandCenterApp
from arena_hero_agent.command_center.projections import (
    aggregate_alignment,
    load_alignment_audit,
)
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

NOW_MS = 1_752_000_000_000


def _empty_decision() -> dict:
    return {"decision": {"actionMix": {"move": 0, "harvest": 0, "deposit": 0, "wait": 0, "repair": 0}}}


def _base_inputs() -> dict:
    """All tenants empty so only the overridden tenants produce grades."""
    return {
        "decisions": {t: _empty_decision() for t in ("t1", "t2", "t3", "t4")},
        "mines": {t: {"visibleNever": 0} for t in ("t1", "t2", "t3", "t4")},
        "effectiveness": {
            "perTenant": {
                t: {"assigned": 0, "open": 0, "stale": 0, "harvested": 0}
                for t in ("t1", "t2", "t3", "t4")
            }
        },
        "trends": {},
        "workers_by_tenant": {},
    }


def _harvest_heavy() -> dict:
    return {"decision": {"actionMix": {"move": 2, "harvest": 5, "deposit": 1, "wait": 2, "repair": 0}}}


def test_aggregate_alignment_grade_aligned() -> None:
    inputs = _base_inputs()
    inputs["decisions"]["t1"] = _harvest_heavy()
    inputs["mines"]["t1"] = {"visibleNever": 5}
    inputs["effectiveness"]["perTenant"]["t1"] = {"assigned": 3, "open": 1, "stale": 0, "harvested": 2}
    inputs["workers_by_tenant"]["t1"] = 8
    payload = aggregate_alignment(**inputs, now_ms=NOW_MS)
    tenant = payload["tenants"]["t1"]
    assert tenant["grade"] == "aligned"
    assert tenant["harvestActionRate"] == 0.5
    assert tenant["depositActionRate"] == 0.1
    assert tenant["workers"] == 8
    assert tenant["reasons"] == ["采集占比 50%，缺口 5——对齐"]


def test_aggregate_alignment_grade_allocation_unfulfilled() -> None:
    inputs = _base_inputs()
    inputs["decisions"]["t2"] = {"decision": {"actionMix": {"move": 3, "harvest": 0, "deposit": 0, "wait": 0, "repair": 0}}}
    inputs["mines"]["t2"] = {"visibleNever": 2}
    inputs["effectiveness"]["perTenant"]["t2"] = {"assigned": 4, "open": 2, "stale": 1, "harvested": 0}
    payload = aggregate_alignment(**inputs, now_ms=NOW_MS)
    tenant = payload["tenants"]["t2"]
    assert tenant["grade"] == "allocation_unfulfilled"
    assert tenant["harvestActionRate"] == 0
    assert tenant["reasons"] == ["分工 4 矿 0 兑现（2 在途/1 失效）——需派 worker"]
    assert payload["global"]["unfulfilledAssignments"] == 1


def test_aggregate_alignment_grade_gap_widening_with_idle_workers() -> None:
    inputs = _base_inputs()
    inputs["decisions"]["t3"] = {"decision": {"actionMix": {"move": 20, "harvest": 0, "deposit": 0, "wait": 1, "repair": 0}}}
    inputs["mines"]["t3"] = {"visibleNever": 15}
    inputs["trends"]["t3"] = {"visibleNever": 15, "visibleNeverPrev": 8}
    inputs["workers_by_tenant"]["t3"] = 6
    payload = aggregate_alignment(**inputs, now_ms=NOW_MS)
    tenant = payload["tenants"]["t3"]
    assert tenant["grade"] == "gap_widening"
    assert tenant["gapTrendDelta"] == 7
    assert tenant["reasons"] == [
        "缺口 15 但采集动作占比 0%——决策未对齐矿分配",
        "缺口较上窗口 +7",
        "有 6 个 worker 但采集占比 0%——worker 空闲/在移动",
    ]


def test_aggregate_alignment_grade_data_gap() -> None:
    inputs = _base_inputs()
    inputs["decisions"] = {t: _empty_decision() for t in ("t1", "t2", "t3", "t4")}
    inputs["mines"] = {t: {"visibleNever": 0} for t in ("t1", "t2", "t3", "t4")}
    inputs["effectiveness"] = {"perTenant": {}}
    payload = aggregate_alignment(**inputs, now_ms=NOW_MS)
    for t in ("t1", "t2", "t3", "t4"):
        assert payload["tenants"][t]["grade"] == "data_gap"
        assert payload["tenants"][t]["reasons"] == []
    assert payload["global"] == {"aligned": 0, "misaligned": 0, "dataGap": 4, "unfulfilledAssignments": 0}


def test_aggregate_alignment_global_totals() -> None:
    inputs = _base_inputs()
    inputs["decisions"]["t1"] = _harvest_heavy()
    inputs["mines"]["t1"] = {"visibleNever": 5}
    inputs["effectiveness"]["perTenant"]["t1"] = {"assigned": 3, "open": 1, "stale": 0, "harvested": 2}
    inputs["decisions"]["t2"] = {"decision": {"actionMix": {"move": 3, "harvest": 0, "deposit": 0, "wait": 0, "repair": 0}}}
    inputs["effectiveness"]["perTenant"]["t2"] = {"assigned": 4, "open": 2, "stale": 1, "harvested": 0}
    inputs["decisions"]["t3"] = {"decision": {"actionMix": {"move": 20, "harvest": 0, "deposit": 0, "wait": 1, "repair": 0}}}
    inputs["mines"]["t3"] = {"visibleNever": 15}
    inputs["trends"]["t3"] = {"visibleNever": 15, "visibleNeverPrev": 8}
    inputs["workers_by_tenant"]["t3"] = 6
    payload = aggregate_alignment(**inputs, now_ms=NOW_MS)
    assert payload["global"] == {"aligned": 1, "misaligned": 2, "dataGap": 1, "unfulfilledAssignments": 1}


def _loader_fixture() -> dict:
    return {
        "nowMs": NOW_MS,
        "telemetry": {
            "t1": {
                "decision": [
                    {"tick": 10, "decisionSource": "ai", "moveCount": 2, "waitCount": 0, "harvestCount": 1, "depositCount": 0, "repairCount": 0, "planHash": "p1", "intent": "harvest"},
                    {"tick": 11, "decisionSource": "ai", "moveCount": 0, "waitCount": 3, "harvestCount": 0, "depositCount": 0, "repairCount": 0, "planHash": "p2", "intent": "wait"},
                ],
                "outcome": [
                    {"tick": 10, "coreResourceDelta": 4, "workerCount": 2, "workersWithCargo": 1, "humanOverride": {"applied": ["a1"], "rejected": []}},
                    {"tick": 11, "coreResourceDelta": -1, "workerCount": 2, "workersWithCargo": 2, "humanOverride": {"applied": [], "rejected": [{"reason": "Core is already moving"}]}},
                ],
            }
        },
        "survey": {
            "t1": {
                "syncMeta": [{"run_id": "r1", "tenant": "t1", "cases_synced": 1, "last_tick": 5000, "updated_at": "2025-07-08T18:40:00Z"}],
                "agents": [
                    {
                        "tenant": "t1", "instance": "i1", "tick": 5000, "resources": 10,
                        "population": 3, "core_x": 0, "core_y": 0, "units": 3,
                        "visible_enemies": 0, "status": "ok", "sdk_version": "0.3.0a4",
                        "base_url": "http://localhost", "pid": 1, "platform": "win",
                        "mode": "production", "connection_state": "up",
                        "first_seen": "2025-07-08T18:00:00Z",
                        "last_heartbeat": "2025-07-08T18:40:00Z",
                        "updated_at": "2025-07-08T18:40:00Z",
                    }
                ],
                "resources": [
                    {"cell": "5,5", "x": 5, "y": 5, "first_seen_tick": 3000, "last_seen_tick": 4800, "state": "visible", "last_state_tick": 4800, "seen_count": 3},
                    {"cell": "7,5", "x": 7, "y": 5, "first_seen_tick": 3500, "last_seen_tick": 4900, "state": "visible", "last_state_tick": 4700, "seen_count": 2},
                ],
                "resourceEvents": [
                    {"cell": "5,5", "tick": 4000, "event_type": "HARVEST_SUCCEEDED", "reason_code": None, "amount": 5, "actor_id": None},
                ],
            }
        },
    }


def test_load_alignment_audit_fixture(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_loader_fixture(), tmp_path)
    payload = load_alignment_audit(root, now_ms=NOW_MS)
    tenant = payload["tenants"]["t1"]
    # t1 harvests (1/6 actions) with a visible-never gap -> aligned
    assert tenant["grade"] == "aligned"
    assert tenant["harvestActionRate"] == 0.167
    assert tenant["visibleNever"] == 1
    assert tenant["workers"] is None
    # t2..t4 have no decision/mine data -> data_gap
    assert payload["global"] == {
        "aligned": 1,
        "misaligned": 0,
        "dataGap": 3,
        "unfulfilledAssignments": 0,
    }
    assert payload["generatedAt"] == "2025-07-08T18:40:00.000Z"


def test_load_alignment_audit_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_alignment_audit(tmp_path, now_ms=NOW_MS)
    for t in ("t1", "t2", "t3", "t4"):
        assert payload["tenants"][t]["grade"] == "data_gap"
    assert payload["global"]["dataGap"] == 4


def test_alignment_route_returns_200(tmp_path: Path) -> None:
    app = CommandCenterApp(data_root=tmp_path, now_ms=lambda: NOW_MS)
    response = app.handle(ApiRequest("GET", "/api/audit/alignment"))
    assert response.status == 200
    body = json.loads(response.body.decode("utf-8"))
    assert set(body) == {"generatedAt", "tenants", "global", "cachedAt"}
    assert body["global"]["dataGap"] == 4
