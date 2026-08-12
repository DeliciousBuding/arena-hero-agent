"""Command Center /api/events route wiring tests (W44 wave 4)."""

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


def test_events_route_is_registered() -> None:
    assert ("GET", "/api/events") in RouteTable().api_route_set()


def test_events_empty_root_returns_200_payload(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/events"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"tenant", "generatedAt", "events"}
    assert body["tenant"] == "t1"
    assert body["events"] == []


def test_events_fixture_filters_and_sorts(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("worlds_plan_events_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/events"))
    assert response.status == 200
    body = _json_body(response)
    events = body["events"]
    # CUSTOM_UNKNOWN filtered; after.state preferred over before.state; tick desc
    kinds = [event["kind"] for event in events]
    assert kinds == [
        "DEPOSIT_SUCCEEDED",
        "UNIT_MOVE_FAILED",
        "WAIT",
        "HARVEST_SUCCEEDED",
        "SHOT_HIT",
    ]
    assert all("CUSTOM_UNKNOWN" not in event["kind"] for event in events)
    first = events[0]
    assert first["tick"] == 110
    assert first["amount"] == 5
    assert events[-1]["tick"] == 100
    assert events[-1]["actor"] == "w2"
    assert events[-1]["amount"] == 12  # values.damage fallback
    assert events[-1]["target"] == "e1"


def test_events_n_clamps(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("worlds_plan_events_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/events", query="n=2"))
    body = _json_body(response)
    assert len(body["events"]) == 2
    response = _app(root).handle(ApiRequest("GET", "/api/events", query="n=9999"))
    assert len(_json_body(response)["events"]) == 5


def test_events_openapi_schema_is_explicit() -> None:
    schema = _openapi_200("/api/events")
    assert schema["type"] == "object"
    assert {"tenant", "generatedAt", "events"} <= set(schema["required"])
    assert schema["properties"]["events"]["type"] == "array"
