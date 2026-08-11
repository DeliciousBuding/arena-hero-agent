"""Application-owned recorder boundary for offline tick-loop persistence."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .tick_loop import TickLoopResult, TickResult


@runtime_checkable
class TickRecorder(Protocol):
    """Offline, per-tenant persistence of tick-loop outcomes.

    A recorder is bound to exactly one tenant and one storage target.
    Implementations must be single-writer: a second recorder claiming the
    same target fails loudly instead of interleaving records. Reads return
    the durable records so callers can verify append and restart behavior.
    """

    def record_tick(self, result: TickResult) -> None: ...

    def record_loop(self, result: TickLoopResult) -> None: ...

    def read_ticks(self) -> tuple[TickResult, ...]: ...

    def read_loop(self) -> TickLoopResult | None: ...

    def close(self) -> None: ...
