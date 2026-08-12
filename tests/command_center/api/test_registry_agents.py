"""Command Center /api/registry/agents route wiring tests (W44 wave 3)."""

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


def test_registry_agents_route_is_registered() -> None:
    assert ("GET", "/api/registry/agents") in RouteTable().api_route_set()


def test_registry_agents_empty_root_returns_200_payload(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/registry/agents"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"generatedAt", "agents"}
    assert body["agents"] == []


def test_registry_agents_fixture_root_lists_agents_and_key_hashes(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("registry_agents_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/registry/agents"))
    assert response.status == 200
    body = _json_body(response)
    assert len(body["agents"]) == 2
    by_id = {a["agent_id"]: a for a in body["agents"]}
    assert by_id["ag-1"]["username"] == "t1-agent"
    assert by_id["ag-1"]["mode"] == "production"
    assert by_id["ag-1"]["plaintext_sim_key"] is None
    assert by_id["ag-1"]["keys"][0]["key_id"] == "key-1"
    assert by_id["ag-2"]["mode"] == "simulation"
    assert len(by_id["ag-2"]["keys"]) == 1


def test_registry_agents_openapi_schema_is_explicit() -> None:
    schema = _openapi_200("/api/registry/agents")
    assert schema["type"] == "object"
    assert {"generatedAt", "agents"} <= set(schema["required"])
    assert schema["properties"]["agents"]["type"] == "array"
