"""OpenAPI 3.1 document generation from the route registry (P5-5).

The generated document is the contract the browser frontend types and client
code are generated from (``apps/command-center-web/README.md``): every API
route from the P5-2 snapshot becomes a path operation with its query/path
parameters and responses, the weak-ETag/304 handling of ``/api/map`` is
documented with headers, and the snapshot facts (cache, stream kind, write
semantics, tenant parameter, ETag) are preserved per operation under
``x-command-center``. Serialization is deterministic (sorted keys) so the
committed artifact regenerates byte-identical.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from typing import Any

from .routes import ETAG_PREFIX, ETAG_SUFFIX, MAP_CACHE_CONTROL, Route, RouteTable, int_query_keys

OPENAPI_VERSION = "3.1.0"
DEFAULT_API_VERSION = "0.1.0"

_ETAG_HEADERS = {
    "ETag": {"schema": {"type": "string", "example": f"{ETAG_PREFIX}<map-sig>{ETAG_SUFFIX}"}},
    "Cache-Control": {"schema": {"type": "string", "example": MAP_CACHE_CONTROL}},
}

_TENANT_ENUMS = {
    "all|tN": ["all", "t1", "t2", "t3", "t4"],
    "t1": ["t1", "t2", "t3", "t4"],
    "tN": ["t1", "t2", "t3", "t4"],
}

__all__ = ["DEFAULT_API_VERSION", "OPENAPI_VERSION", "build_openapi", "openapi_json"]


def _package_version() -> str:
    try:
        return importlib.metadata.version("arena-hero-agent")
    except importlib.metadata.PackageNotFoundError:
        return DEFAULT_API_VERSION


def _operation_id(route: Route) -> str:
    """Deterministic camelCase operation id (``getMap``, ``deleteRegistryAgentsId``)."""
    segments: list[str] = []
    for segment in route.path.split("/"):
        if not segment or segment == "api":
            continue
        for token in re.split(r"[^A-Za-z0-9]+", segment):
            if token:
                segments.append(token[0].upper() + token[1:])
    return route.method.lower() + "".join(segments)


def _query_parameters(route: Route) -> list[dict[str, Any]]:
    int_keys = int_query_keys()
    parameters: list[dict[str, Any]] = []
    for name in route.query:
        if name == "tenant":
            enum = _TENANT_ENUMS.get(str(route.tenant_param))
            schema: dict[str, Any] = (
                {"type": "string", "enum": enum} if enum else {"type": "string"}
            )
        elif name in int_keys:
            schema = {"type": "integer"}
            if name in {"n", "limit", "window", "steps"}:
                schema["minimum"] = 1
            if name == "n":
                schema["maximum"] = 200
        else:
            schema = {"type": "string"}
        parameters.append({"name": name, "in": "query", "required": False, "schema": schema})
    return parameters


def _path_parameters(route: Route) -> list[dict[str, Any]]:
    if ":id" not in route.path:
        return []
    return [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]


def _responses(route: Route) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "200": {"description": "OK", "content": {"application/json": {"schema": {}}}},
        "400": {"description": "Invalid query parameters or tenant"},
        "404": {"description": "Route not found"},
        "500": {"description": "Internal error"},
    }
    if route.etag is not None:
        responses["200"]["headers"] = _ETAG_HEADERS
        responses["304"] = {
            "description": "Not Modified: If-None-Match equals the weak ETag",
            "headers": _ETAG_HEADERS,
        }
    return responses


def _operation(route: Route) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "operationId": _operation_id(route),
        "summary": route.notes or f"{route.method} {route.path}",
        "parameters": [*_query_parameters(route), *_path_parameters(route)],
        "responses": _responses(route),
        "x-command-center": {
            "stream_kind": route.stream_kind,
            "cache": route.cache,
            "write_semantics": route.write_semantics,
            "tenant_param": route.tenant_param,
            "etag": route.etag,
            "query": list(route.query),
        },
    }
    return operation


def build_openapi(table: RouteTable) -> dict[str, Any]:
    """Build the OpenAPI 3.1 document for a route table."""
    paths: dict[str, Any] = {}
    for route in table.api_routes:
        path_key = route.path.replace(":id", "{id}")
        paths.setdefault(path_key, {})[route.method.lower()] = _operation(route)
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "Arena Hero Command Center API",
            "version": _package_version(),
            "description": (
                "Loopback-only JSON polling API for the Arena Hero Command Center "
                f"({len(table.api_routes)} API + {len(table.static_routes)} static routes). "
                "Every route is request/response poll-json; there is no SSE or WebSocket "
                "surface. Only /api/map uses a weak HTTP ETag (304 when If-None-Match "
                "matches) with cache-control public, max-age=2. The WebSocket wire "
                "contract lives in the arena-hero-ts package."
            ),
        },
        "paths": paths,
        "x-command-center": {
            "api_route_count": len(table.api_routes),
            "static_route_count": len(table.static_routes),
            "static_routes": [
                {"method": route.method, "path": route.path} for route in table.static_routes
            ],
            "stream_kind": "poll-json",
            "etag_routes": [route.path for route in table.api_routes if route.etag],
            "write_routes": [
                {"method": route.method, "path": route.path}
                for route in table.api_routes
                if route.write_semantics != "read-only"
            ],
        },
    }


def openapi_json(table: RouteTable) -> str:
    """Serialize the OpenAPI document deterministically (sorted keys)."""
    return json.dumps(build_openapi(table), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
