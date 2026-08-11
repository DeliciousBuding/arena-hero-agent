"""Mine discovery-utilization gap audit (port of legacy ``mine-utilization.ts``).

Answers "many mines are discovered but never assigned/harvested": per tenant
it aggregates the survey ``resources`` ledger (first/last seen, seen count)
against per-cell harvest events into total / harvested / neverHarvested /
visibleNever / staleNever, a utilization rate, a discovery-to-first-harvest
median, a candidate list of visible-but-unharvested mines (last-seen
descending), and a top-mine leaderboard. ``/api/audit/mines`` and
``/api/audit/mines/trend``.

Registered differences from the TS oracle:

- The TS survey DB carries ``sync_meta`` (survey-sync watermark) and
  ``resource_events`` (harvest ledger) tables that are not part of the P5-3
  Python survey schema. The loader derives ``currentTick`` from the P5-3
  ``agents`` table ``MAX(tick)`` and reads ``resource_events`` when present,
  treating a missing table as an empty harvest ledger (every mine is then
  ``neverHarvested``) until the Python survey-sync phase populates it.
- ``generatedAt``/``cachedAt`` are injectable via ``now_ms``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from ..goal_store import iso_utc
from ..paths import TENANTS, survey_db_path, validate_data_root, validate_survey_tenant
from ._common import current_epoch_ms, num

RESOURCE_FRESH_WINDOW_TICKS = 200
TREND_FRESH_WINDOW_TICKS = 2000
DEFAULT_TREND_WINDOW = 2000
DEFAULT_TREND_STEPS = 6

__all__ = [
    "DEFAULT_TREND_STEPS",
    "DEFAULT_TREND_WINDOW",
    "RESOURCE_FRESH_WINDOW_TICKS",
    "aggregate_mine_utilization",
    "aggregate_mine_utilization_trend",
    "load_mine_utilization",
    "load_mine_utilization_trend",
]


def aggregate_mine_utilization(
    tenant: str,
    current_tick: int | None,
    resources: list[dict[str, Any]],
    harvest_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate resources + harvest events into the discovery-utilization gap (TS parity)."""
    by_cell: dict[str, dict[str, Any]] = {}
    for event in harvest_events:
        event_type = event.get("eventType")
        is_ok = event_type == "HARVEST_SUCCEEDED"
        is_fail = event_type == "HARVEST_FAILED"
        if not is_ok and not is_fail:
            continue
        cell = event.get("cell")
        if not isinstance(cell, str):
            continue
        entry = by_cell.get(cell)
        if entry is None:
            fresh: dict[str, Any] = {"ok": 0, "fail": 0, "amount": 0, "first": None, "last": None}
            entry = fresh
            by_cell[cell] = entry
        if is_ok:
            entry["ok"] += 1
            entry["amount"] += num(event.get("amount"))
            tick = num(event.get("tick"))
            if entry["first"] is None:
                entry["first"] = tick
            entry["last"] = tick
        else:
            entry["fail"] += 1

    cutoff = 0 if current_tick is None else current_tick - RESOURCE_FRESH_WINDOW_TICKS
    entries: list[dict[str, Any]] = []
    total = 0
    harvested = 0
    never_harvested = 0
    visible_never = 0
    stale_never = 0
    first_harvest_times: list[int | float] = []
    for r in resources:
        total += 1
        cell = r.get("cell")
        harvest = by_cell.get(cell)
        ok = harvest["ok"] if harvest else 0
        fail = harvest["fail"] if harvest else 0
        state = "visible" if num(r.get("lastSeenTick")) >= cutoff else "stale"
        never = ok == 0
        if never:
            never_harvested += 1
        else:
            harvested += 1
            first = harvest["first"] if harvest else None
            if first is not None:
                ttf = num(first) - num(r.get("firstSeenTick"))
                if ttf >= 0:
                    first_harvest_times.append(ttf)
        if never and state == "visible":
            visible_never += 1
        if never and state == "stale":
            stale_never += 1
        age = max(1, num(r.get("lastSeenTick")) - num(r.get("firstSeenTick")))
        first_seen = num(r.get("firstSeenTick"))
        last_seen = num(r.get("lastSeenTick"))
        entries.append(
            {
                "cell": cell,
                "x": num(r.get("x")),
                "y": num(r.get("y")),
                "firstSeenTick": first_seen or None,
                "lastSeenTick": last_seen or None,
                "seenCount": num(r.get("seenCount")),
                "state": state,
                "harvestOk": ok,
                "harvestFail": fail,
                "harvestAmount": harvest["amount"] if harvest else 0,
                "lastHarvestTick": harvest["last"] if harvest else None,
                "firstHarvestTick": harvest["first"] if harvest else None,
                "neverHarvested": never,
                "timeToFirstHarvest": (
                    None
                    if never or harvest is None or harvest["first"] is None
                    else num(harvest["first"]) - num(r.get("firstSeenTick"))
                ),
                "activity": num(r.get("seenCount")) / age,
                "gapAgeTicks": (
                    max(0, (current_tick if current_tick is not None else 0) - first_seen)
                    if never
                    else None
                ),
            }
        )

    candidates = [
        entry for entry in entries if entry["neverHarvested"] and entry["state"] == "visible"
    ]
    candidates.sort(
        key=lambda entry: entry["lastSeenTick"] if entry["lastSeenTick"] is not None else -1,
        reverse=True,
    )

    first_harvest_times.sort()
    median = first_harvest_times[len(first_harvest_times) // 2] if first_harvest_times else None
    gap_ages = sorted(
        (entry["gapAgeTicks"] if entry["gapAgeTicks"] is not None else 0) for entry in candidates
    )
    max_gap_age = gap_ages[-1] if gap_ages else None
    median_gap_age = gap_ages[len(gap_ages) // 2] if gap_ages else None

    top_by_amount = [entry for entry in entries if entry["harvestAmount"] > 0]
    top_by_amount.sort(
        key=lambda entry: (
            -entry["harvestAmount"],
            -(entry["lastHarvestTick"] if entry["lastHarvestTick"] is not None else -1),
        )
    )
    top_by_count = [entry for entry in entries if entry["harvestOk"] > 0]
    top_by_count.sort(
        key=lambda entry: (
            -entry["harvestOk"],
            -(entry["lastHarvestTick"] if entry["lastHarvestTick"] is not None else -1),
        )
    )

    return {
        "tenant": tenant,
        "currentTick": current_tick,
        "total": total,
        "harvested": harvested,
        "neverHarvested": never_harvested,
        "visibleNever": visible_never,
        "staleNever": stale_never,
        "utilizationRate": round(harvested / total * 1000) / 1000 if total > 0 else None,
        "medianTimeToFirstHarvest": median,
        "maxGapAgeTicks": max_gap_age,
        "medianGapAgeTicks": median_gap_age,
        "candidates": candidates,
        "topMines": {"byAmount": top_by_amount[:20], "byCount": top_by_count[:20]},
    }


def aggregate_mine_utilization_trend(
    tenant: str,
    window: int,
    steps: int,
    resources: list[dict[str, Any]],
    harvest_events: list[dict[str, Any]],
    current_tick: int | None,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Slice resources/harvest history into N windows (TS parity)."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    first_harvest: dict[str, int | float] = {}
    for event in harvest_events:
        if event.get("eventType") != "HARVEST_SUCCEEDED":
            continue
        cell = event.get("cell")
        if not isinstance(cell, str):
            continue
        tick = num(event.get("tick"))
        prev = first_harvest.get(cell)
        if prev is None or tick < prev:
            first_harvest[cell] = tick
    base = current_tick if current_tick is not None else 0
    trend: list[dict[str, Any]] = []
    for index in range(steps):
        end_tick = base - (steps - 1 - index) * window
        total = 0
        visible = 0
        visible_never = 0
        cutoff = end_tick - TREND_FRESH_WINDOW_TICKS
        for r in resources:
            total += 1
            if num(r.get("firstSeenTick")) <= end_tick and num(r.get("lastSeenTick")) >= cutoff:
                visible += 1
                first = first_harvest.get(r.get("cell"))
                if first is None or first > end_tick:
                    visible_never += 1
        trend.append(
            {
                "index": index,
                "endTick": end_tick,
                "total": total,
                "visible": visible,
                "visibleNever": visible_never,
            }
        )
    return {
        "generatedAt": at,
        "tenant": tenant,
        "window": window,
        "steps": steps,
        "currentTick": current_tick,
        "trend": trend,
        "cachedAt": at,
    }


def _read_resources(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT cell, x, y, first_seen_tick, last_seen_tick, seen_count FROM resources"
    ).fetchall()
    return [
        {
            "cell": row[0],
            "x": row[1],
            "y": row[2],
            "firstSeenTick": row[3],
            "lastSeenTick": row[4],
            "seenCount": row[5],
        }
        for row in rows
    ]


def _read_harvest_events(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            "SELECT cell, tick, event_type, amount FROM resource_events"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"cell": row[0], "tick": row[1], "eventType": row[2], "amount": row[3]} for row in rows]


def _tenant_current_tick(connection: sqlite3.Connection) -> int | None:
    row = connection.execute("SELECT MAX(tick) FROM agents").fetchone()
    value = row[0] if row else None
    return value if isinstance(value, int) else None


def _tenant_utilization(path: Path, tenant: str) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "tenant": tenant,
        "currentTick": None,
        "total": 0,
        "harvested": 0,
        "neverHarvested": 0,
        "visibleNever": 0,
        "staleNever": 0,
        "utilizationRate": None,
        "medianTimeToFirstHarvest": None,
        "maxGapAgeTicks": None,
        "medianGapAgeTicks": None,
        "candidates": [],
        "topMines": {"byAmount": [], "byCount": []},
    }
    if not path.exists():
        return empty
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return empty
    try:
        current_tick = _tenant_current_tick(connection)
        return aggregate_mine_utilization(
            tenant,
            current_tick,
            _read_resources(connection),
            _read_harvest_events(connection),
        )
    except sqlite3.Error:
        return empty
    finally:
        connection.close()


def load_mine_utilization(
    data_root: str | os.PathLike[str],
    tenant: str = "all",
) -> dict[str, Any]:
    """Read per-tenant survey tables and aggregate (``/api/audit/mines``)."""
    root = validate_data_root(data_root)
    tenants = list(TENANTS) if tenant == "all" else [validate_survey_tenant(tenant)]
    per_tenant: dict[str, Any] = {}
    for t in tenants:
        per_tenant[t] = _tenant_utilization(survey_db_path(root, t), t)
    at = iso_utc(current_epoch_ms())
    return {"generatedAt": at, "tenant": tenant, "tenants": per_tenant, "cachedAt": at}


def load_mine_utilization_trend(
    data_root: str | os.PathLike[str],
    tenant: str,
    window: int = DEFAULT_TREND_WINDOW,
    steps: int = DEFAULT_TREND_STEPS,
) -> dict[str, Any]:
    """Read survey tables and aggregate into a mine-utilization trend."""
    root = validate_data_root(data_root)
    tenant_value = validate_survey_tenant(tenant)
    path = survey_db_path(root, tenant_value)
    at = iso_utc(current_epoch_ms())
    empty: dict[str, Any] = {
        "generatedAt": at,
        "tenant": tenant_value,
        "window": window,
        "steps": steps,
        "currentTick": None,
        "trend": [],
        "cachedAt": at,
    }
    if not path.exists():
        return empty
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return empty
    try:
        current_tick = _tenant_current_tick(connection)
        return aggregate_mine_utilization_trend(
            tenant_value,
            window,
            steps,
            _read_resources(connection),
            _read_harvest_events(connection),
            current_tick,
        )
    except sqlite3.Error:
        return empty
    finally:
        connection.close()
