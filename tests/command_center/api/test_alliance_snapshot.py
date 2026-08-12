"""Command Center /api/alliance/snapshot route wiring tests (W20)."""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.api import (
    ApiRequest,
    ApiResponse,
    CommandCenterApp,
    RouteTable,
)


def _app(data_root: Path, **kwargs) -> CommandCenterApp:
    return CommandCenterApp(data_root=data_root, **kwargs)


def _json_body(response: ApiResponse) -> dict[str, object]:
    body = json.loads(response.body.decode("utf-8"))
    assert isinstance(body, dict)
    return body


def test_alliance_snapshot_route_is_registered() -> None:
    assert ("GET", "/api/alliance/snapshot") in RouteTable().api_route_set()


def test_alliance_snapshot_empty_root_returns_200_payload(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/alliance/snapshot"))
    assert response.status == 200
    body = _json_body(response)
    assert body["members"] == {}
    assert body["sightings"] == []
    assert body["counts"] == {
        "currentVisibleCombat": 0,
        "recentUniqueCombat": 0,
        "historicalSightingCount": 0,
        "estimatedForce": 0.0,
    }
    assert body["treasuryTenant"] == ""
    assert "generatedAt" in body
    assert "cachedAt" in body


def test_alliance_snapshot_unknown_tenant_query_is_ignored(tmp_path: Path) -> None:
    # The route has no tenant parameter; unknown query keys are ignored (TS parity).
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/alliance/snapshot", query="tenant=t5"))
    assert response.status == 200
    assert _json_body(response)["members"] == {}
