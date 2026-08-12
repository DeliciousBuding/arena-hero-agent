"""Command Center /api/audit/human/conflicts route wiring tests (W44)."""

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


def test_human_conflicts_route_is_registered() -> None:
    assert ("GET", "/api/audit/human/conflicts") in RouteTable().api_route_set()


def test_human_conflicts_empty_root_returns_200_map(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/audit/human/conflicts"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"t1", "t2", "t3", "t4"}
    assert body["t1"]["applied"] == 0


def test_human_conflicts_fixture_root_returns_200(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("audit_decisions_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/audit/human/conflicts", query="tenant=t1"))
    assert response.status == 200
    body = _json_body(response)
    assert body["tenant"] == "t1"
    assert body["applied"] == 1
    assert body["rejected"] == 1
    assert body["topRejectedReasons"][0]["reason"] == "Core is already moving"


def test_human_conflicts_window_is_clamped(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(
        ApiRequest("GET", "/api/audit/human/conflicts", query="tenant=t1&window=1")
    )
    assert response.status == 200
    assert _json_body(response)["window"] == 200


def test_human_conflicts_invalid_tenant_returns_400(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(
        ApiRequest("GET", "/api/audit/human/conflicts", query="tenant=t9")
    )
    assert response.status == 400


def test_human_conflicts_openapi_schema_is_explicit() -> None:
    doc = build_openapi(RouteTableImpl())
    schema = _schema(doc, "/api/audit/human/conflicts")
    assert "oneOf" in schema
    single = schema["oneOf"][0]
    assert set(single["properties"]) == {
        "generatedAt",
        "tenant",
        "window",
        "currentTick",
        "applied",
        "rejected",
        "rejectedRate",
        "topRejectedReasons",
        "commandKinds",
        "cachedAt",
    }
