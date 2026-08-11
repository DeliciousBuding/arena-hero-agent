"""Decision-outcome audit projection (port of legacy ``decision-audit.ts``).

Aggregates per-tenant ``decision.jsonl`` (per-tick actions/intent/planHash)
and ``outcome.jsonl`` (economy/delivery/worker/human-override) into the
decision-health audit and a sliding-window trend used by ``/api/audit/decisions``
and ``/api/audit/decisions/trend``. Pure read path, 30 s cache semantics live
at the API layer (P5-5); the aggregation cores here are deterministic and
testable.

Registered differences from the TS oracle:

- The pure cores take already-parsed JSON rows instead of raw line strings;
  parsing is delegated to the P5-3 JSONL base (``read_jsonl_tail``), which is
  fail-closed on valid-JSON non-object rows and skips malformed lines.
- ``generatedAt``/``cachedAt`` are injectable via ``now_ms`` so tests are
  deterministic; loaders default to the current wall clock.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import read_jsonl_tail
from ..paths import TENANTS, telemetry_dir, validate_data_root, validate_tenant
from ._common import current_epoch_ms, num

DEFAULT_RECORDS = 3000
DECISION_TREND_WINDOW = 500
DECISION_TREND_STEPS = 6

__all__ = [
    "DEFAULT_RECORDS",
    "DECISION_TREND_STEPS",
    "DECISION_TREND_WINDOW",
    "aggregate_decision_audit",
    "aggregate_decision_trend",
    "load_decision_audit",
    "load_decision_trend",
]


def _generated_at(now_ms: int | None) -> str:
    return iso_utc(now_ms if now_ms is not None else current_epoch_ms())


def _empty_audit(tenant: str, window: int, now_ms: int | None) -> dict[str, Any]:
    at = _generated_at(now_ms)
    return {
        "generatedAt": at,
        "tenant": tenant,
        "window": window,
        "currentTick": None,
        "decision": {
            "records": 0,
            "actionMix": {},
            "intentTop": [],
            "sourceMix": {},
            "planChurn": None,
            "stallTicks": 0,
        },
        "outcome": {
            "records": 0,
            "coreDeltaSum": 0,
            "coreDeltaPositiveTicks": 0,
            "depositSucceeded": 0,
            "depositFailed": 0,
            "harvestSucceeded": 0,
            "harvestFailed": 0,
            "depositSuccessRate": None,
            "cargoEfficiency": None,
            "workerMeanDistFromCore": None,
            "humanApplied": 0,
            "humanRejected": 0,
        },
        "cachedAt": at,
    }


def aggregate_decision_audit(
    tenant: str,
    window: int,
    d_rows: list[dict[str, Any]],
    o_rows: list[dict[str, Any]],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Aggregate decision/outcome rows into the decision-health audit (TS parity)."""
    at = _generated_at(now_ms)
    if not d_rows and not o_rows:
        return _empty_audit(tenant, window, now_ms)

    action_mix: dict[str, int] = {"move": 0, "harvest": 0, "deposit": 0, "wait": 0, "repair": 0}
    intent_counts: dict[str, int] = {}
    source_mix: dict[str, int] = {}
    plan_hashes: set[str] = set()
    d_records = 0
    stall_ticks = 0
    current_tick = 0
    for d in d_rows:
        d_records += 1
        action_mix["move"] += int(num(d.get("moveCount")))
        action_mix["harvest"] += int(num(d.get("harvestCount")))
        action_mix["deposit"] += int(num(d.get("depositCount")))
        action_mix["wait"] += int(num(d.get("waitCount")))
        action_mix["repair"] += int(num(d.get("repairCount")))
        src = str(d.get("decisionSource") or "unknown")
        source_mix[src] = source_mix.get(src, 0) + 1
        plan_hash = d.get("planHash")
        if plan_hash is not None:
            plan_hashes.add(str(plan_hash))
        if (
            num(d.get("waitCount")) > 0
            and num(d.get("harvestCount")) == 0
            and num(d.get("depositCount")) == 0
        ):
            stall_ticks += 1
        tick = num(d.get("tick"))
        if tick > current_tick:
            current_tick = tick
        raw_intents = d.get("intentCounts")
        if isinstance(raw_intents, dict):
            for key, value in raw_intents.items():
                count = num(value)
                if count > 0:
                    intent_counts[str(key)] = intent_counts.get(str(key), 0) + int(count)

    intent_top = [
        {"intent": intent, "count": count}
        for intent, count in sorted(intent_counts.items(), key=lambda item: item[1], reverse=True)[
            :12
        ]
    ]

    o_records = 0
    core_delta_sum = 0
    core_delta_positive = 0
    dep_ok = 0
    dep_fail = 0
    harv_ok = 0
    harv_fail = 0
    cargo_sum = 0
    cargo_n = 0
    dist_sum = 0
    dist_n = 0
    applied = 0
    rejected = 0

    def count_event(event: object) -> None:
        nonlocal dep_ok, dep_fail, harv_ok, harv_fail
        if isinstance(event, str):
            name = event
        elif isinstance(event, dict):
            name = event.get("eventType")
        else:
            name = None
        label = str(name or "")
        if label.startswith("DEPOSIT_SUCCEEDED"):
            dep_ok += 1
        elif label.startswith("DEPOSIT_FAILED"):
            dep_fail += 1
        elif label.startswith("HARVEST_SUCCEEDED"):
            harv_ok += 1
        elif label.startswith("HARVEST_FAILED"):
            harv_fail += 1

    for o in o_rows:
        o_records += 1
        core_delta_sum += num(o.get("coreResourceDelta"))
        if num(o.get("coreResourceDelta")) > 0:
            core_delta_positive += 1
        events = o.get("events")
        if isinstance(events, list):
            for event in events:
                count_event(event)
        failed_events = o.get("failedEvents")
        if isinstance(failed_events, list):
            for event in failed_events:
                count_event(event)
        worker_count = num(o.get("workerCount"))
        workers_with_cargo = num(o.get("workersWithCargo"))
        if worker_count > 0:
            cargo_sum += min(1, workers_with_cargo / worker_count)
            cargo_n += 1
        dist = num(o.get("workerMeanDistanceFromCore"))
        if dist > 0:
            dist_sum += dist
            dist_n += 1
        human_override = o.get("humanOverride")
        if isinstance(human_override, dict):
            if isinstance(human_override.get("applied"), list):
                applied += len(human_override["applied"])
            if isinstance(human_override.get("rejected"), list):
                rejected += len(human_override["rejected"])

    return {
        "generatedAt": at,
        "tenant": tenant,
        "window": window,
        "currentTick": current_tick if current_tick > 0 else None,
        "decision": {
            "records": d_records,
            "actionMix": action_mix,
            "intentTop": intent_top,
            "sourceMix": source_mix,
            "planChurn": (
                {
                    "unique": len(plan_hashes),
                    "records": d_records,
                    "rate": round(len(plan_hashes) / d_records * 1000) / 1000,
                }
                if d_records > 0
                else None
            ),
            "stallTicks": stall_ticks,
        },
        "outcome": {
            "records": o_records,
            "coreDeltaSum": core_delta_sum,
            "coreDeltaPositiveTicks": core_delta_positive,
            "depositSucceeded": dep_ok,
            "depositFailed": dep_fail,
            "harvestSucceeded": harv_ok,
            "harvestFailed": harv_fail,
            "depositSuccessRate": (
                round(dep_ok / (dep_ok + dep_fail) * 1000) / 1000 if dep_ok + dep_fail > 0 else None
            ),
            "cargoEfficiency": round(cargo_sum / cargo_n * 1000) / 1000 if cargo_n > 0 else None,
            "workerMeanDistFromCore": round(dist_sum / dist_n * 10) / 10 if dist_n > 0 else None,
            "humanApplied": applied,
            "humanRejected": rejected,
        },
        "cachedAt": at,
    }


def aggregate_decision_trend(
    tenant: str,
    window: int,
    steps: int,
    d_rows: list[dict[str, Any]],
    o_rows: list[dict[str, Any]],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Slice decision/outcome rows into N consecutive windows (TS parity)."""
    at = _generated_at(now_ms)
    trend: list[dict[str, Any]] = []
    for index in range(steps):
        start = len(d_rows) - steps * window + index * window
        end = start + window
        d_slice = d_rows[max(0, start) : max(0, end)]
        o_slice = o_rows[max(0, start) : max(0, end)]
        audit = aggregate_decision_audit(tenant, window, d_slice, o_slice, now_ms=now_ms)
        decision = audit["decision"]
        outcome = audit["outcome"]
        trend.append(
            {
                "index": index,
                "window": decision["records"],
                "tick": audit["currentTick"],
                "stallRate": (
                    decision["stallTicks"] / decision["records"]
                    if decision["records"] > 0
                    else None
                ),
                "planChurn": decision["planChurn"]["rate"] if decision["planChurn"] else None,
                "cargoEff": outcome["cargoEfficiency"],
                "coreDelta": outcome["coreDeltaSum"],
                "humanApplied": outcome["humanApplied"],
                "humanRejected": outcome["humanRejected"],
            }
        )
    return {
        "generatedAt": at,
        "tenant": tenant,
        "window": window,
        "steps": steps,
        "trend": trend,
        "cachedAt": at,
    }


def _decision_jsonl(data_root: str | os.PathLike[str], tenant: str, name: str) -> Path:
    return telemetry_dir(data_root, tenant) / name


def load_decision_audit(
    data_root: str | os.PathLike[str],
    tenant: str = "all",
    window: int = DEFAULT_RECORDS,
) -> dict[str, Any]:
    """Read per-tenant decision/outcome tails and aggregate (``/api/audit/decisions``)."""
    root = validate_data_root(data_root)
    if tenant == "all":
        per_tenant: dict[str, Any] = {}
        for t in TENANTS:
            per_tenant[t] = _audit_tenant(root, t, window)
        return per_tenant
    return _audit_tenant(root, validate_tenant(tenant), window)


def _audit_tenant(root: Path, tenant: str, window: int) -> dict[str, Any]:
    d_rows = read_jsonl_tail(_decision_jsonl(root, tenant, "decision.jsonl"), window)
    o_rows = read_jsonl_tail(_decision_jsonl(root, tenant, "outcome.jsonl"), window)
    return aggregate_decision_audit(tenant, window, d_rows, o_rows)


def load_decision_trend(
    data_root: str | os.PathLike[str],
    tenant: str,
    window: int = DECISION_TREND_WINDOW,
    steps: int = DECISION_TREND_STEPS,
) -> dict[str, Any]:
    """Read decision/outcome tails and aggregate into a sliding-window trend."""
    root = validate_data_root(data_root)
    tenant_value = validate_tenant(tenant)
    total = min(max(window * steps, 500), 20_000)
    d_rows = read_jsonl_tail(_decision_jsonl(root, tenant_value, "decision.jsonl"), total)
    o_rows = read_jsonl_tail(_decision_jsonl(root, tenant_value, "outcome.jsonl"), total)
    return aggregate_decision_trend(tenant_value, window, steps, d_rows, o_rows)
