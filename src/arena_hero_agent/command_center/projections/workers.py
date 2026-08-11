"""Worker liveness audit projection (port of legacy ``worker-liveness-audit.ts``).

Forensic read over ``runtime.jsonl`` ``worker_liveness`` anomaly events:
which worker is stuck, the anomaly kind, the position/cargo trajectory at
trigger time, and whether targeted recovery was issued or repeated. Only
anomalies are persisted, so the tail read is small. ``/api/audit/workers``.

Registered difference from the TS oracle: the pure core takes already-parsed
rows (P5-3 ``read_jsonl_tail``); ``generatedAt``/``cachedAt`` are injectable
via ``now_ms``.
"""

from __future__ import annotations

import math
import os
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import read_jsonl_tail
from ..paths import TENANTS, telemetry_dir, validate_data_root, validate_tenant
from ._common import current_epoch_ms

TTL_MS = 5000
DEFAULT_WINDOW = 4000
RECENT_TICKS = 16
MIN_WINDOW = 200
MAX_WINDOW = 20_000

__all__ = [
    "DEFAULT_WINDOW",
    "RECENT_TICKS",
    "aggregate_worker_liveness",
    "load_worker_liveness_audit",
]


def _finite_number(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _position(value: object) -> list[int | float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    x = _finite_number(value[0])
    y = _finite_number(value[1])
    if x is None or y is None:
        return None
    return [x, y]


def _positions(value: object) -> list[list[int | float]]:
    if not isinstance(value, list):
        return []
    out: list[list[int | float]] = []
    for item in value:
        pos = _position(item)
        if pos is not None:
            out.append(pos)
    return out


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def aggregate_worker_liveness(tenant: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate worker-liveness anomaly rows into a per-tenant audit (TS parity)."""
    current_tick: int | float | None = None
    incidents: list[dict[str, Any]] = []
    for row in rows:
        tick = _finite_number(row.get("tick"))
        if tick is not None and (current_tick is None or tick > current_tick):
            current_tick = tick
        if row.get("telemetryType") == "worker_liveness" and isinstance(row.get("unitId"), str):
            incidents.append(row)

    by_kind: dict[str, int] = {}
    latest: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    repeated: set[str] = set()
    for row in incidents:
        unit_id = str(row["unitId"])
        kind = _text(row.get("workerLivenessKind")) or "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if unit_id in seen:
            repeated.add(unit_id)
        seen.add(unit_id)
        prev = latest.get(unit_id)
        row_tick_value = _finite_number(row.get("tick"))
        prev_tick_value = _finite_number(prev.get("tick")) if prev is not None else None
        row_tick: int | float = row_tick_value if row_tick_value is not None else -1
        prev_tick: int | float = prev_tick_value if prev_tick_value is not None else -1
        if prev is None or row_tick >= prev_tick:
            latest[unit_id] = row

    latest_by_worker: list[dict[str, Any]] = []
    for unit_id, row in latest.items():
        tick = _finite_number(row.get("tick"))
        tick_value = tick if tick is not None else 0
        age_ticks = None if current_tick is None else max(0, current_tick - tick_value)
        recovery_count = _finite_number(row.get("recoveryCount"))
        recovery_value = recovery_count if recovery_count is not None else 0
        if recovery_value > 1 or unit_id in repeated:
            status = "repeated"
        elif age_ticks is not None and age_ticks <= RECENT_TICKS:
            status = "recent"
        else:
            status = "historical"
        latest_by_worker.append(
            {
                "tenant": tenant,
                "unitId": unit_id,
                "kind": _text(row.get("workerLivenessKind")) or "unknown",
                "tick": tick_value,
                "ageTicks": age_ticks,
                "streak": _finite_number(row.get("streak"))
                if _finite_number(row.get("streak")) is not None
                else 0,
                "position": _position(row.get("position")),
                "cargo": _finite_number(row.get("cargo")),
                "priorActionType": _text(row.get("priorActionType")),
                "priorIntent": _text(row.get("priorIntent")),
                "recentPositions": _positions(row.get("recentPositions")),
                "uniqueRecentPositions": _finite_number(row.get("uniqueRecentPositions")),
                "explorationChunk": _text(row.get("explorationChunk")),
                "knownExplorationChunks": _finite_number(row.get("knownExplorationChunks")),
                "recoveryCount": recovery_value,
                "recoveryApplied": row.get("recoveryApplied") is True,
                "recoveryError": _text(row.get("recoveryError")),
                "status": status,
            }
        )

    priority = {"repeated": 0, "recent": 1, "historical": 2}
    latest_by_worker.sort(
        key=lambda item: (
            priority[item["status"]],
            -item["tick"],
            item["unitId"],
        )
    )

    return {
        "tenant": tenant,
        "currentTick": current_tick,
        "eventCount": len(incidents),
        "affectedWorkers": len(latest),
        "repeatedWorkers": sum(1 for item in latest_by_worker if item["status"] == "repeated"),
        "byKind": by_kind,
        "latestByWorker": latest_by_worker,
    }


def _bounded_window(window: int) -> int:
    return min(max(math.floor(window), MIN_WINDOW), MAX_WINDOW)


def load_worker_liveness_audit(
    data_root: str | os.PathLike[str],
    tenant: str = "all",
    window: int = DEFAULT_WINDOW,
) -> dict[str, Any]:
    """Read per-tenant ``runtime.jsonl`` tails and aggregate (``/api/audit/workers``)."""
    root = validate_data_root(data_root)
    bounded = _bounded_window(window)
    selected = list(TENANTS) if tenant == "all" else [validate_tenant(tenant)]
    tenants: list[dict[str, Any]] = []
    for t in selected:
        rows = read_jsonl_tail(telemetry_dir(root, t) / "runtime.jsonl", bounded)
        tenants.append(aggregate_worker_liveness(t, rows))
    latest = [item for tenant in tenants for item in tenant["latestByWorker"]]
    at = iso_utc(current_epoch_ms())
    return {
        "generatedAt": at,
        "tenant": tenant,
        "window": bounded,
        "totals": {
            "eventCount": sum(item["eventCount"] for item in tenants),
            "affectedWorkers": len(latest),
            "repeatedWorkers": sum(1 for item in latest if item["status"] == "repeated"),
            "recentWorkers": sum(1 for item in latest if item["status"] == "recent"),
        },
        "tenants": tenants,
        "cachedAt": at,
    }
