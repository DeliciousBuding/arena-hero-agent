"""Command Center /api/shop/history route wiring tests (W44)."""

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


def test_shop_history_route_is_registered() -> None:
    assert ("GET", "/api/shop/history") in RouteTable().api_route_set()


def test_shop_history_empty_root_returns_200(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/shop/history"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {
        "generatedAt",
        "snapshots",
        "productCount",
        "lastSnapshotAt",
        "trends",
        "refreshedAt",
        "cachedAt",
    }
    assert body["snapshots"] == 0
    assert body["trends"] == []
    assert body["generatedAt"] == body["cachedAt"]


def test_shop_history_fixture_root_returns_200(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("shop_history_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/shop/history"))
    assert response.status == 200
    body = _json_body(response)
    assert body["snapshots"] == 2
    by_id = {trend["id"]: trend for trend in body["trends"]}
    assert by_id["p1"]["currentCost"] == 120
    assert by_id["p1"]["costDelta"] == 20
    assert by_id["p1"]["stockDelta"] == -2


def test_shop_history_openapi_schema_is_explicit() -> None:
    doc = build_openapi(RouteTableImpl())
    schema = doc["paths"]["/api/shop/history"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert schema["required"] == ["generatedAt", "snapshots", "productCount", "trends"]
    assert set(schema["properties"]) == {
        "generatedAt",
        "snapshots",
        "productCount",
        "lastSnapshotAt",
        "trends",
        "refreshedAt",
        "cachedAt",
    }
