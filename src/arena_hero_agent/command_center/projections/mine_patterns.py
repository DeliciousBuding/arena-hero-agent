"""Mine-pattern projection (W25).

Port of the legacy TypeScript ``packages/command-center/lib/mine-patterns.ts``
(``tenantPattern`` / ``loadMinePatterns``): analyze the survey-db resource
ledger into per-tenant mine lifecycle patterns — total / visible / stale
mines, age and seen-count statistics, harvest success, and the
activity-ranked ``topActive`` list (fresh mines first, then activity, then
most recently seen). The advice layer consumes ``visible`` and ``topActive``
for collection-opportunity guidance.

Refill prediction / absence analysis is not required by the advice layer; the
loader returns the TS empty-data defaults for those fields (``refill`` /
``absentStats`` null, ``predictions`` / ``deadMines`` empty, ``refillSource``
``"none"``) so downstream mining assignment behaves identically when the
fixture carries no absence/history rows.

Registered divergence from the TS oracle: ``now_ms`` is injectable; TS
``generatedAt``/``cachedAt`` use the wall clock.
"""

from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..goal_store import iso_utc
from ..paths import TENANTS, survey_db_path, validate_data_root
from ._common import current_epoch_ms, num
from .mines import RESOURCE_FRESH_WINDOW_TICKS

__all__ = ["load_mine_patterns"]

_TOP_ACTIVE_LIMIT = 20


def _js_round(value: float) -> int:
    return math.floor(value + 0.5)


def _tenant_pattern(path: Path, tenant: str) -> dict[str, Any]:
    """One tenant's mine pattern (TS ``tenantPattern``, advice-relevant fields)."""
    empty: dict[str, Any] = {
        "tenant": tenant,
        "total": 0,
        "visible": 0,
        "stale": 0,
        "avgAgeTicks": 0,
        "medianSeenCount": 0,
        "harvestSuccessRate": None,
        "harvestSucceeded": 0,
        "harvestFailed": 0,
        "topActive": [],
        "refill": None,
        "refillSource": "none",
        "absentStats": None,
        "deadMines": [],
        "predictions": [],
        "predictionAccuracy": None,
    }
    if not path.is_file():
        return empty
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return empty
    try:
        meta = connection.execute("SELECT MAX(last_tick) AS m FROM sync_meta").fetchone()
        current_tick = num(meta[0]) if meta is not None else 0
        rows = connection.execute(
            "SELECT x, y, first_seen_tick AS f, last_seen_tick AS l, seen_count AS n, state"
            " FROM resources"
        ).fetchall()
        entries: list[dict[str, Any]] = []
        total = 0
        visible = 0
        stale = 0
        age_sum = 0
        seen_counts: list[int] = []
        for row in rows:
            total += 1
            last_seen = num(row[3])
            first_seen = num(row[2])
            state = (
                "visible" if last_seen >= current_tick - RESOURCE_FRESH_WINDOW_TICKS else "stale"
            )
            if state == "visible":
                visible += 1
            else:
                stale += 1
            age = max(0, last_seen - first_seen)
            age_sum += age
            seen = int(num(row[4]))
            seen_counts.append(seen)
            n = max(1, seen)
            activity = n / max(1, age)
            entries.append(
                {
                    "cell": f"{int(num(row[0]))},{int(num(row[1]))}",
                    "x": int(num(row[0])),
                    "y": int(num(row[1])),
                    "seenCount": seen,
                    "ageTicks": age,
                    "activity": activity,
                    "lastSeenTick": last_seen,
                    "state": state,
                }
            )
        cutoff = current_tick - RESOURCE_FRESH_WINDOW_TICKS
        entries.sort(
            key=lambda e: (
                -(1 if e["lastSeenTick"] >= cutoff else 0),
                -e["activity"],
                -e["lastSeenTick"],
            )
        )
        median_seen_count = sorted(seen_counts)[len(seen_counts) // 2] if seen_counts else 0
        event_rows = connection.execute(
            "SELECT event_type AS e, COUNT(*) AS c FROM resource_events GROUP BY event_type"
        ).fetchall()
        succeeded = 0
        failed = 0
        for row in event_rows:
            if str(row[0]) == "HARVEST_SUCCEEDED":
                succeeded = int(num(row[1]))
            elif str(row[0]) == "HARVEST_FAILED":
                failed = int(num(row[1]))
        rate = succeeded / (succeeded + failed) if succeeded + failed > 0 else None
        return {
            "tenant": tenant,
            "total": total,
            "visible": visible,
            "stale": stale,
            "avgAgeTicks": _js_round(age_sum / total) if total > 0 else 0,
            "medianSeenCount": median_seen_count,
            "harvestSuccessRate": None if rate is None else _js_round(rate * 1000) / 1000,
            "harvestSucceeded": succeeded,
            "harvestFailed": failed,
            "topActive": entries[:_TOP_ACTIVE_LIMIT],
            "refill": None,
            "refillSource": "none",
            "absentStats": None,
            "deadMines": [],
            "predictions": [],
            "predictionAccuracy": None,
        }
    except sqlite3.Error:
        return empty
    finally:
        connection.close()


def load_mine_patterns(
    data_root: str | os.PathLike[str],
    tenant: str = "all",
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load mine-pattern payload (``/api/mines/patterns`` source)."""
    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    tenants = list(TENANTS) if tenant == "all" else [tenant]
    per_tenant: dict[str, Any] = {}
    for t in tenants:
        per_tenant[t] = _tenant_pattern(survey_db_path(root, t), t)
    return {
        "generatedAt": iso_utc(now),
        "tenant": tenant,
        "tenants": per_tenant,
        "modelCaveat": "refill 预测命中率正常（样本不足或命中率高），可作刷新参考。",
        "cachedAt": iso_utc(now),
    }
