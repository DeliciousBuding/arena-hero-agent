"""Stream/polling and backpressure semantics (P5-5 B5D).

``/api/stream`` is bounded poll-json: a stateless bounded tail read of the
per-tenant decision stream with ``n`` clamped to 1..200 (default 60). A
client reconnect is just a fresh GET — no server-held cursor or connection
state — and reads are bounded so no client can request an unbounded response.
When a parameter cannot be interpreted deterministically the request fails
closed with 400 instead of guessing.
"""

from __future__ import annotations

import json
from pathlib import Path

from arena_hero_agent.command_center.api import ApiRequest, ApiResponse, CommandCenterApp

FIXED_NOW_MS = 1_700_000_000_000


def _app(data_root: Path, **kwargs) -> CommandCenterApp:
    kwargs.setdefault("now_ms", lambda: FIXED_NOW_MS)
    return CommandCenterApp(data_root=data_root, **kwargs)


def _body(response: ApiResponse) -> dict:
    return json.loads(response.body.decode("utf-8"))


def _write_stream(data_root: Path, tenant: str, ticks: int) -> None:
    stream_file = data_root / "runtime" / tenant / "telemetry" / "runtime.jsonl"
    stream_file.parent.mkdir(parents=True, exist_ok=True)
    stream_file.write_text(
        "".join(json.dumps({"tick": i}) + "\n" for i in range(ticks)), encoding="utf-8"
    )


def test_stream_is_stateless_reconnect(tmp_path: Path) -> None:
    _write_stream(tmp_path, "t1", 5)
    app = _app(tmp_path)
    first = _body(app.handle(ApiRequest("GET", "/api/stream")))
    second = _body(app.handle(ApiRequest("GET", "/api/stream")))
    # reconnect returns an identical bounded view with no cursor drift
    assert first == second
    assert [row["tick"] for row in first["rows"]] == [0, 1, 2, 3, 4]


def test_stream_missing_file_returns_empty_rows(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/stream"))
    assert response.status == 200
    body = _body(response)
    assert body["tenant"] == "t1"
    assert body["rows"] == []


def test_stream_n_defaults_to_60(tmp_path: Path) -> None:
    _write_stream(tmp_path, "t1", 100)
    body = _body(_app(tmp_path).handle(ApiRequest("GET", "/api/stream")))
    assert len(body["rows"]) == 60
    assert body["rows"][-1]["tick"] == 99


def test_stream_n_bounded_above(tmp_path: Path) -> None:
    _write_stream(tmp_path, "t1", 300)
    body = _body(_app(tmp_path).handle(ApiRequest("GET", "/api/stream", query="n=10000")))
    assert len(body["rows"]) == 200
    assert body["rows"][-1]["tick"] == 299


def test_stream_n_bounded_below(tmp_path: Path) -> None:
    _write_stream(tmp_path, "t1", 10)
    for n in ("0", "-5"):
        body = _body(_app(tmp_path).handle(ApiRequest("GET", "/api/stream", query=f"n={n}")))
        assert len(body["rows"]) == 1, n


def test_stream_invalid_n_fails_closed_400(tmp_path: Path) -> None:
    app = _app(tmp_path)
    for n in ("abc", "3.5", "1e3"):
        response = app.handle(ApiRequest("GET", "/api/stream", query=f"n={n}"))
        assert response.status == 400, n
        assert "n must be an integer" in str(_body(response)["error"])


def test_stream_empty_n_treated_as_missing(tmp_path: Path) -> None:
    _write_stream(tmp_path, "t1", 100)
    body = _body(_app(tmp_path).handle(ApiRequest("GET", "/api/stream", query="n=")))
    assert len(body["rows"]) == 60


def test_stream_invalid_tenant_fails_closed_400(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(ApiRequest("GET", "/api/stream", query="tenant=other"))
    assert response.status == 400


def test_stream_is_poll_json_not_sse(tmp_path: Path) -> None:
    response = _app(tmp_path).handle(
        ApiRequest("GET", "/api/stream", headers={"Upgrade": "websocket", "Connection": "Upgrade"})
    )
    assert response.status == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "text/event-stream" not in response.headers.get("content-type", "")
    assert "websocket" not in response.headers.get("upgrade", "")


def test_stream_reconnect_does_not_accumulate_server_state(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _write_stream(tmp_path, "t1", 3)
    app.handle(ApiRequest("GET", "/api/stream"))
    app.handle(ApiRequest("GET", "/api/stream"))
    body = _body(app.handle(ApiRequest("GET", "/api/stream")))
    assert [row["tick"] for row in body["rows"]] == [0, 1, 2]


def test_map_signature_stable_across_polls(tmp_path: Path) -> None:
    app = _app(tmp_path)
    first = app.handle(ApiRequest("GET", "/api/map"))
    second = app.handle(ApiRequest("GET", "/api/map"))
    assert first.headers["ETag"] == second.headers["ETag"]
    assert first.headers["ETag"].startswith('W/"')
