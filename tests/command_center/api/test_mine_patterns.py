"""Command Center /api/survey/mine-patterns route wiring tests (W44)."""

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


def _app(data_root: Path, **kwargs) -> CommandCenterApp:
    return CommandCenterApp(data_root=data_root, **kwargs)


def _json_body(response: ApiResponse) -> dict[str, Any]:
    body = json.loads(response.body.decode("utf-8"))
    assert isinstance(body, dict)
    return body


def test_mine_patterns_route_is_registered() -> None:
    assert ("GET", "/api/survey/mine-patterns") in RouteTable().api_route_set()


def test_mine_patterns_empty_root_returns_200_payload(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/survey/mine-patterns"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"generatedAt", "tenant", "tenants", "modelCaveat", "cachedAt"}
    assert body["tenant"] == "all"
    assert {t: body["tenants"][t]["total"] for t in ("t1", "t2", "t3", "t4")} == {
        "t1": 0,
        "t2": 0,
        "t3": 0,
        "t4": 0,
    }
    assert body["generatedAt"] == body["cachedAt"]


def test_mine_patterns_fixture_root_returns_200_payload(tmp_path: Path) -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "projections"
            / "fixtures"
            / "cc_wiring"
            / "mine_patterns_basic.json"
        ).read_text(encoding="utf-8")
    )
    materialize_advice_data_root(fixture, tmp_path)
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/survey/mine-patterns"))
    assert response.status == 200
    body = _json_body(response)
    assert body["tenants"]["t1"]["total"] == 2
    assert body["tenants"]["t1"]["harvestSuccessRate"] == 1.0


def test_mine_patterns_single_tenant_query(tmp_path: Path) -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "projections"
            / "fixtures"
            / "cc_wiring"
            / "mine_patterns_basic.json"
        ).read_text(encoding="utf-8")
    )
    materialize_advice_data_root(fixture, tmp_path)
    response = _app(tmp_path).handle(
        ApiRequest("GET", "/api/survey/mine-patterns", query="tenant=t1")
    )
    assert response.status == 200
    assert _json_body(response)["tenant"] == "t1"


def test_mine_patterns_invalid_tenant_returns_400(tmp_path: Path) -> None:
    # manifest tenant_param is "all|tN": fail-closed on unknown tenants.
    response = _app(tmp_path).handle(
        ApiRequest("GET", "/api/survey/mine-patterns", query="tenant=t5")
    )
    assert response.status == 400
    assert "tenant" in _json_body(response)["error"]


def test_mine_patterns_openapi_schema_is_non_empty() -> None:
    doc = build_openapi(RouteTableImpl())
    schema = doc["paths"]["/api/survey/mine-patterns"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert schema["type"] == "object"
    required = schema["required"]
    assert {"generatedAt", "tenant", "tenants", "modelCaveat", "cachedAt"} <= set(required)
    tenant_schema = schema["properties"]["tenants"]["additionalProperties"]
    assert set(tenant_schema["properties"]) >= {
        "total",
        "visible",
        "stale",
        "topActive",
        "harvestSuccessRate",
    }
