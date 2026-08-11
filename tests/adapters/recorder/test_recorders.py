"""Offline recorder backend tests: JSONL and SQLite persistence semantics."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest

from arena_hero_agent.adapters.recorder import (
    JsonlTickRecorder,
    RecorderBackend,
    RecorderConfig,
    RecorderError,
    SqliteTickRecorder,
    open_tick_recorder,
)
from arena_hero_agent.adapters.recorder._common import acquire_process_lock, release_process_lock
from arena_hero_agent.application import (
    DeadlineOutcome,
    Decision,
    PlayerLifecycle,
    SingleTenantTickLoop,
    StoppedReason,
    SubmitOutcome,
    SubmitResult,
    TickLoopConfig,
    TickLoopResult,
    TickRecorder,
    TickResult,
    TurnObservation,
    TurnStream,
)
from arena_hero_agent.domain import (
    DeadlineBudget,
    DecisionId,
    RulesVersion,
    TenantId,
    WorldProjection,
)

TENANT = TenantId("tenant-a")
OTHER_TENANT = TenantId("tenant-b")


def _decision(tick: int) -> DecisionId:
    return DecisionId.from_deterministic_input((TENANT, tick, f"state-{tick}"))


def _accepted(tick: int) -> TickResult:
    return TickResult(
        tick=tick,
        decision_id=_decision(tick),
        deadline_outcome=DeadlineOutcome.CANDIDATE,
        submit_result=SubmitResult.ACCEPTED,
    )


def _rejected(tick: int, error: str = "server rejected") -> TickResult:
    return TickResult(
        tick=tick,
        decision_id=_decision(tick),
        deadline_outcome=DeadlineOutcome.CANDIDATE,
        submit_result=SubmitResult.REJECTED,
        submit_error=error,
    )


def _soft_deadline(tick: int) -> TickResult:
    return TickResult(
        tick=tick,
        decision_id=_decision(tick),
        deadline_outcome=DeadlineOutcome.SOFT_DEADLINE,
        submit_result=SubmitResult.NOT_SUBMITTED,
    )


def _loop(
    tenant: TenantId = TENANT,
    *,
    last_tick: int = 3,
    ticks_processed: int = 3,
    stopped_reason: StoppedReason = StoppedReason.STREAM_ENDED,
) -> TickLoopResult:
    return TickLoopResult(
        tenant_id=tenant,
        last_tick=last_tick,
        ticks_processed=ticks_processed,
        duplicate_ticks=0,
        out_of_order_ticks=0,
        gap_ticks=0,
        reconnect_count=0,
        stopped_reason=stopped_reason,
        outcomes=(),
    )


def _jsonl_target(tmp_path: Path) -> Path:
    return tmp_path / TENANT.value / "ticks.jsonl"


def _sqlite_target(tmp_path: Path) -> Path:
    return tmp_path / TENANT.value / "ticks.sqlite3"


def _append_raw(tmp_path: Path, line: str) -> None:
    with _jsonl_target(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


# ---------------------------------------------------------------------------
# JSONL backend
# ---------------------------------------------------------------------------


def test_jsonl_append_and_readback(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    with JsonlTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
        recorder.record_tick(_rejected(2, error="blocked"))
        recorder.record_tick(_soft_deadline(3))
        ticks = recorder.read_ticks()
    assert [tick.tick for tick in ticks] == [1, 2, 3]
    assert ticks[0].deadline_outcome is DeadlineOutcome.CANDIDATE
    assert ticks[0].submit_result is SubmitResult.ACCEPTED
    assert ticks[1].submit_result is SubmitResult.REJECTED
    assert ticks[1].submit_error == "blocked"
    assert ticks[2].deadline_outcome is DeadlineOutcome.SOFT_DEADLINE
    assert ticks[2].submit_result is SubmitResult.NOT_SUBMITTED
    assert ticks[0].decision_id == _decision(1)
    assert _jsonl_target(tmp_path).read_text(encoding="utf-8").endswith("\n")


def test_jsonl_loop_summary_readback(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    with JsonlTickRecorder(config) as recorder:
        recorder.record_loop(_loop())
        recorder.record_loop(_loop(last_tick=5, ticks_processed=5))
        summary = recorder.read_loop()
    assert summary is not None
    assert summary.tenant_id == TENANT
    assert summary.last_tick == 5
    assert summary.ticks_processed == 5
    assert summary.stopped_reason is StoppedReason.STREAM_ENDED


def test_jsonl_restart_continues(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    recorder = JsonlTickRecorder(config)
    recorder.record_tick(_accepted(1))
    recorder.record_tick(_accepted(2))
    recorder.record_loop(_loop(last_tick=2, ticks_processed=2))
    recorder.close()

    reopened = JsonlTickRecorder(config)
    reopened.record_tick(_accepted(3))
    reopened.record_loop(_loop(last_tick=3, ticks_processed=3))
    ticks = reopened.read_ticks()
    summary = reopened.read_loop()
    reopened.close()

    assert [tick.tick for tick in ticks] == [1, 2, 3]
    assert summary is not None and summary.last_tick == 3


def test_jsonl_torn_tail_recovered_to_last_complete_record(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    with JsonlTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
        recorder.record_tick(_accepted(2))
    # Simulate a crash mid-append: a partial third line without a newline.
    with _jsonl_target(tmp_path).open("ab") as handle:
        handle.write(b'{"schemaVersion":1,"recordType":"tick","tenantId":"tenant-a",')
    with JsonlTickRecorder(config) as recorder:
        assert recorder.recovered_partial == 0
        recorder.record_tick(_accepted(3))
        assert recorder.recovered_partial == 1
        ticks = recorder.read_ticks()
    assert [tick.tick for tick in ticks] == [1, 2, 3]
    assert _jsonl_target(tmp_path).read_text(encoding="utf-8").endswith("\n")


def test_jsonl_torn_tail_without_complete_record_recovers_to_empty(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    _jsonl_target(tmp_path).parent.mkdir(parents=True)
    _jsonl_target(tmp_path).write_text('{"schemaVersion":1,"recordType":"tick",', encoding="utf-8")
    with JsonlTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
        assert recorder.recovered_partial == 1
        assert [tick.tick for tick in recorder.read_ticks()] == [1]


def test_jsonl_corrupt_middle_line_fails_loudly(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    with JsonlTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
    target = _jsonl_target(tmp_path)
    lines = target.read_text(encoding="utf-8").splitlines()
    lines.insert(1, "this is not json")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (
        JsonlTickRecorder(config) as recorder,
        pytest.raises(RecorderError, match="corrupt recorder record"),
    ):
        recorder.read_ticks()


def test_jsonl_unsupported_schema_version_fails_loudly(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    with JsonlTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
    _append_raw(
        tmp_path,
        json.dumps({"schemaVersion": 99, "recordType": "tick", "tenantId": "tenant-a"}),
    )
    with (
        JsonlTickRecorder(config) as recorder,
        pytest.raises(RecorderError, match="unsupported recorder schemaVersion"),
    ):
        recorder.read_ticks()


def test_jsonl_wrong_tenant_fails_loudly(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    with JsonlTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
    _append_raw(
        tmp_path,
        json.dumps(
            {
                "schemaVersion": 1,
                "recordType": "tick",
                "tenantId": "other-tenant",
                "tick": 2,
                "decisionId": "decision:x",
                "deadlineOutcome": "candidate",
                "submitResult": "accepted",
                "submitError": None,
            }
        ),
    )
    with (
        JsonlTickRecorder(config) as recorder,
        pytest.raises(RecorderError, match="does not match recorder tenant"),
    ):
        recorder.read_ticks()


def test_jsonl_empty_read_when_no_file(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    with JsonlTickRecorder(config) as recorder:
        assert recorder.read_ticks() == ()
        assert recorder.read_loop() is None


def test_jsonl_single_writer_same_target_raises(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    recorder = JsonlTickRecorder(config)
    try:
        with pytest.raises(RecorderError, match="another active recorder"):
            JsonlTickRecorder(config)
        other = JsonlTickRecorder(RecorderConfig(data_root=tmp_path, tenant_id=OTHER_TENANT))
        other.close()
    finally:
        recorder.close()
    reopened = JsonlTickRecorder(config)
    reopened.close()


def test_jsonl_cross_process_lock_observable(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    recorder = JsonlTickRecorder(config)
    try:
        lock_path = Path(f"{recorder.path}.lock")
        with pytest.raises(RecorderError, match="another process"):
            acquire_process_lock(lock_path)
    finally:
        recorder.close()
    handle = acquire_process_lock(Path(f"{recorder.path}.lock"))
    release_process_lock(handle)


def test_jsonl_relative_data_root_creates_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = RecorderConfig(data_root=Path("nested") / "root", tenant_id=TENANT)
    with JsonlTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
        assert [tick.tick for tick in recorder.read_ticks()] == [1]
    assert (tmp_path / "nested" / "root" / TENANT.value / "ticks.jsonl").is_file()


def test_jsonl_record_fields_stable(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    with JsonlTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(7))
    row = json.loads(_jsonl_target(tmp_path).read_text(encoding="utf-8").strip())
    assert row["schemaVersion"] == 1
    assert row["recordType"] == "tick"
    assert row["tenantId"] == "tenant-a"
    assert row["tick"] == 7
    assert row["decisionId"] == _decision(7).value
    assert row["deadlineOutcome"] == "candidate"
    assert row["submitResult"] == "accepted"
    assert row["submitError"] is None
    assert isinstance(row["recordedAtNs"], int)


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


def test_sqlite_append_and_readback(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    with SqliteTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
        recorder.record_tick(_rejected(2, error="blocked"))
        recorder.record_loop(_loop())
        ticks = recorder.read_ticks()
        summary = recorder.read_loop()
    assert [tick.tick for tick in ticks] == [1, 2]
    assert ticks[1].submit_result is SubmitResult.REJECTED
    assert ticks[1].submit_error == "blocked"
    assert summary is not None and summary.last_tick == 3


def test_sqlite_restart_continues(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    recorder = SqliteTickRecorder(config)
    recorder.record_tick(_accepted(1))
    recorder.close()
    reopened = SqliteTickRecorder(config)
    reopened.record_tick(_accepted(2))
    assert [tick.tick for tick in reopened.read_ticks()] == [1, 2]
    reopened.close()


def test_sqlite_duplicate_write_is_idempotent(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    with SqliteTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
        recorder.record_tick(_accepted(1))
        recorder.record_loop(_loop())
        recorder.record_loop(_loop())
        ticks = recorder.read_ticks()
        summary = recorder.read_loop()
    assert [tick.tick for tick in ticks] == [1]
    assert summary is not None and summary.last_tick == 3


def test_sqlite_conflicting_tick_raises_and_rolls_back(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    with SqliteTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
        with pytest.raises(RecorderError, match="conflicting tick record"):
            recorder.record_tick(_rejected(1, error="different outcome"))
        ticks = recorder.read_ticks()
    assert len(ticks) == 1
    assert ticks[0].submit_result is SubmitResult.ACCEPTED


def test_sqlite_conflicting_decision_id_raises(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    with SqliteTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
        same_decision_different_tick = TickResult(
            tick=2,
            decision_id=_decision(1),
            deadline_outcome=DeadlineOutcome.CANDIDATE,
            submit_result=SubmitResult.ACCEPTED,
        )
        with pytest.raises(RecorderError, match="conflicting tick record"):
            recorder.record_tick(same_decision_different_tick)
        ticks = recorder.read_ticks()
    assert [tick.tick for tick in ticks] == [1]


def test_sqlite_locked_database_raises_clear_error_and_recovers(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT, busy_timeout_ms=50)
    with SqliteTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
        other = sqlite3.connect(_sqlite_target(tmp_path), isolation_level=None)
        other.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(RecorderError, match="locked"):
                recorder.record_tick(_accepted(2))
        finally:
            other.execute("ROLLBACK")
            other.close()
        recorder.record_tick(_accepted(2))
        assert [tick.tick for tick in recorder.read_ticks()] == [1, 2]


def test_sqlite_corrupt_database_fails_loudly_on_open(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    with SqliteTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
    _sqlite_target(tmp_path).write_bytes(b"this is not a sqlite database")
    with pytest.raises(RecorderError, match="failed to open recorder database"):
        SqliteTickRecorder(config)


def test_sqlite_single_writer_same_target_raises(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    recorder = SqliteTickRecorder(config)
    try:
        with pytest.raises(RecorderError, match="another active recorder"):
            SqliteTickRecorder(config)
        other = SqliteTickRecorder(RecorderConfig(data_root=tmp_path, tenant_id=OTHER_TENANT))
        other.close()
    finally:
        recorder.close()
    reopened = SqliteTickRecorder(config)
    reopened.close()


def test_sqlite_relative_data_root_creates_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = RecorderConfig(data_root=Path("nested") / "root", tenant_id=TENANT)
    with SqliteTickRecorder(config) as recorder:
        recorder.record_tick(_accepted(1))
        assert [tick.tick for tick in recorder.read_ticks()] == [1]
    assert (tmp_path / "nested" / "root" / TENANT.value / "ticks.sqlite3").is_file()


# ---------------------------------------------------------------------------
# Shared behavior
# ---------------------------------------------------------------------------


def test_recorders_conform_to_tick_recorder_protocol(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    jsonl_recorder = JsonlTickRecorder(config)
    sqlite_recorder = SqliteTickRecorder(config)
    try:
        assert isinstance(jsonl_recorder, TickRecorder)
        assert isinstance(sqlite_recorder, TickRecorder)
    finally:
        jsonl_recorder.close()
        sqlite_recorder.close()


def test_factory_selects_backend_and_rejects_unknown(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    jsonl_recorder = open_tick_recorder(config)
    assert isinstance(jsonl_recorder, JsonlTickRecorder)
    jsonl_recorder.close()
    sqlite_recorder = open_tick_recorder(config, backend=RecorderBackend.SQLITE)
    assert isinstance(sqlite_recorder, SqliteTickRecorder)
    sqlite_recorder.close()
    string_backend = open_tick_recorder(config, backend="jsonl")
    assert isinstance(string_backend, JsonlTickRecorder)
    string_backend.close()
    with pytest.raises(RecorderError, match="unknown recorder backend"):
        open_tick_recorder(config, backend="parquet")


def test_recorder_rejects_wrong_tenant_loop_record(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    for recorder in (JsonlTickRecorder(config), SqliteTickRecorder(config)):
        try:
            with pytest.raises(RecorderError, match="does not match recorder tenant"):
                recorder.record_loop(_loop(tenant=OTHER_TENANT))
        finally:
            recorder.close()


def test_recorder_closed_operations_raise(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    for recorder in (JsonlTickRecorder(config), SqliteTickRecorder(config)):
        recorder.record_tick(_accepted(1))
        recorder.close()
        recorder.close()
        with pytest.raises(RecorderError, match="recorder is closed"):
            recorder.record_tick(_accepted(2))
        with pytest.raises(RecorderError, match="recorder is closed"):
            recorder.read_ticks()
        with pytest.raises(RecorderError, match="recorder is closed"):
            recorder.read_loop()


def test_recorder_config_rejects_traversal_and_bad_values(tmp_path: Path) -> None:
    with pytest.raises(RecorderError, match=r"\.\."):
        RecorderConfig(data_root="nested/../escape", tenant_id=TENANT)
    with pytest.raises(RecorderError, match="tenant_id"):
        RecorderConfig(data_root=tmp_path, tenant_id=cast(TenantId, "tenant-a"))
    with pytest.raises(RecorderError, match="busy_timeout_ms"):
        RecorderConfig(data_root=tmp_path, tenant_id=TENANT, busy_timeout_ms=-1)


async def test_recorder_persists_tick_loop_results(tmp_path: Path) -> None:
    class _TurnStream:
        def __init__(self, ticks: tuple[int, ...]) -> None:
            self._ticks = list(ticks)

        def __aiter__(self) -> AsyncIterator[TurnObservation]:
            return self

        async def __anext__(self) -> TurnObservation:
            if not self._ticks:
                raise StopAsyncIteration
            tick = self._ticks.pop(0)
            return TurnObservation(
                tick=tick,
                lifecycle=PlayerLifecycle.ACTIVE,
                resources=1,
                population=1,
                projection=WorldProjection(tick=tick, rules_version=RulesVersion.V0_14),
            )

        async def aclose(self) -> None:
            return None

    class Source:
        def __init__(self) -> None:
            self.ticks = (1, 2)

        def stream(self) -> TurnStream:
            return _TurnStream(self.ticks)

    def decide(_observation: TurnObservation, _budget: DeadlineBudget) -> Decision:
        return Decision(tick=_observation.tick)

    async def submit(decision: Decision, _observation: TurnObservation) -> SubmitOutcome:
        return SubmitOutcome(accepted=True)

    loop = SingleTenantTickLoop(
        TickLoopConfig(tenant_id=TENANT, tick_budget=DeadlineBudget.from_milliseconds(100))
    )
    result = await loop.run(Source(), decide, submit)
    assert result.ticks_processed == 2

    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    for recorder in (JsonlTickRecorder(config), SqliteTickRecorder(config)):
        try:
            for outcome in result.outcomes:
                recorder.record_tick(outcome)
            recorder.record_loop(result)
            assert [tick.tick for tick in recorder.read_ticks()] == [1, 2]
            summary = recorder.read_loop()
            assert summary is not None and summary.ticks_processed == 2
        finally:
            recorder.close()


def _static_protocol_conformance(tmp_path: Path) -> None:
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    jsonl_recorder: TickRecorder = JsonlTickRecorder(config)
    sqlite_recorder: TickRecorder = SqliteTickRecorder(config)
    jsonl_recorder.close()
    sqlite_recorder.close()
