"""Command Center /api/intel/heat route wiring tests (W44)."""

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


def test_enemy_heat_route_is_registered() -> None:
    assert ("GET", "/api/intel/heat") in RouteTable().api_route_set()


def test_enemy_heat_empty_root_returns_200(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/intel/heat"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {
        "generatedAt",
        "tenant",
        "currentTick",
        "buckets",
        "fullBuckets",
        "summary",
        "cachedAt",
    }
    assert body["tenant"] == "all"
    assert body["buckets"] == []
    assert body["summary"]["totalSightings"] == 0
    assert body["generatedAt"] == body["cachedAt"]


def test_enemy_heat_fixture_root_returns_200(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("enemy_heat_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/intel/heat", query="tenant=t1"))
    assert response.status == 200
    body = _json_body(response)
    assert body["tenant"] == "t1"
    assert body["summary"]["totalSightings"] == 3
    assert body["summary"]["combatSightings"] == 2


def test_enemy_heat_window_is_clamped(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("enemy_heat_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/intel/heat", query="tenant=t1&window=1"))
    assert response.status == 200
    # TS clamps window to 100..50000
    assert _json_body(response)["summary"]["totalSightings"] == 1


def test_enemy_heat_invalid_tenant_returns_400(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/intel/heat", query="tenant=t8"))
    assert response.status == 400


def test_enemy_heat_openapi_schema_is_explicit() -> None:
    doc = build_openapi(RouteTableImpl())
    schema = doc["paths"]["/api/intel/heat"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert schema["required"] == [
        "generatedAt",
        "tenant",
        "currentTick",
        "buckets",
        "fullBuckets",
        "summary",
        "cachedAt",
    ]
    assert set(schema["properties"]["summary"]["properties"]) == {
        "totalSightings",
        "distinctCells",
        "combatSightings",
        "workerSightings",
        "tenants",
    }
