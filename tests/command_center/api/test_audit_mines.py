"""Command Center /api/audit/mines + /api/audit/mines/trend route wiring tests (W44)."""

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


def _schema(doc: dict, path: str) -> dict:
    return doc["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]


def test_mines_routes_are_registered() -> None:
    assert ("GET", "/api/audit/mines") in RouteTable().api_route_set()
    assert ("GET", "/api/audit/mines/trend") in RouteTable().api_route_set()


def test_mines_empty_root_returns_200_payload(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/audit/mines"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"generatedAt", "tenant", "tenants", "cachedAt"}
    assert body["tenant"] == "all"
    assert {t: body["tenants"][t]["total"] for t in ("t1", "t2", "t3", "t4")} == {
        "t1": 0,
        "t2": 0,
        "t3": 0,
        "t4": 0,
    }
    assert body["generatedAt"] == body["cachedAt"]


def test_mines_fixture_root_returns_200(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("mines_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/audit/mines", query="tenant=t1"))
    assert response.status == 200
    body = _json_body(response)
    assert body["tenant"] == "t1"
    assert body["tenants"]["t1"]["total"] == 3
    assert body["tenants"]["t1"]["harvested"] == 1


def test_mines_invalid_tenant_returns_400(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/audit/mines", query="tenant=t0"))
    assert response.status == 400


def test_mines_trend_fixture_root_returns_200(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("mines_basic"), tmp_path)
    response = _app(root).handle(
        ApiRequest("GET", "/api/audit/mines/trend", query="tenant=t1&window=2000&steps=6")
    )
    assert response.status == 200
    body = _json_body(response)
    assert body["tenant"] == "t1"
    assert len(body["trend"]) == 6
    assert body["currentTick"] == 5000


def test_mines_trend_all_tenant_returns_400(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(
        ApiRequest("GET", "/api/audit/mines/trend", query="tenant=all")
    )
    assert response.status == 400


def test_mines_trend_steps_are_clamped(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(
        ApiRequest("GET", "/api/audit/mines/trend", query="tenant=t1&steps=99")
    )
    assert response.status == 200
    assert _json_body(response)["steps"] == 10


def test_mines_openapi_schema_is_explicit() -> None:
    doc = build_openapi(RouteTableImpl())
    mines_schema = _schema(doc, "/api/audit/mines")
    assert mines_schema["required"] == ["generatedAt", "tenant", "tenants"]
    per_tenant = mines_schema["properties"]["tenants"]["additionalProperties"]
    assert "harvested" in per_tenant["properties"]
    assert "candidates" in per_tenant["properties"]
    trend_schema = _schema(doc, "/api/audit/mines/trend")
    assert trend_schema["required"] == ["generatedAt", "tenant", "window", "steps", "trend"]
