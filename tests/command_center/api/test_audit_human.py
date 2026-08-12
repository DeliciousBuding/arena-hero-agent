"""Command Center /api/audit/human route wiring tests (W44 wave 3)."""

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


def test_audit_human_route_is_registered() -> None:
    assert ("GET", "/api/audit/human") in RouteTable().api_route_set()


def test_audit_human_empty_root_returns_200_payload(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/audit/human"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"generatedAt", "tenant", "count", "records"}
    assert body["tenant"] == "t1"
    assert body["count"] == 0
    assert body["records"] == []


def test_audit_human_fixture_root_defaults_t1_and_filters(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("audit_human_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/audit/human"))
    assert response.status == 200
    body = _json_body(response)
    assert body["tenant"] == "t1"
    assert body["count"] == 2
    assert [r["kind"] for r in body["records"]] == ["goal", "command"]
    assert body["records"][0]["tenant"] == "t1"


def test_audit_human_fixture_root_tenant_filter(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("audit_human_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/audit/human", query="tenant=t2&limit=1"))
    assert response.status == 200
    body = _json_body(response)
    assert body["tenant"] == "t2"
    assert body["count"] == 1
    assert body["records"][0]["kind"] == "delete"


def test_audit_human_fails_closed_on_all_and_invalid_tenants(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("audit_human_basic"), tmp_path)
    for query in ("tenant=all", "tenant=zz"):
        response = _app(root).handle(ApiRequest("GET", "/api/audit/human", query=query))
        assert response.status == 400
        body = _json_body(response)
        assert "error" in body


def test_audit_human_openapi_schema_is_explicit() -> None:
    schema = _openapi_200("/api/audit/human")
    assert schema["type"] == "object"
    assert {"generatedAt", "tenant", "count", "records"} <= set(schema["required"])
    assert schema["properties"]["records"]["type"] == "array"
