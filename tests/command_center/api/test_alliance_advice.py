"""Command Center /api/alliance/advice route wiring tests (W25)."""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.api import (
    ApiRequest,
    ApiResponse,
    CommandCenterApp,
    RouteTable,
)
from arena_hero_agent.command_center.api.openapi import build_openapi
from arena_hero_agent.command_center.api.routes import RouteTable as RouteTableImpl


def _app(data_root: Path, **kwargs) -> CommandCenterApp:
    return CommandCenterApp(data_root=data_root, **kwargs)


def _json_body(response: ApiResponse) -> dict[str, object]:
    body = json.loads(response.body.decode("utf-8"))
    assert isinstance(body, dict)
    return body


def test_alliance_advice_route_is_registered() -> None:
    assert ("GET", "/api/alliance/advice") in RouteTable().api_route_set()


def test_alliance_advice_empty_root_returns_200_payload(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/alliance/advice"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {
        "generatedAt",
        "advice",
        "summary",
        "dedupCount",
        "avgConfidence",
        "cachedAt",
    }
    assert body["advice"] == []
    assert body["summary"] == {"critical": 0, "high": 0, "medium": 0, "info": 0}
    assert body["dedupCount"] == 0
    assert body["avgConfidence"] == 0
    assert body["generatedAt"] == body["cachedAt"]


def test_alliance_advice_openapi_schema_is_non_empty() -> None:
    doc = build_openapi(RouteTableImpl())
    schema = doc["paths"]["/api/alliance/advice"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert schema["type"] == "object"
    required = schema["required"]
    assert {
        "generatedAt",
        "advice",
        "summary",
        "dedupCount",
        "avgConfidence",
        "cachedAt",
    } <= set(required)
    advice_items = schema["properties"]["advice"]["items"]
    assert set(advice_items["required"]) >= {
        "severity",
        "category",
        "tenant",
        "title",
        "detail",
        "action",
        "weight",
        "confidence",
        "evidence",
        "at",
    }


def test_alliance_advice_unknown_tenant_query_is_ignored(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/alliance/advice", query="tenant=t5"))
    assert response.status == 200
    body = _json_body(response)
    assert body["advice"] == []
