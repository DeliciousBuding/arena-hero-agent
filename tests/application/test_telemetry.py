"""Application telemetry port and runtime trace record mapper tests."""

from __future__ import annotations

from arena_hero_agent.application import (
    MemoryRuntimeTelemetry,
    NoopRuntimeTelemetry,
    RuntimeTelemetry,
    default_run_id,
    loop_trace_record,
    runtime_trace_record,
)
from arena_hero_agent.application.tick_loop import (
    DeadlineOutcome,
    StoppedReason,
    SubmitResult,
    TickLoopResult,
    TickResult,
)
from arena_hero_agent.domain import DecisionId, TenantId
from arena_hero_agent.telemetry import (
    RuntimeTraceRecord,
    to_json_object,
    validate_trace_record,
)

TENANT = TenantId("tenant-a")
DECISION = DecisionId("decision:sample")


def _tick(
    *,
    tick: int = 7,
    deadline: DeadlineOutcome = DeadlineOutcome.CANDIDATE,
    submit: SubmitResult = SubmitResult.ACCEPTED,
    submit_error: str | None = None,
) -> TickResult:
    return TickResult(
        tick=tick,
        decision_id=DECISION,
        deadline_outcome=deadline,
        submit_result=submit,
        submit_error=submit_error,
    )


def _loop(
    *,
    reason: StoppedReason = StoppedReason.STREAM_ENDED,
    last_tick: int = 7,
    outcomes: tuple[TickResult, ...] = (),
) -> TickLoopResult:
    return TickLoopResult(
        tenant_id=TENANT,
        last_tick=last_tick,
        ticks_processed=2,
        duplicate_ticks=0,
        out_of_order_ticks=0,
        gap_ticks=0,
        reconnect_count=1,
        stopped_reason=reason,
        outcomes=outcomes,
    )


def test_runtime_trace_record_maps_candidate_fields() -> None:
    record = runtime_trace_record(_tick(), tenant_id=TENANT)

    assert isinstance(record, RuntimeTraceRecord)
    validate_trace_record(record)
    data = to_json_object(record)
    assert data["tenantId"] == "tenant-a"
    assert data["tick"] == 7
    assert data["runId"] == "decision:sample"
    assert data["deadlineOutcome"] == "candidate"
    assert data["submitResult"] == "accepted"
    assert data["agentLatencyMs"] is None
    assert data["selectionLatencyMs"] == 0
    assert data["abortRequested"] is False
    assert data["rotationGeneration"] == 0
    assert data["processRunId"] == "unknown"
    assert "submitError" not in data


def test_runtime_trace_record_maps_rejected_submit_error() -> None:
    record = runtime_trace_record(
        _tick(submit=SubmitResult.REJECTED, submit_error="policy rejected"),
        tenant_id=TENANT,
        process_run_id="process-1",
        rotation_generation=3,
    )

    data = to_json_object(record)
    assert data["submitResult"] == "rejected"
    assert data["submitError"] == "policy rejected"
    assert data["processRunId"] == "process-1"
    assert data["rotationGeneration"] == 3


def test_runtime_trace_record_maps_deadline_outcomes() -> None:
    soft = runtime_trace_record(
        _tick(deadline=DeadlineOutcome.SOFT_DEADLINE, submit=SubmitResult.NOT_SUBMITTED),
        tenant_id=TENANT,
    )
    timed_out = runtime_trace_record(
        _tick(deadline=DeadlineOutcome.SELECTION_TIMEOUT, submit=SubmitResult.NOT_SUBMITTED),
        tenant_id=TENANT,
    )

    assert to_json_object(soft)["deadlineOutcome"] == "soft_deadline"
    assert to_json_object(soft)["submitResult"] == "not_submitted"
    assert to_json_object(timed_out)["deadlineOutcome"] == "selection_timeout"


def test_runtime_trace_record_explicit_run_id_wins() -> None:
    record = runtime_trace_record(_tick(), tenant_id=TENANT, run_id="run-abc123")

    assert to_json_object(record)["runId"] == "run-abc123"


def test_runtime_trace_record_has_no_credential_fields() -> None:
    record = runtime_trace_record(
        _tick(submit=SubmitResult.REJECTED, submit_error="rejected"),
        tenant_id=TENANT,
    )

    data = to_json_object(record)
    markers = ("token", "apikey", "api_key", "secret", "password", "cookie", "authorization")
    assert not any(marker in key.lower() for key in data for marker in markers)


def test_loop_trace_record_maps_stop_reason_vocabulary() -> None:
    ended = loop_trace_record(_loop(), tenant_id=TENANT, run_id="run-abc123")
    failed = loop_trace_record(
        _loop(reason=StoppedReason.SUBMIT_FAILURE),
        tenant_id=TENANT,
        run_id="run-abc123",
    )
    soft = loop_trace_record(
        _loop(reason=StoppedReason.SOFT_DEADLINE),
        tenant_id=TENANT,
        run_id="run-abc123",
    )

    assert to_json_object(ended)["deadlineOutcome"] == "not_applicable"
    assert to_json_object(failed)["deadlineOutcome"] == "error"
    assert to_json_object(soft)["deadlineOutcome"] == "soft_deadline"
    for record in (ended, failed, soft):
        validate_trace_record(record)
        assert to_json_object(record)["submitResult"] == "not_submitted"
        assert to_json_object(record)["runId"] == "run-abc123"
        assert to_json_object(record)["tick"] == 7


def test_loop_trace_record_uses_last_outcome_tick_when_unprocessed() -> None:
    outcome = _tick(
        tick=1, deadline=DeadlineOutcome.SOFT_DEADLINE, submit=SubmitResult.NOT_SUBMITTED
    )
    record = loop_trace_record(
        _loop(reason=StoppedReason.SOFT_DEADLINE, last_tick=0, outcomes=(outcome,)),
        tenant_id=TENANT,
        run_id="run-abc123",
    )

    assert to_json_object(record)["tick"] == 1


def test_loop_trace_record_default_run_id_is_deterministic() -> None:
    first = loop_trace_record(_loop(), tenant_id=TENANT)
    second = loop_trace_record(_loop(), tenant_id=TENANT)
    other = loop_trace_record(_loop(), tenant_id=TenantId("tenant-b"))

    assert to_json_object(first)["runId"] == to_json_object(second)["runId"]
    assert to_json_object(first)["runId"] != to_json_object(other)["runId"]
    assert to_json_object(first)["runId"] == default_run_id("unknown", TENANT)


def test_default_run_id_matches_ts_style() -> None:
    run_id = default_run_id("process-1", TENANT)

    assert run_id.startswith("run-")
    assert len(run_id) == len("run-") + 12
    assert default_run_id("process-1", TENANT) == run_id
    assert default_run_id("process-2", TENANT) != run_id


async def test_memory_runtime_telemetry_records_events() -> None:
    sink = MemoryRuntimeTelemetry()
    tick = _tick()
    loop = _loop()

    assert isinstance(sink, RuntimeTelemetry)
    await sink.emit_tick(tick)
    await sink.emit_tick(tick)
    await sink.emit_loop(loop)
    await sink.flush()
    await sink.close()

    assert sink.ticks == [tick, tick]
    assert sink.loops == [loop]
    assert sink.flush_count == 1
    assert sink.close_count == 1


async def test_noop_runtime_telemetry_is_a_noop() -> None:
    sink = NoopRuntimeTelemetry()

    assert isinstance(sink, RuntimeTelemetry)
    await sink.emit_tick(_tick())
    await sink.emit_loop(_loop())
    await sink.flush()
    await sink.close()
