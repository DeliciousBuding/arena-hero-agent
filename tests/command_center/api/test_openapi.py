"""OpenAPI document generation tests (P5-5).

The generated OpenAPI 3.1 document is the contract the browser frontend types
and client code are generated from. These tests pin: path coverage equals the
P5-2 snapshot API route set, parameter translation (query/path), the weak-ETag
304 handling of ``/api/map``, per-operation ``x-command-center`` snapshot
metadata, and deterministic regeneration of the committed artifact.
"""

from __future__ import annotations

import json

from arena_hero_agent.command_center.api import (
    ETAG_PREFIX,
    ETAG_SUFFIX,
    MAP_CACHE_CONTROL,
    RouteTable,
    build_openapi,
)
from scripts.emit_openapi import OPENAPI_PATH, regenerate

TABLE = RouteTable()
DOC = build_openapi(TABLE)


def test_openapi_version_and_info() -> None:
    assert DOC["openapi"] == "3.1.0"
    assert DOC["info"]["title"] == "Arena Hero Command Center API"
    assert DOC["info"]["version"]
    assert "poll-json" in DOC["info"]["description"]


def test_paths_cover_api_route_set() -> None:
    path_keys = set(DOC["paths"])
    expected = {route.path.replace(":id", "{id}") for route in TABLE.api_routes}
    assert path_keys == expected


def test_methods_per_path() -> None:
    """GET+POST on /api/registry/agents and GET+DELETE on /api/command share a path."""
    assert set(DOC["paths"]["/api/registry/agents"]) == {"get", "post"}
    assert set(DOC["paths"]["/api/command"]) == {"post", "delete"}
    assert set(DOC["paths"]["/api/commands"]) == {"get"}
    assert set(DOC["paths"]["/api/registry/agents/{id}"]) == {"delete"}


def test_operation_id_unique_and_deterministic() -> None:
    ids = [
        operation["operationId"] for path in DOC["paths"].values() for operation in path.values()
    ]
    assert len(ids) == 66
    assert len(set(ids)) == 66
    assert "getMap" in ids
    assert "deleteRegistryAgentsId" in ids
    assert "getSurveyEnemyCores" in ids


def test_query_parameters_present() -> None:
    stream = DOC["paths"]["/api/stream"]["get"]
    names = [param["name"] for param in stream["parameters"]]
    assert names == ["tenant", "n"]
    n_param = next(param for param in stream["parameters"] if param["name"] == "n")
    assert n_param["schema"] == {"type": "integer", "minimum": 1, "maximum": 200}
    tenant_param = next(param for param in stream["parameters"] if param["name"] == "tenant")
    assert tenant_param["schema"]["enum"] == ["t1", "t2", "t3", "t4"]


def test_tenant_enum_allows_all_for_all_tn() -> None:
    lod = DOC["paths"]["/api/map/lod"]["get"]
    tenant_param = next(param for param in lod["parameters"] if param["name"] == "tenant")
    assert tenant_param["schema"]["enum"] == ["all", "t1", "t2", "t3", "t4"]


def test_path_parameter_id() -> None:
    operation = DOC["paths"]["/api/registry/agents/{id}"]["delete"]
    path_params = [param for param in operation["parameters"] if param["in"] == "path"]
    assert path_params == [
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
    ]


def test_map_etag_and_304_documented() -> None:
    operation = DOC["paths"]["/api/map"]["get"]
    assert operation["responses"]["304"]["description"]
    assert operation["responses"]["200"]["headers"]["ETag"]["schema"]["example"] == (
        f"{ETAG_PREFIX}<map-sig>{ETAG_SUFFIX}"
    )
    assert (
        operation["responses"]["200"]["headers"]["Cache-Control"]["schema"]["example"]
        == MAP_CACHE_CONTROL
    )
    assert "304" in operation["responses"]


def test_no_other_route_documents_etag() -> None:
    for path, operations in DOC["paths"].items():
        if path == "/api/map":
            continue
        for operation in operations.values():
            assert "304" not in operation["responses"]
            assert "headers" not in operation["responses"].get("200", {})


def test_write_routes_preserved_in_x_command_center() -> None:
    write_routes = DOC["x-command-center"]["write_routes"]
    expected = {
        (route.method, route.path)
        for route in TABLE.api_routes
        if route.write_semantics != "read-only"
    }
    assert {(item["method"], item["path"]) for item in write_routes} == expected
    assert len(write_routes) == len(expected)


def test_operation_metadata_matches_route() -> None:
    operation = DOC["paths"]["/api/stream"]["get"]
    meta = operation["x-command-center"]
    route = next(r for r in TABLE.api_routes if r.path == "/api/stream")
    assert meta["stream_kind"] == route.stream_kind
    assert meta["cache"] == route.cache
    assert meta["write_semantics"] == route.write_semantics
    assert meta["tenant_param"] == route.tenant_param
    assert meta["etag"] == route.etag
    assert meta["query"] == list(route.query)


def test_x_command_center_summary() -> None:
    summary = DOC["x-command-center"]
    assert summary["api_route_count"] == 66
    assert summary["static_route_count"] == 5
    assert summary["stream_kind"] == "poll-json"
    assert summary["etag_routes"] == ["/api/map"]
    assert len(summary["static_routes"]) == 5


def _committed_document() -> str:
    return OPENAPI_PATH.read_text(encoding="utf-8")


def test_committed_document_regenerates_identically() -> None:
    assert _committed_document() == regenerate()


def test_every_200_response_schema_is_non_empty() -> None:
    """No operation may document an empty (``unknown``) response type anymore."""
    for path, operations in DOC["paths"].items():
        for method, operation in operations.items():
            schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
            assert schema, f"{method} {path} has an empty 200 schema"


def test_main_endpoint_response_schemas_have_real_fields() -> None:
    stream = DOC["paths"]["/api/stream"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert set(stream["properties"]) == {"tenant", "generatedAt", "rows"}
    assert stream["required"] == ["tenant", "generatedAt", "rows"]

    map_lod = DOC["paths"]["/api/map/lod"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert "chunkSize" in map_lod["properties"]
    assert "chunks" in map_lod["properties"]

    survey = DOC["paths"]["/api/alliance/survey"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert "tenantSummaries" in survey["properties"]
    assert "consensusResources" in survey["properties"]

    decisions = DOC["paths"]["/api/audit/decisions"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert "decision" in decisions["properties"]
    assert "outcome" in decisions["properties"]

    workers = DOC["paths"]["/api/audit/workers"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert "totals" in workers["properties"]
    assert "tenants" in workers["properties"]

    trail = DOC["paths"]["/api/audit/trail"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert set(trail["properties"]) == {"generatedAt", "entries", "counts", "filters", "cachedAt"}

    snapshot = DOC["paths"]["/api/alliance/snapshot"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert set(snapshot["properties"]) == {
        "generatedAt",
        "cachedAt",
        "currentTick",
        "revision",
        "members",
        "sightings",
        "counts",
        "intel",
        "threat",
        "threatSummaries",
        "treasuryTenant",
        "leaderboardAggression",
    }
    assert snapshot["required"] == [
        "generatedAt",
        "cachedAt",
        "currentTick",
        "revision",
        "members",
        "sightings",
        "counts",
        "intel",
        "threat",
        "threatSummaries",
        "treasuryTenant",
        "leaderboardAggression",
    ]
    assert "maxDirect" in snapshot["properties"]["threat"]["properties"]
    assert snapshot["properties"]["threat"]["properties"]["maxDirect"]["nullable"] is True


def test_committed_document_is_valid_json_object() -> None:
    parsed = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    assert parsed["openapi"] == "3.1.0"
    assert len(parsed["paths"]) == len(DOC["paths"])
