"""Command Center route registry (P5-5).

The route table is the Python API layer's single source of truth for routing.
It is derived from the committed P5-2 snapshot manifest
(``docs/command-center/snapshot/command-center-snapshot-v1.json``), so the
Python route set is compatible with the legacy TypeScript Command Center by
construction: 66 API routes + 5 static routes, with the same method/path pairs,
tenant parameters, query keys, ETag, cache, stream kind, and write semantics.

Matching semantics mirror the legacy Hono registrations:

- exact method + path matches win (``/api/registry/agents`` GET never shadows
  ``/api/registry/agents/:id`` DELETE);
- one path parameter exists: ``/api/registry/agents/:id``;
- static routes are exact (``/``, ``/app``, ``/style.css``) or prefix
  (``/app/*``, ``/assets/*``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import CommandCenterError

STREAM_MIN_N = 1
STREAM_MAX_N = 200
STREAM_DEFAULT_N = 60
MAP_CACHE_CONTROL = "public, max-age=2"
ETAG_PREFIX = 'W/"'
ETAG_SUFFIX = '"'

_MANIFEST_REL = Path("docs/command-center/snapshot/command-center-snapshot-v1.json")
_REPO_MARKER = "pyproject.toml"

_INT_QUERY_KEYS = frozenset({"n", "limit", "window", "steps", "minStar"})


@dataclass(frozen=True, slots=True)
class Route:
    """One Command Center API route with its snapshot metadata."""

    method: str
    path: str
    tenant_param: str | None
    etag: str | None
    cache: str
    stream_kind: str
    write_semantics: str
    query: tuple[str, ...]
    notes: str

    @classmethod
    def from_manifest(cls, entry: dict[str, Any]) -> Route:
        return cls(
            method=str(entry["method"]),
            path=str(entry["path"]),
            tenant_param=entry.get("tenant_param"),
            etag=entry.get("etag"),
            cache=str(entry.get("cache") or ""),
            stream_kind=str(entry.get("stream_kind") or "poll-json"),
            write_semantics=str(entry.get("write_semantics") or "read-only"),
            query=tuple(str(item) for item in entry.get("query") or ()),
            notes=str(entry.get("notes") or ""),
        )


@dataclass(frozen=True, slots=True)
class MatchedRoute:
    """A route match plus captured path parameters."""

    route: Route
    path_params: dict[str, str]


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / _REPO_MARKER).is_file():
            return candidate
    raise CommandCenterError(
        f"unable to resolve the repository root from {here}; the route registry "
        "requires the P5-2 snapshot manifest in a checkout"
    )


def _manifest_path() -> Path:
    root = _repo_root()
    manifest = root / _MANIFEST_REL
    if not manifest.is_file():
        raise CommandCenterError(f"P5-2 snapshot manifest missing: {manifest}")
    return manifest


def load_route_entries() -> tuple[tuple[Route, ...], tuple[Route, ...]]:
    """Load API and static routes from the committed P5-2 snapshot manifest."""
    manifest = json.loads(_manifest_path().read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise CommandCenterError("snapshot manifest must be a JSON object")
    api = tuple(Route.from_manifest(entry) for entry in manifest.get("routes") or ())
    static = tuple(Route.from_manifest(entry) for entry in manifest.get("static_routes") or ())
    return api, static


class RouteTable:
    """Exact, path-parameter, and static-prefix route matching (P5-5)."""

    def __init__(
        self,
        api_routes: tuple[Route, ...] | None = None,
        static_routes: tuple[Route, ...] | None = None,
    ) -> None:
        if api_routes is None or static_routes is None:
            api_routes, static_routes = load_route_entries()
        self.api_routes = api_routes
        self.static_routes = static_routes
        self._exact: dict[tuple[str, str], Route] = {}
        self._param: dict[tuple[str, str], tuple[Route, str]] = {}
        for route in self.api_routes:
            key = (route.method, route.path)
            if ":id" in route.path:
                at = route.path.index(":id")
                self._param[(route.method, route.path[:at])] = (route, route.path[:at])
            else:
                self._exact[key] = route
        self._static_exact: dict[tuple[str, str], Route] = {}
        self._static_prefix: list[tuple[str, str, Route]] = []
        for route in self.static_routes:
            if route.path.endswith("/*"):
                self._static_prefix.append((route.method, route.path[:-2], route))
            else:
                self._static_exact[(route.method, route.path)] = route

    def route_set(self) -> frozenset[tuple[str, str]]:
        """All method/path pairs: API plus static routes (parity oracle)."""
        pairs = {(r.method, r.path) for r in self.api_routes}
        pairs |= {(r.method, r.path) for r in self.static_routes}
        return frozenset(pairs)

    def api_route_set(self) -> frozenset[tuple[str, str]]:
        return frozenset((r.method, r.path) for r in self.api_routes)

    def match(self, method: str, path: str) -> MatchedRoute | None:
        """Match a request method+path to a route (None when unknown)."""
        key = (method.upper(), path)
        route = self._exact.get(key)
        if route is not None:
            return MatchedRoute(route, {})
        for (param_method, prefix), (param_route, _prefix) in self._param.items():
            if param_method == key[0] and path.startswith(prefix) and len(path) > len(prefix):
                value = path[len(prefix) :]
                if "/" not in value:
                    return MatchedRoute(param_route, {":id": value})
        for static_method, prefix, static in self._static_prefix:
            if static_method == key[0] and path.startswith(prefix + "/"):
                return MatchedRoute(static, {})
        static = self._static_exact.get(key)
        if static is not None:
            return MatchedRoute(static, {})
        return None

    def default_tenant(self, route: Route) -> str | None:
        """Legacy per-route tenant default (``?? "all"`` / ``?? "t1"``)."""
        if route.tenant_param is None:
            return None
        if route.tenant_param == "all|tN":
            return "all"
        return "t1"


def int_query_keys() -> frozenset[str]:
    return _INT_QUERY_KEYS


__all__ = [
    "ETAG_PREFIX",
    "ETAG_SUFFIX",
    "MAP_CACHE_CONTROL",
    "MatchedRoute",
    "Route",
    "RouteTable",
    "STREAM_DEFAULT_N",
    "STREAM_MAX_N",
    "STREAM_MIN_N",
    "int_query_keys",
    "load_route_entries",
]
