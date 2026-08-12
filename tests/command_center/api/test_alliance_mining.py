"""Command Center /api/alliance/mining route wiring tests (W44 wave 3)."""

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


def test_alliance_mining_route_is_registered() -> None:
    assert ("GET", "/api/alliance/mining") in RouteTable().api_route_set()


def test_alliance_mining_empty_root_returns_200_payload(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/alliance/mining"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {
        "generatedAt",
        "currentTick",
        "assignments",
        "perTenant",
        "unassigned",
        "global",
        "cachedAt",
    }
    assert body["assignments"] == []
    assert body["unassigned"] == []
    assert body["global"] == {
        "totalCandidates": 0,
        "assigned": 0,
        "shared": 0,
        "conflict": 0,
        "unassigned": 0,
    }
    assert body["generatedAt"] == body["cachedAt"]


def test_alliance_mining_fixture_root_assigns_nearest_observer(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("alliance_mining_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/alliance/mining"))
    assert response.status == 200
    body = _json_body(response)
    by_cell = {item["cell"]: item for item in body["assignments"]}
    assert set(by_cell) == {"10,10", "170,170"}
    assert by_cell["10,10"]["assignedTenant"] == "t1"
    assert by_cell["10,10"]["shared"] is True
    assert by_cell["170,170"]["assignedTenant"] == "t2"
    assert by_cell["170,170"]["shared"] is False
    assert body["global"]["totalCandidates"] == 2
    assert body["global"]["assigned"] == 2
    assert body["global"]["shared"] == 1
    assert body["perTenant"]["t1"]["assigned"] == 1
    assert body["perTenant"]["t2"]["assigned"] == 1
    assert body["perTenant"]["t1"]["workers"] == 2


def test_alliance_mining_openapi_schema_is_explicit() -> None:
    schema = _openapi_200("/api/alliance/mining")
    assert schema["type"] == "object"
    assert {"generatedAt", "assignments", "perTenant", "unassigned", "global", "cachedAt"} <= set(
        schema["required"]
    )
    assert {
        "totalCandidates",
        "assigned",
        "shared",
        "conflict",
        "unassigned",
    } <= set(schema["properties"]["global"]["properties"])
