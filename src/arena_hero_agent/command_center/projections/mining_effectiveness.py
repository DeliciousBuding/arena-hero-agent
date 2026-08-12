"""Mining allocation fulfillment audit (port of legacy ``mining-effectiveness.ts``).

Closes the "assignment -> actual harvest" feedback loop: each alliance/mining
assignment is aligned against the per-tenant per-cell harvest statistics to
classify it as harvested / harvestedByOther / open / stale, with per-tenant
resolved/progress rates and a global effective rate. ``/api/audit/mining-effectiveness``.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..goal_store import iso_utc
from ..paths import TENANTS, survey_db_path, validate_data_root
from ._common import current_epoch_ms, num

FRESH_TICKS = 2000

__all__ = ["FRESH_TICKS", "aggregate_allocation_effectiveness", "load_mining_effectiveness"]


def aggregate_allocation_effectiveness(
    assignments: list[dict[str, Any]],
    harvest_by_tenant_cell: dict[str, dict[str, dict[str, Any]]],
    current_tick: int | None,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Classify assignment fulfillment against harvest stats (TS parity)."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    cutoff = 0 if current_tick is None else current_tick - FRESH_TICKS
    items: list[dict[str, Any]] = []
    per_tenant: dict[str, dict[str, Any]] = {}

    def empty_tenant() -> dict[str, Any]:
        return {
            "assigned": 0,
            "harvested": 0,
            "harvestedByOther": 0,
            "open": 0,
            "stale": 0,
            "resolvedRate": None,
            "progressRate": None,
            "avgTimeToHarvest": None,
        }

    for t in TENANTS:
        per_tenant[t] = empty_tenant()
    g_assigned = 0
    g_harvested = 0
    g_other = 0
    g_open = 0
    g_stale = 0
    ttf_by_tenant: dict[str, list[int | float]] = {}

    for assignment in assignments:
        tenant = assignment["assignedTenant"]
        mine = harvest_by_tenant_cell.get(tenant, {}).get(assignment["cell"])
        ok = mine["ok"] if mine else 0
        fail = mine["fail"] if mine else 0
        amount = mine["amount"] if mine else 0
        first = mine["first"] if mine else None
        last_seen = (
            num(assignment.get("lastSeenTick")) if num(assignment.get("lastSeenTick")) else None
        )
        other_ok = 0
        for t in TENANTS:
            if t == tenant:
                continue
            other_ok += num(harvest_by_tenant_cell.get(t, {}).get(assignment["cell"], {}).get("ok"))
        if ok > 0:
            status = "harvested"
        elif other_ok > 0:
            status = "harvestedByOther"
        elif last_seen is not None and last_seen >= cutoff:
            status = "open"
        else:
            status = "stale"

        time_to_harvest = (
            max(0, first - last_seen) if first is not None and last_seen is not None else None
        )

        entry = per_tenant.get(tenant)
        if entry is None:
            entry = empty_tenant()
            per_tenant[tenant] = entry
        entry["assigned"] += 1
        if status == "harvested":
            entry["harvested"] += 1
            g_harvested += 1
            ttf_by_tenant.setdefault(tenant, []).append(
                time_to_harvest if time_to_harvest is not None else 0
            )
        elif status == "harvestedByOther":
            entry["harvestedByOther"] += 1
            g_other += 1
        elif status == "open":
            entry["open"] += 1
            g_open += 1
        else:
            entry["stale"] += 1
            g_stale += 1
        g_assigned += 1

        items.append(
            {
                "cell": assignment["cell"],
                "x": num(assignment.get("x")),
                "y": num(assignment.get("y")),
                "assignedTenant": tenant,
                "distanceToCore": assignment.get("distanceToCore"),
                "lastSeenTick": last_seen,
                "harvestOk": ok,
                "harvestFail": fail,
                "harvestAmount": amount,
                "firstHarvestTick": first,
                "status": status,
                "timeToHarvest": time_to_harvest,
            }
        )

    for t in TENANTS:
        entry = per_tenant[t]
        closed = entry["harvested"] + entry["stale"]
        entry["resolvedRate"] = (
            round(entry["harvested"] / closed * 1000) / 1000 if closed > 0 else None
        )
        entry["progressRate"] = (
            round(entry["harvested"] / entry["assigned"] * 1000) / 1000
            if entry["assigned"] > 0
            else None
        )
        ttf = ttf_by_tenant.get(t) or []
        if ttf:
            entry["avgTimeToHarvest"] = round(sum(ttf) / len(ttf) * 10) / 10

    closed_all = g_harvested + g_stale
    effective_rate = round(g_harvested / closed_all * 1000) / 1000 if closed_all > 0 else None
    progress_rate = round(g_harvested / g_assigned * 1000) / 1000 if g_assigned > 0 else None

    return {
        "generatedAt": at,
        "currentTick": current_tick,
        "items": items,
        "perTenant": per_tenant,
        "global": {
            "assigned": g_assigned,
            "harvested": g_harvested,
            "harvestedByOther": g_other,
            "open": g_open,
            "stale": g_stale,
            "effectiveRate": effective_rate,
            "progressRate": progress_rate,
        },
        "cachedAt": at,
    }


# --- thin loader over the P5-3 data base (W25) ----------------------------


def _read_harvest_map(path: Path) -> dict[str, dict[str, Any]]:
    """Survey-db resource_events -> per-cell harvest stats (TS ``readTenantHarvestMap``)."""
    out: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return out
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        rows = connection.execute(
            "SELECT cell, tick, event_type AS e, amount FROM resource_events"
        ).fetchall()
    except sqlite3.Error:
        return out
    finally:
        connection.close()
    for row in rows:
        cell = str(row[0])
        event_type = str(row[2])
        is_ok = event_type == "HARVEST_SUCCEEDED"
        is_fail = event_type == "HARVEST_FAILED"
        if not is_ok and not is_fail:
            continue
        stat = out.setdefault(
            cell,
            {"ok": 0, "fail": 0, "amount": 0, "first": None, "last": None},
        )
        if is_ok:
            stat["ok"] += 1
            stat["amount"] += num(row[3])
            if stat["first"] is None:
                stat["first"] = num(row[1])
            stat["last"] = num(row[1])
        else:
            stat["fail"] += 1
    return out


def _observers_by_cell(survey: Mapping[str, Any]) -> dict[str, list[str]]:
    """Alliance-survey resource rows -> observers per cell (TS ``buildObserversByCell``)."""
    from .alliance_mining import build_observers_by_cell

    resources = survey.get("resources") or ()
    return build_observers_by_cell([dict(row) for row in resources if isinstance(row, Mapping)])


def _conflict_cells(survey: Mapping[str, Any]) -> set[str]:
    """Survey conflict overlaps -> cell set (TS ``conflictCells``)."""
    conflicts = survey.get("conflicts") or {}
    overlaps = conflicts.get("resourceOverlaps") or ()
    cells: set[str] = set()
    for overlap in overlaps:
        if isinstance(overlap, Mapping):
            cell = str(overlap.get("cell") or "")
            if cell:
                cells.add(cell)
    return cells


def load_mining_effectiveness(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load the ``/api/audit/mining/effectiveness`` payload from the P5-3 base.

    Composes the same chain the TS ``loadMiningEffectiveness`` reads: alliance
    mining assignments (snapshot cores/workers + survey resources + mine
    utilization candidates + mine-pattern predictions + enemy heat) aligned
    against each tenant's survey-db harvest events.
    """
    from .alliance_mining import assign_alliance_mining
    from .alliance_snapshot import load_alliance_snapshot
    from .alliance_survey import load_alliance_survey
    from .enemy_heat import load_enemy_heat
    from .mine_patterns import load_mine_patterns
    from .mines import load_mine_utilization

    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    snapshot = load_alliance_snapshot(root, now_ms=now)
    survey = load_alliance_survey(root, now_ms=now)
    mines = load_mine_utilization(root, "all")
    members = snapshot.get("members") or {}
    cores: dict[str, tuple[int | float, int | float] | None] = {}
    workers: dict[str, int | float | None] = {}
    for tenant in TENANTS:
        member = members.get(tenant)
        core = member.get("core") if isinstance(member, Mapping) else None
        position = (
            core.get("position")
            if isinstance(core, Mapping) and isinstance(core.get("position"), (list, tuple))
            else None
        )
        cores[tenant] = (
            (num(position[0]), num(position[1])) if position and len(position) >= 2 else None
        )
        workers[tenant] = (
            num(member.get("workers"))
            if isinstance(member, Mapping) and member.get("workers") is not None
            else None
        )
    candidates_by_tenant: dict[str, list[dict[str, Any]]] = {}
    mine_tenants = mines.get("tenants") or {}
    for tenant in TENANTS:
        tenant_mines = mine_tenants.get(tenant) if isinstance(mine_tenants, Mapping) else None
        candidates_by_tenant[tenant] = []
        if isinstance(tenant_mines, Mapping):
            for candidate in tenant_mines.get("candidates") or ():
                if not isinstance(candidate, Mapping):
                    continue
                candidates_by_tenant[tenant].append(
                    {
                        "cell": str(candidate.get("cell") or ""),
                        "x": num(candidate.get("x")),
                        "y": num(candidate.get("y")),
                        "lastSeenTick": candidate.get("lastSeenTick"),
                    }
                )
    observers_by_cell = _observers_by_cell(survey)
    conflict_cells = _conflict_cells(survey)
    patterns = load_mine_patterns(root, "all", now_ms=now)
    pattern_tenants = patterns.get("tenants") or {}
    meta_by_cell: dict[str, dict[str, Any]] = {}
    for tenant in TENANTS:
        tenant_pattern = (
            pattern_tenants.get(tenant) if isinstance(pattern_tenants, Mapping) else None
        )
        for prediction in (
            (tenant_pattern.get("predictions") or ()) if isinstance(tenant_pattern, Mapping) else ()
        ):
            if not isinstance(prediction, Mapping):
                continue
            cell = str(prediction.get("cell") or "")
            if not cell:
                continue
            current = meta_by_cell.setdefault(cell, {})
            if prediction.get("predictedNextTick") is not None:
                current["predictedNextTick"] = prediction.get("predictedNextTick")
            if prediction.get("dueInTicks") is not None:
                current["dueInTicks"] = prediction.get("dueInTicks")
    heat = load_enemy_heat(root, "all", now_ms=now)
    heat_by_bucket: dict[str, dict[str, Any]] = {}
    for bucket in heat.get("buckets") or ():
        if not isinstance(bucket, Mapping):
            continue
        key = f"{bucket.get('bx')},{bucket.get('by')}"
        current = heat_by_bucket.setdefault(key, {"combatCount": 0, "count": 0, "lastTick": 0})
        current["combatCount"] += num(bucket.get("combatCount"))
        current["count"] += num(bucket.get("count"))
        if num(bucket.get("lastTick")) > num(current["lastTick"]):
            current["lastTick"] = num(bucket.get("lastTick"))
    mining = assign_alliance_mining(
        cores,
        workers,
        candidates_by_tenant,
        observers_by_cell,
        conflict_cells,
        meta_by_cell,
        heat_by_bucket,
        now_ms=now,
    )
    current_tick = snapshot.get("currentTick")
    harvest_by_tenant_cell: dict[str, dict[str, dict[str, Any]]] = {}
    for tenant in TENANTS:
        harvest_by_tenant_cell[tenant] = _read_harvest_map(survey_db_path(root, tenant))
    payload = aggregate_allocation_effectiveness(
        list(mining.get("assignments") or ()),
        harvest_by_tenant_cell,
        current_tick,
        now_ms=now,
    )
    payload["currentTick"] = current_tick
    return payload
