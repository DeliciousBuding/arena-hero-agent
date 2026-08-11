"""P5-9 write API security gate tests.

Covers the P5-2 write-path parity (16 paths), default-deny behavior,
authorization, CSRF, replay detection (idempotency + time window), and the
gate audit log (accepted/rejected/expired/duplicate/unauthorized).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from arena_hero_agent.command_center import write_api_audit_path
from arena_hero_agent.command_center.api import (
    AUTH_HEADER,
    CSRF_HEADER,
    IDEMPOTENCY_HEADER,
    TIMESTAMP_HEADER,
    ApiRequest,
    CommandCenterApp,
    ReplayStore,
    RouteTable,
    WriteSecurity,
    write_route_pairs,
)

NOW = 1_750_000_000_000
WINDOW_MS = 5 * 60 * 1000
AUTH = "auth-secret-xyz"
CSRF = "csrf-secret-xyz"

# The P5-2 security baseline write-path list (security-baseline-v1.md):
# every route with ``write:`` semantics plus the health/pipeline side effect.
EXPECTED_WRITE_PATHS = {
    ("POST", "/api/alliance/survey/arbitrate"),
    ("POST", "/api/alliance/survey/arbitrate/clear"),
    ("POST", "/api/leaderboard/refresh"),
    ("POST", "/api/command"),
    ("POST", "/api/command/goal"),
    ("DELETE", "/api/command"),
    ("POST", "/api/command/clear"),
    ("POST", "/api/command/mode"),
    ("POST", "/api/shop/history/refresh"),
    ("POST", "/api/shop/order"),
    ("POST", "/api/redeem"),
    ("POST", "/api/ingest/agents"),
    ("POST", "/api/registry/agents"),
    ("POST", "/api/registry/keys"),
    ("DELETE", "/api/registry/agents/:id"),
    ("GET", "/api/health/pipeline"),
}


def _security(tmp_path: Path, *, now_ms: Callable[[], int] | None = None) -> WriteSecurity:
    return WriteSecurity(
        auth_token=AUTH,
        csrf_token=CSRF,
        audit_path=write_api_audit_path(tmp_path),
        now_ms=now_ms if now_ms is not None else lambda: NOW,
    )


def _app(tmp_path: Path, **kwargs) -> CommandCenterApp:
    security = kwargs.pop("security", None) or _security(tmp_path)
    return CommandCenterApp(data_root=tmp_path, now_ms=lambda: NOW, security=security, **kwargs)


def _headers(**overrides) -> dict[str, str]:
    headers = {
        AUTH_HEADER: f"Bearer {AUTH}",
        CSRF_HEADER: CSRF,
        TIMESTAMP_HEADER: str(NOW),
        IDEMPOTENCY_HEADER: "idem-1",
    }
    headers.update(overrides)
    return headers


def _write_request(
    path: str = "/api/command", *, query: str = "", headers: dict[str, str] | None = None
) -> ApiRequest:
    """POST write request; ``headers`` are merged onto the valid defaults."""
    final = _headers()
    if headers is not None:
        final.update(headers)
    return ApiRequest("POST", path, query=query, headers=final)


def _exact_request(
    path: str = "/api/command", *, query: str = "", headers: dict[str, str] | None = None
) -> ApiRequest:
    """POST write request using exactly the given headers (no defaults)."""
    return ApiRequest("POST", path, query=query, headers=dict(headers or {}))


def _audit_rows(tmp_path: Path) -> list[dict]:
    path = write_api_audit_path(tmp_path)
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _echo_handler(request: ApiRequest, match, query, tenant):
    del request, match
    return {"ok": True, "tenant": tenant, "query": query}


# ---------------------------------------------------------------------------
# P5-2 write-path parity
# ---------------------------------------------------------------------------


def test_write_route_set_matches_p5_2_baseline() -> None:
    pairs = set(write_route_pairs(RouteTable()))
    assert pairs == EXPECTED_WRITE_PATHS
    assert len(pairs) == 16


def test_health_pipeline_is_gated_but_commands_reconcile_is_not() -> None:
    table = RouteTable()
    pairs = set(write_route_pairs(table))
    assert ("GET", "/api/health/pipeline") in pairs
    assert ("GET", "/api/commands") not in pairs


def test_every_write_route_is_an_api_route() -> None:
    table = RouteTable()
    api_pairs = table.api_route_set()
    for pair in write_route_pairs(table):
        assert pair in api_pairs


# ---------------------------------------------------------------------------
# Default deny
# ---------------------------------------------------------------------------


def test_default_deny_without_configured_gate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ARENA_WRITE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ARENA_WRITE_CSRF_TOKEN", raising=False)
    app = CommandCenterApp(data_root=tmp_path, now_ms=lambda: NOW)
    response = app.handle(_write_request())
    assert response.status == 401
    body = json.loads(response.body.decode("utf-8"))
    assert "not configured" in body["error"]
    assert [row["outcome"] for row in _audit_rows(tmp_path)] == ["unauthorized"]


def test_unauthorized_write_never_learns_tenant_details(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(_write_request(query="tenant=t9", headers={AUTH_HEADER: "Bearer wrong"}))
    assert response.status == 401
    assert "tenant" not in response.body.decode("utf-8")


def test_accepted_write_keeps_default_tenant_for_command_route(tmp_path: Path) -> None:
    # P5-5 parity: /api/command has no declared tenant query key, so the
    # default tenant t1 applies and the request proceeds (gate accepted).
    app = _app(tmp_path, handlers={("POST", "/api/command"): _echo_handler})
    response = app.handle(_write_request(query="tenant=t9"))
    assert response.status == 200
    assert json.loads(response.body.decode("utf-8"))["tenant"] == "t1"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_write_without_bearer_token_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(_exact_request(headers={CSRF_HEADER: CSRF}))
    assert response.status == 401
    assert json.loads(response.body.decode("utf-8"))["error"] == "missing bearer token"


def test_write_with_wrong_bearer_token_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(_write_request(headers={AUTH_HEADER: "Bearer wrong"}))
    assert response.status == 401
    assert json.loads(response.body.decode("utf-8"))["error"] == "invalid bearer token"


def test_write_with_valid_bearer_dispatches_handler(tmp_path: Path) -> None:
    app = _app(tmp_path, handlers={("POST", "/api/command"): _echo_handler})
    response = app.handle(_write_request())
    assert response.status == 200
    assert json.loads(response.body.decode("utf-8"))["ok"] is True


def test_read_routes_do_not_require_auth(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(ApiRequest("GET", "/api/tenants"))
    assert response.status == 501  # unwired read route, gate not applied


def test_health_pipeline_side_effect_write_is_gated(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(ApiRequest("GET", "/api/health/pipeline"))
    assert response.status == 401


def test_unauthorized_delete_registry_agent_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(
        ApiRequest(
            "DELETE", "/api/registry/agents/agent-7", headers={"Authorization": "Bearer wrong"}
        )
    )
    assert response.status == 401


def test_rejected_request_never_reaches_handler(tmp_path: Path) -> None:
    def boom(request: ApiRequest, match, query, tenant) -> object:
        del request, match, query, tenant
        raise AssertionError("handler must not run for rejected writes")

    app = _app(tmp_path, handlers={("POST", "/api/command"): boom})
    response = app.handle(_write_request(headers={AUTH_HEADER: "Bearer wrong"}))
    assert response.status == 401


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_write_without_csrf_token_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    headers = {
        AUTH_HEADER: f"Bearer {AUTH}",
        TIMESTAMP_HEADER: str(NOW),
        IDEMPOTENCY_HEADER: "idem-1",
    }
    response = app.handle(_exact_request(headers=headers))
    assert response.status == 403
    assert json.loads(response.body.decode("utf-8"))["error"] == "missing csrf token"


def test_write_with_wrong_csrf_token_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(_write_request(headers={CSRF_HEADER: "wrong"}))
    assert response.status == 403
    assert json.loads(response.body.decode("utf-8"))["error"] == "invalid csrf token"


def test_write_with_mismatched_origin_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(
        _write_request(headers={"Origin": "http://evil.example", "Host": "127.0.0.1:8787"})
    )
    assert response.status == 403
    assert json.loads(response.body.decode("utf-8"))["error"] == "origin does not match host"


def test_write_with_matching_origin_accepted(tmp_path: Path) -> None:
    app = _app(tmp_path, handlers={("POST", "/api/command"): _echo_handler})
    response = app.handle(
        _write_request(headers={"Origin": "http://127.0.0.1:5173", "Host": "127.0.0.1:8787"})
    )
    assert response.status == 200


# ---------------------------------------------------------------------------
# Timestamp / time window
# ---------------------------------------------------------------------------


def test_write_without_timestamp_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    headers = {
        AUTH_HEADER: f"Bearer {AUTH}",
        CSRF_HEADER: CSRF,
        IDEMPOTENCY_HEADER: "idem-1",
    }
    response = app.handle(_exact_request(headers=headers))
    assert response.status == 400
    assert json.loads(response.body.decode("utf-8"))["error"] == "missing timestamp"


def test_write_with_invalid_timestamp_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(_write_request(headers={TIMESTAMP_HEADER: "not-a-number"}))
    assert response.status == 400
    assert json.loads(response.body.decode("utf-8"))["error"] == "invalid timestamp"


def test_write_with_stale_timestamp_expired(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(_write_request(headers={TIMESTAMP_HEADER: str(NOW - WINDOW_MS - 1)}))
    assert response.status == 408
    assert json.loads(response.body.decode("utf-8"))["error"] == "timestamp outside replay window"


def test_write_with_future_timestamp_expired(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(_write_request(headers={TIMESTAMP_HEADER: str(NOW + WINDOW_MS + 1)}))
    assert response.status == 408


# ---------------------------------------------------------------------------
# Idempotency / replay
# ---------------------------------------------------------------------------


def test_write_without_idempotency_key_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    headers = {
        AUTH_HEADER: f"Bearer {AUTH}",
        CSRF_HEADER: CSRF,
        TIMESTAMP_HEADER: str(NOW),
    }
    response = app.handle(_exact_request(headers=headers))
    assert response.status == 400
    assert json.loads(response.body.decode("utf-8"))["error"] == "missing idempotency key"


def test_write_with_too_long_idempotency_key_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(_write_request(headers={IDEMPOTENCY_HEADER: "k" * 129}))
    assert response.status == 400
    assert json.loads(response.body.decode("utf-8"))["error"] == "invalid idempotency key"


def test_replayed_key_within_window_is_duplicate(tmp_path: Path) -> None:
    app = _app(tmp_path, handlers={("POST", "/api/command"): _echo_handler})
    first = app.handle(_write_request())
    second = app.handle(_write_request())
    assert first.status == 200
    assert second.status == 409
    assert json.loads(second.body.decode("utf-8"))["error"] == "duplicate idempotency key"


def test_replayed_key_after_window_is_expired(tmp_path: Path) -> None:
    clock = {"now": NOW}
    security = _security(tmp_path, now_ms=lambda: clock["now"])
    app = _app(tmp_path, security=security, handlers={("POST", "/api/command"): _echo_handler})
    first = app.handle(_write_request())
    assert first.status == 200
    clock["now"] = NOW + WINDOW_MS + 1
    stale = app.handle(_write_request(headers={TIMESTAMP_HEADER: str(NOW + WINDOW_MS + 1)}))
    assert stale.status == 408
    assert json.loads(stale.body.decode("utf-8"))["error"] == "idempotency key expired"


def test_replay_store_classifies_new_duplicate_expired() -> None:
    store = ReplayStore()
    assert store.check("k", NOW, WINDOW_MS) == "new"
    assert store.check("k", NOW, WINDOW_MS) == "duplicate"
    assert store.check("k", NOW + WINDOW_MS + 1, WINDOW_MS) == "expired"
    # stale entries are forgotten; a fresh retry is treated as new
    assert store.check("k", NOW + WINDOW_MS + 2, WINDOW_MS) == "new"
    assert store.size() == 1


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_audit_records_every_gate_outcome(tmp_path: Path) -> None:
    app = _app(tmp_path, handlers={("POST", "/api/command"): _echo_handler})

    app.handle(_write_request(headers={AUTH_HEADER: "Bearer wrong"}))  # unauthorized
    app.handle(_write_request(headers={CSRF_HEADER: "wrong"}))  # rejected
    app.handle(_write_request(headers={TIMESTAMP_HEADER: str(NOW - WINDOW_MS - 1)}))  # expired
    app.handle(_write_request())  # accepted
    app.handle(_write_request())  # duplicate

    outcomes = [row["outcome"] for row in _audit_rows(tmp_path)]
    assert outcomes == ["unauthorized", "rejected", "expired", "accepted", "duplicate"]
    assert set(outcomes) == {"accepted", "rejected", "expired", "duplicate", "unauthorized"}


def test_audit_record_shape_and_no_secrets(tmp_path: Path) -> None:
    app = _app(tmp_path)
    app.handle(_write_request(headers={AUTH_HEADER: "Bearer wrong"}))
    rows = _audit_rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {
        "at",
        "ts",
        "outcome",
        "reason",
        "status",
        "method",
        "path",
        "pathParams",
        "tenant",
        "idempotencyKey",
    }
    assert row["outcome"] == "unauthorized"
    assert row["status"] == 401
    assert row["method"] == "POST"
    assert row["path"] == "/api/command"
    serialized = write_api_audit_path(tmp_path).read_text(encoding="utf-8")
    assert AUTH not in serialized
    assert CSRF not in serialized


def test_audit_accepted_records_tenant_and_path_params(tmp_path: Path) -> None:
    app = _app(tmp_path, handlers={("POST", "/api/command"): _echo_handler})
    app.handle(_write_request())
    rows = _audit_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "accepted"
    assert rows[0]["status"] == 200
    assert rows[0]["tenant"] == "t1"
    assert rows[0]["idempotencyKey"] == "idem-1"

    app.handle(
        ApiRequest(
            "DELETE",
            "/api/registry/agents/agent-7",
            headers=_headers(IDEMPOTENCY_HEADER="idem-2"),
        )
    )
    rows = _audit_rows(tmp_path)
    assert rows[-1]["path"] == "/api/registry/agents/:id"
    assert rows[-1]["pathParams"] == {":id": "agent-7"}


def test_audit_accepted_covers_unwired_501(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(_write_request(path="/api/redeem"))
    assert response.status == 501
    rows = _audit_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "accepted"
    assert rows[0]["status"] == 501
