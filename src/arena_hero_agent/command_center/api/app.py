"""Command Center request pipeline (P5-5).

A minimal, dependency-free HTTP application for the Command Center API: route
matching against the P5-5 route registry, fail-closed tenant/query validation,
JSON translation, weak-ETag/304 handling, bounded poll-json streaming with
backpressure, and 404/400/500 error translation that mirrors the legacy Hono
server (``{error: \"not found\", path}`` on unknown paths, 400 on invalid
input, 500 on handler failure).

Stream semantics (B5D): every route is request/response ``poll-json`` — there
is no SSE or WebSocket surface here (the WS wire contract lives in the
arena-hero-ts package). ``/api/stream`` returns a bounded tail of the
per-tenant decision stream with ``n`` clamped to 1..200 (default 60); a
reconnect is just a fresh stateless GET, and reads are bounded so a client can
never ask for an unbounded response. When a parameter cannot be interpreted
deterministically (for example a non-integer ``n``) the request fails closed
with 400 instead of guessing.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

from ..errors import CommandCenterError
from ..goal_store import iso_utc
from ..jsonl import read_jsonl_tail
from ..paths import resolve_data_root, telemetry_dir, validate_tenant, write_api_audit_path
from ..projections import load_alliance_snapshot
from ..projections._common import current_epoch_ms
from ..projections.map_lod import load_map_lod
from .map import load_merged_map
from .routes import (
    ETAG_PREFIX,
    ETAG_SUFFIX,
    MAP_CACHE_CONTROL,
    STREAM_DEFAULT_N,
    STREAM_MAX_N,
    STREAM_MIN_N,
    MatchedRoute,
    Route,
    RouteTable,
)
from .security import (
    WRITE_AUTH_TOKEN_ENV,
    WRITE_CSRF_TOKEN_ENV,
    WriteSecurity,
    is_write_route,
)

JSON_MEDIA_TYPE = "application/json; charset=utf-8"


class RequestValidationError(CommandCenterError):
    """A request parameter failed validation (translated to HTTP 400)."""


@dataclass(frozen=True, slots=True)
class ApiRequest:
    """One decoded HTTP request."""

    method: str
    path: str
    query: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """One HTTP response."""

    status: int
    headers: Mapping[str, str]
    body: bytes


Handler = Callable[[ApiRequest, MatchedRoute, dict[str, str], str | None], object]


def json_response(
    status: int,
    payload: object,
    *,
    headers: Mapping[str, str] | None = None,
) -> ApiResponse:
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )
    merged = {"content-type": JSON_MEDIA_TYPE}
    if headers:
        merged.update(headers)
    return ApiResponse(status, merged, body)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _stream_n(raw: str | None) -> int:
    """Parse/clamp the ``n`` query parameter (fail-closed on non-integers)."""
    if raw is None or raw == "":
        return STREAM_DEFAULT_N
    try:
        value = int(raw)
    except ValueError as exc:
        raise RequestValidationError(f"n must be an integer; actual={raw!r}") from exc
    return min(max(value, STREAM_MIN_N), STREAM_MAX_N)


class CommandCenterApp:
    """Route/dispatch/ETag/stream pipeline for the Command Center API."""

    def __init__(
        self,
        table: RouteTable | None = None,
        *,
        data_root: str | os.PathLike[str] | None = None,
        handlers: Mapping[tuple[str, str], Handler] | None = None,
        map_loader: Callable[[], tuple[dict[str, Any], str]] | None = None,
        now_ms: Callable[[], int] | None = None,
        security: WriteSecurity | None = None,
    ) -> None:
        self.table = table if table is not None else RouteTable()
        self._data_root = resolve_data_root(override=data_root)
        self._now_ms = now_ms if now_ms is not None else current_epoch_ms
        self._map_loader = map_loader
        self._handlers: dict[tuple[str, str], Handler] = {
            ("GET", "/api/stream"): self._handle_stream,
            ("GET", "/api/map"): self._handle_map,
            ("GET", "/api/map/lod"): self._handle_map_lod,
            ("GET", "/api/alliance/snapshot"): self._handle_alliance_snapshot,
        }
        if handlers:
            self._handlers.update(handlers)
        # P5-9: default-deny write gate. Writes are only enabled when the
        # operator explicitly configures the gate tokens (env by default);
        # otherwise every write request is denied (fail-closed).
        self._security = (
            security
            if security is not None
            else WriteSecurity(
                auth_token=os.environ.get(WRITE_AUTH_TOKEN_ENV),
                csrf_token=os.environ.get(WRITE_CSRF_TOKEN_ENV),
                audit_path=write_api_audit_path(self._data_root),
                now_ms=self._now_ms,
            )
        )

    def handle(self, request: ApiRequest) -> ApiResponse:
        """Process one request into a response (404/400/500 translation)."""
        match = self.table.match(request.method, request.path)
        if match is None:
            return json_response(404, {"error": "not found", "path": request.path})
        route = match.route
        decision = None
        if is_write_route(route):
            # P5-9 default-deny gate runs before any route validation so an
            # unauthenticated request can never learn route/tenant details.
            decision = self._security.check(request)
            if decision.outcome != "accepted":
                self._security.audit(
                    decision, request, route, None, match.path_params, status=decision.status
                )
                return json_response(decision.status, {"error": decision.reason})
        try:
            query = self._parse_query(route, request.query)
            tenant = self._tenant_for(route, query)
        except RequestValidationError as exc:
            return json_response(400, {"error": str(exc)})
        handler = self._handlers.get((route.method, route.path))
        if handler is None:
            response = json_response(501, {"error": "not implemented"})
        else:
            try:
                result = handler(request, match, query, tenant)
            except RequestValidationError as exc:
                response = json_response(400, {"error": str(exc)})
            except CommandCenterError as exc:
                response = json_response(500, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - Hono onError parity: handler failure -> 500
                response = json_response(500, {"error": str(exc)})
            else:
                response = result if isinstance(result, ApiResponse) else json_response(200, result)
        if decision is not None:
            # Accepted write requests are audited with the final response
            # status (dispatch outcome); rejected decisions are audited above.
            self._security.audit(
                decision, request, route, tenant, match.path_params, status=response.status
            )
        return self._apply_etag(request, response)

    def _parse_query(self, route: Route, raw: str) -> dict[str, str]:
        """First value per declared query key; unknown keys are ignored (TS parity)."""
        parsed = parse_qs(raw, keep_blank_values=False) if raw else {}
        return {key: parsed[key][0] for key in route.query if parsed.get(key)}

    def _tenant_for(self, route: Route, query: dict[str, str]) -> str | None:
        """Fail-closed tenant validation per the route's ``tenant_param``."""
        tenant_param = route.tenant_param
        if tenant_param is None:
            return None
        raw = query.get("tenant")
        if raw is None:
            raw = "all" if tenant_param == "all|tN" else "t1"
        if tenant_param == "all|tN" and raw == "all":
            return raw
        try:
            return validate_tenant(raw)
        except CommandCenterError as exc:
            raise RequestValidationError(str(exc)) from exc

    def _apply_etag(self, request: ApiRequest, response: ApiResponse) -> ApiResponse:
        """Weak ETag 304 handling: exact If-None-Match match returns 304."""
        etag = _header(response.headers, "etag")
        if etag is None:
            return response
        incoming = _header(request.headers, "if-none-match")
        if incoming is not None and incoming == etag:
            cache_control = _header(response.headers, "cache-control") or MAP_CACHE_CONTROL
            return ApiResponse(304, {"ETag": etag, "Cache-Control": cache_control}, b"")
        return response

    # -- handlers ---------------------------------------------------------

    def _handle_stream(
        self,
        request: ApiRequest,
        match: MatchedRoute,
        query: dict[str, str],
        tenant: str | None,
    ) -> dict[str, Any]:
        del request, match
        tenant_value = tenant or "t1"
        n = _stream_n(query.get("n"))
        path = telemetry_dir(self._data_root, tenant_value) / "runtime.jsonl"
        rows = read_jsonl_tail(path, n)
        return {"tenant": tenant_value, "generatedAt": iso_utc(self._now_ms()), "rows": rows}

    def _handle_map(
        self,
        request: ApiRequest,
        match: MatchedRoute,
        query: dict[str, str],
        tenant: str | None,
    ) -> ApiResponse:
        del request, match, query, tenant
        loader = self._map_loader if self._map_loader is not None else self._default_map_loader
        payload, signature = loader()
        etag = f"{ETAG_PREFIX}{signature}{ETAG_SUFFIX}"
        headers = {"ETag": etag, "Cache-Control": MAP_CACHE_CONTROL}
        return json_response(200, payload, headers=headers)

    def _default_map_loader(self) -> tuple[dict[str, Any], str]:
        return load_merged_map(self._data_root)

    def _handle_map_lod(
        self,
        request: ApiRequest,
        match: MatchedRoute,
        query: dict[str, str],
        tenant: str | None,
    ) -> dict[str, Any]:
        del request, match, query
        return load_map_lod(self._data_root, tenant or "all")

    def _handle_alliance_snapshot(
        self,
        request: ApiRequest,
        match: MatchedRoute,
        query: dict[str, str],
        tenant: str | None,
    ) -> dict[str, Any]:
        del request, match, query, tenant
        return load_alliance_snapshot(self._data_root, now_ms=self._now_ms())


__all__ = [
    "ApiRequest",
    "ApiResponse",
    "CommandCenterApp",
    "JSON_MEDIA_TYPE",
    "RequestValidationError",
    "json_response",
]
