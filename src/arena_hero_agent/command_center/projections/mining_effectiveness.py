"""Mining allocation fulfillment audit (port of legacy ``mining-effectiveness.ts``).

Closes the "assignment -> actual harvest" feedback loop: each alliance/mining
assignment is aligned against the per-tenant per-cell harvest statistics to
classify it as harvested / harvestedByOther / open / stale, with per-tenant
resolved/progress rates and a global effective rate. ``/api/audit/mining-effectiveness``.
"""

from __future__ import annotations

from typing import Any

from ..goal_store import iso_utc
from ..paths import TENANTS
from ._common import current_epoch_ms, num

FRESH_TICKS = 2000

__all__ = ["FRESH_TICKS", "aggregate_allocation_effectiveness"]


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
