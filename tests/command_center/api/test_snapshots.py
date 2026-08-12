"""Command Center /api/plan + /api/world route wiring tests (W44 wave 4)."""

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


def test_plan_route_is_registered() -> None:
    assert ("GET", "/api/plan") in RouteTable().api_route_set()


def test_world_route_is_registered() -> None:
    assert ("GET", "/api/world") in RouteTable().api_route_set()


def test_plan_empty_root_returns_200_null_plan(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/plan"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"tenant", "generatedAt", "plan", "tick"}
    assert body["plan"] is None
    assert body["tick"] is None


def test_world_empty_root_returns_200_null_state(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/world"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"tenant", "generatedAt", "state", "caseFile"}
    assert body["state"] is None
    assert body["caseFile"] is None


def test_plan_fixture_reads_latest_case_plan(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("worlds_plan_events_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/plan"))
    assert response.status == 200
    body = _json_body(response)
    assert body["tick"] == 110
    assert body["plan"]["unitActions"] == {"w1": {"action": "MOVE", "target": [5, 5]}}


def test_world_fixture_reads_latest_case_state(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("worlds_plan_events_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/world"))
    assert response.status == 200
    body = _json_body(response)
    assert body["tick"] == 110
    assert body["caseFile"] == "110.json"
    assert body["runId"] == "run-1"
    kinds = {obj["kind"] for obj in body["state"]["objects"]}
    assert kinds == {"WORKER", "CORE"}


def test_plan_openapi_schema_is_explicit() -> None:
    schema = _openapi_200("/api/plan")
    assert {"tenant", "generatedAt", "plan", "tick"} <= set(schema["required"])
    assert schema["properties"]["plan"]["nullable"] is True


def test_world_openapi_schema_is_explicit() -> None:
    schema = _openapi_200("/api/world")
    assert {"tenant", "generatedAt", "state", "caseFile"} <= set(schema["required"])
    assert schema["properties"]["state"]["nullable"] is True
