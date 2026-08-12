"""Alliance snapshot projection: loader + pure payload core (W20).

The pure composition core is golden-tested against the TS oracle in
``test_golden_parity.py``; these tests cover the thin I/O layer — the loader
reads the same runtime artifacts the TS ``loadAllianceSnapshot`` reads
(calibration world cases, survey-db ``core_hunts``, leaderboard snapshots) and
degrades fail-open exactly like the oracle.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from arena_hero_agent.command_center.paths import calibration_dir, survey_db_path
from arena_hero_agent.command_center.projections import (
    build_alliance_snapshot_payload,
    load_alliance_snapshot,
)
from arena_hero_agent.domain import TenantId

NOW_MS = 1_752_000_000_000


def _write_case(data_root: Path, tenant: str, run: str, tick: int, state: dict) -> None:
    cases = calibration_dir(data_root, tenant) / run / "cases"
    cases.mkdir(parents=True, exist_ok=True)
    (cases / f"{tick}.json").write_text(
        json.dumps({"after": {"tick": tick, "state": state}}), encoding="utf-8"
    )


def _mk_survey_db(data_root: Path, tenant: str, *, cores: list[tuple] | None = None) -> None:
    path = survey_db_path(data_root, tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE core_hunts (
          cell TEXT PRIMARY KEY, x INTEGER NOT NULL, y INTEGER NOT NULL,
          owner TEXT, source TEXT NOT NULL DEFAULT 'CORE',
          first_seen_tick INTEGER NOT NULL, last_seen_tick INTEGER NOT NULL
        );
        """
    )
    if cores:
        connection.executemany(
            "INSERT INTO core_hunts (cell, x, y, owner, first_seen_tick, last_seen_tick) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            [(f"{x},{y}", x, y, owner, tick) for (x, y, owner, tick) in cores],
        )
    connection.commit()
    connection.close()


def _write_leaderboard(data_root: Path, rows: list[dict]) -> None:
    directory = data_root / "leaderboard"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "leaderboard-2026-08-12-00-00-00.json").write_text(
        json.dumps({"damage_dealt": rows}), encoding="utf-8"
    )


def _t1_world(tick: int = 100) -> dict:
    return {
        "resources": 50,
        "resource_capacity": 100,
        "population": 6,
        "status": "ACTIVE",
        "objects": [
            {
                "id": "core-t1",
                "kind": "CORE",
                "controlled": True,
                "position": [0, 0],
                "hp": 500,
                "shield": 100,
                "moving": False,
            },
            {
                "id": "u1",
                "kind": "UNIT",
                "controlled": True,
                "unit_type": "WORKER",
                "position": [5, 5],
                "cargo": 3,
            },
            {
                "id": "u2",
                "kind": "UNIT",
                "controlled": True,
                "unit_type": "VANGUARD",
                "position": [6, 6],
                "cargo": 0,
            },
            {
                "id": "e1",
                "kind": "UNIT",
                "controlled": False,
                "unit_type": "RANGER",
                "position": [40, 40],
                "cargo": 0,
            },
        ],
    }


def test_load_alliance_snapshot_builds_payload_from_world_and_survey(tmp_path: Path) -> None:
    _write_case(tmp_path, "t1", "run-1", 100, _t1_world())
    _write_case(
        tmp_path,
        "t2",
        "run-1",
        95,
        {
            "resources": 12,
            "resource_capacity": 100,
            "population": 1,
            "status": "DEGRADED",
            "objects": [
                {
                    "id": "core-t2",
                    "kind": "CORE",
                    "controlled": True,
                    "position": [200, 200],
                    "hp": 480,
                    "shield": 50,
                    "moving": False,
                },
                {
                    "id": "u3",
                    "kind": "UNIT",
                    "controlled": True,
                    "unit_type": "WORKER",
                    "position": [205, 205],
                    "cargo": 0,
                },
            ],
        },
    )
    _mk_survey_db(tmp_path, "t3", cores=[(300, 300, "enemy-core", 98)])
    _write_leaderboard(tmp_path, [{"rank": 5, "username": "aggr", "score": 100}])

    payload = load_alliance_snapshot(tmp_path, now_ms=NOW_MS)
    assert payload["generatedAt"] == "2025-07-08T18:40:00.000Z"
    assert payload["cachedAt"] == payload["generatedAt"]
    assert payload["currentTick"] == 100
    assert payload["revision"] == 1
    assert payload["treasuryTenant"] == "t1"
    assert payload["leaderboardAggression"] == {"aggr": 0.9}
    assert set(payload["members"]) == {"t1", "t2"}
    t1_member = payload["members"]["t1"]
    assert t1_member["core"] == {
        "id": "core-t1",
        "position": [0, 0],
        "hp": 500,
        "shield": 100,
        "moving": False,
    }
    assert t1_member["workers"] == 1
    assert t1_member["vanguards"] == 1
    assert t1_member["status"] == "READY"
    assert payload["members"]["t2"]["status"] == "DEGRADED"
    # enemy unit from world (LIVE) + enemy core from survey-db (CALIBRATION)
    kinds = {(s["key"], s["evidence"]) for s in payload["sightings"]}
    assert ("UNIT:e1", "LIVE") in kinds
    assert ("CORE:enemy-core", "CALIBRATION") in kinds
    assert payload["counts"]["currentVisibleCombat"] == 1
    assert payload["intel"]["counts"]["currentEnemyUnits"] == 1
    assert payload["intel"]["counts"]["historicalEnemyCores"] == 1
    assert payload["threat"]["cellCount"] > 0
    assert len(payload["threatSummaries"]) == 2
    assert payload["threatSummaries"][0]["tenantId"] == "t1"


def test_load_alliance_snapshot_empty_data_root_is_fail_closed(tmp_path: Path) -> None:
    payload = load_alliance_snapshot(tmp_path, now_ms=NOW_MS)
    assert payload["currentTick"] == 0
    assert payload["members"] == {}
    assert payload["sightings"] == []
    assert payload["counts"]["currentVisibleCombat"] == 0
    assert payload["intel"]["counts"]["currentEnemyUnits"] == 0
    assert payload["threat"]["cellCount"] == 0
    assert payload["threat"]["maxDirect"] is None
    assert payload["threatSummaries"] == []
    assert payload["treasuryTenant"] == ""
    assert payload["leaderboardAggression"] == {}


def test_load_alliance_snapshot_corrupt_case_fails_open(tmp_path: Path) -> None:
    cases = calibration_dir(tmp_path, "t1") / "run-1" / "cases"
    cases.mkdir(parents=True, exist_ok=True)
    (cases / "100.json").write_text("{not json", encoding="utf-8")
    payload = load_alliance_snapshot(tmp_path, now_ms=NOW_MS)
    assert payload["members"] == {}
    assert payload["sightings"] == []
    assert payload["currentTick"] == 0


def test_load_alliance_snapshot_missing_survey_db_degrades_to_empty(tmp_path: Path) -> None:
    _write_case(tmp_path, "t1", "run-1", 100, _t1_world())
    payload = load_alliance_snapshot(tmp_path, now_ms=NOW_MS)
    assert all(s["kind"] != "CORE" for s in payload["sightings"])
    assert payload["members"]["t1"]["workers"] == 1


def test_load_alliance_snapshot_missing_leaderboard_aggression_is_empty(
    tmp_path: Path,
) -> None:
    _write_case(tmp_path, "t1", "run-1", 100, _t1_world())
    payload = load_alliance_snapshot(tmp_path, now_ms=NOW_MS)
    assert payload["leaderboardAggression"] == {}


def test_leaderboard_aggression_tiers(tmp_path: Path) -> None:
    _write_leaderboard(
        tmp_path,
        [
            {"rank": 5, "username": "elite", "score": 1},
            {"rank": 20, "username": "aggr", "score": 1},
            {"rank": 99, "username": "std", "score": 1},
        ],
    )
    _write_case(tmp_path, "t1", "run-1", 100, _t1_world())
    payload = load_alliance_snapshot(tmp_path, now_ms=NOW_MS)
    assert payload["leaderboardAggression"] == {"elite": 0.9, "aggr": 0.6, "std": 0.2}


def test_pure_core_empty_inputs_build_stable_payload() -> None:
    payload = build_alliance_snapshot_payload(
        revision=1,
        members=[],
        sightings=[],
        ally_entity_ids=[],
        now_tick=100,
        generated_at_ms=NOW_MS,
    )
    assert payload["members"] == {}
    assert payload["sightings"] == []
    assert payload["counts"]["currentVisibleCombat"] == 0
    assert payload["threat"]["maxDirect"] is None
    assert payload["threatSummaries"] == []
    assert payload["treasuryTenant"] == ""
    assert payload["leaderboardAggression"] == {}


def test_pure_core_treasury_reflects_highest_resource_member(tmp_path: Path) -> None:
    from arena_hero_agent.alliance.snapshot import (
        AllianceMemberState,
        CoreRef,
        EntitySighting,
        EvidenceKind,
        MemberStatus,
        SightingKind,
        UnitType,
    )
    from arena_hero_agent.domain import Coordinate

    def member(tenant: str, resources: int) -> AllianceMemberState:
        return AllianceMemberState(
            tenant_id=TenantId(tenant),
            tick=100,
            observed_at_ms=NOW_MS,
            core=CoreRef(
                id=f"core-{tenant}", position=Coordinate(0, 0), hp=100, shield=0, moving=False
            ),
            resources=resources,
            resource_capacity=100,
            population=1,
            workers=0,
            vanguards=0,
            rangers=0,
            carried_resources=0,
            active_fleet_ids=(),
            local_threat=0.0,
            local_harvest_rate=0.0,
            status=MemberStatus.READY,
        )

    sighting = EntitySighting(
        key="UNIT:e1",
        kind=SightingKind.UNIT,
        unit_type=UnitType.VANGUARD,
        entity_id="e1",
        owner_username=None,
        position=Coordinate(10, 10),
        source_tenant=TenantId("t1"),
        first_seen_tick=100,
        last_seen_tick=100,
        currently_visible=True,
        confidence=1.0,
        evidence=EvidenceKind.LIVE,
    )
    payload = build_alliance_snapshot_payload(
        revision=1,
        members=[member("t1", 5), member("t2", 80)],
        sightings=[sighting],
        ally_entity_ids=[],
        now_tick=100,
        generated_at_ms=NOW_MS,
        treasury_tenant=TenantId("t2"),
    )
    assert payload["treasuryTenant"] == "t2"
    assert payload["counts"]["currentVisibleCombat"] == 1
    assert payload["intel"]["counts"]["currentEnemyUnits"] == 1
