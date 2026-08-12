"""Consensus mining projection (W44).

Port of the legacy TypeScript ``packages/command-center/lib/consensus-mining.ts``:
join the alliance shared-survey consensus mines (``alliance-survey
consensusResources``) with the assignment-fulfillment labels
(``mining-effectiveness`` items) per cell — the frontend "全联盟矿" map layer
gets assignedTenant / miningStatus / gapAgeTicks in one read instead of
stitching three endpoints. Enemy-heat bucket threat (combat 0-3) is attached
per 16x16 bucket, and the most backlogged assigned-but-unharvested mines are
ranked into ``summary.topStale``.

Registered divergence from the TS oracle: ``now_ms`` is injectable for the
``generatedAt``/``cachedAt`` wall-clock fields; the TS ``TtlCache`` warm-up is
replaced by the Python command-center cache layer at the API surface (the
projection itself stays a pure read).
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Any

from ..goal_store import iso_utc
from ..paths import TENANTS, validate_data_root
from ._common import current_epoch_ms, num
from .alliance_survey import load_alliance_survey
from .enemy_heat import load_enemy_heat
from .mines import load_mine_utilization
from .mining_effectiveness import load_mining_effectiveness

__all__ = ["THREAT_LEVELS", "enrich_consensus_mining", "load_consensus_mining"]

BUCKET = 16
TOP_STALE_LIMIT = 10
# enemy-heat bucket combat -> threat level (TS 2026-08-08 contract).
THREAT_LEVELS: tuple[tuple[int, int], ...] = ((10, 3), (3, 2), (1, 1))


def _threat_level(combat: int | float) -> int:
    """Map enemy-heat bucket combat sighting count to a 0-3 threat level."""
    for threshold, level in THREAT_LEVELS:
        if combat >= threshold:
            return level
    return 0


def _bucket_key(x: int | float, y: int | float) -> str:
    """16x16 chunk key for an enemy-heat bucket (TS ``Math.floor``)."""
    return f"{math.floor(x / BUCKET)},{math.floor(y / BUCKET)}"


def enrich_consensus_mining(
    survey: dict[str, Any] | None,
    effectiveness: dict[str, Any] | None,
    mines: dict[str, Any] | None = None,
    heat_by_bucket: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pure join: consensus mines + fulfillment labels + heat threat (TS parity).

    Inputs are the loader payloads of ``load_alliance_survey`` /
    ``load_mining_effectiveness`` / ``load_mine_utilization`` plus a merged
    enemy-heat bucket map. Returns the payload body without the injectable
    wall-clock fields (``generatedAt``/``cachedAt`` are added by the loader).
    """
    heat_by_bucket = heat_by_bucket or {}
    by_cell: dict[str, dict[str, Any]] = {}
    for item in (effectiveness or {}).get("items") or ():
        if not isinstance(item, Mapping):
            continue
        cell = item.get("cell")
        if not isinstance(cell, str):
            continue
        by_cell[cell] = {
            "assignedTenant": item.get("assignedTenant"),
            "status": item.get("status"),
            "gapAge": None,
        }
    # gapAge（发现后仍未采时长）来自 mine-utilization 候选（mining-effectiveness
    # 事件流无首见 tick）：对已分工矿取各租户候选 gapAgeTicks 的最大值。
    mine_tenants = (mines or {}).get("tenants") if mines else None
    if isinstance(mine_tenants, Mapping):
        for t in TENANTS:
            tenant_mines = mine_tenants.get(t)
            if not isinstance(tenant_mines, Mapping):
                continue
            for candidate in tenant_mines.get("candidates") or ():
                if not isinstance(candidate, Mapping):
                    continue
                cur = by_cell.get(str(candidate.get("cell") or ""))
                if cur is None:
                    continue
                gap = num(candidate.get("gapAgeTicks"))
                if gap > (cur["gapAge"] or 0):
                    cur["gapAge"] = gap

    resources: list[dict[str, Any]] = []
    assigned = 0
    open_count = 0
    stale = 0
    harvested = 0
    harvested_by_other = 0
    high_threat = 0
    top_stale: list[dict[str, Any]] = []
    for row in (survey or {}).get("consensusResources") or ():
        if not isinstance(row, Mapping):
            continue
        x = num(row.get("x"))
        y = num(row.get("y"))
        cell = f"{x},{y}"
        mine = by_cell.get(cell)
        status = mine["status"] if mine else None
        heat = heat_by_bucket.get(_bucket_key(x, y))
        combat = num(heat.get("combatCount")) if heat else 0
        threat = _threat_level(combat)
        if threat >= 2:
            high_threat += 1
        if status == "open":
            open_count += 1
        elif status == "stale":
            stale += 1
        elif status == "harvested":
            harvested += 1
        elif status == "harvestedByOther":
            harvested_by_other += 1
        if status is not None:
            assigned += 1
        if status == "open" or status == "stale":
            top_stale.append(
                {
                    "cell": cell,
                    "x": x,
                    "y": y,
                    "assignedTenant": mine["assignedTenant"] if mine else "",
                    "gapAgeTicks": mine["gapAge"] if mine else None,
                }
            )
        resources.append(
            {
                **dict(row),
                "cell": cell,
                "x": x,
                "y": y,
                "assignedTenant": mine["assignedTenant"] if mine else None,
                "miningStatus": status,
                "gapAgeTicks": mine["gapAge"] if mine else None,
                "threatLevel": threat,
                "threatCombat": combat,
            }
        )
    top_stale.sort(
        key=lambda item: (
            -(item["gapAgeTicks"] if item["gapAgeTicks"] is not None else 0),
            item["x"],
            item["y"],
        )
    )
    return {
        "resources": resources,
        "summary": {
            "assigned": assigned,
            "open": open_count,
            "stale": stale,
            "harvested": harvested,
            "harvestedByOther": harvested_by_other,
            "highThreat": high_threat,
            "topStale": top_stale[:TOP_STALE_LIMIT],
        },
        "colors": (survey or {}).get("colors") or {},
        "tenantSummaries": (survey or {}).get("tenantSummaries") or {},
    }


def load_consensus_mining(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load the ``/api/alliance/survey/mining`` payload (P5-3 data base)."""
    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    survey = load_alliance_survey(root, now_ms=now)
    effectiveness = load_mining_effectiveness(root, now_ms=now)
    mines = load_mine_utilization(root, "all")
    heat_by_bucket: dict[str, dict[str, Any]] = {}
    for bucket in load_enemy_heat(root, "all", now_ms=now).get("buckets") or ():
        if not isinstance(bucket, Mapping):
            continue
        key = f"{bucket.get('bx')},{bucket.get('by')}"
        current = heat_by_bucket.setdefault(key, {"combatCount": 0, "count": 0, "lastTick": 0})
        current["combatCount"] += num(bucket.get("combatCount"))
        current["count"] += num(bucket.get("count"))
        last_tick = num(bucket.get("lastTick"))
        if last_tick > num(current["lastTick"]):
            current["lastTick"] = last_tick
    body = enrich_consensus_mining(survey, effectiveness, mines, heat_by_bucket)
    return {"generatedAt": iso_utc(now), **body, "cachedAt": iso_utc(now)}
