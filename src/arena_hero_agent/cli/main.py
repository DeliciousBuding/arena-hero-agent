"""Offline CLI: replay-based run and health contracts for one tenant.

P4-7 implements the offline process contract:

- ``arena-hero-agent run --tenant <id> --input <path> ...`` replays canonical
  turn observations through the single-tenant tick loop (P4-4), the offline
  recorder (P4-5), and the runtime telemetry port (P4-6), then persists and
  prints a stable health snapshot.
- ``arena-hero-agent health --tenant <id> ...`` reads that snapshot and
  reports ready/not-ready through its exit code.

P4-20 makes the runtime a deterministic offline contestant that Lab can
consume in bulk:

- every successful ``run`` persists a content-addressed ``manifest.json``
  (per-artifact and combined SHA-256 digests over health, telemetry, and
  ticks with non-semantic timestamps stripped);
- ``arena-hero-agent batch --input-dir <dir> ...`` replays every regular file
  in a directory as one scenario with a stable ``scenario-<name>-seed-<n>``
  run id, writing each scenario under ``<data-root>/<run-id>/<tenant>/``;
- explicit duplicate run ids fail closed before any artifact is written.

Safety contract: this module never reads credentials or environment secrets,
never opens network connections, never submits to a live game API, and never
accepts an API key. Errors are reported as fixed safe summaries; health output
contains no keys, cookies, absolute local paths, or tracebacks.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import signal
import sys
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arena_hero_agent.adapters.recorder import (
    RecorderBackend,
    RecorderConfig,
    open_tick_recorder,
)
from arena_hero_agent.adapters.replay import (
    ReplayError,
    ReplayTickSource,
    load_observations,
)
from arena_hero_agent.adapters.telemetry import RuntimeTraceJsonlSink
from arena_hero_agent.application import (
    CoreAction,
    CoreIntent,
    Decision,
    RuntimeSnapshot,
    SubmitOutcome,
    TenantRuntime,
    TickLoopConfig,
    TickSource,
)
from arena_hero_agent.application.turns import TurnObservation
from arena_hero_agent.cli.canonical import (
    MANIFEST_FILENAME,
    build_manifest,
    read_manifest,
)
from arena_hero_agent.domain import DeadlineBudget, TenantId, canonical_sha256

PROG = "arena-hero-agent"

EXIT_OK = 0
EXIT_NOT_READY = 1
EXIT_ERROR = 2
EXIT_INTERRUPT = 130
EXIT_TERMINATED = 143

HEALTH_SCHEMA_VERSION = 1
DEFAULT_DATA_ROOT = ".arena-hero-agent/data"
DEFAULT_TICK_BUDGET_MS = 100
DEFAULT_MAX_RECONNECTS = 3
SHUTDOWN_TIMEOUT_SECONDS = 5.0
HEALTH_FILENAME = "health.json"
TELEMETRY_FILENAME = "telemetry.jsonl"
BATCH_RUN_ID_PREFIX = "scenario-"
BATCH_RUN_ID_SEED_MARKER = "-seed-"

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_COMPONENT_MESSAGES = frozenset(
    {"cancelled", "initialized", "not_configured", "not_started", "ok", "started"}
)
_SAFE_LAST_ERRORS = frozenset({"cancelled"})


@dataclass(frozen=True, slots=True)
class ComponentState:
    """One safe, path-free readiness signal for a runtime component."""

    name: str
    healthy: bool
    message: str | None


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """Stable, environment-neutral health document persisted by ``run``."""

    schema_version: int
    ready: bool
    status: str
    tenant_id: str
    process_run_id: str
    run_id: str
    started_at_ns: int | None
    updated_at_ns: int
    last_tick: int | None
    ticks_processed: int | None
    duplicate_ticks: int | None
    out_of_order_ticks: int | None
    gap_ticks: int | None
    reconnect_count: int | None
    stopped_reason: str | None
    components: tuple[ComponentState, ...]
    last_error: str | None
    completed: bool

    def to_json_object(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "ready": self.ready,
            "status": self.status,
            "tenantId": self.tenant_id,
            "processRunId": self.process_run_id,
            "runId": self.run_id,
            "startedAtNs": self.started_at_ns,
            "updatedAtNs": self.updated_at_ns,
            "lastTick": self.last_tick,
            "ticksProcessed": self.ticks_processed,
            "duplicateTicks": self.duplicate_ticks,
            "outOfOrderTicks": self.out_of_order_ticks,
            "gapTicks": self.gap_ticks,
            "reconnectCount": self.reconnect_count,
            "stoppedReason": self.stopped_reason,
            "components": [
                {"name": component.name, "healthy": component.healthy, "message": component.message}
                for component in self.components
            ],
            "lastError": self.last_error,
            "completed": self.completed,
        }


@dataclass(slots=True)
class RunState:
    """Live handles created by one run, for bounded shutdown inspection."""

    recorder: Any = None
    sink: RuntimeTraceJsonlSink | None = None
    source: Any = None
    runtime: TenantRuntime | None = None


class WaitDecider:
    """Deterministic offline decider: a WAIT core intent for every tick."""

    def __call__(self, observation: TurnObservation, budget: DeadlineBudget) -> Decision:
        del budget
        return Decision(tick=observation.tick, core_intent=CoreIntent(CoreAction.WAIT))


class LocalSubmitter:
    """Offline submitter that accepts every decision locally; never sends network."""

    async def __call__(
        self,
        decision: Decision,
        observation: TurnObservation,
    ) -> SubmitOutcome:
        del decision, observation
        return SubmitOutcome(accepted=True)


def _safe_component_message(message: str | None) -> str | None:
    if message is None or message in _SAFE_COMPONENT_MESSAGES:
        return message
    return "unhealthy"


def _safe_last_error(value: str | None) -> str | None:
    if value is None or value in _SAFE_LAST_ERRORS:
        return value
    return "run failed"


def _health_from_snapshot(
    snapshot: RuntimeSnapshot,
    *,
    completed: bool,
) -> HealthSnapshot:
    ready = (
        completed
        and snapshot.last_error is None
        and all(component.healthy for component in snapshot.components)
    )
    return HealthSnapshot(
        schema_version=HEALTH_SCHEMA_VERSION,
        ready=ready,
        status=snapshot.status.value,
        tenant_id=snapshot.tenant_id.value,
        process_run_id=snapshot.process_run_id,
        run_id=snapshot.run_id,
        started_at_ns=snapshot.started_at_ns,
        updated_at_ns=snapshot.updated_at_ns,
        last_tick=snapshot.last_tick,
        ticks_processed=snapshot.ticks_processed,
        duplicate_ticks=snapshot.duplicate_ticks,
        out_of_order_ticks=snapshot.out_of_order_ticks,
        gap_ticks=snapshot.gap_ticks,
        reconnect_count=snapshot.reconnect_count,
        stopped_reason=(
            snapshot.stopped_reason.value if snapshot.stopped_reason is not None else None
        ),
        components=tuple(
            ComponentState(
                name=component.name,
                healthy=component.healthy,
                message=_safe_component_message(component.message),
            )
            for component in snapshot.components
        ),
        last_error=_safe_last_error(snapshot.last_error),
        completed=completed,
    )


def _tenant_dir(data_root: str | Path, tenant: TenantId) -> Path:
    return Path(data_root) / tenant.value


def _health_path(data_root: str | Path, tenant: TenantId) -> Path:
    return _tenant_dir(data_root, tenant) / HEALTH_FILENAME


def _telemetry_path(data_root: str | Path, tenant: TenantId) -> Path:
    return _tenant_dir(data_root, tenant) / TELEMETRY_FILENAME


def _manifest_path(data_root: str | Path, tenant: TenantId) -> Path:
    return _tenant_dir(data_root, tenant) / MANIFEST_FILENAME


def _parse_tenant(value: str) -> TenantId | None:
    try:
        return TenantId(value)
    except (TypeError, ValueError):
        return None


def _parse_backend(value: str) -> RecorderBackend | None:
    try:
        return RecorderBackend(value)
    except ValueError:
        return None


def _print_health(health: HealthSnapshot) -> None:
    print(json.dumps(health.to_json_object(), ensure_ascii=False, indent=2, sort_keys=True))


def _print_error(message: str) -> None:
    print(f"{PROG}: error: {message}", file=sys.stderr)


def _write_health(path: Path, health: HealthSnapshot) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(health.to_json_object(), ensure_ascii=False, indent=2, sort_keys=True)
        path.write_text(payload + "\n", encoding="utf-8")
    except OSError:
        _print_error("health snapshot could not be written")


def _write_manifest(path: Path, manifest: dict[str, object]) -> bool:
    """Persist a run manifest; returns False when the artifact is incomplete."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        path.write_text(payload + "\n", encoding="utf-8")
        return True
    except OSError:
        _print_error("run manifest could not be written")
        return False


async def _bounded(operation: Awaitable[None], timeout: float) -> None:
    try:
        await asyncio.wait_for(operation, timeout=timeout)
    except TimeoutError:
        pass
    except Exception:
        pass


async def _sync_close(recorder: Any) -> None:
    recorder.close()


async def _execute_run(
    args: argparse.Namespace,
    *,
    shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
    state: RunState | None = None,
    source_factory: Callable[[tuple[TurnObservation, ...]], TickSource] | None = None,
) -> int:
    """Run one offline replay and return a process exit code.

    Only ``asyncio.CancelledError`` propagates (after bounded cleanup); all
    other failures are converted to a safe error summary and exit code.
    """
    tenant = _parse_tenant(args.tenant)
    if tenant is None:
        _print_error("invalid tenant id")
        return EXIT_ERROR
    if args.run_id is not None and _SAFE_RUN_ID.fullmatch(args.run_id) is None:
        _print_error("invalid run id")
        return EXIT_ERROR
    backend = _parse_backend(args.backend)
    if backend is None:
        _print_error("invalid recorder backend")
        return EXIT_ERROR
    if isinstance(args.tick_budget_ms, bool) or args.tick_budget_ms <= 0:
        _print_error("tick budget must be a positive integer")
        return EXIT_ERROR
    if isinstance(args.max_reconnects, bool) or args.max_reconnects < 0:
        _print_error("max reconnects must be a non-negative integer")
        return EXIT_ERROR
    try:
        observations = load_observations(args.input)
    except (ReplayError, OSError):
        _print_error("replay input could not be loaded")
        return EXIT_ERROR
    if args.run_id is not None:
        existing = read_manifest(_manifest_path(args.data_root, tenant))
        if existing is not None and existing.get("runId") == args.run_id:
            _print_error("run id conflict: run id already recorded for this tenant")
            return EXIT_ERROR

    factory = source_factory if source_factory is not None else ReplayTickSource
    source = factory(observations)
    recorder: Any = None
    sink: RuntimeTraceJsonlSink | None = None
    try:
        recorder = open_tick_recorder(
            RecorderConfig(data_root=args.data_root, tenant_id=tenant),
            backend=backend,
        )
        telemetry_path = _telemetry_path(args.data_root, tenant)
        telemetry_path.parent.mkdir(parents=True, exist_ok=True)
        sink = RuntimeTraceJsonlSink(tenant_id=tenant, path=telemetry_path)
        runtime = TenantRuntime(
            TickLoopConfig(
                tenant_id=tenant,
                tick_budget=DeadlineBudget.from_milliseconds(args.tick_budget_ms),
                max_reconnects=args.max_reconnects,
            ),
            recorder=recorder,
            telemetry=sink,
            run_id=args.run_id,
        )
    except Exception:
        _print_error("runtime storage could not be initialized")
        if recorder is not None:
            await _bounded(_sync_close(recorder), shutdown_timeout)
        if sink is not None:
            await _bounded(sink.close(), shutdown_timeout)
        return EXIT_ERROR

    if state is not None:
        state.recorder = recorder
        state.sink = sink
        state.source = source
        state.runtime = runtime

    completed = False
    cancelled = False
    try:
        await runtime.run(source, WaitDecider(), LocalSubmitter())
        completed = True
    except asyncio.CancelledError:
        cancelled = True
    except Exception:
        completed = False
    finally:
        if recorder is not None:
            await _bounded(_sync_close(recorder), shutdown_timeout)
        if sink is not None:
            await _bounded(sink.close(), shutdown_timeout)
        close_source = getattr(source, "close", None)
        if callable(close_source):
            close_source()

    snapshot = runtime.snapshot()
    health = _health_from_snapshot(snapshot, completed=completed)
    _write_health(_health_path(args.data_root, tenant), health)
    if cancelled:
        raise asyncio.CancelledError
    if completed:
        manifest_ok = _write_manifest(
            _manifest_path(args.data_root, tenant),
            build_manifest(
                _tenant_dir(args.data_root, tenant),
                tenant_id=tenant.value,
                run_id=snapshot.run_id,
                process_run_id=snapshot.process_run_id,
            ),
        )
        if not manifest_ok:
            return EXIT_ERROR
        _print_health(health)
        return EXIT_OK
    _print_error("run failed")
    return EXIT_ERROR


def _make_signal_handler(
    on_signal: Callable[[int], None],
    signum: int,
) -> Callable[[int, Any], None]:
    def _handler(_signum: int, _frame: Any) -> None:
        on_signal(signum)

    return _handler


def _install_signal_handlers(
    on_signal: Callable[[int], None],
) -> tuple[tuple[int, Any], ...]:
    """Install SIGINT/SIGTERM handlers where the platform permits.

    Uses the stdlib ``signal.signal`` path, which works on Windows for both
    SIGINT and SIGTERM even where ``loop.add_signal_handler`` is unavailable.
    Returns ``(signum, previous_handler)`` pairs for restoration.
    """
    installed: list[tuple[int, Any]] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous = signal.getsignal(signum)
            signal.signal(signum, _make_signal_handler(on_signal, signum))
        except (OSError, RuntimeError, ValueError):
            continue
        installed.append((signum, previous))
    return tuple(installed)


def _restore_signal_handlers(installed: tuple[tuple[int, Any], ...]) -> None:
    for signum, previous in installed:
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            signal.signal(signum, previous)


def _exit_code_for_signal(signum: int | None) -> int:
    if signum is signal.SIGTERM:
        return EXIT_TERMINATED
    return EXIT_INTERRUPT


async def _run_cancellable(operation: Callable[[], Coroutine[Any, Any, int]]) -> int:
    """Run one async CLI operation with bounded SIGINT/SIGTERM handling."""
    loop = asyncio.get_running_loop()
    requests: list[int] = []
    run_task = asyncio.create_task(operation())

    def _on_signal(signum: int) -> None:
        requests.append(signum)
        loop.call_soon_threadsafe(run_task.cancel)

    installed = _install_signal_handlers(_on_signal)
    try:
        return await run_task
    except asyncio.CancelledError:
        run_task.cancel()
        with contextlib.suppress(BaseException):
            await run_task
        return _exit_code_for_signal(requests[-1] if requests else None)
    finally:
        _restore_signal_handlers(installed)


async def _run_async(
    args: argparse.Namespace,
    *,
    shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
    state: RunState | None = None,
    source_factory: Callable[[tuple[TurnObservation, ...]], TickSource] | None = None,
) -> int:
    return await _run_cancellable(
        lambda: _execute_run(
            args,
            shutdown_timeout=shutdown_timeout,
            state=state,
            source_factory=source_factory,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Offline Arena Hero agent runtime: replay-based run and health contracts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="run one offline replay for a tenant and persist a health snapshot",
    )
    run_parser.add_argument("--tenant", required=True, metavar="ID", help="tenant id")
    run_parser.add_argument(
        "--input",
        required=True,
        metavar="PATH",
        help="replay input: JSON array/object or JSONL of canonical turn observations",
    )
    run_parser.add_argument(
        "--data-root",
        default=DEFAULT_DATA_ROOT,
        metavar="PATH",
        help=f"output root for recorder, telemetry, and health (default: {DEFAULT_DATA_ROOT})",
    )
    run_parser.add_argument(
        "--backend",
        default=RecorderBackend.JSONL.value,
        choices=[backend.value for backend in RecorderBackend],
        help="recorder backend (default: jsonl)",
    )
    run_parser.add_argument(
        "--tick-budget-ms",
        type=int,
        default=DEFAULT_TICK_BUDGET_MS,
        metavar="MS",
        help="per-tick deadline budget in milliseconds (default: 100)",
    )
    run_parser.add_argument(
        "--max-reconnects",
        type=int,
        default=DEFAULT_MAX_RECONNECTS,
        metavar="N",
        help="stream reopen bound (default: 3)",
    )
    run_parser.add_argument(
        "--run-id",
        default=None,
        metavar="ID",
        help="optional stable run identifier (safe identifier characters only)",
    )

    batch_parser = subparsers.add_parser(
        "batch",
        help="run one offline replay per input file with deterministic scenario run ids",
    )
    batch_parser.add_argument(
        "--input-dir",
        required=True,
        metavar="DIR",
        help="directory of replay inputs; each regular file is one scenario",
    )
    batch_parser.add_argument("--tenant", required=True, metavar="ID", help="tenant id")
    batch_parser.add_argument(
        "--data-root",
        default=DEFAULT_DATA_ROOT,
        metavar="PATH",
        help=(
            "output root: one <run-id>/<tenant>/ directory per scenario "
            f"(default: {DEFAULT_DATA_ROOT})"
        ),
    )
    batch_parser.add_argument(
        "--backend",
        default=RecorderBackend.JSONL.value,
        choices=[backend.value for backend in RecorderBackend],
        help="recorder backend (default: jsonl)",
    )
    batch_parser.add_argument(
        "--tick-budget-ms",
        type=int,
        default=DEFAULT_TICK_BUDGET_MS,
        metavar="MS",
        help="per-tick deadline budget in milliseconds (default: 100)",
    )
    batch_parser.add_argument(
        "--max-reconnects",
        type=int,
        default=DEFAULT_MAX_RECONNECTS,
        metavar="N",
        help="stream reopen bound (default: 3)",
    )
    batch_parser.add_argument(
        "--seed",
        type=int,
        default=0,
        metavar="N",
        help="stable seed folded into each derived scenario run id (default: 0)",
    )

    health_parser = subparsers.add_parser(
        "health",
        help="read and print the persisted health snapshot for a tenant",
    )
    health_parser.add_argument("--tenant", required=True, metavar="ID", help="tenant id")
    health_parser.add_argument(
        "--data-root",
        default=DEFAULT_DATA_ROOT,
        metavar="PATH",
        help=f"data root written by run (default: {DEFAULT_DATA_ROOT})",
    )
    return parser


def _sanitize_health_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe = dict(payload)
    if isinstance(safe.get("lastError"), str):
        safe["lastError"] = _safe_last_error(safe["lastError"])
    components = safe.get("components")
    if isinstance(components, list):
        cleaned: list[Any] = []
        for component in components:
            if not isinstance(component, dict):
                cleaned.append(component)
                continue
            item = dict(component)
            if isinstance(item.get("message"), str):
                item["message"] = _safe_component_message(item["message"])
            cleaned.append(item)
        safe["components"] = cleaned
    return safe


def health_command(args: argparse.Namespace) -> int:
    tenant = _parse_tenant(args.tenant)
    if tenant is None:
        _print_error("invalid tenant id")
        return EXIT_ERROR
    path = _health_path(args.data_root, tenant)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _print_error("no health snapshot available")
        return EXIT_ERROR
    if not isinstance(payload, dict):
        _print_error("health snapshot is malformed")
        return EXIT_ERROR
    if payload.get("schemaVersion") != HEALTH_SCHEMA_VERSION:
        _print_error("health snapshot has an unsupported schema")
        return EXIT_ERROR
    safe_payload = _sanitize_health_payload(payload)
    print(json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_OK if safe_payload.get("ready") is True else EXIT_NOT_READY


def run_command(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_run_async(args))
    except KeyboardInterrupt:
        return EXIT_INTERRUPT


def _derive_scenario_run_id(stem: str, seed: int) -> str | None:
    """Derive a stable scenario run id from a filename stem and a seed.

    The result follows the ``scenario-<name>-seed-<n>`` shape and satisfies the
    ``_SAFE_RUN_ID`` contract. Returns ``None`` when the stem cannot produce a
    safe identifier (for example an empty result after cleaning).
    """
    if isinstance(seed, bool) or seed < 0:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9._:-]", "-", stem)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-").lstrip("._:-")
    if not cleaned:
        return None
    suffix = f"{BATCH_RUN_ID_SEED_MARKER}{seed}"
    limit = 128 - len(BATCH_RUN_ID_PREFIX) - len(suffix)
    if len(cleaned) > limit:
        tail = canonical_sha256(stem)[:8]
        cleaned = f"{cleaned[: max(0, limit - 8)]}{tail}"
    run_id = f"{BATCH_RUN_ID_PREFIX}{cleaned}{suffix}"
    if _SAFE_RUN_ID.fullmatch(run_id) is None:
        return None
    return run_id


def _plan_batch(
    args: argparse.Namespace,
) -> tuple[list[argparse.Namespace], str | None]:
    """Validate a batch request and build one run namespace per scenario.

    Returns ``(scenarios, None)`` on success or ``([], message)`` when any
    validation fails. Every check runs before any scenario executes, so a bad
    batch never leaves partial artifacts behind.
    """
    tenant = _parse_tenant(args.tenant)
    if tenant is None:
        return [], "invalid tenant id"
    if isinstance(args.seed, bool) or args.seed < 0:
        return [], "seed must be a non-negative integer"
    backend = _parse_backend(args.backend)
    if backend is None:
        return [], "invalid recorder backend"
    if isinstance(args.tick_budget_ms, bool) or args.tick_budget_ms <= 0:
        return [], "tick budget must be a positive integer"
    if isinstance(args.max_reconnects, bool) or args.max_reconnects < 0:
        return [], "max reconnects must be a non-negative integer"
    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        return [], "input directory not found"
    files = sorted(path for path in input_dir.iterdir() if path.is_file())
    if not files:
        return [], "no scenario inputs found in input directory"
    run_ids: dict[str, str] = {}
    scenarios: list[argparse.Namespace] = []
    for path in files:
        run_id = _derive_scenario_run_id(path.stem, args.seed)
        if run_id is None:
            return [], f"scenario file cannot produce a safe run id: {path.name}"
        if run_id in run_ids:
            return [], f"run id conflict: multiple scenario files map to run id {run_id!r}"
        run_ids[run_id] = path.name
        try:
            load_observations(path)
        except (ReplayError, OSError):
            return [], f"replay input could not be loaded: {path.name}"
        scenario_root = Path(args.data_root) / run_id
        existing = read_manifest(scenario_root / tenant.value / MANIFEST_FILENAME)
        if existing is not None and existing.get("runId") == run_id:
            return [], "run id conflict: run id already recorded for this tenant"
        scenarios.append(
            argparse.Namespace(
                command="run",
                tenant=args.tenant,
                input=str(path),
                data_root=str(scenario_root),
                backend=args.backend,
                tick_budget_ms=args.tick_budget_ms,
                max_reconnects=args.max_reconnects,
                run_id=run_id,
            )
        )
    return scenarios, None


async def _execute_scenarios(
    scenarios: Sequence[argparse.Namespace],
    *,
    shutdown_timeout: float,
) -> int:
    """Execute scenarios in sorted order, aborting on the first failure."""
    completed = 0
    for scenario in scenarios:
        code = await _execute_run(scenario, shutdown_timeout=shutdown_timeout)
        if code != EXIT_OK:
            _print_error(f"batch failed at scenario {scenario.run_id}")
            return EXIT_ERROR
        completed += 1
        print(f"scenario {scenario.run_id}: ok")
    print(f"batch: {completed} scenario(s) completed")
    return EXIT_OK


async def _run_batch_async(
    args: argparse.Namespace,
    *,
    shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
) -> int:
    scenarios, error = _plan_batch(args)
    if error is not None:
        _print_error(error)
        return EXIT_ERROR
    return await _run_cancellable(
        lambda: _execute_scenarios(scenarios, shutdown_timeout=shutdown_timeout)
    )


def batch_command(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_run_batch_async(args))
    except KeyboardInterrupt:
        return EXIT_INTERRUPT


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_command(args)
        if args.command == "batch":
            return batch_command(args)
        return health_command(args)
    except KeyboardInterrupt:
        return EXIT_INTERRUPT
    except SystemExit:
        raise
    except Exception:
        _print_error("internal error")
        return EXIT_ERROR


def console_entrypoint() -> None:
    raise SystemExit(main())


__all__ = [
    "DEFAULT_DATA_ROOT",
    "EXIT_ERROR",
    "EXIT_INTERRUPT",
    "EXIT_NOT_READY",
    "EXIT_OK",
    "EXIT_TERMINATED",
    "HealthSnapshot",
    "LocalSubmitter",
    "PROG",
    "RunState",
    "WaitDecider",
    "batch_command",
    "build_parser",
    "console_entrypoint",
    "health_command",
    "main",
    "run_command",
]
