"""JSONL runtime trace sink adapter tests."""

from __future__ import annotations

import json

import pytest

from arena_hero_agent.adapters.telemetry import RuntimeTraceJsonlSink
from arena_hero_agent.application import (
    RuntimeTelemetry,
    TickLoopResult,
    TickResult,
    default_run_id,
)
from arena_hero_agent.application.tick_loop import (
    DeadlineOutcome,
    StoppedReason,
    SubmitResult,
)
from arena_hero_agent.domain import DecisionId, TenantId
from arena_hero_agent.telemetry import (
    DEFAULT_PROCESS_RUN_ID,
    JsonlWriterError,
    validate_trace_record,
)

TENANT = TenantId("tenant-a")
DECISION = DecisionId("decision:sample")


def _tick(
    *,
    tick: int = 7,
    submit: SubmitResult = SubmitResult.ACCEPTED,
    submit_error: str | None = None,
) -> TickResult:
    return TickResult(
        tick=tick,
        decision_id=DECISION,
        deadline_outcome=DeadlineOutcome.CANDIDATE,
        submit_result=submit,
        submit_error=submit_error,
    )


def _loop() -> TickLoopResult:
    return TickLoopResult(
        tenant_id=TENANT,
        last_tick=7,
        ticks_processed=2,
        duplicate_ticks=0,
        out_of_order_ticks=0,
        gap_ticks=0,
        reconnect_count=1,
        stopped_reason=StoppedReason.STREAM_ENDED,
        outcomes=(),
    )


def _read_lines(path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


async def test_writes_tick_and_loop_records(tmp_path) -> None:
    path = tmp_path / "runtime.jsonl"
    sink = RuntimeTraceJsonlSink(tenant_id=TENANT, path=path, run_id="run-abc")

    assert isinstance(sink, RuntimeTelemetry)
    await sink.emit_tick(_tick())
    await sink.emit_tick(_tick(tick=8, submit=SubmitResult.REJECTED, submit_error="nope"))
    await sink.emit_loop(_loop())
    await sink.flush()
    await sink.close()

    records = _read_lines(path)
    assert len(records) == 3
    for record in records:
        validate_trace_record(record)
        assert record["tenantId"] == "tenant-a"
        assert record["processRunId"] == DEFAULT_PROCESS_RUN_ID
    assert records[0]["tick"] == 7
    assert records[0]["deadlineOutcome"] == "candidate"
    assert records[0]["submitResult"] == "accepted"
    assert records[0]["runId"] == "run-abc"
    assert records[1]["submitResult"] == "rejected"
    assert records[1]["submitError"] == "nope"
    assert records[2]["deadlineOutcome"] == "not_applicable"
    assert records[2]["submitResult"] == "not_submitted"
    assert records[2]["runId"] == "run-abc"


async def test_default_run_id_falls_back_to_decision_id(tmp_path) -> None:
    path = tmp_path / "runtime.jsonl"
    sink = RuntimeTraceJsonlSink(tenant_id=TENANT, path=path)

    await sink.emit_tick(_tick())

    records = _read_lines(path)
    assert records[0]["runId"] == "decision:sample"


async def test_loop_default_run_id_is_deterministic(tmp_path) -> None:
    path = tmp_path / "runtime.jsonl"
    sink = RuntimeTraceJsonlSink(tenant_id=TENANT, path=path)

    await sink.emit_loop(_loop())

    records = _read_lines(path)
    assert records[0]["runId"] == default_run_id(DEFAULT_PROCESS_RUN_ID, TENANT)


async def test_redacts_sensitive_submit_error(tmp_path) -> None:
    path = tmp_path / "runtime.jsonl"
    sink = RuntimeTraceJsonlSink(tenant_id=TENANT, path=path)

    await sink.emit_tick(_tick(submit=SubmitResult.REJECTED, submit_error="token=placeholder"))

    text = path.read_text(encoding="utf-8")
    assert "token=placeholder" not in text
    assert "[REDACTED]" in text


async def test_close_is_idempotent_and_emit_after_close_raises(tmp_path) -> None:
    path = tmp_path / "runtime.jsonl"
    sink = RuntimeTraceJsonlSink(tenant_id=TENANT, path=path)

    await sink.flush()
    await sink.close()
    await sink.close()

    with pytest.raises(JsonlWriterError):
        await sink.emit_tick(_tick())


async def test_loop_tenant_mismatch_raises_without_writing(tmp_path) -> None:
    path = tmp_path / "runtime.jsonl"
    sink = RuntimeTraceJsonlSink(tenant_id=TENANT, path=path)

    other = TickLoopResult(
        tenant_id=TenantId("tenant-b"),
        last_tick=1,
        ticks_processed=1,
        duplicate_ticks=0,
        out_of_order_ticks=0,
        gap_ticks=0,
        reconnect_count=0,
        stopped_reason=StoppedReason.STREAM_ENDED,
        outcomes=(),
    )
    with pytest.raises(ValueError, match="does not match"):
        await sink.emit_loop(other)
    assert not path.exists()


def test_requires_exactly_one_of_writer_or_path(tmp_path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        RuntimeTraceJsonlSink(tenant_id=TENANT)
    writer_path = tmp_path / "w.jsonl"
    writer = None
    from arena_hero_agent.telemetry import JsonlWriter

    writer = JsonlWriter(writer_path)
    try:
        with pytest.raises(ValueError, match="exactly one"):
            RuntimeTraceJsonlSink(tenant_id=TENANT, writer=writer, path=tmp_path / "x.jsonl")
    finally:
        writer.close()
