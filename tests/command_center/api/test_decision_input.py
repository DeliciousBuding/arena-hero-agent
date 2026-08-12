"""Command Center /api/survey/decision-input route wiring tests (W44 wave 4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arena_hero_agent.command_center.api import (
    ApiRequest,
    ApiResponse,
    CommandCenterApp,
    RouteTable,
)
from arena_hero_agent.command_center.api.openapi import build_openapi
from arena_hero_agent.command_center.api.routes import RouteTable as RouteTableImpl
from arena_hero_agent.command_center.projections import build_decision_input
from tests.command_center.projections.tools.advice_fixture import (
    materialize_advice_data_root,
)

FIXTURES = Path(__file__).parents[1] / "projections" / "fixtures" / "cc_wiring"


def _app(data_root: Path, **kwargs) -> CommandCenterApp:
    return CommandCenterApp(data_root=data_root, **kwargs)


def _json_body(response: ApiResponse) -> dict[str, Any]:
    body = json.loads(response.body.decode("utf-8"))
    assert isinstance(body, dict)
    return body


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _openapi_200(path: str) -> dict:
    doc = build_openapi(RouteTableImpl())
    return doc["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]


def test_decision_input_route_is_registered() -> None:
    assert ("GET", "/api/survey/decision-input") in RouteTable().api_route_set()


def test_decision_input_empty_root_returns_200_empty_inputs(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/survey/decision-input"))
    assert response.status == 200
    body = _json_body(response)
    assert body["tenant"] == "t1"
    assert body["currentTick"] is None
    for key in (
        "refillPredictions",
        "chunkCoverage",
        "resurveyTargets",
        "coreThreats",
        "miningCandidates",
    ):
        assert body[key] == []
    assert body["generatedAt"] == body["cachedAt"]


def test_decision_input_tenant_fail_closed(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(
        ApiRequest("GET", "/api/survey/decision-input", query="tenant=all")
    )
    assert response.status == 400


def test_decision_input_fixture_composes_all_inputs(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("decision_input_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/survey/decision-input"))
    assert response.status == 200
    body = _json_body(response)
    assert body["currentTick"] == 5000
    # refill predictions from mine-patterns (wave-4 port)
    assert body["refillPredictions"] == [
        {
            "cell": "10,10",
            "x": 10,
            "y": 10,
            "dueInTicks": 2000,
            "predictedNextTick": 7000,
            "lastSeenTick": 4002,
            "threatLevel": 0,
            "threatCombat": 0,
        }
    ]
    assert body["chunkCoverage"] == [{"key": "0,0", "cx": 0, "cy": 0, "lastSeenTick": 1000}]
    assert body["resurveyTargets"] == [
        {
            "key": "0,0",
            "cx": 0,
            "cy": 0,
            "lastSeenTick": 1000,
            "stalenessTicks": 4000,
            "distChunks": 0,
            "threatLevel": 0,
            "threatCombat": 0,
        }
    ]
    assert body["coreThreats"] == [
        {
            "username": "raider",
            "kind": "approaching",
            "distCells": 50,
            "speedCellsPerTick": 0.05,
            "lastSeenTick": 3000,
            "x": 50,
            "y": 50,
            "stale": False,
        }
    ]
    candidate = body["miningCandidates"][0]
    assert candidate["cell"] == "10,10"
    assert candidate["gapAgeTicks"] == 4000
    assert candidate["harvestFail"] == 1
    assert candidate["lastSeenTick"] == 4900


def test_build_decision_input_pure_sorts() -> None:
    payload = build_decision_input(
        "t1",
        5000,
        [
            {
                "cell": "b",
                "x": 2,
                "y": 2,
                "dueInTicks": 10,
                "predictedNextTick": 5010,
                "lastSeenTick": 4000,
            },
            {
                "cell": "a",
                "x": 1,
                "y": 1,
                "dueInTicks": -5,
                "predictedNextTick": 4995,
                "lastSeenTick": 3900,
            },
        ],
        [
            {"key": "0,0", "cx": 0, "cy": 0, "lastSeenTick": 1000},
            {"cx": 2, "cy": 2, "lastSeenTick": 500},
        ],
        threat_by_cell={"a": {"threatLevel": 2, "threatCombat": 7}},
        resurvey=[
            {
                "key": "0,0",
                "cx": 0,
                "cy": 0,
                "lastSeenTick": 1000,
                "stalenessTicks": 4000,
                "distChunks": 0,
            }
        ],
        now_ms=1_752_000_000_000,
    )
    # refill sorted by dueInTicks ascending; chunk coverage by lastSeenTick ascending
    assert [item["cell"] for item in payload["refillPredictions"]] == ["a", "b"]
    assert payload["refillPredictions"][0]["threatLevel"] == 2
    assert [item["key"] for item in payload["chunkCoverage"]] == ["2,2", "0,0"]
    assert payload["resurveyTargets"][0]["key"] == "0,0"


def test_decision_input_openapi_schema_is_explicit() -> None:
    schema = _openapi_200("/api/survey/decision-input")
    assert schema["type"] == "object"
    assert {
        "generatedAt",
        "tenant",
        "currentTick",
        "refillPredictions",
        "chunkCoverage",
        "resurveyTargets",
        "coreThreats",
        "miningCandidates",
        "cachedAt",
    } <= set(schema["required"])
    assert schema["properties"]["refillPredictions"]["type"] == "array"
