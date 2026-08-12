"""Command Center /api/exploration route wiring tests (W44)."""

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


def test_exploration_route_is_registered() -> None:
    assert ("GET", "/api/exploration") in RouteTable().api_route_set()


def test_exploration_empty_root_returns_200_payload(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/exploration"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {
        "generatedAt",
        "world",
        "perTenant",
        "alliance",
        "gaps",
        "resurveyTargets",
        "cachedAt",
    }
    assert body["world"]["exploredChunks"] == 0
    assert body["resurveyTargets"] == []
    assert body["gaps"] == []
    assert body["generatedAt"] == body["cachedAt"]


def test_exploration_fixture_root_returns_200_payload(tmp_path: Path) -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[1] / "projections" / "fixtures" / "exploration_basic.json"
        ).read_text(encoding="utf-8")
    )
    materialize_advice_data_root(fixture, tmp_path)
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/exploration"))
    assert response.status == 200
    body = _json_body(response)
    assert body["world"]["exploredChunks"] == 4
    assert body["alliance"]["unionChunks"] == 4


def test_exploration_invalid_tenant_returns_400(tmp_path: Path) -> None:
    # manifest tenant_param is "t1": fail-closed on unknown tenants.
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/exploration", query="tenant=t5"))
    assert response.status == 400
    assert "tenant" in _json_body(response)["error"]


def test_exploration_openapi_schema_is_non_empty() -> None:
    doc = build_openapi(RouteTableImpl())
    schema = doc["paths"]["/api/exploration"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert schema["type"] == "object"
    required = schema["required"]
    assert {
        "generatedAt",
        "world",
        "perTenant",
        "alliance",
        "gaps",
        "resurveyTargets",
        "cachedAt",
    } <= set(required)
    assert set(schema["properties"]["alliance"]["required"]) >= {
        "unionChunks",
        "unionRecent",
        "exclusiveByTenant",
    }
