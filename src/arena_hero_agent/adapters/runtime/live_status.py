"""Best-effort live status snapshot for one tenant's resource observability.

The legacy TypeScript oracle wrote ``waaiging/<t>/results/live_status.json`` so
external monitoring could read the game core resources. The Python live writer
replaces that with a per-tenant ``live_status.json`` under the tenant data
directory. The root ``scripts/arena_common.py`` already resolves both the
``waaiging/`` and ``python/`` layouts, so writing this file keeps resource
observability live without exposing decisions or telemetry.

This adapter is a pure filesystem side effect: it never affects the tick loop,
the decision, or the submission. Write failures are swallowed so a full disk or
a lost directory can only degrade observability, never the live writer.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from arena_hero_agent.application.turns import PlayerLifecycle, TurnObservation
from arena_hero_agent.domain import TenantId

LIVE_STATUS_FILENAME = "live_status.json"

_PLAYER_STATUS: dict[PlayerLifecycle, str] = {
    PlayerLifecycle.ACTIVE: "ACTIVE",
    PlayerLifecycle.RESPAWNING: "RESPAWNING",
}


@dataclass(frozen=True, slots=True)
class LiveStatusWriterConfig:
    """Immutable location for one tenant's live status snapshot."""

    data_root: str | Path
    tenant_id: TenantId

    def __post_init__(self) -> None:
        if not isinstance(self.data_root, (str, Path)):
            raise TypeError("data_root must be a string or Path")
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("tenant_id must be a TenantId")


class LiveStatusWriter:
    """Write a compact, non-sensitive resource snapshot per observation.

    The snapshot carries only tick, lifecycle, resources, and population plus a
    process-relative uptime. It is written atomically (temp file then
    ``os.replace``) so a concurrent reader never observes a torn document.
    """

    def __init__(self, config: LiveStatusWriterConfig) -> None:
        self._config = config
        self._path = Path(config.data_root) / config.tenant_id.value / LIVE_STATUS_FILENAME
        self._start_monotonic = time.monotonic()

    @property
    def path(self) -> Path:
        return self._path

    def write(self, observation: TurnObservation) -> None:
        """Refresh the snapshot for one observed turn; best-effort on IO failure."""

        if not isinstance(observation, TurnObservation):
            raise TypeError("observation must be a TurnObservation")
        try:
            self._write(observation)
        except OSError:
            return

    def _write(self, observation: TurnObservation) -> None:
        payload = {
            "kind": "live",
            "tenantId": self._config.tenant_id.value,
            "tick": observation.tick,
            "uptime": round(time.monotonic() - self._start_monotonic, 3),
            "player_status": _PLAYER_STATUS[observation.lifecycle],
            "core": {"resources": observation.resources},
            "population": observation.population,
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._path)


__all__ = [
    "LIVE_STATUS_FILENAME",
    "LiveStatusWriter",
    "LiveStatusWriterConfig",
]
