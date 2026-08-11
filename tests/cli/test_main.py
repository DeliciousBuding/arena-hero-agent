"""Offline CLI run/health contract, shutdown, and output-privacy tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from arena_hero_agent.application.tick_loop import TurnStream
from arena_hero_agent.application.turns import TurnObservation
from arena_hero_agent.cli.main import (
    DEFAULT_DATA_ROOT,
    EXIT_ERROR,
    EXIT_INTERRUPT,
    EXIT_NOT_READY,
    EXIT_OK,
    HealthSnapshot,
    RunState,
    _run_async,
    _write_health,
    build_parser,
    main,
)

FIXTURE = Path(__file__).parent / "fixtures" / "replay_turns_v1.json"

FORBIDDEN_OUTPUT = (
    "sk-",
    "api_key",
    "apikey",
    "api-key",
    "cookie",
    "authorization",
    "bearer",
    "D:\\",
    "C:\\",
    "/home/",
    "Users\\",
    "Users/",
    "Traceback",
)


def _minimal_payload(tick: int, resources: int = 10) -> dict[str, object]:
    return {
        "tick": tick,
        "lifecycle": "active",
        "resources": resources,
        "population": 0,
        "projection": {
            "tick": tick,
            "rules_version": "v0.14",
            "core": None,
            "units": [],
            "entities": [],
            "resources": [],
            "terrain": [],
            "beacon": None,
        },
        "events": [],
        "respawn_at_tick": None,
    }


class BlockGate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()


class _BlockingStream:
    def __init__(self, observations: tuple[TurnObservation, ...], gate: BlockGate) -> None:
        self._observations = observations
        self._gate = gate
        self._index = 0

    def __aiter__(self) -> AsyncIterator[TurnObservation]:
        return self

    async def __anext__(self) -> TurnObservation:
        if self._index >= len(self._observations):
            raise StopAsyncIteration
        observation = self._observations[self._index]
        self._index += 1
        if self._index == 2:
            self._gate.entered.set()
            await self._gate.release.wait()
        return observation

    async def aclose(self) -> None:
        return None


class BlockingSource:
    def __init__(self, observations: tuple[TurnObservation, ...], gate: BlockGate) -> None:
        self._observations = observations
        self._gate = gate
        self.closed = False

    def stream(self) -> TurnStream:
        return _BlockingStream(self._observations, self._gate)

    def close(self) -> None:
        self.closed = True


def _run_args(tmp_path: Path, tenant: str = "tenant-a", **overrides: object) -> list[str]:
    args = [
        "run",
        "--tenant",
        tenant,
        "--input",
        str(FIXTURE),
        "--data-root",
        str(tmp_path / "data"),
    ]
    for key, value in overrides.items():
        args.extend([f"--{key}", str(value)])
    return args


def test_parser_requires_tenant_and_input() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["run"])
    assert exc.value.code == 2


def test_parser_run_defaults() -> None:
    args = build_parser().parse_args(["run", "--tenant", "t1", "--input", "x.json"])
    assert args.command == "run"
    assert args.tenant == "t1"
    assert args.input == "x.json"
    assert args.data_root == DEFAULT_DATA_ROOT
    assert args.backend == "jsonl"
    assert args.tick_budget_ms == 100
    assert args.max_reconnects == 3
    assert args.run_id is None


def test_parser_rejects_unknown_backend() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(
            ["run", "--tenant", "t1", "--input", "x.json", "--backend", "csv"]
        )
    assert exc.value.code == 2


def test_parser_help_mentions_run_and_health(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "run" in output
    assert "health" in output


def test_run_happy_path_writes_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(_run_args(tmp_path))
    assert exit_code == EXIT_OK

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["schemaVersion"] == 1
    assert payload["ready"] is True
    assert payload["completed"] is True
    assert payload["tenantId"] == "tenant-a"
    assert payload["lastTick"] == 3
    assert payload["ticksProcessed"] == 3
    assert payload["stoppedReason"] == "stream_ended"
    assert payload["lastError"] is None

    data_root = tmp_path / "data"
    tenant_dir = data_root / "tenant-a"
    assert (tenant_dir / "health.json").exists()
    assert (tenant_dir / "telemetry.jsonl").exists()
    ticks_lines = (tenant_dir / "ticks.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(ticks_lines) == 4  # three tick records plus one loop record


def test_run_jsonl_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / "replay.jsonl"
    input_path.write_text(
        "\n".join(json.dumps(_minimal_payload(tick)) for tick in (1, 2, 3)),
        encoding="utf-8",
    )
    exit_code = main(
        [
            "run",
            "--tenant",
            "tenant-a",
            "--input",
            str(input_path),
            "--data-root",
            str(tmp_path / "data"),
        ]
    )
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ticksProcessed"] == 3


def test_run_sqlite_backend(tmp_path: Path) -> None:
    exit_code = main(
        _run_args(tmp_path, backend="sqlite"),
    )
    assert exit_code == EXIT_OK
    assert (tmp_path / "data" / "tenant-a" / "ticks.sqlite3").exists()


def test_run_invalid_tenant(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(_run_args(tmp_path, tenant="Bad Tenant!"))
    assert exit_code == EXIT_ERROR
    assert "invalid tenant id" in capsys.readouterr().err


def test_run_missing_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        [
            "run",
            "--tenant",
            "tenant-a",
            "--input",
            str(tmp_path / "nope.json"),
            "--data-root",
            str(tmp_path / "data"),
        ]
    )
    assert exit_code == EXIT_ERROR
    assert "replay input could not be loaded" in capsys.readouterr().err


def test_run_empty_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / "empty.json"
    input_path.write_text('{"version": 1, "observations": []}', encoding="utf-8")
    exit_code = main(
        [
            "run",
            "--tenant",
            "tenant-a",
            "--input",
            str(input_path),
            "--data-root",
            str(tmp_path / "data"),
        ]
    )
    assert exit_code == EXIT_ERROR
    assert "replay input could not be loaded" in capsys.readouterr().err


def test_run_invalid_observation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / "bad.json"
    input_path.write_text(json.dumps([{"tick": 1}]), encoding="utf-8")
    exit_code = main(
        [
            "run",
            "--tenant",
            "tenant-a",
            "--input",
            str(input_path),
            "--data-root",
            str(tmp_path / "data"),
        ]
    )
    assert exit_code == EXIT_ERROR
    assert "replay input could not be loaded" in capsys.readouterr().err


def test_health_ready_exit_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_root = tmp_path / "data"
    assert main(_run_args(tmp_path)) == EXIT_OK
    capsys.readouterr()

    exit_code = main(["health", "--tenant", "tenant-a", "--data-root", str(data_root)])
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True


def test_health_not_ready_exit_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_root = tmp_path / "data"
    health = HealthSnapshot(
        schema_version=1,
        ready=False,
        status="stopped",
        tenant_id="tenant-a",
        process_run_id="unknown",
        run_id="run-x",
        started_at_ns=1,
        updated_at_ns=1,
        last_tick=0,
        ticks_processed=0,
        duplicate_ticks=0,
        out_of_order_ticks=0,
        gap_ticks=0,
        reconnect_count=0,
        stopped_reason=None,
        components=(),
        last_error="run failed",
        completed=False,
    )
    _write_health(data_root / "tenant-a" / "health.json", health)

    exit_code = main(["health", "--tenant", "tenant-a", "--data-root", str(data_root)])
    assert exit_code == EXIT_NOT_READY
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["completed"] is False


def test_health_missing_snapshot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["health", "--tenant", "tenant-a", "--data-root", str(tmp_path / "data")])
    assert exit_code == EXIT_ERROR
    assert "no health snapshot" in capsys.readouterr().err


async def test_cancel_shuts_down_and_maps_interrupt_exit_code(
    tmp_path: Path,
) -> None:
    gate = BlockGate()
    args = build_parser().parse_args(_run_args(tmp_path))
    state = RunState()
    task = asyncio.create_task(
        _run_async(
            args,
            shutdown_timeout=0.5,
            state=state,
            source_factory=lambda observations: BlockingSource(observations, gate),
        )
    )
    await gate.entered.wait()
    task.cancel()
    result = await task

    assert result == EXIT_INTERRUPT
    assert state.recorder is not None and state.recorder.closed is True
    assert state.sink is not None and state.sink.writer.closed is True
    assert state.source is not None and state.source.closed is True

    health_path = tmp_path / "data" / "tenant-a" / "health.json"
    assert health_path.exists()
    payload = json.loads(health_path.read_text(encoding="utf-8"))
    assert payload["ready"] is False
    assert payload["completed"] is False


def test_output_privacy_scan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_root = tmp_path / "data"
    assert main(_run_args(tmp_path)) == EXIT_OK
    assert main(["health", "--tenant", "tenant-a", "--data-root", str(data_root)]) == EXIT_OK
    assert main(_run_args(tmp_path, tenant="Bad Tenant!")) == EXIT_ERROR
    assert (
        main(_run_args(tmp_path, tenant="tenant-a", input=str(tmp_path / "missing.json")))
        == EXIT_ERROR
    )
    assert (
        main(
            [
                "health",
                "--tenant",
                "tenant-a",
                "--data-root",
                str(tmp_path / "data"),
            ]
        )
        == EXIT_OK
    )

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for forbidden in FORBIDDEN_OUTPUT:
        assert forbidden not in combined
