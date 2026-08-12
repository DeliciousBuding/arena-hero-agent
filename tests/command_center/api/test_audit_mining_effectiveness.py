"""Command Center /api/audit/mining-effectiveness route wiring tests (W44)."""

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


def test_mining_effectiveness_route_is_registered() -> None:
    assert ("GET", "/api/audit/mining-effectiveness") in RouteTable().api_route_set()


def test_mining_effectiveness_empty_root_returns_200(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/audit/mining-effectiveness"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"generatedAt", "currentTick", "items", "perTenant", "global", "cachedAt"}
    assert body["items"] == []
    assert body["global"]["assigned"] == 0


def test_mining_effectiveness_fixture_root_returns_200(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("mines_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/audit/mining-effectiveness"))
    assert response.status == 200
    body = _json_body(response)
    assert body["global"]["assigned"] == 1
    assert body["global"]["open"] == 1
    assert body["perTenant"]["t1"]["assigned"] == 1


def test_mining_effectiveness_openapi_schema_is_explicit() -> None:
    doc = build_openapi(RouteTableImpl())
    schema = _schema(doc, "/api/audit/mining-effectiveness")
    assert schema["required"] == ["generatedAt", "items", "perTenant", "global"]
    assert "effectiveRate" in schema["properties"]["global"]["properties"]
