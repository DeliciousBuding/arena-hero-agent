"""Decision-allocation alignment audit (W44 wave 5).

Port of the legacy TypeScript ``packages/command-center/lib/alignment-audit.ts``
(``aggregateAlignment`` / ``loadAlignmentAudit``): combine the decision-audit
harvest/deposit action share, the mine-utilization visible-never gap plus its
trend, the mining-effectiveness assignment fulfillment, and the alliance
snapshot worker count into a per-tenant alignment grade
(``aligned`` / ``gap_widening`` / ``allocation_unfulfilled`` / ``data_gap``)
with Chinese-language reasons. Answers "why is an assignment never
harvested" from the decision side. ``/api/audit/alignment``.

The aggregation core is a pure port of the TS oracle (1:1 grade/reason
semantics, including the ``(rate*100).toFixed(0)`` percentage rendering and
``Math.round(rate*1000)/1000`` rate rounding). The loader composes the
existing Python projections (decision-audit, mine-utilization + trend,
mining-effectiveness, alliance-snapshot) — no new I/O.

Registered difference from the TS oracle: ``now_ms`` is injectable for the
``generatedAt``/``cachedAt`` wall-clock fields (TS ``new Date().toISOString()``).
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Any

from ..goal_store import iso_utc
from ..paths import TENANTS, validate_data_root
from ._common import current_epoch_ms, num

__all__ = ["aggregate_alignment", "load_alignment_audit"]

# TS ``AlignmentGrade``: the only grades the aggregation may emit.
ALIGNMENT_GRADES: tuple[str, ...] = (
    "aligned",
    "gap_widening",
    "allocation_unfulfilled",
    "data_gap",
)

# Loader parameters matching the TS ``loadAlignmentAudit`` composition.
TREND_WINDOW = 2000
TREND_STEPS = 3


def _js_round(value: float) -> int:
    """Mirror the TS ``Math.round`` (half away from zero for positives)."""
    return math.floor(value + 0.5)


def _pct0(rate: float) -> str:
    """Render ``(rate*100).toFixed(0)`` — one integer percent (TS parity)."""
    return str(math.floor(rate * 100 + 0.5))


def _is_number(value: object) -> bool:
    """TS ``typeof x === "number"`` — strings/bools are not numbers here."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def aggregate_alignment(
    decisions: Mapping[str, Any] | None,
    mines: Mapping[str, Any] | None,
    effectiveness: Mapping[str, Any] | None,
    trends: Mapping[str, Any] | None = None,
    workers_by_tenant: Mapping[str, Any] | None = None,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Pure port of TS ``aggregateAlignment`` (grade + reasons per tenant)."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    tenants: dict[str, Any] = {}
    aligned_n = 0
    misaligned_n = 0
    data_gap_n = 0
    unfulfilled = 0
    for t in TENANTS:
        dec: Any = None
        if isinstance(decisions, Mapping):
            tenant_dec = decisions.get(t)
            if isinstance(tenant_dec, Mapping):
                dec = tenant_dec.get("decision")
        action_mix = (
            dec.get("actionMix")
            if isinstance(dec, Mapping) and isinstance(dec.get("actionMix"), Mapping)
            else {}
        )
        total = (
            num(action_mix.get("move"))
            + num(action_mix.get("harvest"))
            + num(action_mix.get("deposit"))
            + num(action_mix.get("wait"))
            + num(action_mix.get("repair"))
        )
        harvest_rate = num(action_mix.get("harvest")) / total if total > 0 else None
        deposit_rate = num(action_mix.get("deposit")) / total if total > 0 else None
        tenant_mines = mines.get(t) if isinstance(mines, Mapping) else None
        visible_never = (
            num(tenant_mines.get("visibleNever")) if isinstance(tenant_mines, Mapping) else 0
        )
        eff: Any = None
        if isinstance(effectiveness, Mapping):
            per_tenant = effectiveness.get("perTenant")
            if isinstance(per_tenant, Mapping):
                eff = per_tenant.get(t)
        assigned = num(eff.get("assigned")) if isinstance(eff, Mapping) else 0
        open_count = num(eff.get("open")) if isinstance(eff, Mapping) else 0
        stale = num(eff.get("stale")) if isinstance(eff, Mapping) else 0
        harvested = num(eff.get("harvested")) if isinstance(eff, Mapping) else 0
        workers = workers_by_tenant.get(t) if isinstance(workers_by_tenant, Mapping) else None
        trend = trends.get(t) if isinstance(trends, Mapping) else None
        gap_trend_delta: int | float | None = None
        if (
            isinstance(trend, Mapping)
            and _is_number(trend.get("visibleNever"))
            and _is_number(trend.get("visibleNeverPrev"))
        ):
            gap_trend_delta = num(trend.get("visibleNever")) - num(trend.get("visibleNeverPrev"))

        reasons: list[str] = []
        grade = "aligned"
        if harvest_rate is None and assigned == 0 and visible_never == 0:
            grade = "data_gap"
        else:
            if assigned > 0 and harvested == 0 and open_count + stale > 0:
                grade = "allocation_unfulfilled"
                stale_suffix = f"/{stale} 失效" if stale > 0 else ""
                reasons.append(
                    f"分工 {assigned} 矿 0 兑现（{open_count} 在途{stale_suffix}）——需派 worker"
                )
                unfulfilled += 1
            if visible_never >= 10 and (harvest_rate if harvest_rate is not None else 0) < 0.3:
                if grade == "aligned":
                    grade = "gap_widening"
                share = _pct0(harvest_rate) if harvest_rate is not None else "-"
                reasons.append(f"缺口 {visible_never} 但采集动作占比 {share}%——决策未对齐矿分配")
            if gap_trend_delta is not None and gap_trend_delta > 0:
                reasons.append(f"缺口较上窗口 +{gap_trend_delta}")
            if (
                workers is not None
                and workers > 0
                and (harvest_rate if harvest_rate is not None else 0) < 0.05
            ):
                share = _pct0(harvest_rate) if harvest_rate is not None else "-"
                reasons.append(f"有 {workers} 个 worker 但采集占比 {share}%——worker 空闲/在移动")
            if grade == "aligned" and not reasons and harvest_rate is not None:
                reasons.append(f"采集占比 {_pct0(harvest_rate)}%，缺口 {visible_never}——对齐")
            if grade == "aligned":
                aligned_n += 1
            else:
                misaligned_n += 1
        if grade == "data_gap":
            data_gap_n += 1

        tenants[t] = {
            "tenant": t,
            "harvestActionRate": (
                None if harvest_rate is None else _js_round(harvest_rate * 1000) / 1000
            ),
            "depositActionRate": (
                None if deposit_rate is None else _js_round(deposit_rate * 1000) / 1000
            ),
            "visibleNever": visible_never,
            "gapTrendDelta": gap_trend_delta,
            "assigned": assigned,
            "open": open_count,
            "stale": stale,
            "harvested": harvested,
            "workers": workers,
            "grade": grade,
            "reasons": reasons,
        }
    return {
        "generatedAt": at,
        "tenants": tenants,
        "global": {
            "aligned": aligned_n,
            "misaligned": misaligned_n,
            "dataGap": data_gap_n,
            "unfulfilledAssignments": unfulfilled,
        },
        "cachedAt": at,
    }


def load_alignment_audit(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load the ``/api/audit/alignment`` payload (all inputs already exist)."""
    from .alliance_snapshot import load_alliance_snapshot
    from .decisions import load_decision_audit
    from .mines import load_mine_utilization, load_mine_utilization_trend
    from .mining_effectiveness import load_mining_effectiveness

    root = validate_data_root(data_root)
    now = now_ms if now_ms is not None else current_epoch_ms()
    decisions = load_decision_audit(root, "all")
    mines = load_mine_utilization(root, "all")
    effectiveness = load_mining_effectiveness(root, now_ms=now)
    snapshot = load_alliance_snapshot(root, now_ms=now)
    members = snapshot.get("members") or {}
    workers_by_tenant: dict[str, Any] = {}
    for t in TENANTS:
        member = members.get(t)
        raw_workers = member.get("workers") if isinstance(member, Mapping) else None
        workers_by_tenant[t] = num(raw_workers) if _is_number(raw_workers) else None
    trends: dict[str, dict[str, Any]] = {}
    for t in TENANTS:
        try:
            trend_payload = load_mine_utilization_trend(root, t, TREND_WINDOW, TREND_STEPS)
        except Exception:  # noqa: BLE001 - TS try/catch parity: trend optional
            continue
        trend_rows = list(trend_payload.get("trend") or ())
        if not trend_rows:
            continue
        last = trend_rows[-1]
        prev = trend_rows[-2] if len(trend_rows) >= 2 else None
        if last is not None:
            trends[t] = {
                "visibleNever": num(last.get("visibleNever")),
                "visibleNeverPrev": num(prev.get("visibleNever")) if prev is not None else 0,
            }
    return aggregate_alignment(
        decisions,
        mines.get("tenants") or {},
        effectiveness,
        trends,
        workers_by_tenant,
        now_ms=now,
    )
