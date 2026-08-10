"""Clock boundary for deterministic budget accounting."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Monotonic clock used to measure elapsed work, never to form decision keys."""

    def monotonic_ns(self) -> int:
        """Return a monotonically non-decreasing process-local reading."""
        ...
