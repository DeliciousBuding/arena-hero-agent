"""Command Center /api/redeem/history route wiring tests (W44 wave 4)."""

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


def test_redeem_history_route_is_registered() -> None:
    assert ("GET", "/api/redeem/history") in RouteTable().api_route_set()


def test_redeem_history_empty_root_returns_200_empty(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/redeem/history"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"generatedAt", "records", "count"}
    assert body["records"] == []
    assert body["count"] == 0


def test_redeem_history_fixture_reads_persisted_tail(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("redeem_history_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/redeem/history"))
    assert response.status == 200
    body = _json_body(response)
    assert body["count"] == 3
    assert [record["codeMask"] for record in body["records"]] == [
        "ABC123***",
        "XYZ789***",
        "broken",
    ]
    assert body["records"][0]["status"] == "pending"


def test_redeem_history_openapi_schema_is_explicit() -> None:
    schema = _openapi_200("/api/redeem/history")
    assert schema["type"] == "object"
    assert {"generatedAt", "records", "count"} <= set(schema["required"])
    assert schema["properties"]["count"]["type"] == "integer"
