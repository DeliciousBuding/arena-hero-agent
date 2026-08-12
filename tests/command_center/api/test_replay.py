"""Command Center /api/replay route wiring tests (W44 wave 8)."""

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


def test_replay_route_is_registered() -> None:
    assert ("GET", "/api/replay") in RouteTable().api_route_set()


def test_replay_empty_root_returns_200_null_replay(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/replay"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"tenant", "generatedAt", "replay"}
    assert body["tenant"] == "t1"
    assert body["replay"] is None


def test_replay_fixture_rebuilds_trajectory(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("replay_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/replay", query="tenant=t1"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"generatedAt", "replay"}
    replay = body["replay"]
    assert replay["tenant"] == "t1"
    assert replay["runId"] == "run-1"
    assert replay["ticks"] == [100, 200]
    assert {unit["id"] for unit in replay["units"]} == {"u1"}
    assert {core["id"] for core in replay["cores"]} == {"c1"}
    assert len(replay["eventFrames"]) == 1


def test_replay_invalid_tenant_fails_closed(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/replay", query="tenant=bogus"))
    assert response.status == 400


def test_replay_openapi_schema_is_explicit() -> None:
    schema = build_openapi(RouteTableImpl())["paths"]["/api/replay"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert schema["type"] == "object"
    assert {"generatedAt", "replay"} <= set(schema["required"])
    assert schema["properties"]["replay"]["nullable"] is True
