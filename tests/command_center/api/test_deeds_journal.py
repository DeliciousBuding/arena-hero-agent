"""Command Center /api/deeds/journal route wiring tests (W44 wave 8)."""

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

FIXTURES = Path(__file__).parents[1] / "projections" / "fixtures"


def _app(data_root: Path) -> CommandCenterApp:
    return CommandCenterApp(data_root=data_root)


def _json_body(response: ApiResponse) -> dict[str, Any]:
    body = json.loads(response.body.decode("utf-8"))
    assert isinstance(body, dict)
    return body


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_journal_route_is_registered() -> None:
    assert ("GET", "/api/deeds/journal") in RouteTable().api_route_set()


def test_journal_empty_root_returns_200(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/deeds/journal"))
    assert response.status == 200
    body = _json_body(response)
    assert body["tenant"] == "all"
    assert body["headline"] is None
    assert body["counts"] == {}
    assert body["deeds"] == []
    assert body["filters"]["minStar"] == 1  # 0 clamps to 1 (TS Math.round semantics)


def test_journal_window_clamps(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(
        ApiRequest("GET", "/api/deeds/journal", query="tenant=t1&window=100")
    )
    body = _json_body(response)
    assert body["windowTicks"] == 500  # 100 -> 500 lower bound


def test_journal_filters_and_groups(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("deeds_journal_basic"), tmp_path)
    response = _app(root).handle(
        ApiRequest("GET", "/api/deeds/journal", query="tenant=t1&category=harvest,deposit")
    )
    assert response.status == 200
    body = _json_body(response)
    assert set(body["groups"]) <= {"harvest", "deposit"}
    assert body["counts"].get("spawn", 0) == 0


def test_journal_min_star_filters(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("deeds_journal_basic"), tmp_path)
    response = _app(root).handle(
        ApiRequest("GET", "/api/deeds/journal", query="tenant=t1&minStar=3")
    )
    body = _json_body(response)
    assert all(deed["star"] >= 3 for deed in body["deeds"])


def test_journal_invalid_tenant_fails_closed(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/deeds/journal", query="tenant=bogus"))
    assert response.status == 400


def test_journal_openapi_schema_is_explicit() -> None:
    schema = build_openapi(RouteTableImpl())["paths"]["/api/deeds/journal"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]
    assert schema["type"] == "object"
    assert {
        "generatedAt",
        "tenant",
        "windowTicks",
        "headline",
        "counts",
        "narrative",
        "deeds",
    } <= set(schema["required"])
