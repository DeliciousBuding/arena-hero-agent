"""CLI ``live`` command: fake-client end-to-end, lease fencing, and privacy."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from arena_hero import (
    Accepted,
    AsyncGameEvent,
    AsyncTurn,
    ChampionBeacon,
    CommandPlan,
    CommandSource,
    CoreState,
    CoreView,
    PlayerState,
    PlayerStatus,
    TransportError,
    UnitType,
    UnitView,
)

from arena_hero_agent.adapters.recorder import (
    RecorderBackend,
    RecorderConfig,
    open_tick_recorder,
)
from arena_hero_agent.adapters.runtime.process_leases import FileWriterLeaseCoordinator
from arena_hero_agent.adapters.sdk import LiveSubmitter, LiveTurnSource
from arena_hero_agent.adapters.telemetry import RuntimeTraceJsonlSink
from arena_hero_agent.application import RuntimeStatus, TenantRuntime, TickLoopConfig
from arena_hero_agent.cli.main import (
    EXIT_ERROR,
    EXIT_INTERRUPT,
    EXIT_OK,
    LiveState,
    _acquire_live_writer,
    _run_live_async,
    _run_live_loop,
    build_parser,
)
from arena_hero_agent.domain import DeadlineBudget, DecisionId, Generation, TenantId
from arena_hero_agent.strategies.composition import compose_decider

WORKER_ID = "aaaaaaaa-0000-0000-0000-000000000001"
CORE_ID = "cccccccc-0000-0000-0000-000000000004"
API_KEY = "test-live-key-do-not-log"
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


async def _noop_submit(plan: CommandPlan, idempotency_key: str | None = None) -> Accepted:
    raise AssertionError("fake client never calls the SDK turn callback")


def _worker(*, x: int = 0, y: int = 0) -> UnitView:
    return UnitView(
        kind="UNIT",
        id=WORKER_ID,
        controlled=True,
        position=(x, y),
        hp=4,
        unit_type=UnitType.WORKER,
        cargo=1,
    )


def _core() -> CoreView:
    return CoreView(
        kind="CORE",
        id=CORE_ID,
        controlled=True,
        owner_username="player",
        position=(0, 0),
        hp=10,
        shield=4,
        state=CoreState.NORMAL,
    )


def _turn(*, tick: int, resources: int = 3) -> AsyncTurn:
    return AsyncTurn(
        tick=tick,
        state=PlayerState(
            status=PlayerStatus.ACTIVE,
            resources=resources,
            population=1,
            champion_beacon=ChampionBeacon(position=(1, 2)),
            objects=(_worker(), _core()),
            events=(),
        ),
        submitter=_noop_submit,
    )


def _events(*items: object, error: BaseException | None = None) -> AsyncIterator[AsyncGameEvent]:
    async def iterate() -> AsyncIterator[AsyncGameEvent]:
        for item in items:
            yield cast(AsyncGameEvent, item)
        if error is not None:
            raise error

    return iterate()


class _StreamResult:
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0


class _EventStream:
    def __init__(
        self,
        client: FakeLiveClient,
        events: AsyncIterator[AsyncGameEvent],
        result: _StreamResult,
    ) -> None:
        self._client = client
        self._events = events
        self._result = result
        self._closed = False

    def __aiter__(self) -> AsyncIterator[AsyncGameEvent]:
        return self

    async def __anext__(self) -> AsyncGameEvent:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._events.__anext__()
        except StopAsyncIteration:
            if self._client.block_after is not None:
                await self._client.block_after.wait()
            raise
        except BaseException:
            if self._client.block_after is not None:
                await self._client.block_after.wait()
            raise

    async def aclose(self) -> None:
        self._closed = True
        self._result.closed += 1


class FakeLiveClient:
    """GameClient double: bounded turn stream, recorded submissions/closes."""

    def __init__(
        self,
        *,
        turns: tuple[AsyncTurn, ...] = (),
        event_error: BaseException | None = None,
        block_after: asyncio.Event | None = None,
        submit_error: BaseException | None = None,
    ) -> None:
        self.turns = turns
        self.event_error = event_error
        self.block_after = block_after
        self.submit_error = submit_error
        self.submissions: list[tuple[CommandPlan, DecisionId]] = []
        self.close_calls = 0
        self.close_lock = asyncio.Lock()
        self._stream_result = _StreamResult()

    def events(self) -> AsyncIterator[AsyncGameEvent]:
        self._stream_result.started += 1
        return _EventStream(self, _events(*self.turns, error=self.event_error), self._stream_result)

    async def submit(self, plan: CommandPlan, *, decision_id: DecisionId) -> Accepted:
        self.submissions.append((plan, decision_id))
        if self.submit_error is not None:
            raise self.submit_error
        return Accepted(
            accepted=True,
            tick=plan.tick,
            source=CommandSource.AGENT,
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def close(self) -> None:
        async with self.close_lock:
            self.close_calls += 1

    @property
    def stream_result(self) -> _StreamResult:
        return self._stream_result


def _live_args(tmp_path: Path, **overrides: object) -> list[str]:
    args = [
        "live",
        "--tenant",
        "t4",
        "--data-root",
        str(tmp_path / "data"),
    ]
    for key, value in overrides.items():
        args.extend([f"--{key.replace('_', '-')}", str(value)])
    return args


def _make_client(turns: tuple[AsyncTurn, ...] = (_turn(tick=1), _turn(tick=2))) -> FakeLiveClient:
    return FakeLiveClient(turns=turns)


def _coordinator_factory(root: Path) -> FileWriterLeaseCoordinator:
    return FileWriterLeaseCoordinator(
        root,
        lease_duration_ns=5 * 60_000_000_000,
        holder_id="test-live",
    )


def test_live_parser_defaults(tmp_path: Path) -> None:
    args = build_parser().parse_args(_live_args(tmp_path))
    assert args.command == "live"
    assert args.tenant == "t4"
    assert args.tick_budget_ms == 100
    assert args.max_reconnects == 3
    assert args.base_url is None


async def test_live_e2e_fake_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARENA_HERO_API_KEY", API_KEY)
    data_root = tmp_path / "data"
    client = _make_client()
    state = LiveState()
    args = build_parser().parse_args(_live_args(tmp_path))

    exit_code = await _run_live_async(
        args,
        shutdown_timeout=1.0,
        state=state,
        client_factory=lambda api_key, base_url: client,
        lease_factory=lambda: _coordinator_factory(data_root),
    )

    assert exit_code == EXIT_OK
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ready"] is True
    assert payload["completed"] is True
    assert payload["tenantId"] == "t4"
    assert payload["lastTick"] == 2
    assert payload["ticksProcessed"] == 2

    assert len(client.submissions) == 2
    for plan, decision_id in client.submissions:
        assert plan.tick in (1, 2)
        assert decision_id.value.startswith("decision:")

    tenant_dir = data_root / "t4"
    assert (tenant_dir / "health.json").exists()
    assert (tenant_dir / "telemetry.jsonl").exists()
    ticks_lines = (tenant_dir / "ticks.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(ticks_lines) == 3  # two tick records plus one loop record

    # The lease is released and the client closed after a clean session.
    assert client.close_calls == 1
    observed = FileWriterLeaseCoordinator(
        data_root, lease_duration_ns=5 * 60_000_000_000, holder_id="probe"
    ).observed_writer_lease(TenantId("t4"))
    assert observed is not None
    assert observed.fencing_token.value == 1
    assert observed.expires_at_ns <= time.time_ns()


async def test_live_missing_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ARENA_HERO_API_KEY", raising=False)
    args = build_parser().parse_args(_live_args(tmp_path))
    exit_code = await _run_live_async(args, shutdown_timeout=1.0)
    assert exit_code == EXIT_ERROR
    assert "ARENA_HERO_API_KEY is not set" in capsys.readouterr().err


async def test_live_lease_conflict_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARENA_HERO_API_KEY", API_KEY)
    data_root = tmp_path / "data"
    coordinator = _coordinator_factory(data_root)
    holder = await coordinator.acquire_writer(
        TenantId("t4"), Generation(1), DeadlineBudget.from_milliseconds(1000)
    )
    assert holder is not None
    args = build_parser().parse_args(_live_args(tmp_path))

    exit_code = await _run_live_async(
        args,
        shutdown_timeout=1.0,
        client_factory=lambda api_key, base_url: _make_client(),
        lease_factory=lambda: coordinator,
    )

    assert exit_code == EXIT_ERROR
    assert "writer lease is not available" in capsys.readouterr().err
    await holder.release()


async def test_live_takeover_after_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARENA_HERO_API_KEY", API_KEY)
    data_root = tmp_path / "data"
    coordinator = _coordinator_factory(data_root)
    first = await coordinator.acquire_writer(
        TenantId("t4"), Generation(1), DeadlineBudget.from_milliseconds(1000)
    )
    assert first is not None
    await first.release()

    client = _make_client((_turn(tick=1),))
    args = build_parser().parse_args(_live_args(tmp_path))
    exit_code = await _run_live_async(
        args,
        shutdown_timeout=1.0,
        client_factory=lambda api_key, base_url: client,
        lease_factory=lambda: coordinator,
    )
    assert exit_code == EXIT_OK
    capsys.readouterr()

    observed = coordinator.observed_writer_lease(TenantId("t4"))
    assert observed is not None
    assert observed.fencing_token.value == 2
    assert len(client.submissions) == 1


async def test_live_cancel_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARENA_HERO_API_KEY", API_KEY)
    data_root = tmp_path / "data"
    gate = asyncio.Event()
    client = FakeLiveClient(turns=(_turn(tick=1), _turn(tick=2)), block_after=gate)
    state = LiveState()
    args = build_parser().parse_args(_live_args(tmp_path))

    task = asyncio.create_task(
        _run_live_async(
            args,
            shutdown_timeout=1.0,
            state=state,
            client_factory=lambda api_key, base_url: client,
            lease_factory=lambda: _coordinator_factory(data_root),
        )
    )
    while not client.submissions:
        await asyncio.sleep(0.01)
    gate.set()
    task.cancel()
    result = await task

    assert result == EXIT_INTERRUPT
    assert state.lease is not None
    assert client.close_calls == 1
    observed = FileWriterLeaseCoordinator(
        data_root, lease_duration_ns=5 * 60_000_000_000, holder_id="probe"
    ).observed_writer_lease(TenantId("t4"))
    assert observed is not None
    assert observed.expires_at_ns <= time.time_ns()
    health_path = tmp_path / "data" / "t4" / "health.json"
    assert health_path.exists()
    payload = json.loads(health_path.read_text(encoding="utf-8"))
    assert payload["ready"] is False
    assert payload["completed"] is False


async def test_live_lease_lost_stops_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARENA_HERO_API_KEY", API_KEY)
    data_root = tmp_path / "data"
    gate = asyncio.Event()
    client = FakeLiveClient(turns=(_turn(tick=1), _turn(tick=2)), block_after=gate)
    args = build_parser().parse_args(_live_args(tmp_path))

    coordinator = FileWriterLeaseCoordinator(
        data_root,
        lease_duration_ns=30_000_000,
        holder_id="test-live",
    )
    exit_code = await _run_live_async(
        args,
        shutdown_timeout=1.0,
        client_factory=lambda api_key, base_url: client,
        lease_factory=lambda: coordinator,
        lease_renew_interval=0.1,
    )

    assert exit_code == EXIT_ERROR
    assert "writer lease lost" in capsys.readouterr().err
    assert client.close_calls == 1
    observed = coordinator.observed_writer_lease(TenantId("t4"))
    assert observed is not None
    assert observed.expires_at_ns <= time.time_ns()


async def test_live_reconnect_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARENA_HERO_API_KEY", API_KEY)
    client = FakeLiveClient(turns=(), event_error=TransportError("stream down"))
    args = build_parser().parse_args(_live_args(tmp_path, max_reconnects=0))

    exit_code = await _run_live_async(
        args,
        shutdown_timeout=1.0,
        client_factory=lambda api_key, base_url: client,
        lease_factory=lambda: _coordinator_factory(tmp_path / "data"),
    )

    assert exit_code == EXIT_ERROR
    assert "live session failed" in capsys.readouterr().err


async def test_live_output_privacy_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARENA_HERO_API_KEY", API_KEY)
    data_root = tmp_path / "data"

    assert (
        await _run_live_async(
            build_parser().parse_args(_live_args(tmp_path)),
            shutdown_timeout=1.0,
            client_factory=lambda api_key, base_url: _make_client((_turn(tick=1),)),
            lease_factory=lambda: _coordinator_factory(data_root),
        )
        == EXIT_OK
    )
    capsys.readouterr()

    monkeypatch.delenv("ARENA_HERO_API_KEY", raising=False)
    assert (
        await _run_live_async(
            build_parser().parse_args(_live_args(tmp_path)),
            shutdown_timeout=1.0,
            client_factory=lambda api_key, base_url: _make_client((_turn(tick=1),)),
            lease_factory=lambda: _coordinator_factory(data_root),
        )
        == EXIT_ERROR
    )

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for forbidden in FORBIDDEN_OUTPUT:
        assert forbidden not in combined
    assert API_KEY not in combined


async def test_live_shutdown_cancels_runtime_before_sinks_close(tmp_path: Path) -> None:
    """SIGTERM-style cancellation must stop the runtime before sinks close.

    Regression for the live shutdown race: ``_run_live_loop`` was cancelled
    while its runtime task was still running, so ``_execute_live`` closed the
    recorder and telemetry sink in its ``finally`` while the runtime could
    still call ``record_tick`` / ``emit_loop`` (and later ``record_loop`` /
    ``emit_loop``) against the closed sinks and mark recorder / telemetry
    unhealthy in the final health snapshot.
    """
    data_root = tmp_path / "data"
    gate = asyncio.Event()
    client = FakeLiveClient(turns=(_turn(tick=1), _turn(tick=2)), block_after=gate)
    tenant = TenantId("t4")
    recorder = open_tick_recorder(
        RecorderConfig(data_root=data_root, tenant_id=tenant),
        backend=RecorderBackend.JSONL,
    )
    telemetry_path = data_root / "t4" / "telemetry.jsonl"
    telemetry_path.parent.mkdir(parents=True, exist_ok=True)
    sink = RuntimeTraceJsonlSink(tenant_id=tenant, path=telemetry_path)
    runtime = TenantRuntime(
        TickLoopConfig(
            tenant_id=tenant,
            tick_budget=DeadlineBudget.from_milliseconds(100),
            max_reconnects=0,
        ),
        recorder=recorder,
        telemetry=sink,
        process_run_id="shutdown-race-test",
    )
    coordinator = FileWriterLeaseCoordinator(
        data_root,
        lease_duration_ns=5 * 60_000_000_000,
        holder_id="test-live",
    )
    handle = await _acquire_live_writer(coordinator, tenant)
    assert handle is not None

    loop_task = asyncio.create_task(
        _run_live_loop(
            runtime,
            LiveTurnSource(client),
            compose_decider(),
            LiveSubmitter(client, tenant_id=tenant),
            handle,
            renew_interval=0.05,
        )
    )
    try:
        for _ in range(500):
            if len(client.submissions) == 2:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("runtime never processed two ticks")

        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task

        # _execute_live closes the recorder and sink right after cancellation;
        # the fixed loop has already fully stopped the runtime task by now.
        recorder.close()
        await sink.close()
        # A pre-fix runtime task would still be running here; unblock it so it
        # can finish its loop and expose the closed-sink pollution.
        gate.set()
        for _ in range(500):
            if runtime.snapshot().status is RuntimeStatus.STOPPED:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("runtime never stopped after shutdown")
    finally:
        if not loop_task.done():
            loop_task.cancel()
            with contextlib.suppress(BaseException):
                await loop_task
        gate.set()
        recorder.close()
        await sink.close()
        await handle.release()

    snapshot = runtime.snapshot()
    components = {component.name: component for component in snapshot.components}
    assert snapshot.status is RuntimeStatus.STOPPED
    assert components["recorder"].healthy is True, components["recorder"].message
    assert components["telemetry"].healthy is True, components["telemetry"].message
    assert components["source"].healthy is False
