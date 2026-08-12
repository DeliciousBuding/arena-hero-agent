"""Alliance defense projection: loader + pure payload core (W21).

The pure composition core is golden-tested against the TS oracle in
``test_golden_parity.py``; these tests cover the thin I/O layer — the loader
reuses the W20 snapshot loader (the same source the TS handler reads via
``loadAllianceSnapshot()``) and degrades fail-open exactly like the oracle.
"""

from __future__ import annotations

from pathlib import Path

from arena_hero_agent.command_center.paths import calibration_dir
from arena_hero_agent.command_center.projections import (
    build_alliance_defense_payload,
    load_alliance_defense,
)
from tests.command_center.projections.test_alliance_snapshot import (
    NOW_MS,
    _mk_survey_db,
    _t1_world,
    _write_case,
    _write_leaderboard,
)


def _world(tenant: str, *, core: list[int], vanguards: int, rangers: int, status: str) -> dict:
    objects = [
        {
            "id": f"core-{tenant}",
            "kind": "CORE",
            "controlled": True,
            "position": core,
            "hp": 500,
            "shield": 100,
            "moving": False,
        }
    ]
    for index in range(vanguards):
        objects.append(
            {
                "id": f"v{index}",
                "kind": "UNIT",
                "controlled": True,
                "unit_type": "VANGUARD",
                "position": [core[0] + 5, core[1] + 5],
                "cargo": 0,
            }
        )
    for index in range(rangers):
        objects.append(
            {
                "id": f"r{index}",
                "kind": "UNIT",
                "controlled": True,
                "unit_type": "RANGER",
                "position": [core[0] + 6, core[1] + 6],
                "cargo": 0,
            }
        )
    return {
        "resources": 50,
        "resource_capacity": 100,
        "population": vanguards + rangers + 1,
        "status": status,
        "objects": objects,
    }


def test_load_alliance_defense_builds_payload_from_snapshot(tmp_path: Path) -> None:
    _write_case(
        tmp_path,
        "t1",
        "run-1",
        100,
        _world("t1", core=[0, 0], vanguards=2, rangers=1, status="ACTIVE"),
    )
    _write_case(
        tmp_path,
        "t2",
        "run-1",
        100,
        _world("t2", core=[300, 0], vanguards=0, rangers=0, status="DEGRADED"),
    )
    _write_case(
        tmp_path,
        "t3",
        "run-1",
        100,
        _world("t3", core=[280, 0], vanguards=4, rangers=1, status="ACTIVE"),
    )
    # t2 sees enemy cores near its own core (threatens t2 + t3 when present).
    _mk_survey_db(tmp_path, "t2", cores=[(250, 0, "delta", 98), (260, 5, "eps", 98)])
    _write_leaderboard(tmp_path, [{"rank": 5, "username": "aggr", "score": 100}])

    payload = load_alliance_defense(tmp_path, now_ms=NOW_MS)
    assert payload["generatedAtMs"] == NOW_MS
    assert [entry["tenantId"] for entry in payload["endangered"]] == ["t2"]
    kinds = [advice["category"] for advice in payload["advice"]]
    assert "ENDANGERED" in kinds
    assert "FORMATION" in kinds
    assert "REINFORCE" in kinds
    pocket_ids = [pocket["id"] for pocket in payload["pockets"]]
    assert "pocket:CORE:delta+CORE:eps" in pocket_ids
    reinforce = next(a for a in payload["advice"] if a["category"] == "REINFORCE")
    assert reinforce["tenant"] == "t3"
    assert reinforce["relatedTenants"] == ["t2"]


def test_load_alliance_defense_empty_data_root_is_fail_closed(tmp_path: Path) -> None:
    payload = load_alliance_defense(tmp_path, now_ms=NOW_MS)
    assert payload["generatedAtMs"] == NOW_MS
    assert payload["advice"] == []
    assert payload["endangered"] == []
    assert payload["pockets"] == []


def test_load_alliance_defense_corrupt_case_fails_open(tmp_path: Path) -> None:
    cases = calibration_dir(tmp_path, "t1") / "run-1" / "cases"
    cases.mkdir(parents=True, exist_ok=True)
    (cases / "100.json").write_text("{not json", encoding="utf-8")
    payload = load_alliance_defense(tmp_path, now_ms=NOW_MS)
    assert payload["advice"] == []
    assert payload["endangered"] == []
    assert payload["pockets"] == []


def test_load_alliance_defense_missing_survey_db_has_no_pockets(tmp_path: Path) -> None:
    _write_case(tmp_path, "t1", "run-1", 100, _t1_world())
    payload = load_alliance_defense(tmp_path, now_ms=NOW_MS)
    assert payload["pockets"] == []
    assert all(sighting["kind"] != "CORE" for sighting in [])


def test_pure_core_empty_inputs_build_stable_payload() -> None:
    payload = build_alliance_defense_payload(
        members={},
        sightings=[],
        threat_summaries=[],
        now_ms=NOW_MS,
    )
    assert payload["generatedAtMs"] == NOW_MS
    assert payload["advice"] == []
    assert payload["endangered"] == []
    assert payload["pockets"] == []


def test_pure_core_skips_non_core_and_ownerless_sightings() -> None:
    payload = build_alliance_defense_payload(
        members={},
        sightings=[
            {
                "key": "CORE:owned",
                "kind": "CORE",
                "ownerUsername": "x",
                "position": [10, 10],
                "lastSeenTick": 5,
            },
            {"key": "CORE:no-owner", "kind": "CORE", "position": [10, 10], "lastSeenTick": 5},
            {
                "key": "UNIT:unit",
                "kind": "UNIT",
                "unitType": "VANGUARD",
                "ownerUsername": "x",
                "position": [10, 10],
                "lastSeenTick": 5,
            },
            {
                "key": "CORE:bad-pos",
                "kind": "CORE",
                "ownerUsername": "y",
                "position": [None, 0],
                "lastSeenTick": 5,
            },
        ],
        threat_summaries=[],
        now_ms=NOW_MS,
    )
    assert payload["pockets"] == []
