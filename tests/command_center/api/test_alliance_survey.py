"""Command Center /api/alliance/survey route wiring tests (W44).

Covers GET /api/alliance/survey (default full view + ?view=consensus
lightweight mode) and GET /api/alliance/survey/arbitrations: empty-root
fail-open, fixture payload, and explicit OpenAPI 200 schemas.
"""

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


def test_survey_route_is_registered() -> None:
    assert ("GET", "/api/alliance/survey") in RouteTable().api_route_set()
    assert ("GET", "/api/alliance/survey/arbitrations") in RouteTable().api_route_set()


def test_survey_empty_root_returns_200_payload(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/alliance/survey"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {
        "generatedAt",
        "colors",
        "tenantSummaries",
        "enemyCores",
        "resources",
        "obstacles",
        "chunks",
        "lifecycle",
        "conflicts",
        "consensusResources",
        "consensusCores",
        "consensusChunks",
        "cachedAt",
    }
    assert body["consensusResources"] == []
    assert body["generatedAt"] == body["cachedAt"]


def test_survey_fixture_root_returns_200_payload(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("alliance_survey_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/alliance/survey"))
    assert response.status == 200
    body = _json_body(response)
    assert body["tenantSummaries"]["t1"]["resources"] == 2
    assert body["tenantSummaries"]["t2"]["resources"] == 1


def test_survey_view_consensus_returns_subset(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("alliance_survey_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/alliance/survey", query="view=consensus"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {
        "generatedAt",
        "colors",
        "tenantSummaries",
        "conflicts",
        "consensusResources",
        "consensusCores",
        "consensusChunks",
        "cachedAt",
    }
    assert body["consensusResources"]
    assert "resources" not in body
    assert "obstacles" not in body


def test_arbitrations_empty_root_returns_200_payload(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/alliance/survey/arbitrations"))
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"generatedAt", "arbitrations"}
    assert body["arbitrations"] == []


def test_arbitrations_fixture_root_returns_effective_list(tmp_path: Path) -> None:
    root = materialize_advice_data_root(_fixture("alliance_survey_basic"), tmp_path)
    response = _app(root).handle(ApiRequest("GET", "/api/alliance/survey/arbitrations"))
    assert response.status == 200
    body = _json_body(response)
    assert body["arbitrations"] == [
        {
            "cell": "1,1",
            "winnerTenant": "t2",
            "createdAt": "2025-07-08T18:30:00Z",
            "note": "t2 latest",
        }
    ]


def test_survey_openapi_schema_is_explicit() -> None:
    doc = build_openapi(RouteTableImpl())
    schema = doc["paths"]["/api/alliance/survey"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert set(schema["properties"]) == {
        "generatedAt",
        "colors",
        "tenantSummaries",
        "enemyCores",
        "resources",
        "obstacles",
        "chunks",
        "lifecycle",
        "conflicts",
        "consensusResources",
        "consensusCores",
        "consensusChunks",
        "cachedAt",
    }
    assert schema["required"] == ["generatedAt"]


def test_arbitrations_openapi_schema_is_explicit() -> None:
    doc = build_openapi(RouteTableImpl())
    schema = doc["paths"]["/api/alliance/survey/arbitrations"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert schema["required"] == ["generatedAt", "arbitrations"]
    assert schema["properties"]["arbitrations"]["type"] == "array"


def test_survey_route_unwired_no_longer_returns_501(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/alliance/survey"))
    assert response.status == 200
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/alliance/survey/arbitrations"))
    assert response.status == 200
