"""SQLite-backed offline tick recorder for one tenant."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import BinaryIO

from arena_hero_agent.application import (
    DeadlineOutcome,
    StoppedReason,
    SubmitResult,
    TickLoopResult,
    TickResult,
)
from arena_hero_agent.domain import DecisionId

from ._common import (
    RecorderConfig,
    RecorderError,
    acquire_process_lock,
    register_target,
    release_process_lock,
    sqlite_target_path,
    unregister_target,
)
from .records import RECORD_SCHEMA_VERSION

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tick_records (
    tenant_id TEXT NOT NULL,
    tick INTEGER NOT NULL,
    decision_id TEXT NOT NULL,
    deadline_outcome TEXT NOT NULL,
    submit_result TEXT NOT NULL,
    submit_error TEXT,
    recorded_at_ns INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, tick)
);
CREATE UNIQUE INDEX IF NOT EXISTS tick_records_decision_id_uniq
    ON tick_records (tenant_id, decision_id);
CREATE TABLE IF NOT EXISTS loop_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    recorded_at_ns INTEGER NOT NULL,
    last_tick INTEGER NOT NULL,
    ticks_processed INTEGER NOT NULL,
    duplicate_ticks INTEGER NOT NULL,
    out_of_order_ticks INTEGER NOT NULL,
    gap_ticks INTEGER NOT NULL,
    reconnect_count INTEGER NOT NULL,
    stopped_reason TEXT NOT NULL,
    outcome_count INTEGER NOT NULL,
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS loop_records_tenant_id_idx
    ON loop_records (tenant_id);
"""


class SqliteTickRecorder:
    """SQLite persistence of tick-loop outcomes for one tenant.

    - A durable primary key on ``(tenant_id, tick)`` plus a unique index on
      ``(tenant_id, decision_id)``.
    - Each write runs inside a transaction; failures roll back and raise
      ``RecorderError`` instead of silently dropping data.
    - Replaying an identical record is idempotent; replaying a *different*
      record for the same tick or decision raises a conflict error.
    - A locked or corrupt database raises ``RecorderError`` after the
      configured busy timeout; nothing is silently discarded.
    - Single-writer: an in-process registry plus an advisory cross-process
      lock on a sidecar file make a second recorder for the same target raise
      ``RecorderError`` instead of interleaving.
    """

    def __init__(self, config: RecorderConfig) -> None:
        self._config = config
        self._path = sqlite_target_path(config)
        self._lock_path = Path(f"{self._path}.lock")
        self._registry_key = str(self._path.resolve())
        self._closed = True
        self._connection: sqlite3.Connection | None = None
        self._process_lock: BinaryIO | None = None
        register_target(self._registry_key, self)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._process_lock = acquire_process_lock(self._lock_path)
            self._connection = sqlite3.connect(self._path)
            self._connection.execute(f"PRAGMA busy_timeout = {config.busy_timeout_ms}")
            self._verify_existing_database()
            self._connection.executescript(_SCHEMA)
            self._connection.commit()
        except RecorderError:
            self._cleanup_partial_init()
            raise
        except sqlite3.Error as exc:
            self._cleanup_partial_init()
            raise RecorderError(f"failed to open recorder database {self._path}: {exc}") from exc
        except BaseException:
            self._cleanup_partial_init()
            raise
        self._closed = False

    _EXPECTED_TABLES = ("tick_records", "loop_records")

    def _verify_existing_database(self) -> None:
        """Fail closed on a torn/truncated pre-existing database.

        ``CREATE TABLE IF NOT EXISTS`` silently rebuilds a missing schema, so a
        torn write that destroyed the recorder tables would otherwise be masked
        as a brand-new database while the old rows are lost. Before creating
        anything, verify that a pre-existing non-empty file is a healthy
        recorder database: ``PRAGMA quick_check`` must be ``ok`` and the
        expected tables must be present. An empty file is indistinguishable
        from a fresh database (for example a crash before the first write) and
        is initialized normally.
        """
        if not self._path.exists() or self._path.stat().st_size == 0:
            return
        connection = self._connection
        if connection is None:
            return
        try:
            rows = connection.execute("PRAGMA quick_check").fetchall()
        except sqlite3.Error as exc:
            raise RecorderError(
                f"recorder database integrity check failed for {self._path}: {exc}"
            ) from exc
        if rows != [("ok",)]:
            raise RecorderError(
                f"recorder database integrity check failed for {self._path}: "
                "database is torn or truncated; refusing to silently recreate it"
            )
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        except sqlite3.Error as exc:
            raise RecorderError(
                f"recorder database integrity check failed for {self._path}: {exc}"
            ) from exc
        missing = [name for name in self._EXPECTED_TABLES if name not in tables]
        if missing:
            raise RecorderError(
                f"recorder database {self._path} is missing expected tables "
                f"{missing}; refusing to silently recreate the schema after a "
                "truncated or torn write"
            )

    def _cleanup_partial_init(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._process_lock is not None:
            release_process_lock(self._process_lock)
            self._process_lock = None
        unregister_target(self._registry_key, self)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> sqlite3.Connection:
        if self._closed:
            raise RecorderError("recorder is closed")
        if self._connection is None:
            raise RecorderError("recorder is not initialized")
        return self._connection

    def record_tick(self, result: TickResult) -> None:
        connection = self._require_open()
        tenant = self._config.tenant_id.value
        try:
            with connection:
                connection.execute(
                    "INSERT INTO tick_records "
                    "(tenant_id, tick, decision_id, deadline_outcome, submit_result, "
                    "submit_error, recorded_at_ns, schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tenant,
                        result.tick,
                        result.decision_id.value,
                        result.deadline_outcome.value,
                        result.submit_result.value,
                        result.submit_error,
                        time.time_ns(),
                        RECORD_SCHEMA_VERSION,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if self._row_matches(result, tenant):
                return
            raise RecorderError(
                f"conflicting tick record for tenant {tenant!r} tick {result.tick}: {exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise RecorderError(
                f"failed to persist tick record for tenant {tenant!r}: {exc}"
            ) from exc

    def _row_matches(self, result: TickResult, tenant: str) -> bool:
        connection = self._connection
        if connection is None:
            return False
        row = connection.execute(
            "SELECT decision_id, deadline_outcome, submit_result, submit_error, schema_version "
            "FROM tick_records WHERE tenant_id = ? AND tick = ?",
            (tenant, result.tick),
        ).fetchone()
        if row is None:
            return False
        decision_id, deadline_outcome, submit_result, submit_error, schema_version = row
        return (
            decision_id == result.decision_id.value
            and deadline_outcome == result.deadline_outcome.value
            and submit_result == result.submit_result.value
            and submit_error == result.submit_error
            and schema_version == RECORD_SCHEMA_VERSION
        )

    def record_loop(self, result: TickLoopResult) -> None:
        connection = self._require_open()
        if result.tenant_id != self._config.tenant_id:
            raise RecorderError(
                f"loop record tenant {result.tenant_id.value!r} does not match "
                f"recorder tenant {self._config.tenant_id.value!r}"
            )
        try:
            with connection:
                connection.execute(
                    "INSERT INTO loop_records "
                    "(tenant_id, recorded_at_ns, last_tick, ticks_processed, duplicate_ticks, "
                    "out_of_order_ticks, gap_ticks, reconnect_count, stopped_reason, "
                    "outcome_count, schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        result.tenant_id.value,
                        time.time_ns(),
                        result.last_tick,
                        result.ticks_processed,
                        result.duplicate_ticks,
                        result.out_of_order_ticks,
                        result.gap_ticks,
                        result.reconnect_count,
                        result.stopped_reason.value,
                        len(result.outcomes),
                        RECORD_SCHEMA_VERSION,
                    ),
                )
        except sqlite3.Error as exc:
            raise RecorderError(
                f"failed to persist loop record for tenant {result.tenant_id.value!r}: {exc}"
            ) from exc

    def read_ticks(self) -> tuple[TickResult, ...]:
        connection = self._require_open()
        tenant = self._config.tenant_id.value
        try:
            rows = connection.execute(
                "SELECT tick, decision_id, deadline_outcome, submit_result, submit_error "
                "FROM tick_records WHERE tenant_id = ? ORDER BY tick",
                (tenant,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise RecorderError(
                f"failed to read tick records for tenant {tenant!r}: {exc}"
            ) from exc
        results: list[TickResult] = []
        for tick, decision_id, deadline_outcome, submit_result, submit_error in rows:
            try:
                results.append(
                    TickResult(
                        tick=tick,
                        decision_id=DecisionId(decision_id),
                        deadline_outcome=DeadlineOutcome(deadline_outcome),
                        submit_result=SubmitResult(submit_result),
                        submit_error=submit_error,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise RecorderError(f"corrupt tick record in database: {exc}") from exc
        return tuple(results)

    def read_loop(self) -> TickLoopResult | None:
        connection = self._require_open()
        tenant = self._config.tenant_id.value
        try:
            row = connection.execute(
                "SELECT last_tick, ticks_processed, duplicate_ticks, out_of_order_ticks, "
                "gap_ticks, reconnect_count, stopped_reason "
                "FROM loop_records WHERE tenant_id = ? ORDER BY id DESC LIMIT 1",
                (tenant,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise RecorderError(
                f"failed to read loop records for tenant {tenant!r}: {exc}"
            ) from exc
        if row is None:
            return None
        (
            last_tick,
            ticks_processed,
            duplicate_ticks,
            out_of_order_ticks,
            gap_ticks,
            reconnect_count,
            stopped_reason,
        ) = row
        try:
            return TickLoopResult(
                tenant_id=self._config.tenant_id,
                last_tick=last_tick,
                ticks_processed=ticks_processed,
                duplicate_ticks=duplicate_ticks,
                out_of_order_ticks=out_of_order_ticks,
                gap_ticks=gap_ticks,
                reconnect_count=reconnect_count,
                stopped_reason=StoppedReason(stopped_reason),
                outcomes=(),
            )
        except (TypeError, ValueError) as exc:
            raise RecorderError(f"corrupt loop record in database: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._process_lock is not None:
            release_process_lock(self._process_lock)
            self._process_lock = None
        unregister_target(self._registry_key, self)

    def __enter__(self) -> SqliteTickRecorder:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
