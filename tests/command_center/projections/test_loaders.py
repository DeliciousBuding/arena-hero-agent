"""Loader integration: projections read the P5-3 data base exactly like the TS oracle.

The pure aggregation cores are golden-tested against the TS oracle in
``test_golden_parity.py``; these tests cover the thin I/O layer — the loaders
read the same runtime artifacts (jsonl tails, survey databases) the oracle
reads and delegate parsing to the P5-3 JSONL base.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arena_hero_agent.command_center import (
    AgentIngestEvent,
    CommandCenterError,
    SurveyDb,
    append_jsonl,
)
from arena_hero_agent.command_center.projections import (
    aggregate_decision_audit,
    load_alliance_survey,
    load_audit_trail,
    load_decision_audit,
    load_human_conflict,
    load_map_lod,
    load_mine_utilization,
    load_shop_history,
    load_worker_liveness_audit,
    read_human_audit,
)


def _write_jsonl(root: Path, relative: str, rows: list[dict[str, Any]]) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    for row in rows:
        append_jsonl(path, row)


def _survey_event(
    tenant: str,
    tick: int,
    *,
    resource_cells: list[tuple[int, int]] | None = None,
    obstacle_cells: list[tuple[int, int]] | None = None,
    enemy_cores: list[tuple[int, int, str]] | None = None,
) -> AgentIngestEvent:
    return AgentIngestEvent(
        tenant=tenant,
        ts=float(tick),
        event="tick_summary",
        instance="i1",
        tick=tick,
        resources=tick,
        resource_cells=resource_cells,
        obstacle_cells=obstacle_cells,
        enemy_cores=enemy_cores,
    )


def test_load_decision_audit_reads_jsonl_tails(tmp_path: Path) -> None:
    d_rows = [
        {
            "tick": 10,
            "decisionSource": "ai",
            "moveCount": 1,
            "waitCount": 0,
            "harvestCount": 0,
            "depositCount": 0,
            "repairCount": 0,
            "planHash": "p1",
        },
        {
            "tick": 11,
            "decisionSource": "ai",
            "moveCount": 0,
            "waitCount": 2,
            "harvestCount": 0,
            "depositCount": 0,
            "repairCount": 0,
            "planHash": "p1",
        },
    ]
    o_rows = [
        {"tick": 10, "coreResourceDelta": 4, "workerCount": 2, "workersWithCargo": 1},
        {"tick": 11, "coreResourceDelta": -1, "workerCount": 2, "workersWithCargo": 2},
    ]
    _write_jsonl(tmp_path, "runtime/t1/telemetry/decision.jsonl", d_rows)
    _write_jsonl(tmp_path, "runtime/t1/telemetry/outcome.jsonl", o_rows)

    payload = load_decision_audit(tmp_path, "t1", window=10)
    assert aggregate_decision_audit("t1", 10, d_rows, o_rows)["decision"]["records"] == 2
    assert payload["tenant"] == "t1"
    assert payload["decision"]["records"] == 2
    assert payload["decision"]["stallTicks"] == 1
    assert payload["decision"]["planChurn"] == {"unique": 1, "records": 2, "rate": 0.5}
    assert payload["outcome"]["coreDeltaSum"] == 3
    assert payload["outcome"]["cargoEfficiency"] == 0.75
    assert payload["currentTick"] == 11


def test_load_decision_audit_all_tenants(tmp_path: Path) -> None:
    _write_jsonl(tmp_path, "runtime/t1/telemetry/decision.jsonl", [{"tick": 1, "moveCount": 1}])
    payload = load_decision_audit(tmp_path, "all", window=10)
    assert set(payload) == {"t1", "t2", "t3", "t4"}
    assert payload["t1"]["decision"]["records"] == 1
    assert payload["t2"]["decision"]["records"] == 0


def test_load_decision_audit_invalid_tenant_raises(tmp_path: Path) -> None:
    with pytest.raises(CommandCenterError):
        load_decision_audit(tmp_path, "t9")


def test_load_worker_liveness_reads_runtime_jsonl(tmp_path: Path) -> None:
    rows = [
        {
            "tick": 5,
            "telemetryType": "worker_liveness",
            "unitId": "u1",
            "workerLivenessKind": "stuck",
            "streak": 2,
            "recoveryCount": 0,
            "recoveryApplied": False,
        },
        {
            "tick": 6,
            "telemetryType": "worker_liveness",
            "unitId": "u1",
            "workerLivenessKind": "stuck",
            "streak": 3,
            "recoveryCount": 1,
            "recoveryApplied": True,
        },
    ]
    _write_jsonl(tmp_path, "runtime/t1/telemetry/runtime.jsonl", rows)

    payload = load_worker_liveness_audit(tmp_path, "t1", window=200)
    tenant_audit = payload["tenants"][0]
    assert tenant_audit["tenant"] == "t1"
    assert tenant_audit["eventCount"] == 2
    assert tenant_audit["affectedWorkers"] == 1
    assert tenant_audit["latestByWorker"][0]["unitId"] == "u1"
    assert tenant_audit["latestByWorker"][0]["status"] == "repeated"
    assert payload["totals"]["eventCount"] == 2


def test_read_human_audit_and_conflicts(tmp_path: Path) -> None:
    audit = [
        {
            "at": "2026-08-08T00:00:00Z",
            "tenant": "t1",
            "kind": "goal",
            "unitId": "u1",
            "action": "mine [3,4]",
        },
        {"at": "2026-08-08T00:00:01Z", "tenant": "t2", "kind": "mode"},
        {"at": "2026-08-08T00:00:02Z", "tenant": "t1", "kind": "clear"},
        {"at": "2026-08-08T00:00:03Z", "tenant": "t1", "kind": "delete"},
    ]
    _write_jsonl(tmp_path, "runtime/human-command-audit.jsonl", audit)

    entries = read_human_audit(tmp_path, tenant="t1", limit=2)
    assert [entry["kind"] for entry in entries] == ["delete", "clear"]

    o_rows = [
        {
            "tick": 10,
            "humanOverride": {"applied": ["a"], "rejected": [{"reason": "Core is already moving"}]},
        }
    ]
    _write_jsonl(tmp_path, "runtime/t1/telemetry/outcome.jsonl", o_rows)
    conflict = load_human_conflict(tmp_path, "t1", window=100)
    assert conflict["applied"] == 1
    assert conflict["rejected"] == 1
    assert conflict["topRejectedReasons"][0]["reason"] == "Core is already moving"
    assert conflict["commandKinds"] == {"goal": 1, "clear": 1, "delete": 1}


def test_load_audit_trail_reads_four_sources(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path,
        "runtime/human-command-audit.jsonl",
        [
            {
                "at": "2026-08-08T00:00:00Z",
                "tenant": "t1",
                "kind": "goal",
                "unitId": "u1",
                "action": "mine [3,4]",
            }
        ],
    )
    _write_jsonl(
        tmp_path,
        "runtime/command-audit/t1.jsonl",
        [
            {
                "at": "2026-08-08T00:00:01Z",
                "kind": "worker_mine",
                "action": "MINE",
                "evidence": {"target": [3, 4]},
            }
        ],
    )
    _write_jsonl(
        tmp_path,
        "runtime/survey/arbitration.jsonl",
        [{"createdAt": "2026-08-08T00:00:02Z", "cell": "5,5", "winnerTenant": "t2"}],
    )
    _write_jsonl(
        tmp_path,
        "runtime/supervisor.jsonl",
        [
            {
                "ts": "2026-08-08T00:00:03Z",
                "type": "exited",
                "pid": 7,
                "exitCode": 1,
                "tenantId": "t1",
            }
        ],
    )

    payload = load_audit_trail(tmp_path, limit=10)
    assert [entry["source"] for entry in payload["entries"]] == [
        "supervisor",
        "arbitration",
        "command",
        "human",
    ]
    assert payload["counts"] == {"human": 1, "command": 1, "arbitration": 1, "supervisor": 1}
    filtered = load_audit_trail(tmp_path, source="human", limit=10)
    assert all(entry["source"] == "human" for entry in filtered["entries"])


def test_load_arbitrations_last_wins(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path,
        "runtime/survey/arbitration.jsonl",
        [
            {"cell": "5,5", "winnerTenant": "t1", "createdAt": "2026-08-08T00:00:00Z"},
            {"cell": "5,5", "winnerTenant": "t2", "createdAt": "2026-08-08T01:00:00Z"},
        ],
    )
    from arena_hero_agent.command_center.jsonl import load_jsonl_rows
    from arena_hero_agent.command_center.projections.arbitrations import (
        arbitration_file,
        load_arbitrations,
    )

    rows = load_jsonl_rows(arbitration_file(tmp_path))
    effective = load_arbitrations(rows)
    assert effective["5,5"]["winnerTenant"] == "t2"


def _seed_survey_db(tmp_path: Path, tenant: str) -> None:
    with SurveyDb(tmp_path, tenant, write=True) as db:
        db.apply_agent_event(_survey_event(tenant, 100, resource_cells=[(1, 1), (16, 17)]))
        db.apply_agent_event(
            _survey_event(tenant, 200, resource_cells=[(1, 1)], obstacle_cells=[(0, 0)])
        )
        db.apply_agent_event(_survey_event(tenant, 300, enemy_cores=[(32, 33, "enemy1")]))


def test_load_map_lod_reads_survey_db(tmp_path: Path) -> None:
    _seed_survey_db(tmp_path, "t1")
    payload = load_map_lod(tmp_path, "t1")
    assert payload["tenant"] == "t1"
    assert payload["chunkSize"] == 16
    by_key = {(chunk["cx"], chunk["cy"]): chunk for chunk in payload["chunks"]}
    assert by_key[(0, 0)]["resourceCount"] == 1
    assert by_key[(0, 0)]["obstacleCount"] == 1
    assert by_key[(1, 1)]["resourceCount"] == 1
    assert by_key[(2, 2)]["coreCount"] == 1
    assert by_key[(2, 2)]["lastTick"] == 300


def test_load_mine_utilization_survey_db_and_missing_events(tmp_path: Path) -> None:
    _seed_survey_db(tmp_path, "t1")
    payload = load_mine_utilization(tmp_path, "t1")
    tenant = payload["tenants"]["t1"]
    # resources table is read; resource_events table is not part of the P5-3
    # schema yet, so every mine is neverHarvested (registered ALLOWED divergence).
    assert tenant["total"] == 2
    assert tenant["harvested"] == 0
    assert tenant["neverHarvested"] == 2
    assert tenant["currentTick"] == 300  # derived from MAX(agents.tick)
    assert {c["cell"] for c in tenant["candidates"]} == {"1,1", "16,17"}


def test_load_alliance_survey_reads_survey_db_and_arbitration(tmp_path: Path) -> None:
    _seed_survey_db(tmp_path, "t1")
    _seed_survey_db(tmp_path, "t2")
    _write_jsonl(
        tmp_path,
        "runtime/survey/arbitration.jsonl",
        [{"cell": "1,1", "winnerTenant": "t2", "createdAt": "2026-08-08T00:00:00Z"}],
    )
    payload = load_alliance_survey(tmp_path)
    assert payload["tenantSummaries"]["t1"]["resources"] == 2
    assert payload["tenantSummaries"]["t2"]["resources"] == 2
    overlap = next(
        item for item in payload["conflicts"]["resourceOverlaps"] if item["cell"] == "1,1"
    )
    assert overlap["arbitration"]["winner"] == "t2"
    assert overlap["arbitration"]["arbitrated"] is True
    assert len(payload["consensusResources"]) == 2
    consensus = next(
        item for item in payload["consensusResources"] if item["x"] == 1 and item["y"] == 1
    )
    assert consensus["observers"] == ["t1", "t2"]
    assert consensus["consensus"] == 2
    assert consensus["arbitrated"] is True


def test_load_shop_history_reads_jsonl(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path,
        "runtime/shop-history.jsonl",
        [
            {
                "at": "2026-08-08T00:00:00Z",
                "products": [
                    {
                        "id": "p1",
                        "name": "Item A",
                        "resourceCost": 100,
                        "availableStock": 5,
                        "purchaseLimit": 1,
                    }
                ],
            },
            {
                "at": "2026-08-08T01:00:00Z",
                "products": [
                    {
                        "id": "p1",
                        "name": "Item A",
                        "resourceCost": 120,
                        "availableStock": 3,
                        "purchaseLimit": 1,
                    }
                ],
            },
        ],
    )
    payload = load_shop_history(tmp_path)
    assert payload["snapshots"] == 2
    assert payload["productCount"] == 1
    assert payload["trends"][0]["currentCost"] == 120
    assert payload["trends"][0]["costDelta"] == 20
    assert payload["trends"][0]["stockDelta"] == -2


def test_loaders_tolerate_missing_files(tmp_path: Path) -> None:
    assert load_decision_audit(tmp_path, "t1", window=10)["decision"]["records"] == 0
    assert load_worker_liveness_audit(tmp_path, "all", window=200)["totals"]["eventCount"] == 0
    assert read_human_audit(tmp_path) == []
    assert load_human_conflict(tmp_path, "t1", window=100)["applied"] == 0
    assert load_map_lod(tmp_path, "all")["chunks"] == []
    assert load_shop_history(tmp_path)["snapshots"] == 0
