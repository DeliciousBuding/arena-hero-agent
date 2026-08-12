"""Deed journal summary (port of legacy ``deeds-journal.ts``).

Ports the core of ``loadDeedsJournal`` from the TypeScript oracle: aggregate
the tenant/alliance deed stream into a tick-windowed "journal" layer — window
headline, per-category counts, per-tenant tallies, grouped deeds, a Chinese
narrative paragraph, and a previous-window delta. ``windowTicks`` /
``categories`` / ``minStar`` filters are part of the pure aggregation.

``/api/deeds/journal?tenant=all|tN&window=5000&category=...&minStar=N``.

Registered differences from the TS oracle (fail-open, never guessed):

- ``buildAuditDeeds`` (the AUDIT_INSIGHT deed group) is not ported: it reads
  ``loadAuditOverview`` which depends on the 8120 supervisor pipeline (still a
  501 route). The journal therefore carries no audit-insight deeds.
- The ``tenant=all`` narrative enrichment lines (shop history, alliance
  coverage, decision health, threat, mining execution, pipeline health) are
  not ported: they depend on the external shop fetch and the 8120 /
  write-side-effect pipeline projections. The base narrative only.
- The 30 s memory cache is not ported (Python recomputes per request).
"""

from __future__ import annotations

import math
import os
from typing import Any

from ..goal_store import iso_utc
from ..paths import TENANTS, validate_data_root
from ._common import current_epoch_ms
from .alliance_deeds import load_alliance_deeds
from .alliance_snapshot import load_alliance_snapshot
from .deeds import load_deeds

__all__ = ["build_narrative", "build_window_delta", "load_deeds_journal"]

KIND_GROUP: dict[str, str] = {
    "HARVEST_SUCCEEDED": "harvest",
    "DEPOSIT_SUCCEEDED": "deposit",
    "CORE_SPAWN_SUCCEEDED": "spawn",
    "UNIT_DESTROYED": "death",
    "MILESTONE_HARVEST": "milestone",
    "MILESTONE_BIRTH": "milestone",
    "MILESTONE_SPEND": "milestone",
    "MILESTONE_DEATH": "milestone",
    "MILESTONE_RESOURCES": "milestone",
    "RESOURCE_PEAK": "milestone",
    "ALLIANCE_NEW_CORE": "newCore",
    "ALLIANCE_HEAT_ZONE": "heatZone",
    "ALLIANCE_MINE_CONFLICT": "conflict",
    "ALLIANCE_ECONOMY": "economy",
    "ALLIANCE_STATUS": "status",
    "AUDIT_INSIGHT": "audit",
}


def _clamp_min_star(value: object) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        # JS Math.round half-toward-infinity (Python round() uses banker rounding)
        return min(max(math.floor(value + 0.5), 1), 4)
    return 0


def build_window_delta(
    cur: list[dict[str, Any]],
    prev: list[dict[str, Any]],
) -> dict[str, Any]:
    """Previous-window vs current-window category delta (TS ``buildWindowDelta``)."""

    def tally(deeds: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for deed in deeds:
            if deed["kind"] == "AUDIT_INSIGHT":
                continue
            group = KIND_GROUP.get(deed["kind"], "other")
            counts[group] = counts.get(group, 0) + 1
        return counts

    cur_counts = tally(cur)
    prev_counts = tally(prev)
    categories: list[str] = []
    for key in [*cur_counts.keys(), *prev_counts.keys()]:
        if key not in categories:
            categories.append(key)
    counts: dict[str, dict[str, int]] = {}
    for key in categories:
        counts[key] = {
            "cur": cur_counts.get(key, 0),
            "prev": prev_counts.get(key, 0),
            "delta": cur_counts.get(key, 0) - prev_counts.get(key, 0),
        }
    label: dict[str, str] = {
        "harvest": "采集",
        "deposit": "交付",
        "spawn": "产兵",
        "death": "阵亡",
        "milestone": "里程碑",
        "newCore": "新敌核",
        "heatZone": "热区",
        "conflict": "抢矿冲突",
        "economy": "资源濒危",
        "audit": "审计",
    }
    parts: list[str] = []
    for key, value in counts.items():
        delta = value["delta"]
        if delta == 0:
            continue
        key_label = label.get(key, key)
        if value["cur"] > 0 and value["prev"] == 0:
            parts.append(f"{key_label} 新增 {value['cur']}")
        elif value["cur"] == 0 and value["prev"] > 0:
            parts.append(f"{key_label} 归零（-{value['prev']}）")
        elif abs(delta) >= 2:
            parts.append(
                f"{key_label} {'+' if delta > 0 else ''}{delta}（{value['prev']}→{value['cur']}）"
            )
    return {
        "counts": counts,
        "narrative": f"较上一窗口：{'，'.join(parts)}。" if parts else "较上一窗口无显著变化。",
    }


def build_narrative(
    deeds: list[dict[str, Any]],
    counts: dict[str, int],
    per_tenant: dict[str, dict[str, int]],
    tenant: str,
) -> str:
    """Chinese narrative paragraph for the window (TS ``buildNarrative``)."""
    if not deeds:
        return "该窗口内无事迹。"
    tenant_label = "联盟" if tenant == "all" else tenant
    lead = f"{tenant_label}最近 {len(deeds)} 条事迹："
    parts: list[str] = []
    if counts.get("harvest"):
        parts.append(f"采集 {counts['harvest']} 次")
    if counts.get("deposit"):
        parts.append(f"交付 {counts['deposit']} 次")
    if counts.get("spawn"):
        parts.append(f"产兵 {counts['spawn']} 次")
    if counts.get("death"):
        parts.append(f"阵亡 {counts['death']} 个")
    if counts.get("milestone"):
        parts.append(f"里程碑 {counts['milestone']} 个")
    if counts.get("newCore"):
        parts.append(f"新敌核 {counts['newCore']} 处")
    if counts.get("heatZone"):
        parts.append(f"敌情高浓度区 {counts['heatZone']} 处")
    if counts.get("conflict"):
        parts.append(f"抢矿冲突 {counts['conflict']} 处")
    if counts.get("economy"):
        parts.append(f"资源濒危 {counts['economy']} 租户次")
    if counts.get("audit"):
        parts.append(f"数据层审计 {counts['audit']} 条")
    active = sum(1 for t in TENANTS if (per_tenant.get(t) or {}).get("count", 0) > 0)
    parts.append(f"活跃租户 {active}/{len(TENANTS)}")
    return f"{lead}{'，'.join(parts)}。"


def load_deeds_journal(
    data_root: str | os.PathLike[str],
    tenant: str,
    window_ticks: int = 5000,
    categories: list[str] | None = None,
    min_star: int = 0,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Deed journal summary (TS ``loadDeedsJournal`` core)."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    root = validate_data_root(data_root)
    cats = [c for c in (categories or []) if c]
    star = _clamp_min_star(min_star)
    snap = load_alliance_snapshot(root, now_ms=now_ms)
    current_tick = int(snap.get("currentTick") or 0)
    window_start = current_tick - window_ticks
    prev_window_start = current_tick - window_ticks * 2

    if tenant == "all":
        all_deeds = [*load_deeds(root, "all", 500), *load_alliance_deeds(root, now_ms=now_ms)]
    else:
        all_deeds = load_deeds(root, tenant, 500)

    cat_set = set(cats)

    def apply_filter(deeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = list(deeds)
        if star > 0:
            out = [d for d in out if d["star"] >= star]
        if cat_set:
            out = [d for d in out if KIND_GROUP.get(d["kind"], "other") in cat_set]
        return out

    cur_raw = [d for d in all_deeds if window_start <= d["tick"] <= current_tick]
    prev_raw = [d for d in all_deeds if prev_window_start <= d["tick"] < window_start]
    windowed = sorted(apply_filter(cur_raw), key=lambda d: (-d["star"], -d["tick"]))
    prev_windowed = apply_filter(prev_raw)
    headline = windowed[0] if windowed else None
    counts: dict[str, int] = {}
    per_tenant: dict[str, dict[str, int]] = {}
    groups: dict[str, list[dict[str, Any]]] = {}
    for deed in windowed:
        group = KIND_GROUP.get(deed["kind"], "other")
        counts[group] = counts.get(group, 0) + 1
        groups.setdefault(group, []).append(deed)
        t = per_tenant.get(deed["tenant"], {"count": 0, "topStar": 0})
        t["count"] += 1
        if deed["star"] > t["topStar"]:
            t["topStar"] = deed["star"]
        per_tenant[deed["tenant"]] = t

    narrative = build_narrative(windowed, counts, per_tenant, tenant)
    delta = build_window_delta(windowed, prev_windowed)
    return {
        "generatedAt": at,
        "tenant": tenant,
        "windowTicks": window_ticks,
        "currentTick": current_tick,
        "windowStartTick": window_start,
        "headline": headline,
        "counts": counts,
        "perTenant": per_tenant,
        "narrative": narrative,
        "groups": {k: v[:20] for k, v in groups.items()},
        "filters": {"categories": cats, "minStar": star},
        "delta": {"prevWindowStartTick": prev_window_start, **delta},
        "deeds": windowed[:30],
        "cachedAt": at,
    }
