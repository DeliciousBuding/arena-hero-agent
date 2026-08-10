"""Telemetry and health reporting boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, TypeVar, runtime_checkable

TelemetryT_contra = TypeVar("TelemetryT_contra", contravariant=True)


@runtime_checkable
class TelemetrySink(Protocol[TelemetryT_contra]):
    """Best-effort sink for versioned telemetry records."""

    async def emit(self, event: TelemetryT_contra) -> None: ...


@runtime_checkable
class HealthReporter(Protocol):
    """Publish component health without granting lifecycle control."""

    async def report(
        self,
        component: str,
        *,
        healthy: bool,
        message: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None: ...
