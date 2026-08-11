"""P5-5 route parity: the Python route table must match the P5-2 snapshot.

Reuses the committed P5-2 snapshot manifest as the frozen contract: the
Python route registry is derived from it, and these tests pin the derived
table (66 API + 5 static) plus the B5D acceptance facts: only ``/api/map``
carries an ETag and every route is ``poll-json`` (no SSE/WS surface).
"""

from __future__ import annotations

import pytest

from arena_hero_agent.command_center.api import (
    MAP_CACHE_CONTROL,
    Route,
    RouteTable,
)
from scripts.snapshot_command_center import load_manifest

MANIFEST = load_manifest()
TABLE = RouteTable()


def _manifest_entry(method: str, path: str) -> dict:
    for group in ("routes", "static_routes"):
        for entry in MANIFEST[group]:
            if entry["method"] == method and entry["path"] == path:
                return entry
    raise AssertionError(f"manifest entry missing: {method} {path}")


def test_route_set_matches_snapshot_manifest() -> None:
    manifest_routes = {(r["method"], r["path"]) for r in MANIFEST["routes"]} | {
        (r["method"], r["path"]) for r in MANIFEST["static_routes"]
    }
    assert TABLE.route_set() == frozenset(manifest_routes)


def test_api_route_set_matches_snapshot_api() -> None:
    manifest_api = {(r["method"], r["path"]) for r in MANIFEST["routes"]}
    assert TABLE.api_route_set() == frozenset(manifest_api)


def test_route_counts_frozen() -> None:
    assert len(TABLE.api_routes) == 66
    assert len(TABLE.static_routes) == 5
    methods = {r.method for r in TABLE.api_routes} | {r.method for r in TABLE.static_routes}
    assert methods <= {"GET", "POST", "DELETE"}


@pytest.mark.parametrize("route", TABLE.api_routes, ids=lambda r: f"{r.method} {r.path}")
def test_api_route_metadata_matches_manifest(route: Route) -> None:
    entry = _manifest_entry(route.method, route.path)
    assert route.tenant_param == entry["tenant_param"]
    assert route.etag == entry["etag"]
    assert route.cache == entry["cache"]
    assert route.stream_kind == entry["stream_kind"]
    assert route.write_semantics == entry["write_semantics"]
    assert route.query == tuple(entry["query"])
    assert route.notes == entry["notes"]


@pytest.mark.parametrize("route", TABLE.static_routes, ids=lambda r: f"{r.method} {r.path}")
def test_static_route_metadata_matches_manifest(route: Route) -> None:
    entry = _manifest_entry(route.method, route.path)
    assert route.cache == entry["cache"]
    assert route.stream_kind == entry["stream_kind"]
    assert route.write_semantics == "read-only"


def test_etag_snapshot_consistent() -> None:
    """Only /api/map carries an HTTP ETag; all routes are poll-json."""
    etag_routes = [r for r in TABLE.api_routes if r.etag is not None]
    assert [r.path for r in etag_routes] == ["/api/map"]
    map_route = next(r for r in TABLE.api_routes if r.path == "/api/map")
    assert map_route.etag == "W/<map-sig>"
    assert map_route.cache == MAP_CACHE_CONTROL
    for route in TABLE.api_routes:
        assert route.stream_kind == "poll-json"
    for route in TABLE.static_routes:
        assert route.stream_kind == "poll-json"


def test_no_sse_or_websocket_surface() -> None:
    """B5D: the Command Center surface is poll-json; WS lives in arena-hero-ts."""
    assert all(r.stream_kind == "poll-json" for r in TABLE.api_routes)
    assert not any("/ws" in r.path or "socket" in r.path for r in TABLE.api_routes)


def test_match_exact() -> None:
    match = TABLE.match("GET", "/api/tenants")
    assert match is not None
    assert match.route.path == "/api/tenants"
    assert match.path_params == {}


def test_match_path_param() -> None:
    match = TABLE.match("DELETE", "/api/registry/agents/agent-7")
    assert match is not None
    assert match.route.path == "/api/registry/agents/:id"
    assert match.path_params == {":id": "agent-7"}
    assert TABLE.match("DELETE", "/api/registry/agents/a/b") is None
    assert TABLE.match("DELETE", "/api/registry/agents/") is None


def _matched(path: str, method: str = "GET"):
    match = TABLE.match(method, path)
    assert match is not None
    return match


def test_match_does_not_shadow_exact() -> None:
    assert TABLE.match("GET", "/api/registry/agents") is not None
    assert _matched("/api/registry/agents").route.path == "/api/registry/agents"
    assert _matched("/api/registry/agents", "POST").route.path == "/api/registry/agents"


def test_match_static_exact_and_prefix() -> None:
    assert _matched("/").route.path == "/"
    assert _matched("/app").route.path == "/app"
    assert _matched("/style.css").route.path == "/style.css"
    assert _matched("/app/main.js").route.path == "/app/*"
    assert _matched("/assets/logo.svg").route.path == "/assets/*"
    assert TABLE.match("GET", "/app-other") is None


def test_match_unknown_method_or_path() -> None:
    assert TABLE.match("PUT", "/api/tenants") is None
    assert TABLE.match("GET", "/api/does-not-exist") is None
    assert TABLE.match("GET", "/api/map/extra") is None


def test_default_tenant_matches_legacy() -> None:
    stream = next(r for r in TABLE.api_routes if r.path == "/api/stream")
    lod = next(r for r in TABLE.api_routes if r.path == "/api/map/lod")
    command = next(r for r in TABLE.api_routes if r.path == "/api/command")
    tenants = next(r for r in TABLE.api_routes if r.path == "/api/tenants")
    assert TABLE.default_tenant(stream) == "t1"
    assert TABLE.default_tenant(lod) == "all"
    assert TABLE.default_tenant(command) == "t1"
    assert TABLE.default_tenant(tenants) is None
