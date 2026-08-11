"""JSONL-backed runtime trace sink for one tenant.

Reuses the ``arena_hero_agent.telemetry`` JSONL writer: every record is
validated and redacted before it reaches disk, and best-effort IO failures are
counted as drops instead of raising. Closed-writer and invalid-record errors
raise here; ``TenantRuntime`` catches them and degrades only the telemetry
component without affecting the tick loop.
"""

from __future__ import annotations

import os

from arena_hero_agent.application import (
    TickLoopResult,
    TickResult,
    loop_trace_record,
    runtime_trace_record,
)
from arena_hero_agent.domain import TenantId
from arena_hero_agent.telemetry import (
    DEFAULT_JSONL_ROTATION,
    DEFAULT_PROCESS_RUN_ID,
    JsonlRotationOptions,
    JsonlWriter,
)


class RuntimeTraceJsonlSink:
    """Write runtime trace records for one tenant through a shared JsonlWriter."""

    def __init__(
        self,
        *,
        tenant_id: TenantId,
        writer: JsonlWriter | None = None,
        path: str | os.PathLike[str] | None = None,
        rotation: JsonlRotationOptions = DEFAULT_JSONL_ROTATION,
        recover_torn_tail: bool = False,
        process_run_id: str = DEFAULT_PROCESS_RUN_ID,
        run_id: str | None = None,
    ) -> None:
        if writer is None:
            if path is None:
                raise ValueError("exactly one of writer or path must be provided")
            writer = JsonlWriter(path, rotation, recover_torn_tail=recover_torn_tail)
        elif path is not None:
            raise ValueError("exactly one of writer or path must be provided")
        self._writer = writer
        self._tenant_id = tenant_id
        self._process_run_id = process_run_id
        self._run_id = run_id

    @property
    def writer(self) -> JsonlWriter:
        return self._writer

    async def emit_tick(self, result: TickResult) -> None:
        self._writer.write(
            runtime_trace_record(
                result,
                tenant_id=self._tenant_id,
                process_run_id=self._process_run_id,
                run_id=self._run_id,
            )
        )

    async def emit_loop(self, result: TickLoopResult) -> None:
        if result.tenant_id != self._tenant_id:
            raise ValueError(
                f"loop record tenant {result.tenant_id.value!r} does not match "
                f"sink tenant {self._tenant_id.value!r}"
            )
        self._writer.write(
            loop_trace_record(
                result,
                tenant_id=self._tenant_id,
                process_run_id=self._process_run_id,
                run_id=self._run_id,
            )
        )

    async def flush(self) -> None:
        self._writer.flush()

    async def close(self) -> None:
        self._writer.close()


__all__ = ["RuntimeTraceJsonlSink"]
