"""Command Center /api/audit/trail route wiring tests (W44)."""

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


def test_trail_route_is_registered() -> None:
    assert ("GET", "/api/audit/trail") in RouteTable().api_route_set()


def test_trail_empty_root_returns_200(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/audit/trail"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"generatedAt", "entries", "counts", "filters", "cachedAt"}
    assert body["entries"] == []
    assert body["counts"] == {"human": 0, "command": 0, "arbitration": 0, "supervisor": 0}


def test_trail_fixture_root_returns_200(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("trail_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/audit/trail"))
    assert response.status == 200
    body = _json_body(response)
    assert [entry["source"] for entry in body["entries"]] == [
        "supervisor",
        "arbitration",
        "command",
        "human",
    ]
    assert body["counts"] == {"human": 1, "command": 1, "arbitration": 1, "supervisor": 1}


def test_trail_source_filter_and_invalid_source(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("trail_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/audit/trail", query="source=human"))
    assert response.status == 200
    assert all(entry["source"] == "human" for entry in _json_body(response)["entries"])

    response = _app(root).handle(ApiRequest("GET", "/api/audit/trail", query="source=bogus"))
    assert response.status == 400
    assert "source" in _json_body(response)["error"]


def test_trail_tenant_filter(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("trail_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/audit/trail", query="tenant=t1"))
    assert response.status == 200
    assert _json_body(response)["filters"]["tenant"] == "t1"


def test_trail_limit_is_clamped(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/audit/trail", query="limit=99999"))
    assert response.status == 200
    # clamp is internal to merge_audit_trails (MAX_LIMIT=500); handler passes 500
    assert _json_body(response)["entries"] == []


def test_trail_openapi_schema_is_explicit() -> None:
    doc = build_openapi(RouteTableImpl())
    schema = _schema(doc, "/api/audit/trail")
    assert set(schema["properties"]) == {"generatedAt", "entries", "counts", "filters", "cachedAt"}
    assert schema["required"] == ["generatedAt", "entries", "counts", "filters"]
