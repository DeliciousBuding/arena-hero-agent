"""Survey-db read model + /api/survey + /api/exploration tests (W44 wave 6).

Pins the survey-db loaders (loadSurveyDb / chunks / lifecycle / spend trend /
unit detail) against a materialized survey-db data root, the ``/api/survey``
route composition (tenant loop + states filter), the ``/api/exploration``
route composition (survey + lifecycle + current world), and the empty-root
fail-open behavior (200, never 500).
"""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.api import ApiRequest, CommandCenterApp
from arena_hero_agent.command_center.projections import (
    load_chunks_db,
    load_exploration,
    load_lifecycle_db,
    load_spend_trend,
    load_survey,
    load_survey_db,
    load_unit_lifecycle_db,
)
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

NOW_MS = 1_752_000_000_000


def _survey_fixture() -> dict:
    return {
        "worlds": {
            "t1": {
                "cases": [
                    {
                        "tick": 5000,
                        "state": {
                            "objects": [
                                {"kind": "CORE", "position": [0, 0], "hp": 90, "shield": 10}
                            ],
                            "resources": 3,
                            "population": 2,
                            "champion_beacon": {"x": 10, "y": 10},
                        },
                    }
                ]
            }
        },
        "survey": {
            "t1": {
                "syncMeta": [
                    {
                        "run_id": "r1",
                        "tenant": "t1",
                        "cases_synced": 3,
                        "last_tick": 5000,
                        "updated_at": "2025-07-08T18:40:00Z",
                    }
                ],
                "resources": [
                    {
                        "cell": "5,5",
                        "x": 5,
                        "y": 5,
                        "first_seen_tick": 3000,
                        "last_seen_tick": 4900,
                        "state": "visible",
                        "last_state_tick": 4900,
                        "seen_count": 3,
                    },
                    {
                        "cell": "7,5",
                        "x": 7,
                        "y": 5,
                        "first_seen_tick": 3500,
                        "last_seen_tick": 4700,
                        "state": "harvested",
                        "last_state_tick": 4700,
                        "seen_count": 2,
                    },
                    {
                        "cell": "9,9",
                        "x": 9,
                        "y": 9,
                        "first_seen_tick": 1000,
                        "last_seen_tick": 1100,
                        "state": "visible",
                        "last_state_tick": 1100,
                        "seen_count": 1,
                    },
                ],
                "resourceEvents": [
                    {
                        "cell": "5,5",
                        "tick": 4000,
                        "event_type": "HARVEST_SUCCEEDED",
                        "reason_code": None,
                        "amount": 5,
                        "actor_id": None,
                    },
                    {
                        "cell": "5,5",
                        "tick": 4200,
                        "event_type": "HARVEST_SUCCEEDED",
                        "reason_code": None,
                        "amount": 3,
                        "actor_id": None,
                    },
                ],
                "obstacles": [
                    {"cell": "3,3", "x": 3, "y": 3, "first_seen_tick": 2000, "last_seen_tick": 4000}
                ],
                "coreHunts": [
                    {
                        "cell": "10,10",
                        "x": 10,
                        "y": 10,
                        "owner": "other",
                        "source": "CORE",
                        "first_seen_tick": 3000,
                        "last_seen_tick": 4000,
                    },
                    {
                        "cell": "11,11",
                        "x": 11,
                        "y": 11,
                        "owner": "other",
                        "source": "CORE",
                        "first_seen_tick": 3500,
                        "last_seen_tick": 4500,
                    },
                    {
                        "cell": "20,20",
                        "x": 20,
                        "y": 20,
                        "owner": None,
                        "source": "CORE",
                        "first_seen_tick": 4000,
                        "last_seen_tick": 4600,
                    },
                ],
                "chunks": [
                    {"chunk_key": "0,0", "last_seen_tick": 4000},
                    {"chunk_key": "0,1", "last_seen_tick": 3800},
                ],
                "unitLifecycle": [
                    {
                        "unit_id": "u1",
                        "unit_type": "WORKER",
                        "birth_tick": 100,
                        "birth_pos": "1,1",
                        "death_tick": 900,
                        "death_pos": "2,2",
                        "death_reason": "enemy",
                        "last_seen_tick": 900,
                        "last_seen_pos": "2,2",
                        "current_state": "dead",
                    },
                    {
                        "unit_id": "u2",
                        "unit_type": "RANGER",
                        "birth_tick": 200,
                        "birth_pos": None,
                        "death_tick": None,
                        "death_pos": None,
                        "death_reason": None,
                        "last_seen_tick": 4800,
                        "last_seen_pos": "4,4",
                        "current_state": "alive",
                    },
                ],
                "coreSpends": [
                    {
                        "kind": "spawn",
                        "tick": 100,
                        "amount": 5,
                        "unit_type": "WORKER",
                        "unit_id": "u1",
                    },
                    {
                        "kind": "spawn",
                        "tick": 2100,
                        "amount": 8,
                        "unit_type": "RANGER",
                        "unit_id": "u2",
                    },
                ],
            }
        },
    }


def test_load_survey_db_shape(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_survey_fixture(), tmp_path)
    survey = load_survey_db(root, "t1")
    assert survey is not None
    assert survey["fromDb"] is True
    assert survey["caseCount"] == 3
    assert survey["tickMax"] == 5000
    resources = {r["x"]: r for r in survey["resourceCells"]}
    assert resources[5]["state"] == "visible"
    assert resources[5]["fresh"] is True
    assert resources[5]["harvestCount"] == 2
    assert resources[5]["lastHarvestTick"] == 4200
    assert resources[7]["state"] == "harvested"  # persisted negative state wins
    assert resources[9]["state"] == "stale"  # lastSeen 1100 vs tickMax 5000
    cores = survey["coreCells"]
    # last_seen desc: 20,20 (4600), 11,11 (4500); 10,10 (4000) same-owner dropped.
    assert [c["x"] for c in cores] == [20, 11]
    assert survey["chunks"] == [
        {"key": "0,0", "lastSeenTick": 4000, "cx": 0, "cy": 0},
        {"key": "0,1", "lastSeenTick": 3800, "cx": 0, "cy": 1},
    ]


def test_load_survey_db_missing_db_is_none(tmp_path: Path) -> None:
    assert load_survey_db(tmp_path, "t1") is None


def test_load_lifecycle_db_shape(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_survey_fixture(), tmp_path)
    lifecycle = load_lifecycle_db(root, "t1")
    assert lifecycle is not None
    assert lifecycle["units"] == [
        {"state": "alive", "type": "RANGER", "count": 1},
        {"state": "dead", "type": "WORKER", "count": 1},
    ]
    assert lifecycle["spends"] == [{"kind": "spawn", "count": 2, "total": 13}]
    assert lifecycle["harvestCount"] == 2
    assert lifecycle["lastHarvestTick"] == 4200
    assert lifecycle["harvestFailCount"] == 0
    assert lifecycle["recentDeaths"][0]["type"] == "WORKER"


def test_load_spend_trend_and_unit_detail(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_survey_fixture(), tmp_path)
    trend = load_spend_trend(root, "t1")
    # tick 100 -> bucket 0, tick 2100 -> bucket 2000.
    assert trend == [
        {"bucketStart": 0, "kind": "spawn", "count": 1, "total": 5},
        {"bucketStart": 2000, "kind": "spawn", "count": 1, "total": 8},
    ]
    detail = load_unit_lifecycle_db(root, "t1")
    assert [u["unitId"] for u in detail] == ["u2", "u1"]  # last_seen desc
    assert detail[0]["state"] == "alive"


def test_load_chunks_db_filters_by_age(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_survey_fixture(), tmp_path)
    chunks = load_chunks_db(root, "t1", max_age_ticks=100)
    # maxTick 4000, cutoff 3900 -> only 0,0 (4000) survives; 0,1 (3800) dropped.
    assert [c["key"] for c in chunks] == ["0,0"]


def test_load_survey_route_payload(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_survey_fixture(), tmp_path)
    payload = load_survey(root, "t1", states=["visible", "stale"], now_ms=NOW_MS)
    tenant = payload["tenants"]["t1"]
    assert "error" not in tenant
    assert {r["x"] for r in tenant["resources"]} == {5, 9}  # visible+stale, not harvested
    assert [c["x"] for c in tenant["coreHunts"]] == [20, 11]
    assert tenant["caseCount"] == 3
    assert tenant["tickMax"] == 5000
    assert tenant["lifecycle"]["harvestCount"] == 2
    assert payload["colors"]["t1"] == "#69b3d8"
    assert payload["generatedAt"] == "2025-07-08T18:40:00.000Z"


def test_load_survey_all_tenants(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_survey_fixture(), tmp_path)
    payload = load_survey(root, "all", now_ms=NOW_MS)
    assert set(payload["tenants"]) == {"t1", "t2", "t3", "t4"}
    assert len(payload["tenants"]["t2"]["resources"]) == 0


def test_load_exploration_payload(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_survey_fixture(), tmp_path)
    payload = load_exploration(root, "t1", now_ms=NOW_MS)
    assert payload["tenant"] == "t1"
    assert payload["survey"]["fromDb"] is True
    assert payload["survey"]["tickMax"] == 5000
    assert payload["lifecycle"]["harvestCount"] == 2
    current = payload["current"]
    assert current is not None
    assert current["tick"] == 5000
    assert current["objects"][0]["kind"] == "CORE"
    assert current["resources"] == 3
    assert current["population"] == 2
    assert current["champion_beacon"] == {"x": 10, "y": 10}


def test_load_exploration_empty_root_fails_open(tmp_path: Path) -> None:
    payload = load_exploration(tmp_path, "t1", now_ms=NOW_MS)
    assert payload == {"tenant": "t1", "generatedAt": "2025-07-08T18:40:00.000Z", "survey": None}


def test_survey_route_returns_200(tmp_path: Path) -> None:
    app = CommandCenterApp(data_root=tmp_path, now_ms=lambda: NOW_MS)
    response = app.handle(ApiRequest("GET", "/api/survey"))
    assert response.status == 200
    body = json.loads(response.body.decode("utf-8"))
    assert set(body) == {"generatedAt", "tenants", "colors"}
    assert body["tenants"]["t1"]["error"] == "survey db missing"
    filtered = app.handle(ApiRequest("GET", "/api/survey", query="tenant=t1&states=visible"))
    assert filtered.status == 200


def test_exploration_route_returns_200(tmp_path: Path) -> None:
    app = CommandCenterApp(data_root=tmp_path, now_ms=lambda: NOW_MS)
    response = app.handle(ApiRequest("GET", "/api/exploration"))
    assert response.status == 200
    body = json.loads(response.body.decode("utf-8"))
    assert body["tenant"] == "t1"
    assert body["survey"] is None
