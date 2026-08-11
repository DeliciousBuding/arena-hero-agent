"""Human-machine conflict audit projection (port of legacy ``human-conflict.ts``).

Quantifies manual-override versus automatic-decision conflict from the
``outcome.jsonl`` tail (``humanOverride.applied/rejected``) plus the human
command audit stream. ``/api/audit/human/conflicts``. The rejected-reason top
and the command-kind composition are the two signals surfaced by the panel.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import read_jsonl_tail
from ..paths import TENANTS, telemetry_dir, validate_data_root, validate_tenant
from ._common import current_epoch_ms, num
from .human import read_human_audit

DEFAULT_WINDOW = 3000

__all__ = ["DEFAULT_WINDOW", "aggregate_human_conflict", "load_human_conflict"]


def aggregate_human_conflict(
    tenant: str,
    window: int,
    o_rows: list[dict[str, Any]],
    audit_entries: list[dict[str, Any]],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Aggregate outcome rows + human audit entries into a conflict payload (TS parity)."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    applied = 0
    rejected = 0
    current_tick = 0
    reason_counts: dict[str, int] = {}
    for o in o_rows:
        tick = num(o.get("tick"))
        if tick > current_tick:
            current_tick = tick
        human_override = o.get("humanOverride")
        if not isinstance(human_override, dict):
            continue
        if isinstance(human_override.get("applied"), list):
            applied += len(human_override["applied"])
        if isinstance(human_override.get("rejected"), list):
            rejected += len(human_override["rejected"])
            for item in human_override["rejected"]:
                reason = "unknown"
                if isinstance(item, dict) and item.get("reason") is not None:
                    reason = str(item["reason"])
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

    top_rejected_reasons = [
        {
            "reason": reason,
            "count": count,
            "share": round(count / rejected * 1000) / 1000 if rejected > 0 else None,
        }
        for reason, count in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)[
            :10
        ]
    ]

    command_kinds: dict[str, int] = {}
    for entry in audit_entries:
        if entry.get("tenant") != tenant:
            continue
        kind = str(entry.get("kind") or "")
        command_kinds[kind] = command_kinds.get(kind, 0) + 1

    return {
        "generatedAt": at,
        "tenant": tenant,
        "window": window,
        "currentTick": current_tick if current_tick > 0 else None,
        "applied": applied,
        "rejected": rejected,
        "rejectedRate": (
            round(rejected / (applied + rejected) * 1000) / 1000 if applied + rejected > 0 else None
        ),
        "topRejectedReasons": top_rejected_reasons,
        "commandKinds": command_kinds,
        "cachedAt": at,
    }


def _conflict_tenant(root: Path, tenant: str, window: int) -> dict[str, Any]:
    o_rows = read_jsonl_tail(telemetry_dir(root, tenant) / "outcome.jsonl", window)
    audit_entries = read_human_audit(root)
    return aggregate_human_conflict(tenant, window, o_rows, audit_entries)


def load_human_conflict(
    data_root: str | os.PathLike[str],
    tenant: str = "all",
    window: int = DEFAULT_WINDOW,
) -> dict[str, Any]:
    """Read outcome tails + human audit and aggregate (``/api/audit/human/conflicts``)."""
    root = validate_data_root(data_root)
    if tenant == "all":
        per_tenant: dict[str, Any] = {}
        for t in TENANTS:
            per_tenant[t] = _conflict_tenant(root, t, window)
        return per_tenant
    return _conflict_tenant(root, validate_tenant(tenant), window)
