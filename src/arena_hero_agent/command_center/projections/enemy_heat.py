"""Enemy-heat projection (W25).

Port of the legacy TypeScript ``packages/command-center/lib/enemy-heat.ts``:
aggregate the survey-db ``units_seen`` table (``controlled = 0`` enemy
sightings only) into 16x16-chunk enemy activity heat buckets — combat
(VANGUARD/RANGER) and worker counts, freshness, per tenant — plus the
all-tenant merged payload. The recent window (default 2000 ticks) powers the
advice layer; the full window (heat_archive union) is kept for records.
Pure read of the survey SQLite; missing/unreadable databases degrade to
empty (TS parity, fail-open).

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

__all__ = ["RECENT_WINDOW_TICKS", "load_enemy_heat"]

RECENT_WINDOW_TICKS = 2000
BUCKET = 16
_COMBAT_TYPES = {"VANGUARD", "RANGER"}


def _aggregate_rows(rows: list[tuple[Any, ...]], buckets: dict[str, dict[str, Any]]) -> None:
    """Fold one SQL row group into 16x16 buckets (TS ``agg``)."""
    for row in rows:
        x = num(row[0])
        y = num(row[1])
        unit_type = str(row[2] or "")
        count = num(row[3])
        last_tick = num(row[4])
        first_tick = num(row[5])
        bx = math.floor(x / BUCKET)
        by = math.floor(y / BUCKET)
        key = f"{bx},{by}"
        bucket = buckets.get(key)
        if bucket is None:
            bucket: dict[str, Any] = {
                "count": 0,
                "combatCount": 0,
                "workerCount": 0,
                "lastTick": -1,
                "firstTick": 2**63 - 1,
                "cells": set(),
            }
            buckets[key] = bucket
        bucket["count"] += count
        bucket["cells"].add(f"{int(x)},{int(y)}")
        if last_tick > bucket["lastTick"]:
            bucket["lastTick"] = last_tick
        if first_tick < bucket["firstTick"]:
            bucket["firstTick"] = first_tick
        if unit_type in _COMBAT_TYPES:
            bucket["combatCount"] += count
        elif unit_type == "WORKER":
            bucket["workerCount"] += count


def _load_tenant_enemy_heat(
    path: Path, window_ticks: int
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], int]:
    """One tenant's recent/full heat aggregation (TS ``loadTenantEnemyHeat``)."""
    recent: dict[str, dict[str, Any]] = {}
    full: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return recent, full, 0
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return recent, full, 0
    try:
        meta = connection.execute("SELECT MAX(last_tick) AS m FROM sync_meta").fetchone()
        current_tick = int(num(meta[0])) if meta is not None else 0
        cutoff = max(0, current_tick - window_ticks)
        recent_rows = connection.execute(
            "SELECT x, y, unit_type AS type, COUNT(*) AS n, MAX(tick) AS last_tick,"
            " MIN(tick) AS first_tick FROM units_seen WHERE controlled = 0 AND x IS NOT NULL"
            " AND tick > ? GROUP BY x, y, type",
            (cutoff,),
        ).fetchall()
        has_archive = (
            connection.execute(
                "SELECT COUNT(*) AS c FROM sqlite_master WHERE type = 'table' AND name = 'heat_archive'"  # noqa: E501
            ).fetchone()[0]
            > 0
        )
        if has_archive:
            full_rows = connection.execute(
                "SELECT x, y, unit_type AS type, COUNT(*) AS n, MAX(tick) AS last_tick,"
                " MIN(tick) AS first_tick FROM ("
                " SELECT x, y, unit_type, last_tick AS tick FROM heat_archive"
                " UNION ALL"
                " SELECT x, y, unit_type, tick FROM units_seen WHERE controlled = 0"
                " AND x IS NOT NULL AND tick > ?"
                ") GROUP BY x, y, type",
                (cutoff,),
            ).fetchall()
        else:
            full_rows = connection.execute(
                "SELECT x, y, unit_type AS type, COUNT(*) AS n, MAX(tick) AS last_tick,"
                " MIN(tick) AS first_tick FROM units_seen WHERE controlled = 0 AND x IS NOT NULL"
                " GROUP BY x, y, type"
            ).fetchall()
    except sqlite3.Error:
        return {}, {}, 0
    finally:
        connection.close()
    _aggregate_rows(recent_rows, recent)
    _aggregate_rows(full_rows, full)
    return recent, full, current_tick


def _to_all_buckets(buckets: dict[str, dict[str, Any]], tenant: str) -> list[dict[str, Any]]:
    """Bucket map -> sorted payload buckets (TS ``toAllBuckets``)."""
    out: list[dict[str, Any]] = []
    for key, agg in buckets.items():
        bx, by = (int(part) for part in key.split(","))
        out.append(
            {
                "bx": bx,
                "by": by,
                "tenant": tenant,
                "count": agg["count"],
                "combatCount": agg["combatCount"],
                "workerCount": agg["workerCount"],
                "lastTick": agg["lastTick"],
                "firstTick": agg["firstTick"],
            }
        )
    out.sort(key=lambda item: item["count"], reverse=True)
    return out


def load_enemy_heat(
    data_root: str | os.PathLike[str],
    tenant: str = "all",
    *,
    now_ms: int | None = None,
    recent_window_ticks: int = RECENT_WINDOW_TICKS,
) -> dict[str, Any]:
    """Load enemy-heat payload (``/api/heat`` source; tenant=all merges)."""
    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    tenants = list(TENANTS) if tenant == "all" else [tenant]
    current_tick = 0
    total_sightings = 0
    combat_sightings = 0
    worker_sightings = 0
    distinct_cells: set[str] = set()
    merged_recent: dict[str, dict[str, Any]] = {}
    merged_full: dict[str, dict[str, Any]] = {}
    for t in tenants:
        recent, full, tenant_tick = _load_tenant_enemy_heat(
            survey_db_path(root, t), recent_window_ticks
        )
        if tenant_tick > current_tick:
            current_tick = tenant_tick
        for bucket in _to_all_buckets(full, t):
            merged_full[f"{t}:{bucket['bx']},{bucket['by']}"] = bucket
        for bucket in _to_all_buckets(recent, t):
            total_sightings += bucket["count"]
            combat_sightings += bucket["combatCount"]
            worker_sightings += bucket["workerCount"]
            distinct_cells.add(f"{bucket['bx']},{bucket['by']}")
            merged_recent[f"{t}:{bucket['bx']},{bucket['by']}"] = bucket
    buckets = sorted(merged_recent.values(), key=lambda item: item["count"], reverse=True)
    full_buckets = sorted(merged_full.values(), key=lambda item: item["count"], reverse=True)
    return {
        "generatedAt": iso_utc(now),
        "tenant": tenant,
        "currentTick": current_tick,
        "buckets": buckets,
        "fullBuckets": full_buckets,
        "summary": {
            "totalSightings": total_sightings,
            "distinctCells": len(distinct_cells),
            "combatSightings": combat_sightings,
            "workerSightings": worker_sightings,
            "tenants": len(tenants),
        },
        "cachedAt": iso_utc(now),
    }
