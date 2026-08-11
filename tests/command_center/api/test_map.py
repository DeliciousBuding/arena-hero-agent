"""Merged map + weak-ETag signature tests (P5-5)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from arena_hero_agent.command_center.api import (
    CORE_TRAIL_MAX_POINTS,
    CORE_TRAIL_MIN_POINTS,
    load_core_trails_from_survey_db,
    load_merged_map,
    map_signature,
)
from arena_hero_agent.command_center.paths import calibration_dir, survey_db_path


def _mk_survey_db(data_root: Path, tenant: str, *, cores: list[tuple] | None = None) -> None:
    path = survey_db_path(data_root, tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE resources (
          cell TEXT PRIMARY KEY, x INTEGER NOT NULL, y INTEGER NOT NULL,
          first_seen_tick INTEGER NOT NULL, last_seen_tick INTEGER NOT NULL,
          state TEXT NOT NULL DEFAULT 'visible', last_state_tick INTEGER NOT NULL,
          seen_count INTEGER NOT NULL DEFAULT 1, harvest_count INTEGER,
          age_ticks INTEGER
        );
        CREATE TABLE obstacles (
          cell TEXT PRIMARY KEY, x INTEGER NOT NULL, y INTEGER NOT NULL,
          first_seen_tick INTEGER NOT NULL, last_seen_tick INTEGER NOT NULL
        );
        CREATE TABLE core_hunts (
          cell TEXT PRIMARY KEY, x INTEGER NOT NULL, y INTEGER NOT NULL,
          owner TEXT, source TEXT NOT NULL DEFAULT 'CORE',
          first_seen_tick INTEGER NOT NULL, last_seen_tick INTEGER NOT NULL
        );
        """
    )
    connection.executemany(
        "INSERT INTO resources (cell, x, y, first_seen_tick, last_seen_tick, state, "
        "last_state_tick, seen_count) VALUES (?, ?, ?, 1, ?, ?, 1, 1)",
        [(f"{x},{y}", x, y, tick, state) for (x, y, tick, state) in []],
    )
    connection.executemany(
        "INSERT INTO obstacles (cell, x, y, first_seen_tick, last_seen_tick) "
        "VALUES (?, ?, ?, 1, ?)",
        [(f"{x},{y}", x, y, tick) for (x, y, tick) in []],
    )
    if cores:
        connection.executemany(
            "INSERT INTO core_hunts (cell, x, y, owner, first_seen_tick, last_seen_tick) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            [(f"{x},{y}", x, y, owner, tick) for (x, y, owner, tick) in cores],
        )
    connection.commit()
    connection.close()


def _add_resource(data_root: Path, tenant: str, x: int, y: int, tick: int, state: str) -> None:
    path = survey_db_path(data_root, tenant)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO resources (cell, x, y, first_seen_tick, last_seen_tick, state, "
        "last_state_tick, seen_count) VALUES (?, ?, ?, 1, ?, ?, 1, 1)",
        (f"{x},{y}", x, y, tick, state),
    )
    connection.commit()
    connection.close()


def _add_obstacle(data_root: Path, tenant: str, x: int, y: int, tick: int) -> None:
    path = survey_db_path(data_root, tenant)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO obstacles (cell, x, y, first_seen_tick, last_seen_tick) "
        "VALUES (?, ?, ?, 1, ?)",
        (f"{x},{y}", x, y, tick),
    )
    connection.commit()
    connection.close()


def _write_case(data_root: Path, tenant: str, run: str, tick: int, state: dict) -> None:
    cases = calibration_dir(data_root, tenant) / run / "cases"
    cases.mkdir(parents=True, exist_ok=True)
    (cases / f"{tick}.json").write_text(
        json.dumps({"after": {"tick": tick, "state": state}}), encoding="utf-8"
    )


def _fresh_latest_run(data_root: Path, tenant: str) -> str | None:
    base = calibration_dir(data_root, tenant)
    if not base.exists():
        return None
    best: str | None = None
    best_mtime = -1.0
    for entry in base.iterdir():
        cases = entry / "cases"
        if not entry.is_dir() or not cases.is_dir() or not list(cases.glob("*.json")):
            continue
        mtime = entry.stat().st_mtime
        if best is None or mtime > best_mtime:
            best = entry.name
            best_mtime = mtime
    return best


def _fresh_list_cases(data_root: Path, tenant: str, run: str) -> list[str]:
    cases = calibration_dir(data_root, tenant) / run / "cases"
    if not cases.is_dir():
        return []
    return sorted(name for name in os.listdir(cases) if name.endswith(".json"))


@pytest.fixture
def fresh_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the 1.5s run/case memoization so tests can mutate artifacts."""
    import arena_hero_agent.command_center.api.map as map_module

    monkeypatch.setattr(map_module, "latest_run_dir", _fresh_latest_run)
    monkeypatch.setattr(map_module, "list_cases", _fresh_list_cases)


def test_signature_deterministic_empty_root(tmp_path: Path) -> None:
    assert map_signature(tmp_path) == map_signature(tmp_path)
    assert map_signature(tmp_path) == (
        "t1:none|t2:none|t3:none|t4:none#t1:0:0|t2:0:0|t3:0:0|t4:0:0"
    )


def test_signature_changes_when_case_appears(tmp_path: Path, fresh_reads: None) -> None:
    del fresh_reads
    before = map_signature(tmp_path)
    _write_case(tmp_path, "t1", "run-1", 100, {"objects": []})
    after = map_signature(tmp_path)
    assert before != after
    assert "t1:run-1:1:100.json" in after


def test_signature_changes_when_survey_db_changes(tmp_path: Path, fresh_reads: None) -> None:
    del fresh_reads
    _mk_survey_db(tmp_path, "t1")
    before = map_signature(tmp_path)
    assert before == map_signature(tmp_path)
    _add_resource(tmp_path, "t1", 5, 6, 10, "visible")
    after = map_signature(tmp_path)
    assert before != after


def test_merged_map_empty_root_payload(tmp_path: Path) -> None:
    payload, signature = load_merged_map(tmp_path)
    assert signature == map_signature(tmp_path)
    assert payload["generatedAt"]
    assert payload["tenants"] == [
        {
            "tenant": tenant,
            "runId": None,
            "caseCount": 0,
            "latestTick": None,
            "beacon": None,
        }
        for tenant in ("t1", "t2", "t3", "t4")
    ]
    assert payload["bounds"] == {"minX": 0, "maxX": 0, "minY": 0, "maxY": 0}
    assert payload["cellCount"] == 0
    assert payload["cells"] == []
    assert payload["beacons"] == []
    assert payload["coreTrails"] == []


def test_merged_map_terrain_and_dynamic(tmp_path: Path, fresh_reads: None) -> None:
    del fresh_reads
    _mk_survey_db(tmp_path, "t1")
    _add_obstacle(tmp_path, "t1", 0, 0, 5)
    _add_resource(tmp_path, "t1", 1, 1, 7, "visible")
    _add_resource(tmp_path, "t1", 9, 9, 8, "empty")  # empty mines are not shown
    _write_case(
        tmp_path,
        "t1",
        "run-1",
        100,
        {
            "objects": [
                {
                    "id": "u1",
                    "kind": "UNIT",
                    "position": [2, 3],
                    "tick": 100,
                    "hp": 50,
                    "unit_type": "WORKER",
                    "cargo": 5,
                    "controlled": True,
                },
                {
                    "id": "c1",
                    "kind": "CORE",
                    "position": [4, 4],
                    "tick": 100,
                    "hp": 100,
                    "shield": 10,
                    "controlled": True,
                    "owner": "us",
                },
            ],
            "champion_beacon": {"position": [6, 7], "status": "GROUND", "carrier_id": None},
        },
    )
    payload, _signature = load_merged_map(tmp_path)
    by_type = {cell["type"]: cell for cell in payload["cells"]}
    assert by_type["obstacle"]["x"] == 0 and by_type["obstacle"]["y"] == 0
    assert by_type["resource"]["x"] == 1 and by_type["resource"]["y"] == 1
    assert "9,9" not in {f"{c['x']},{c['y']}" for c in payload["cells"]}
    units = [c for c in payload["cells"] if c["type"] == "unit"]
    cores = [c for c in payload["cells"] if c["type"] == "core"]
    assert len(units) == 1 and units[0]["id"] == "u1" and units[0]["unitType"] == "WORKER"
    assert len(cores) == 1 and cores[0]["id"] == "c1" and cores[0]["owner"] == "us"
    assert payload["cellCount"] == len(payload["cells"]) == 4
    assert payload["bounds"] == {"minX": 0, "maxX": 4, "minY": 0, "maxY": 4}
    tenants = {item["tenant"]: item for item in payload["tenants"]}
    assert tenants["t1"]["runId"] == "run-1"
    assert tenants["t1"]["caseCount"] == 1
    assert tenants["t1"]["latestTick"] == 100
    assert tenants["t1"]["beacon"]["x"] == 6
    assert tenants["t1"]["beacon"]["trail"] == []
    assert payload["beacons"] == [
        {"tenant": "t1", "x": 6, "y": 7, "status": "GROUND", "carrier_id": None, "trail": []}
    ]


def test_merged_map_core_trails(tmp_path: Path, fresh_reads: None) -> None:
    del fresh_reads
    _mk_survey_db(
        tmp_path,
        "t1",
        cores=[
            (1, 1, "enemy-1", 10),
            (2, 2, "enemy-1", 20),
            (3, 3, "enemy-1", 20),  # same tick, new cell -> point
            (5, 5, "solo", 30),  # single point -> below min
        ],
    )
    payload, _signature = load_merged_map(tmp_path)
    by_user = {item["username"]: item for item in payload["coreTrails"]}
    assert set(by_user) == {"enemy-1"}
    assert [point["x"] for point in by_user["enemy-1"]["trail"]] == [1, 2, 3]
    assert by_user["enemy-1"]["tenant"] == "t1"


def test_core_trails_min_and_max_points(tmp_path: Path) -> None:
    _mk_survey_db(
        tmp_path,
        "t1",
        cores=[(i, i, "enemy-1", i) for i in range(CORE_TRAIL_MAX_POINTS + 10)],
    )
    trails = load_core_trails_from_survey_db(survey_db_path(tmp_path, "t1"))
    assert len(trails) == 1
    assert len(trails[0]["trail"]) == CORE_TRAIL_MAX_POINTS
    assert len(trails[0]["trail"]) >= CORE_TRAIL_MIN_POINTS


def test_core_trails_missing_db_empty(tmp_path: Path) -> None:
    assert load_core_trails_from_survey_db(survey_db_path(tmp_path, "t1")) == []


def test_merged_map_chunks_from_p5_4(tmp_path: Path, fresh_reads: None) -> None:
    del fresh_reads
    _mk_survey_db(tmp_path, "t1")
    _add_resource(tmp_path, "t1", 100, 100, 50, "visible")
    _write_case(tmp_path, "t1", "run-1", 200, {"objects": []})
    payload, _signature = load_merged_map(tmp_path)
    assert len(payload["chunks"]) == 1
    assert payload["chunks"][0]["cx"] == 6
    assert payload["chunks"][0]["cy"] == 6
    assert payload["chunks"][0]["resourceCount"] == 1
