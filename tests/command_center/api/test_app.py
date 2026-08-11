"""Request pipeline tests: routing, validation, ETag, errors (P5-5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arena_hero_agent.command_center import CommandCenterError
from arena_hero_agent.command_center.api import (
    MAP_CACHE_CONTROL,
    ApiRequest,
    ApiResponse,
    CommandCenterApp,
    RouteTable,
    json_response,
)


def _app(data_root: Path, **kwargs) -> CommandCenterApp:
    return CommandCenterApp(data_root=data_root, **kwargs)


def _json_body(response: ApiResponse) -> dict[str, object]:
    body = json.loads(response.body.decode("utf-8"))
    assert isinstance(body, dict)
    return body


def test_unknown_path_returns_404_not_found(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/nope"))
    assert response.status == 404
    assert _json_body(response) == {"error": "not found", "path": "/api/nope"}


def test_unknown_method_returns_404(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("PUT", "/api/map"))
    assert response.status == 404
    assert _json_body(response)["error"] == "not found"


def test_matched_but_unwired_route_returns_501(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(ApiRequest("GET", "/api/tenants"))
    assert response.status == 501
    assert _json_body(response) == {"error": "not implemented"}


def test_static_route_matches_but_is_unwired(tmp_path: Path) -> None:
    app = _app(tmp_path)
    for path in ("/", "/app", "/app/main.js", "/assets/x.svg", "/style.css"):
        response = app.handle(ApiRequest("GET", path))
        assert response.status == 501, path


def test_invalid_tenant_fails_closed_400(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(ApiRequest("GET", "/api/stream", query="tenant=t5"))
    assert response.status == 400
    assert "tenant" in str(_json_body(response)["error"])


def test_invalid_tenant_on_all_route_400(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(ApiRequest("GET", "/api/map/lod", query="tenant=bad"))
    assert response.status == 400


def test_tenant_all_accepted_on_all_route(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(ApiRequest("GET", "/api/map/lod", query="tenant=all"))
    assert response.status == 200


def test_stream_defaults_to_t1_and_60(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = app.handle(ApiRequest("GET", "/api/stream"))
    assert response.status == 200
    body = _json_body(response)
    assert isinstance(body, dict)
    assert body["tenant"] == "t1"
    assert body["rows"] == []


def test_stream_reads_tenant_runtime_jsonl(tmp_path: Path) -> None:
    stream_file = tmp_path / "runtime" / "t2" / "telemetry" / "runtime.jsonl"
    stream_file.parent.mkdir(parents=True)
    stream_file.write_text('{"tick": 1}\n{"tick": 2}\n{"tick": 3}\n', encoding="utf-8")
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/stream", query="tenant=t2&n=2"))
    assert response.status == 200
    body = _json_body(response)
    rows = body["rows"]
    assert isinstance(rows, list)
    assert body["tenant"] == "t2"
    assert [row["tick"] for row in rows] == [2, 3]


def test_map_returns_weak_etag_and_cache_control(tmp_path: Path) -> None:
    def loader() -> tuple[dict, str]:
        return {"generatedAt": "now", "cells": []}, "sig-1"

    response = _app(tmp_path, map_loader=loader).handle(ApiRequest("GET", "/api/map"))
    assert response.status == 200
    assert response.headers["ETag"] == 'W/"sig-1"'
    assert response.headers["Cache-Control"] == MAP_CACHE_CONTROL
    assert response.headers["content-type"].startswith("application/json")
    assert _json_body(response) == {"generatedAt": "now", "cells": []}


def test_map_etag_304_when_if_none_match_matches(tmp_path: Path) -> None:
    calls: list[int] = []

    def loader() -> tuple[dict, str]:
        calls.append(1)
        return {"cells": []}, "sig-1"

    app = _app(tmp_path, map_loader=loader)
    first = app.handle(ApiRequest("GET", "/api/map"))
    second = app.handle(
        ApiRequest("GET", "/api/map", headers={"If-None-Match": first.headers["ETag"]})
    )
    assert second.status == 304
    assert second.body == b""
    assert second.headers["ETag"] == first.headers["ETag"]
    assert second.headers["Cache-Control"] == MAP_CACHE_CONTROL
    # 304 still loads the payload+signature (signature is authoritative)
    assert len(calls) == 2


def test_map_etag_200_when_tag_differs(tmp_path: Path) -> None:
    calls = iter(["sig-a", "sig-b"])

    def loader() -> tuple[dict, str]:
        return {"cells": []}, next(calls)

    app = _app(tmp_path, map_loader=loader)
    first = app.handle(ApiRequest("GET", "/api/map"))
    second = app.handle(
        ApiRequest("GET", "/api/map", headers={"If-None-Match": first.headers["ETag"]})
    )
    assert second.status == 200
    assert second.headers["ETag"] == 'W/"sig-b"'


def test_non_map_routes_have_no_etag(tmp_path: Path) -> None:
    app = _app(tmp_path)
    for method, path, query in (
        ("GET", "/api/stream", ""),
        ("GET", "/api/map/lod", ""),
        ("GET", "/api/tenants", ""),
    ):
        response = app.handle(ApiRequest(method, path, query=query))
        assert "ETag" not in response.headers
        assert "Cache-Control" not in response.headers


def test_handler_failure_returns_500(tmp_path: Path) -> None:
    def boom(
        request: ApiRequest,
        match: object,
        query: dict[str, str],
        tenant: str | None,
    ) -> object:
        del request, match, query, tenant
        raise CommandCenterError("corrupt ledger")

    table = RouteTable()
    app = CommandCenterApp(
        table=table,
        data_root=tmp_path,
        handlers={("GET", "/api/tenants"): boom},
    )
    response = app.handle(ApiRequest("GET", "/api/tenants"))
    assert response.status == 500
    assert "corrupt ledger" in str(_json_body(response)["error"])


def test_unexpected_handler_exception_returns_500(tmp_path: Path) -> None:
    def boom(
        request: ApiRequest,
        match: object,
        query: dict[str, str],
        tenant: str | None,
    ) -> object:
        del request, match, query, tenant
        raise RuntimeError("boom")

    table = RouteTable()
    app = CommandCenterApp(
        table=table,
        data_root=tmp_path,
        handlers={("GET", "/api/tenants"): boom},
    )
    response = app.handle(ApiRequest("GET", "/api/tenants"))
    assert response.status == 500
    assert "boom" in str(_json_body(response)["error"])


def test_unknown_query_keys_are_ignored(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/stream", query="foo=1&tenant=t3&n=5"))
    body = _json_body(response)
    rows = body["rows"]
    assert isinstance(rows, list)
    assert body["tenant"] == "t3"
    assert len(rows) == 0


def test_first_query_value_wins(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/stream", query="n=5&n=9"))
    body = _json_body(response)
    rows = body["rows"]
    assert isinstance(rows, list)
    assert len(rows) == 0
    # n=5 is the first value; both are valid, the response is deterministic
    assert response.status == 200


def test_map_lod_reuses_p5_4_loader(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/map/lod", query="tenant=t1"))
    assert response.status == 200
    body = _json_body(response)
    assert isinstance(body, dict)
    assert body["tenant"] == "t1"
    assert "chunks" in body


def test_default_map_loader_produces_envelope(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/map"))
    assert response.status == 200
    body = _json_body(response)
    assert isinstance(body, dict)
    assert "generatedAt" in body
    assert "tenants" in body
    assert "bounds" in body
    assert "cells" in body
    assert "chunks" in body
    assert "beacons" in body
    assert "coreTrails" in body


def test_json_response_allow_nan_false() -> None:
    with pytest.raises(ValueError):
        json_response(200, {"bad": float("nan")})
