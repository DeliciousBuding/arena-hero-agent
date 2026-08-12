"""Alliance narrative deeds (port of legacy ``alliance-deeds.ts``).

Ports ``loadAllianceDeeds`` from the TypeScript oracle: alliance-level
narrative deeds merged into ``/api/deeds?tenant=all``. Four sections, all
read from already-cached projections (alliance snapshot / survey / heat):
new enemy core discoveries, high-combat heat zones, cross-tenant mine
conflicts, and member resource/status anomalies.

Registered differences from the TS oracle: the 45 s memory cache is not
ported (Python recomputes per request); ``now_ms`` is injectable through the
underlying snapshot/survey/heat loaders. The TS ``row.firstSeenTick`` reads an
undefined field on the survey core cells (the survey loader emits
``last_seen_tick AS tick``), so ``num()`` coerces it to ``0`` — the port keeps
the same ``0`` fallback for byte parity.
"""

from __future__ import annotations

import os
from typing import Any

from ..paths import validate_data_root
from ._common import num
from .alliance_snapshot import load_alliance_snapshot
from .alliance_survey import load_alliance_survey
from .enemy_heat import load_enemy_heat

__all__ = ["load_alliance_deeds"]

NEW_CORE_WINDOW_TICKS = 1500
HEAT_COMBAT_THRESHOLD = 500
LOW_RESOURCE_WARN = 10


def _cell_position(cell: object) -> list[int | float] | None:
    parts = str(cell).split(",")
    if len(parts) != 2:
        return None
    try:
        x = float(parts[0])
        y = float(parts[1])
    except ValueError:
        return None
    return [int(x) if x.is_integer() else x, int(y) if y.is_integer() else y]


def load_alliance_deeds(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Alliance narrative deeds (TS ``loadAllianceDeeds``)."""
    root = validate_data_root(data_root)
    snap = load_alliance_snapshot(root, now_ms=now_ms)
    survey = load_alliance_survey(root, now_ms=now_ms)
    now = int(num(snap.get("currentTick")))
    out: list[dict[str, Any]] = []

    for row in survey.get("enemyCores") or []:
        first_seen = int(num(row.get("firstSeenTick")))
        if now <= 0 or now - first_seen > NEW_CORE_WINDOW_TICKS:
            continue
        owner = row.get("owner")
        out.append(
            {
                "id": (
                    f"alliance:new-core:{row.get('tenant')}:{row.get('x')},{row.get('y')}"
                    f":{first_seen}"
                ),
                "tick": first_seen,
                "tenant": str(row.get("tenant")),
                "star": 3,
                "kind": "ALLIANCE_NEW_CORE",
                "title": "新敌核发现",
                "detail": (
                    f"{owner or '未知'} 核心 @({row.get('x')},{row.get('y')})"
                    f"（{row.get('tenant')} 目击，{now - first_seen} tick 前首次）"
                ),
                "position": [num(row.get("x")), num(row.get("y"))],
                "actor": None,
                "target": str(owner) if owner is not None else "null",
            }
        )

    heat = load_enemy_heat(root, "all", now_ms=now_ms)
    for bucket in heat.get("buckets") or []:
        if int(num(bucket.get("combatCount"))) < HEAT_COMBAT_THRESHOLD:
            continue
        bx = int(num(bucket.get("bx")))
        by = int(num(bucket.get("by")))
        last_tick = int(num(bucket.get("lastTick")))
        combat_count = int(num(bucket.get("combatCount")))
        out.append(
            {
                "id": f"alliance:heat:{bucket.get('tenant')}:{bx},{by}",
                "tick": last_tick,
                "tenant": str(bucket.get("tenant")),
                "star": 3,
                "kind": "ALLIANCE_HEAT_ZONE",
                "title": "敌情高浓度区",
                "detail": (
                    f"chunk ({bx},{by}) 累计 {combat_count} 条敌战斗目击"
                    f"（{bucket.get('tenant')} 侧，{now - last_tick} tick 前最后目击）"
                ),
                "position": [bx * 16 + 8, by * 16 + 8],
                "actor": None,
                "target": None,
            }
        )

    conflicts = survey.get("conflicts") or {}
    for overlap in conflicts.get("resourceOverlaps") or []:
        raw_ticks = overlap.get("lastSeenTicks")
        ticks = [int(num(t)) for t in raw_ticks] if isinstance(raw_ticks, list) else []
        tick = max(ticks) if ticks else now
        out.append(
            {
                "id": f"alliance:conflict:{overlap.get('cell')}",
                "tick": tick,
                "tenant": "all",
                "star": 2,
                "kind": "ALLIANCE_MINE_CONFLICT",
                "title": "跨租户抢矿",
                "detail": (
                    f"矿格 ({overlap.get('cell')}) 被 "
                    f"{','.join(str(t) for t in overlap.get('tenants') or [])} 共同标注"
                    f"（仲裁：保留最新目击租户）"
                ),
                "position": _cell_position(overlap.get("cell")),
                "actor": None,
                "target": None,
            }
        )

    for member in list((snap.get("members") or {}).values()):
        tenant_id = member.get("tenantId")
        resources = int(num(member.get("resources")))
        tick = int(num(member.get("tick")))
        status = member.get("status")
        if resources < LOW_RESOURCE_WARN:
            out.append(
                {
                    "id": f"alliance:economy:{tenant_id}:{tick}",
                    "tick": tick,
                    "tenant": str(tenant_id),
                    "star": 2 if resources < 5 else 1,
                    "kind": "ALLIANCE_ECONOMY",
                    "title": f"{tenant_id} 资源濒危",
                    "detail": (
                        f"核心资源 {resources}（人口 {member.get('population')}，"
                        f"工{member.get('workers')}/锋{member.get('vanguards')}/射{member.get('rangers')}）"
                    ),
                    "position": (
                        (member.get("core") or {}).get("position")
                        if isinstance(member.get("core"), dict)
                        else None
                    ),
                    "actor": None,
                    "target": None,
                }
            )
        if status != "READY":
            out.append(
                {
                    "id": f"alliance:status:{tenant_id}:{tick}",
                    "tick": tick,
                    "tenant": str(tenant_id),
                    "star": 2,
                    "kind": "ALLIANCE_STATUS",
                    "title": f"{tenant_id} 状态异常",
                    "detail": f"status={status}",
                    "position": (
                        (member.get("core") or {}).get("position")
                        if isinstance(member.get("core"), dict)
                        else None
                    ),
                    "actor": None,
                    "target": None,
                }
            )

    out.sort(key=lambda d: (-int(d["tick"]), -int(d["star"])))
    return out
