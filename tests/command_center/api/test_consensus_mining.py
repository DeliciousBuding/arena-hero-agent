"""Command Center /api/alliance/survey/mining route wiring tests (W44)."""

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


def test_consensus_mining_route_is_registered() -> None:
    assert ("GET", "/api/alliance/survey/mining") in RouteTable().api_route_set()


def test_consensus_mining_empty_root_returns_200_payload(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/alliance/survey/mining"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {
        "generatedAt",
        "resources",
        "summary",
        "colors",
        "tenantSummaries",
        "cachedAt",
    }
    assert body["resources"] == []
    assert body["summary"] == {
        "assigned": 0,
        "open": 0,
        "stale": 0,
        "harvested": 0,
        "harvestedByOther": 0,
        "highThreat": 0,
        "topStale": [],
    }
    assert body["generatedAt"] == body["cachedAt"]


def test_consensus_mining_fixture_root_returns_200_payload(tmp_path: Path) -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "projections"
            / "fixtures"
            / "cc_wiring"
            / "consensus_mining_basic.json"
        ).read_text(encoding="utf-8")
    )
    materialize_advice_data_root(fixture, tmp_path)
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/alliance/survey/mining"))
    assert response.status == 200
    body = _json_body(response)
    by_cell = {item["cell"]: item for item in body["resources"]}
    assert set(by_cell) == {"10,10", "170,170"}
    assert body["summary"]["assigned"] == 2
    assert body["summary"]["highThreat"] == 1


def test_consensus_mining_openapi_schema_is_non_empty() -> None:
    doc = build_openapi(RouteTableImpl())
    schema = doc["paths"]["/api/alliance/survey/mining"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert schema["type"] == "object"
    required = schema["required"]
    assert {
        "generatedAt",
        "resources",
        "summary",
        "colors",
        "tenantSummaries",
        "cachedAt",
    } <= set(required)
    assert set(schema["properties"]["summary"]["required"]) >= {
        "assigned",
        "open",
        "stale",
        "harvested",
        "harvestedByOther",
        "highThreat",
        "topStale",
    }
