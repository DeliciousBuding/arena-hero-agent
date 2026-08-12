"""P4-5 recorder fault injection: SQLite corruption, cross-process lock, bad row.

Empirical note: truncating or appending garbage to a SQLite file silently
self-heals (SQLite recreates the schema), which is exactly the "broken without
anyone noticing" failure mode the suite must catch — so corruption is injected
as a whole-file garbage overwrite (fails closed at open) and a bad trailing row
(invalid enum value, fails closed at read). The JSONL recorder's half-line torn
tail is the "bad trailing line" counterpart and recovers by truncation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from helpers import RECORDER_LOCK_CHILD, read_line, reap, spawn_python, write_line

from arena_hero_agent.adapters.recorder import (
    JsonlTickRecorder,
    RecorderConfig,
    RecorderError,
    SqliteTickRecorder,
)
from arena_hero_agent.application import DeadlineOutcome, SubmitResult, TickResult
from arena_hero_agent.domain import DecisionId, TenantId

TENANT = TenantId("tenant-a")


def _decision(tick: int) -> DecisionId:
    return DecisionId.from_deterministic_input((TENANT, tick, f"state-{tick}"))


def _accepted(tick: int) -> TickResult:
    return TickResult(
        tick=tick,
        decision_id=_decision(tick),
        deadline_outcome=DeadlineOutcome.CANDIDATE,
        submit_result=SubmitResult.ACCEPTED,
    )


def _sqlite_target(tmp_path: Path) -> Path:
    return tmp_path / TENANT.value / "ticks.sqlite3"


def _jsonl_target(tmp_path: Path) -> Path:
    return tmp_path / TENANT.value / "ticks.jsonl"


def test_sqlite_garbage_db_fails_closed_on_open(tmp_path: Path) -> None:
    """A corrupt database must raise at open, never silently reset."""
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    target = _sqlite_target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"this is not a sqlite database at all")

    with pytest.raises(RecorderError, match="integrity check failed"):
        SqliteTickRecorder(config)
    # Fail-closed: the damaged file is left untouched for forensics.
    assert target.read_bytes() == b"this is not a sqlite database at all"


def test_sqlite_bad_trailing_row_fails_closed_on_read(tmp_path: Path) -> None:
    """An invalid trailing record must fail the read, never be silently dropped."""
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    recorder = SqliteTickRecorder(config)
    try:
        recorder.record_tick(_accepted(1))
        # Inject a "bad trailing row" directly into the database: the last row
        # carries an unknown deadline outcome (simulates a torn/corrupt write).
        connection = sqlite3.connect(_sqlite_target(tmp_path))
        connection.execute(
            "INSERT INTO tick_records "
            "(tenant_id, tick, decision_id, deadline_outcome, submit_result, "
            "submit_error, recorded_at_ns, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (TENANT.value, 99, "decision:bad", "garbage", "accepted", None, 0, 1),
        )
        connection.commit()
        connection.close()

        with pytest.raises(RecorderError, match="corrupt tick record"):
            recorder.read_ticks()
        # The good rows are not lost by the failed read.
        assert recorder.read_loop() is None
    finally:
        recorder.close()


def test_sqlite_second_process_holding_lock_fails_closed(tmp_path: Path) -> None:
    """A second process owning the recorder lock must be rejected, not shared."""
    process = spawn_python(RECORDER_LOCK_CHILD, str(tmp_path), TENANT.value)
    try:
        assert read_line(process) == "opened"
        config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
        with pytest.raises(RecorderError, match="another process owns recorder target"):
            SqliteTickRecorder(config)
    finally:
        write_line(process, "close")
        assert read_line(process) == "closed"
        reap(process)


def test_jsonl_recorder_torn_tail_half_line_recovers(tmp_path: Path) -> None:
    """A half-written trailing JSONL line is truncated; complete rows survive."""
    config = RecorderConfig(data_root=tmp_path, tenant_id=TENANT)
    recorder = JsonlTickRecorder(config)
    recorder.record_tick(_accepted(1))
    recorder.record_tick(_accepted(2))
    recorder.close()

    # Simulate a crash mid-append: a partial third line without a newline.
    with _jsonl_target(tmp_path).open("ab") as handle:
        handle.write(b'{"schemaVersion":1,"recordType":"tick","tenantId":"tenant-a",')

    reopened = JsonlTickRecorder(config)
    reopened.record_tick(_accepted(3))
    assert reopened.recovered_partial == 1
    ticks = reopened.read_ticks()
    reopened.close()

    assert [tick.tick for tick in ticks] == [1, 2, 3]
    assert _jsonl_target(tmp_path).read_text(encoding="utf-8").endswith("\n")
