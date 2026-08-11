"""Alliance-level mining allocation projection (port of legacy ``alliance-mining.ts``).

Turns shared-survey visible-but-unharvested candidates into an alliance-scale
"who mines what": each candidate mine is assigned to the observing tenant
whose core is nearest (Chebyshev), preferring the candidate's own tenant on
ties, with shared/conflict flags, backlog age, refill prediction, and enemy
heat threat. Pure read suggestion; nothing is written. ``/api/alliance/mining``.

Registered difference from the TS oracle: ``generatedAt``/``cachedAt`` are
injectable via ``now_ms``.
"""

from __future__ import annotations

from typing import Any

from ..goal_store import iso_utc
from ..paths import TENANTS
from ._common import current_epoch_ms, finite_number, num

__all__ = ["assign_alliance_mining", "build_observers_by_cell"]


def _chebyshev(
    a: tuple[int | float, int | float], b: tuple[int | float, int | float]
) -> int | float:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def build_observers_by_cell(resources: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Derive cell -> observing tenants from alliance-survey resource rows."""
    out: dict[str, list[str]] = {}
    for r in resources:
        tenant = str(r.get("tenant") or "")
        x = finite_number(r.get("x"))
        y = finite_number(r.get("y"))
        if not tenant or x is None or y is None:
            continue
        key = f"{x},{y}"
        arr = out.get(key)
        if arr is None:
            arr = []
            out[key] = arr
        if tenant not in arr:
            arr.append(tenant)
    return out


def assign_alliance_mining(
    cores: dict[str, tuple[int | float, int | float] | None],
    workers: dict[str, int | float | None],
    candidates_by_tenant: dict[str, list[dict[str, Any]]],
    observers_by_cell: dict[str, list[str]],
    conflict_cells: set[str],
    meta_by_cell: dict[str, dict[str, Any]] | None = None,
    heat_by_bucket: dict[str, dict[str, Any]] | None = None,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Assign candidate mines to the nearest observing tenant (TS parity)."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    meta_by_cell = meta_by_cell or {}
    heat_by_bucket = heat_by_bucket or {}
    seen: set[str] = set()
    assignments: list[dict[str, Any]] = []
    per_tenant: dict[str, dict[str, Any]] = {}
    for t in TENANTS:
        per_tenant[t] = {
            "assigned": 0,
            "avgDistance": None,
            "workers": workers.get(t) if workers.get(t) is not None else None,
        }
    unassigned: list[dict[str, Any]] = []

    total_candidates = 0
    assigned = 0
    shared = 0
    conflict = 0
    unassigned_count = 0

    candidates: list[dict[str, Any]] = []
    for t, items in candidates_by_tenant.items():
        for c in items or []:
            cell = c.get("cell")
            if not isinstance(cell, str):
                continue
            if cell not in seen:
                seen.add(cell)
                candidates.append(
                    {
                        "cell": cell,
                        "x": num(c.get("x")),
                        "y": num(c.get("y")),
                        "lastSeenTick": c.get("lastSeenTick"),
                        "prefer": t,
                    }
                )
    total_candidates = len(candidates)

    distance_sums: dict[str, int | float] = {}
    distance_counts: dict[str, int] = {}

    for c in candidates:
        observers = observers_by_cell.get(c["cell"]) or []
        reachable = [t for t in observers if cores.get(t) is not None]
        if not reachable:
            unassigned.append(
                {"cell": c["cell"], "x": c["x"], "y": c["y"], "reason": "no_observer_core"}
            )
            unassigned_count += 1
            continue
        best: str | None = None
        best_dist: int | float = float("inf")
        for t in reachable:
            core = cores[t]
            assert core is not None
            dist = _chebyshev((c["x"], c["y"]), core)
            if dist < best_dist or (dist == best_dist and t == c["prefer"]):
                best = t
                best_dist = dist
        if best is None:
            unassigned.append({"cell": c["cell"], "x": c["x"], "y": c["y"], "reason": "no_core"})
            unassigned_count += 1
            continue
        is_shared = len(observers) > 1
        is_conflict = c["cell"] in conflict_cells
        if is_shared:
            shared += 1
        if is_conflict:
            conflict += 1
        assigned += 1
        per_tenant[best]["assigned"] += 1
        distance_sums[best] = distance_sums.get(best, 0) + best_dist
        distance_counts[best] = distance_counts.get(best, 0) + 1

        meta = meta_by_cell.get(c["cell"]) or {}
        bucket_key = f"{int(c['x'] // 16)},{int(c['y'] // 16)}"
        hb = heat_by_bucket.get(bucket_key)
        combat = num(hb.get("combatCount")) if hb else 0
        threat_level = 3 if combat >= 10 else 2 if combat >= 3 else 1 if combat >= 1 else 0
        assignments.append(
            {
                "cell": c["cell"],
                "x": c["x"],
                "y": c["y"],
                "assignedTenant": best,
                "distanceToCore": best_dist,
                "observers": observers,
                "shared": is_shared,
                "conflict": is_conflict,
                "lastSeenTick": c["lastSeenTick"],
                "gapAgeTicks": meta.get("gapAgeTicks")
                if meta.get("gapAgeTicks") is not None
                else None,
                "predictedNextTick": meta.get("predictedNextTick")
                if meta.get("predictedNextTick") is not None
                else None,
                "dueInTicks": meta.get("dueInTicks")
                if meta.get("dueInTicks") is not None
                else None,
                "threatLevel": threat_level,
                "threatCombat": combat,
                "threatCount": num(hb.get("count")) if hb else 0,
            }
        )

    for t in TENANTS:
        if distance_counts.get(t, 0) > 0:
            per_tenant[t]["avgDistance"] = round(distance_sums[t] / distance_counts[t] * 10) / 10

    assignments.sort(
        key=lambda item: (
            -(item["gapAgeTicks"] if item["gapAgeTicks"] is not None else 0),
            (item["distanceToCore"] if item["distanceToCore"] is not None else 1e9),
        )
    )

    return {
        "generatedAt": at,
        "currentTick": None,
        "assignments": assignments,
        "perTenant": per_tenant,
        "unassigned": unassigned,
        "global": {
            "totalCandidates": total_candidates,
            "assigned": assigned,
            "shared": shared,
            "conflict": conflict,
            "unassigned": unassigned_count,
        },
        "cachedAt": at,
    }
