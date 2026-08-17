"""JSONL-backed offline tick recorder for one tenant."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from arena_hero_agent.application import TickLoopResult, TickResult
from arena_hero_agent.application.turns import Decision, TurnObservation
from arena_hero_agent.telemetry import (
    DEFAULT_JSONL_ROTATION,
    JsonlWriterError,
    append_jsonl_line,
)

from ._common import (
    RecorderConfig,
    RecorderError,
    acquire_process_lock,
    jsonl_target_path,
    register_target,
    release_process_lock,
    unregister_target,
)
from .records import (
    RECORD_TYPE_LOOP,
    RECORD_TYPE_TICK,
    RECORD_TYPE_TICK_STATE,
    parse_loop,
    parse_tick,
    parse_tick_state,
    serialize_loop,
    serialize_tick,
    serialize_tick_state,
)


class JsonlTickRecorder:
    """Append-only JSONL persistence of tick-loop outcomes for one tenant.

    - One complete record per line; appends reuse the telemetry
      ``append_jsonl_line`` primitive (validated path, fail-closed rotation,
      atomic line append).
    - A torn tail (file not ending in a newline) is detected on access and
      truncated to the last complete record; the recovery is observable
      through :attr:`recovered_partial`.
    - A corrupt line anywhere else fails loudly on read.
    - Single-writer: an in-process registry plus an advisory cross-process
      lock on a sidecar file make a second recorder for the same target raise
      ``RecorderError`` instead of interleaving.
    """

    def __init__(self, config: RecorderConfig) -> None:
        self._config = config
        self._path = jsonl_target_path(config)
        self._lock_path = Path(f"{self._path}.lock")
        self._registry_key = str(self._path.resolve())
        self._closed = True
        self._tail_checked = False
        self._recovered_partial = 0
        self._process_lock: BinaryIO | None = None
        register_target(self._registry_key, self)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._process_lock = acquire_process_lock(self._lock_path)
        except BaseException:
            unregister_target(self._registry_key, self)
            raise
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def recovered_partial(self) -> int:
        return self._recovered_partial

    def _require_open(self) -> None:
        if self._closed:
            raise RecorderError("recorder is closed")

    def _ensure_tail_clean(self) -> None:
        if self._tail_checked:
            return
        self._tail_checked = True
        if not self._path.exists():
            return
        size = self._path.stat().st_size
        if size == 0:
            return
        with self._path.open("rb") as handle:
            handle.seek(max(0, size - 1), os.SEEK_SET)
            last = handle.read(1)
        if last == b"\n":
            return
        boundary = self._last_newline_boundary()
        with self._path.open("r+b") as handle:
            handle.truncate(boundary)
        self._recovered_partial += 1

    def _last_newline_boundary(self) -> int:
        """Byte offset just past the final newline, or 0 when none exists."""
        with self._path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            pos = end
            chunk_size = 8192
            while pos > 0:
                start = max(0, pos - chunk_size)
                handle.seek(start)
                chunk = handle.read(pos - start)
                index = chunk.rfind(b"\n")
                if index != -1:
                    return start + index + 1
                pos = start
        return 0

    def _append(self, record: dict[str, object]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        self._ensure_tail_clean()
        try:
            append_jsonl_line(self._path, line, DEFAULT_JSONL_ROTATION)
        except (OSError, JsonlWriterError) as exc:
            raise RecorderError(f"failed to append recorder record to {self._path}: {exc}") from exc

    def record_tick(self, result: TickResult) -> None:
        self._require_open()
        record = serialize_tick(
            result, tenant_id=self._config.tenant_id, recorded_at_ns=time.time_ns()
        )
        self._append(record)

    def record_tick_state(
        self,
        observation: TurnObservation,
        decision: Decision | None,
        result: TickResult,
    ) -> None:
        """Persist the rich tick_state snapshot alongside the thin tick record."""
        self._require_open()
        record = serialize_tick_state(
            observation,
            decision,
            result,
            tenant_id=self._config.tenant_id,
            recorded_at_ns=time.time_ns(),
        )
        self._append(record)

    def record_loop(self, result: TickLoopResult) -> None:
        self._require_open()
        if result.tenant_id != self._config.tenant_id:
            raise RecorderError(
                f"loop record tenant {result.tenant_id.value!r} does not match "
                f"recorder tenant {self._config.tenant_id.value!r}"
            )
        self._append(serialize_loop(result, recorded_at_ns=time.time_ns()))

    def _iter_records(self) -> Iterator[dict[str, object]]:
        if not self._path.exists():
            return
        text = self._path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RecorderError(
                    f"corrupt recorder record at line {line_number} of {self._path}: {exc}"
                ) from exc
            if not isinstance(data, dict):
                raise RecorderError(
                    f"corrupt recorder record at line {line_number} of {self._path}: "
                    "expected a JSON object"
                )
            yield data

    def read_ticks(self) -> tuple[TickResult, ...]:
        self._require_open()
        self._ensure_tail_clean()
        ticks: list[TickResult] = []
        for data in self._iter_records():
            record_type = data.get("recordType")
            if record_type == RECORD_TYPE_TICK:
                ticks.append(parse_tick(data, expected_tenant=self._config.tenant_id))
            elif record_type == RECORD_TYPE_TICK_STATE:
                parse_tick_state(data, expected_tenant=self._config.tenant_id)
            elif record_type == RECORD_TYPE_LOOP:
                parse_loop(data, expected_tenant=self._config.tenant_id)
            else:
                raise RecorderError(f"unknown recorder recordType {record_type!r}")
        return tuple(ticks)

    def read_loop(self) -> TickLoopResult | None:
        self._require_open()
        self._ensure_tail_clean()
        last_loop: TickLoopResult | None = None
        for data in self._iter_records():
            record_type = data.get("recordType")
            if record_type == RECORD_TYPE_LOOP:
                last_loop = parse_loop(data, expected_tenant=self._config.tenant_id)
            elif record_type == RECORD_TYPE_TICK:
                parse_tick(data, expected_tenant=self._config.tenant_id)
            elif record_type == RECORD_TYPE_TICK_STATE:
                parse_tick_state(data, expected_tenant=self._config.tenant_id)
            else:
                raise RecorderError(f"unknown recorder recordType {record_type!r}")
        return last_loop

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process_lock is not None:
            release_process_lock(self._process_lock)
            self._process_lock = None
        unregister_target(self._registry_key, self)

    def __enter__(self) -> JsonlTickRecorder:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
