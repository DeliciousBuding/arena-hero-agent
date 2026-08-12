"""Command-operation event stream (port of legacy ``streams.ts``).

Ports ``loadEvents`` from the TypeScript oracle: aggregate structured events
from the latest run's calibration cases (``after.state.events`` — the tick
start ``before.state.events`` is empty — 2026-08-08 fix), filter to the
known ``EVENT_KINDS`` set, flatten into a compact row shape, sort tick
descending, and clamp to ``n``. Pure read. ``/api/events?tenant=tN&n=N``.

Registered difference from the TS oracle: ``generatedAt`` is injectable via
``now_ms``; a case that fails to parse is skipped (TS ``continue``).
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import calibration_dir, latest_run_dir, list_cases, parse_tick
from ..paths import validate_data_root
from ._common import current_epoch_ms, finite_number, num

__all__ = ["EVENT_KINDS", "load_events"]

# TS ``EVENT_KINDS``: the structured command-operation events surfaced by the
# event stream (move / spawn / harvest / deposit / combat / beacon / heal /
# destroy / wait). Event ``event_type`` values are upper-cased before matching.
EVENT_KINDS: frozenset[str] = frozenset(
    {
        "UNIT_MOVE_SUCCEEDED",
        "UNIT_MOVE_FAILED",
        "CORE_MOVE_SUCCEEDED",
        "CORE_MOVE_FAILED",
        "SPAWN_SUCCEEDED",
        "SPAWN_FAILED",
        "HARVEST_SUCCEEDED",
        "HARVEST_FAILED",
        "DEPOSIT_SUCCEEDED",
        "DEPOSIT_FAILED",
        "SHOT_HIT",
        "SHOT_MISSED",
        "SHOT_BLOCKED",
        "SWEEP_RESOLVED",
        "SWEEP_FAILED",
        "PICKUP_BEACON_SUCCEEDED",
        "PICKUP_BEACON_FAILED",
        "DROP_BEACON_SUCCEEDED",
        "DROP_BEACON_FAILED",
        "SELF_DESTRUCT",
        "HEAL_SUCCEEDED",
        "HEAL_FAILED",
        "REPAIR_SHIELD_SUCCEEDED",
        "UNIT_DESTROYED",
        "CORE_DESTROYED",
        "CORE_DAMAGED",
        "RESPAWN",
        "CORE_RESOURCES_CAPTURED",
        "CORE_RESOURCE_OVERFLOW_DESTROYED",
        "WORKER_CARGO_DROPPED",
        "UNIT_HEAL_SUCCEEDED",
        "UNIT_HEAL_FAILED",
        "CORE_HEAL_SUCCEEDED",
        "CORE_HEAL_FAILED",
        "WAIT",
        "NOTHING_TO_DO",
    }
)

_CASE_SCAN_LIMIT = 20


def load_events(
    data_root: str | os.PathLike[str],
    tenant: str,
    n: int,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Latest run's command events, tick descending, clamped to ``n`` (TS parity)."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    root = validate_data_root(data_root)
    run_dir = latest_run_dir(root, tenant)
    if run_dir is None:
        return {"tenant": tenant, "generatedAt": at, "events": []}
    cases = list_cases(root, tenant, run_dir)[-_CASE_SCAN_LIMIT:]
    events: list[dict[str, Any]] = []
    for case_file in cases:
        file_tick = parse_tick(case_file)
        path = calibration_dir(root, tenant) / run_dir / "cases" / case_file
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        after = raw.get("after")
        before = raw.get("before")
        after_events = after.get("state", {}).get("events") if isinstance(after, dict) else None
        before_events = before.get("state", {}).get("events") if isinstance(before, dict) else None
        raw_events = after_events if after_events is not None else before_events
        if not isinstance(raw_events, list):
            continue
        for ev in raw_events:
            if not isinstance(ev, dict):
                continue
            kind = str(ev.get("event_type") or "").upper()
            if kind not in EVENT_KINDS:
                continue
            values = ev.get("values")
            if not isinstance(values, dict):
                values = {}
            amount = finite_number(values.get("amount"))
            if amount is None:
                amount = finite_number(values.get("damage"))
            events.append(
                {
                    "tick": (num(ev.get("tick")) if ev.get("tick") is not None else file_tick),
                    "kind": kind,
                    "reason": ev.get("reason_code") if ev.get("reason_code") is not None else None,
                    "actor": ev.get("actor_id") if ev.get("actor_id") is not None else None,
                    "target": ev.get("target_id") if ev.get("target_id") is not None else None,
                    "position": ev.get("position") if ev.get("position") is not None else None,
                    "amount": amount,
                    "hp": values.get("hp") if values.get("hp") is not None else None,
                    "source": values.get("source") if values.get("source") is not None else None,
                    "capacity": (
                        values.get("capacity") if values.get("capacity") is not None else None
                    ),
                    "destroyedBy": (
                        values.get("destroyed_by")
                        if values.get("destroyed_by") is not None
                        else None
                    ),
                }
            )
    events.sort(key=lambda item: num(item.get("tick")), reverse=True)
    return {
        "tenant": tenant,
        "generatedAt": at,
        "events": events[: max(1, min(n, 200))],
    }
