"""Command Center /api/survey/enemy-cores route wiring tests (W44 wave 4)."""

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


def test_enemy_cores_route_is_registered() -> None:
    assert ("GET", "/api/survey/enemy-cores") in RouteTable().api_route_set()


def test_enemy_cores_empty_root_returns_200_empty(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/survey/enemy-cores"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"generatedAt", "currentTick", "cores"}
    assert body["currentTick"] == 0
    assert body["cores"] == []


def test_enemy_cores_fixture_aggregates_states(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("enemy_cores_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/survey/enemy-cores"))
    assert response.status == 200
    body = _json_body(response)
    assert body["currentTick"] == 5950
    cores = body["cores"]
    assert [core["owner"] for core in cores] == ["enemyA", "enemyB", "enemyC", "enemyD"]
    by_owner = {core["owner"]: core for core in cores}
    # enemyA relocated across two locations, latest position wins, near t1 core -> high
    enemy_a = by_owner["enemyA"]
    assert enemy_a["status"] == "RELOCATED"
    assert (enemy_a["x"], enemy_a["y"]) == (20, 10)
    assert enemy_a["locationCount"] == 2
    assert enemy_a["threat"] == "high"
    assert by_owner["enemyB"]["status"] == "ACTIVE"
    assert by_owner["enemyB"]["threat"] == "high"
    assert by_owner["enemyC"]["status"] == "ACTIVE"
    # enemyD last seen far in the past -> STALE, threat never high
    enemy_d = by_owner["enemyD"]
    assert enemy_d["status"] == "STALE"
    assert enemy_d["threat"] == "low"
    assert enemy_d["lastSeenTick"] == 100
    # STALE sorts after active cores
    assert cores[-1]["owner"] == "enemyD"


def test_enemy_cores_openapi_schema_is_explicit() -> None:
    schema = _openapi_200("/api/survey/enemy-cores")
    assert schema["type"] == "object"
    assert {"generatedAt", "currentTick", "cores"} <= set(schema["required"])
    assert schema["properties"]["cores"]["type"] == "array"
