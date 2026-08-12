"""Command Center /api/audit/decisions + /api/audit/decisions/trend route wiring tests (W44)."""

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


def test_decision_routes_are_registered() -> None:
    assert ("GET", "/api/audit/decisions") in RouteTable().api_route_set()
    assert ("GET", "/api/audit/decisions/trend") in RouteTable().api_route_set()


def test_decisions_empty_root_returns_200_map(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/audit/decisions"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"t1", "t2", "t3", "t4"}
    assert body["t1"]["decision"]["records"] == 0


def test_decisions_fixture_root_returns_200(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("audit_decisions_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/audit/decisions", query="tenant=t1"))
    assert response.status == 200
    body = _json_body(response)
    assert body["tenant"] == "t1"
    assert body["decision"]["records"] == 2
    assert body["outcome"]["coreDeltaSum"] == 3


def test_decisions_window_is_clamped(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("audit_decisions_basic"), tmp_path)
    response = _app(root).handle(
        ApiRequest("GET", "/api/audit/decisions", query="tenant=t1&window=100000")
    )
    assert response.status == 200
    assert _json_body(response)["window"] == 20_000


def test_decisions_invalid_tenant_returns_400(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/audit/decisions", query="tenant=t5"))
    assert response.status == 400
    assert "tenant" in _json_body(response)["error"]


def test_decisions_trend_fixture_root_returns_200(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("audit_decisions_basic"), tmp_path)
    response = _app(root).handle(
        ApiRequest("GET", "/api/audit/decisions/trend", query="tenant=t1&window=50&steps=3")
    )
    assert response.status == 200
    body = _json_body(response)
    assert body["tenant"] == "t1"
    assert body["steps"] == 3
    assert len(body["trend"]) == 3


def test_decisions_trend_steps_are_clamped(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(
        ApiRequest("GET", "/api/audit/decisions/trend", query="tenant=t1&steps=99")
    )
    assert response.status == 200
    assert _json_body(response)["steps"] == 12


def test_decisions_trend_all_tenant_returns_400(tmp_path: Path) -> None:
    # tenant_param=tN: "all" is rejected (TS "趋势仅支持单租户").
    response = _app(tmp_path).handle(
        ApiRequest("GET", "/api/audit/decisions/trend", query="tenant=all")
    )
    assert response.status == 400


def test_decisions_openapi_schema_is_explicit() -> None:
    doc = build_openapi(RouteTableImpl())
    schema = _schema(doc, "/api/audit/decisions")
    assert "oneOf" in schema
    single = schema["oneOf"][0]
    assert "decision" in single["properties"]
    assert "outcome" in single["properties"]
    trend = _schema(doc, "/api/audit/decisions/trend")
    assert trend["required"] == ["generatedAt", "tenant", "window", "steps", "trend"]
    assert "trend" in trend["properties"]
    assert "steps" in trend["properties"]
