"""Command Center write API security gate (P5-9).

Default-deny authorization, CSRF, and replay protection for the write surface,
plus a JSONL audit of every gate decision. The write-route set is derived from
the P5-2 snapshot: every route whose ``write_semantics`` starts with
``write:`` plus the ``GET /api/health/pipeline`` side-effect route (16 paths,
see ``write_route_pairs`` and ``security-baseline-v1.md``).

The gate is fail-closed by construction:

- ``auth_token`` / ``csrf_token`` are explicit. When either is not configured
  every write request is denied (a write surface must be explicitly enabled).
- A write request must present ``Authorization: Bearer <token>``,
  ``X-CSRF-Token: <token>``, ``X-Timestamp`` (epoch ms within the replay
  window), and an ``Idempotency-Key``. Token comparisons use
  ``secrets.compare_digest``; secrets are never written to the audit log.
- CSRF: a custom header that a cross-origin browser cannot set without CORS
  preflight, plus same-origin enforcement when an ``Origin`` header is present.
- Replay: ``Idempotency-Key`` dedupe within the replay window (``duplicate``)
  and stale replay / out-of-window timestamps (``expired``).

Audit outcomes (one JSONL record per write request):

- ``accepted``: passed the gate and was dispatched to the handler;
- ``rejected``: CSRF or malformed security headers;
- ``expired``: timestamp or idempotency key outside the replay window;
- ``duplicate``: idempotency key replayed within the window;
- ``unauthorized``: missing/invalid bearer token or unconfigured gate.

Records are appended to ``write_api_audit_path(data_root)`` (P5-3
``append_jsonl`` pattern). The per-write-path persistence audits from the P5-2
baseline (e.g. ``human-command-audit.jsonl``) remain the write handler's job.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from ..errors import CommandCenterError
from ..goal_store import iso_utc
from ..jsonl import append_jsonl
from ..projections._common import current_epoch_ms

if TYPE_CHECKING:
    from .app import ApiRequest
    from .routes import Route, RouteTable

AUTH_HEADER = "Authorization"
CSRF_HEADER = "X-CSRF-Token"
IDEMPOTENCY_HEADER = "Idempotency-Key"
TIMESTAMP_HEADER = "X-Timestamp"

DEFAULT_REPLAY_WINDOW_MS = 5 * 60 * 1000
MAX_IDEMPOTENCY_KEY_LEN = 128

# Operator configuration knobs for the default app gate (fail-closed when unset).
WRITE_AUTH_TOKEN_ENV = "ARENA_WRITE_AUTH_TOKEN"
WRITE_CSRF_TOKEN_ENV = "ARENA_WRITE_CSRF_TOKEN"

# Side-effect read route: may spawn a background survey sync (P5-2 baseline).
HEALTH_PIPELINE_PAIR = ("GET", "/api/health/pipeline")

WriteOutcome = Literal["accepted", "rejected", "expired", "duplicate", "unauthorized"]

_BEARER_PREFIX = "Bearer "
_ALLOWED_ORIGIN_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class SecurityDecision:
    """Outcome of one write-request gate check."""

    outcome: WriteOutcome
    status: int
    reason: str
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in ("accepted", "rejected", "expired", "duplicate", "unauthorized"):
            raise ValueError(f"unknown write outcome {self.outcome!r}")
        if isinstance(self.status, bool) or not isinstance(self.status, int):
            raise ValueError("status must be an integer")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("reason must be a non-empty string")
        if self.idempotency_key is not None and not isinstance(self.idempotency_key, str):
            raise ValueError("idempotency_key must be a string when present")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _origin_matches_host(headers: Mapping[str, str]) -> bool:
    """Same-origin check: when Origin is present it must match the Host hostname."""
    origin = _header(headers, "origin")
    if origin is None:
        return True
    host = _header(headers, "host")
    if host is None:
        return False
    try:
        origin_parts = urlsplit(origin)
        host_parts = urlsplit(f"//{host}")
    except ValueError:
        return False
    if origin_parts.scheme not in _ALLOWED_ORIGIN_SCHEMES or not origin_parts.hostname:
        return False
    return bool(host_parts.hostname) and host_parts.hostname == origin_parts.hostname


def is_write_route(route: Route) -> bool:
    """True for routes on the P5-2 write-path list (explicit writes + side effect)."""
    return (
        route.write_semantics.startswith("write:")
        or (
            route.method,
            route.path,
        )
        == HEALTH_PIPELINE_PAIR
    )


def write_route_pairs(table: RouteTable) -> tuple[tuple[str, str], ...]:
    """Sorted (method, path) pairs for the P5-9 write-route set."""
    return tuple(
        sorted((route.method, route.path) for route in table.api_routes if is_write_route(route))
    )


class ReplayStore:
    """In-memory idempotency-key registry for the replay window."""

    def __init__(self) -> None:
        self._keys: dict[str, int] = {}

    def check(
        self, key: str, now_ms: int, window_ms: int
    ) -> Literal["new", "duplicate", "expired"]:
        """Record a new key or classify a replay (duplicate within window, else expired)."""
        seen_at = self._keys.get(key)
        if seen_at is None:
            self._keys[key] = now_ms
            return "new"
        if now_ms - seen_at <= window_ms:
            return "duplicate"
        # Stale replay: forget the old entry; a fresh request may retry the key.
        del self._keys[key]
        return "expired"

    def size(self) -> int:
        return len(self._keys)


class WriteSecurity:
    """Default-deny gate for Command Center write routes (P5-9)."""

    def __init__(
        self,
        *,
        auth_token: str | None = None,
        csrf_token: str | None = None,
        replay_window_ms: int = DEFAULT_REPLAY_WINDOW_MS,
        audit_path: str | os.PathLike[str] | None = None,
        now_ms: Callable[[], int] | None = None,
        replay: ReplayStore | None = None,
    ) -> None:
        if isinstance(replay_window_ms, bool) or not isinstance(replay_window_ms, int):
            raise CommandCenterError(
                f"replay_window_ms must be an integer; actual={replay_window_ms!r}"
            )
        if replay_window_ms < 0:
            raise CommandCenterError(
                f"replay_window_ms cannot be negative; actual={replay_window_ms}"
            )
        self._auth_token = auth_token
        self._csrf_token = csrf_token
        self._replay_window_ms = replay_window_ms
        self._audit_path = Path(audit_path) if audit_path is not None else None
        self._now_ms = now_ms if now_ms is not None else current_epoch_ms
        self._replay = replay if replay is not None else ReplayStore()

    @property
    def audit_path(self) -> Path | None:
        return self._audit_path

    def check(self, request: ApiRequest) -> SecurityDecision:
        """Run the default-deny gate for one write request (fail-closed)."""
        if self._auth_token is None:
            return SecurityDecision("unauthorized", 401, "write api authorization not configured")
        presented = _header(request.headers, AUTH_HEADER)
        expected = f"{_BEARER_PREFIX}{self._auth_token}"
        if presented is None:
            return SecurityDecision("unauthorized", 401, "missing bearer token")
        if not secrets.compare_digest(presented, expected):
            return SecurityDecision("unauthorized", 401, "invalid bearer token")

        if self._csrf_token is None:
            return SecurityDecision("rejected", 403, "csrf protection not configured")
        token = _header(request.headers, CSRF_HEADER)
        if token is None:
            return SecurityDecision("rejected", 403, "missing csrf token")
        if not secrets.compare_digest(token, self._csrf_token):
            return SecurityDecision("rejected", 403, "invalid csrf token")
        if not _origin_matches_host(request.headers):
            return SecurityDecision("rejected", 403, "origin does not match host")

        raw_ts = _header(request.headers, TIMESTAMP_HEADER)
        if raw_ts is None:
            return SecurityDecision("rejected", 400, "missing timestamp")
        if not raw_ts.isdigit():
            return SecurityDecision("rejected", 400, "invalid timestamp")
        timestamp = int(raw_ts)
        now_ms = self._now_ms()
        if abs(now_ms - timestamp) > self._replay_window_ms:
            return SecurityDecision("expired", 408, "timestamp outside replay window")

        key = _header(request.headers, IDEMPOTENCY_HEADER)
        if key is None:
            return SecurityDecision("rejected", 400, "missing idempotency key")
        if not isinstance(key, str) or not key or len(key) > MAX_IDEMPOTENCY_KEY_LEN:
            return SecurityDecision("rejected", 400, "invalid idempotency key")
        state = self._replay.check(key, now_ms, self._replay_window_ms)
        if state == "duplicate":
            return SecurityDecision("duplicate", 409, "duplicate idempotency key", key)
        if state == "expired":
            return SecurityDecision("expired", 408, "idempotency key expired", key)
        return SecurityDecision("accepted", 200, "authorized", key)

    def audit(
        self,
        decision: SecurityDecision,
        request: ApiRequest,
        route: Route,
        tenant: str | None,
        path_params: Mapping[str, str] | None,
        *,
        status: int,
    ) -> None:
        """Append one gate-decision record to the audit log (no-op without a path)."""
        if self._audit_path is None:
            return
        now_ms = self._now_ms()
        append_jsonl(
            self._audit_path,
            {
                "at": iso_utc(now_ms),
                "ts": now_ms,
                "outcome": decision.outcome,
                "reason": decision.reason,
                "status": status,
                "method": request.method,
                "path": route.path,
                "pathParams": dict(path_params) if path_params else None,
                "tenant": tenant,
                "idempotencyKey": decision.idempotency_key,
            },
        )


__all__ = [
    "AUTH_HEADER",
    "CSRF_HEADER",
    "DEFAULT_REPLAY_WINDOW_MS",
    "HEALTH_PIPELINE_PAIR",
    "IDEMPOTENCY_HEADER",
    "MAX_IDEMPOTENCY_KEY_LEN",
    "ReplayStore",
    "SecurityDecision",
    "TIMESTAMP_HEADER",
    "WRITE_AUTH_TOKEN_ENV",
    "WRITE_CSRF_TOKEN_ENV",
    "WriteOutcome",
    "WriteSecurity",
    "is_write_route",
    "write_route_pairs",
]
