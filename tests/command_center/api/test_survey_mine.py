"""Command Center /api/survey/mine route wiring tests (W44 wave 4)."""

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


def test_survey_mine_route_is_registered() -> None:
    assert ("GET", "/api/survey/mine") in RouteTable().api_route_set()


def test_survey_mine_empty_root_returns_200_missing_db(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/survey/mine"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"tenant", "error"}
    assert body["error"] == "survey db missing"
    assert body["tenant"] == "t1"


def test_survey_mine_cell_lookup_with_timeline(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("survey_mine_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/survey/mine", query="cell=10,10"))
    assert response.status == 200
    body = _json_body(response)
    assert body["tenant"] == "t1"
    assert body["cell"] == "10,10"
    mine = body["mine"]
    assert mine["x"] == 10
    assert mine["y"] == 10
    assert mine["tick"] == 4950
    assert mine["firstSeenTick"] == 3000
    assert mine["state"] == "visible"
    assert mine["fresh"] is True
    assert mine["harvestCount"] == 1
    assert mine["lastHarvestTick"] == 4000
    assert [event["eventType"] for event in body["timeline"]] == [
        "HARVEST_SUCCEEDED",
        "HARVEST_FAILED",
    ]
    assert body["timeline"][0]["amount"] == 5


def test_survey_mine_default_most_recent(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("survey_mine_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/survey/mine"))
    body = _json_body(response)
    assert body["cell"] == "10,10"  # most recent last_seen_tick


def test_survey_mine_persisted_negative_state_wins(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("survey_mine_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/survey/mine", query="cell=70,70"))
    body = _json_body(response)
    assert body["mine"]["state"] == "harvested"


def test_survey_mine_unknown_cell_returns_null_mine(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("survey_mine_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/survey/mine", query="cell=99,99"))
    assert response.status == 200
    body = _json_body(response)
    assert body["mine"] is None
    assert body["timeline"] == []


def test_survey_mine_openapi_schema_is_explicit() -> None:
    schema = _openapi_200("/api/survey/mine")
    assert schema["type"] == "object"
    assert "tenant" in schema["required"]
    assert schema["properties"]["mine"]["nullable"] is True
    assert schema["properties"]["timeline"]["type"] == "array"
