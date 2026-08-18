"""Application-owned telemetry port and runtime trace record mappers.

P4-6 wires P4-4 per-tick outcomes and loop summaries through this port to the
existing ``arena_hero_agent.telemetry`` record schema (``RuntimeTraceRecord``)
and, via adapters, the shared JSONL writer. This module never imports concrete
paths, writers, frameworks, or the SDK; sink implementations live in
``arena_hero_agent.adapters.telemetry``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from arena_hero_agent.domain import TenantId, canonical_sha256
from arena_hero_agent.telemetry import (
    DEFAULT_PROCESS_RUN_ID,
    RuntimeTraceRecord,
    runtime_trace,
)

from .tick_loop import (
    DeadlineOutcome,
    StoppedReason,
    SubmitResult,
    TickLoopResult,
    TickResult,
)


def default_run_id(process_run_id: str, tenant_id: TenantId) -> str:
    """Deterministic run-level association key (mirrors the TypeScript oracle)."""
    return f"run-{canonical_sha256((process_run_id, tenant_id.value))[:12]}"


_LOOP_DEADLINE_OUTCOME: Mapping[StoppedReason, DeadlineOutcome] = {
    StoppedReason.STREAM_ENDED: DeadlineOutcome.NOT_APPLICABLE,
    StoppedReason.SOFT_DEADLINE: DeadlineOutcome.SOFT_DEADLINE,
    StoppedReason.SELECTION_TIMEOUT: DeadlineOutcome.SELECTION_TIMEOUT,
    StoppedReason.GAP: DeadlineOutcome.NOT_APPLICABLE,
    StoppedReason.SUBMIT_FAILURE: DeadlineOutcome.ERROR,
}


def runtime_trace_record(
    result: TickResult,
    *,
    tenant_id: TenantId,
    process_run_id: str = DEFAULT_PROCESS_RUN_ID,
    run_id: str | None = None,
    rotation_generation: int = 0,
    config_hash: str | None = None,
    strategy_hash: str | None = None,
) -> RuntimeTraceRecord:
    """Map one finalized tick outcome to a runtime trace record.

    The per-tick ``runId`` defaults to the deterministic decision id so the
    three trace streams stay correlated without wall-clock input.
    Latencies come from the monotonic timing captured by the tick loop.  Hashes
    are optional so offline callers can keep using this mapper without a
    strategy composition root.
    """
    fields: dict[str, object] = {
        "processRunId": process_run_id,
        "tenantId": tenant_id.value,
        "tick": result.tick,
        "runId": run_id if run_id is not None else str(result.decision_id),
        "deadlineOutcome": result.deadline_outcome.value,
        "agentLatencyMs": result.agent_latency_ms,
        "selectionLatencyMs": result.selection_latency_ms,
        "abortRequested": False,
        "rotationGeneration": rotation_generation,
        "submitResult": result.submit_result.value,
    }
    if config_hash is not None:
        fields["configHash"] = config_hash
    if strategy_hash is not None:
        fields["strategyHash"] = strategy_hash
    if result.submit_error is not None:
        fields["submitError"] = result.submit_error
    return runtime_trace(fields)


def loop_trace_record(
    result: TickLoopResult,
    *,
    tenant_id: TenantId,
    process_run_id: str = DEFAULT_PROCESS_RUN_ID,
    run_id: str | None = None,
    rotation_generation: int = 0,
    config_hash: str | None = None,
    strategy_hash: str | None = None,
) -> RuntimeTraceRecord:
    """Map a loop summary to a runtime trace record at the final tick.

    The schema has no dedicated loop-summary family; this reuses the runtime
    family with the stop reason mapped onto ``deadlineOutcome`` and
    ``submitResult=not_submitted``. Detailed counters live in recorder records
    and ``RuntimeSnapshot``, not in this record.
    """
    summary_tick = result.last_tick
    if summary_tick == 0 and result.outcomes:
        summary_tick = result.outcomes[-1].tick
    fields: dict[str, object] = {
        "processRunId": process_run_id,
        "tenantId": tenant_id.value,
        "tick": summary_tick,
        "runId": (run_id if run_id is not None else default_run_id(process_run_id, tenant_id)),
        "deadlineOutcome": _LOOP_DEADLINE_OUTCOME[result.stopped_reason].value,
        "agentLatencyMs": None,
        "selectionLatencyMs": 0,
        "abortRequested": False,
        "rotationGeneration": rotation_generation,
        "submitResult": SubmitResult.NOT_SUBMITTED.value,
    }
    if config_hash is not None:
        fields["configHash"] = config_hash
    if strategy_hash is not None:
        fields["strategyHash"] = strategy_hash
    return runtime_trace(fields)


@runtime_checkable
class RuntimeTelemetry(Protocol):
    """Best-effort runtime trace sink bound to the existing record schema."""

    async def emit_tick(self, result: TickResult) -> None: ...

    async def emit_loop(self, result: TickLoopResult) -> None: ...

    async def flush(self) -> None: ...

    async def close(self) -> None: ...


class NoopRuntimeTelemetry:
    """Default sink: every call is a no-op."""

    async def emit_tick(self, result: TickResult) -> None:
        del result

    async def emit_loop(self, result: TickLoopResult) -> None:
        del result

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        return None


class MemoryRuntimeTelemetry:
    """In-memory sink for offline tests and local development."""

    def __init__(self) -> None:
        self.ticks: list[TickResult] = []
        self.loops: list[TickLoopResult] = []
        self.flush_count = 0
        self.close_count = 0

    async def emit_tick(self, result: TickResult) -> None:
        self.ticks.append(result)

    async def emit_loop(self, result: TickLoopResult) -> None:
        self.loops.append(result)

    async def flush(self) -> None:
        self.flush_count += 1

    async def close(self) -> None:
        self.close_count += 1


__all__ = [
    "MemoryRuntimeTelemetry",
    "NoopRuntimeTelemetry",
    "RuntimeTelemetry",
    "default_run_id",
    "loop_trace_record",
    "runtime_trace_record",
]
