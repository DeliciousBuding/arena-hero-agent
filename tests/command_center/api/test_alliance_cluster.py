"""Command Center /api/alliance/cluster route wiring tests (W44 wave 3)."""

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


def test_alliance_cluster_route_is_registered() -> None:
    assert ("GET", "/api/alliance/cluster") in RouteTable().api_route_set()


def test_alliance_cluster_empty_root_returns_200_payload(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/alliance/cluster"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"generatedAtMs", "groups", "members", "summary"}
    assert body["groups"] == []
    assert body["members"] == []
    assert body["summary"] == {
        "memberCount": 0,
        "groupCount": 0,
        "isolatedCount": 0,
        "maxCohesion": 0,
        "avgCohesion": 0,
    }


def test_alliance_cluster_fixture_root_returns_two_clusters(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("alliance_cluster_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/alliance/cluster"))
    assert response.status == 200
    body = _json_body(response)
    assert body["summary"]["memberCount"] == 4
    assert body["summary"]["groupCount"] == 2
    assert body["summary"]["isolatedCount"] == 0
    by_tenant = {m["tenantId"]: m for m in body["members"]}
    assert by_tenant["t1"]["clusterId"] == by_tenant["t3"]["clusterId"]
    assert by_tenant["t2"]["clusterId"] == by_tenant["t4"]["clusterId"]
    assert by_tenant["t1"]["clusterId"] != by_tenant["t2"]["clusterId"]
    assert by_tenant["t1"]["military"] == 3
    assert by_tenant["t1"]["workers"] == 2


def test_alliance_cluster_openapi_schema_is_explicit() -> None:
    schema = _openapi_200("/api/alliance/cluster")
    assert schema["type"] == "object"
    assert {"generatedAtMs", "groups", "members", "summary"} <= set(schema["required"])
    assert {
        "memberCount",
        "groupCount",
        "isolatedCount",
        "maxCohesion",
        "avgCohesion",
    } <= set(schema["properties"]["summary"]["properties"])
