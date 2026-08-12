"""Command Center /api/deeds route wiring tests (W44 wave 8)."""

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

FIXTURES = Path(__file__).parents[1] / "projections" / "fixtures"


def _app(data_root: Path) -> CommandCenterApp:
    return CommandCenterApp(data_root=data_root)


def _json_body(response: ApiResponse) -> dict[str, Any]:
    body = json.loads(response.body.decode("utf-8"))
    assert isinstance(body, dict)
    return body


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_deeds_route_is_registered() -> None:
    assert ("GET", "/api/deeds") in RouteTable().api_route_set()


def test_deeds_empty_root_returns_200_empty(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/deeds"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"generatedAt", "tenant", "limit", "allianceMerged", "deeds"}
    assert body["tenant"] == "all"
    assert body["allianceMerged"] is True
    assert body["deeds"] == []


def test_deeds_fixture_scans_and_milestones(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("deeds_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/deeds", query="tenant=t1"))
    assert response.status == 200
    body = _json_body(response)
    assert body["allianceMerged"] is False
    kinds = {deed["kind"] for deed in body["deeds"]}
    assert "CORE_DESTROYED" in kinds
    assert "PICKUP_BEACON_SUCCEEDED" in kinds
    assert "MILESTONE_HARVEST" in kinds
    assert "MILESTONE_RESOURCES" in kinds
    # tick descending
    ticks = [deed["tick"] for deed in body["deeds"]]
    assert ticks == sorted(ticks, reverse=True)


def test_deeds_limit_clamps(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("deeds_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/deeds", query="tenant=t1&limit=3"))
    assert len(_json_body(response)["deeds"]) == 3
    response = _app(root).handle(ApiRequest("GET", "/api/deeds", query="tenant=t1&limit=9999"))
    assert len(_json_body(response)["deeds"]) <= 200


def test_deeds_invalid_tenant_fails_closed(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/deeds", query="tenant=bogus"))
    assert response.status == 400


def test_deeds_openapi_schema_is_explicit() -> None:
    schema = build_openapi(RouteTableImpl())["paths"]["/api/deeds"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert schema["type"] == "object"
    assert {"generatedAt", "tenant", "limit", "allianceMerged", "deeds"} <= set(schema["required"])
    assert schema["properties"]["deeds"]["type"] == "array"
