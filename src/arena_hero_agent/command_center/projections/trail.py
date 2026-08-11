"""Unified audit trail projection (port of legacy ``audit-trail.ts``).

Normalizes four persisted audit streams into one time-descending feed —
``human`` (human-command-audit.jsonl), ``command`` (command-audit/<t>.jsonl),
``arbitration`` (arbitration.jsonl), ``supervisor`` (supervisor.jsonl) —
for ``/api/audit/trail`` with optional tenant/source filters.

Registered difference from the TS oracle: entries with an absent reference
omit the ``ref`` key (the oracle's ``JSON.stringify`` drops ``undefined``),
so the Python port conditionally includes it.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import load_jsonl_rows
from ..paths import TENANTS, validate_data_root
from ._common import current_epoch_ms

SOURCES: tuple[str, ...] = ("human", "command", "arbitration", "supervisor")
DEFAULT_LIMIT = 200
MAX_LIMIT = 500

__all__ = [
    "DEFAULT_LIMIT",
    "SOURCES",
    "load_audit_trail",
    "merge_audit_trails",
    "normalize_audit_trails",
]


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _norm_time(row: dict[str, Any]) -> str:
    for key in ("at", "ts", "createdAt"):
        value = row.get(key)
        if isinstance(value, str):
            return value
    return ""


def normalize_audit_trails(
    human: list[dict[str, Any]],
    commands_by_tenant: dict[str, list[dict[str, Any]]],
    arbitrations: list[dict[str, Any]],
    supervisors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize the four sources into unified entries (TS parity, unsorted)."""
    out: list[dict[str, Any]] = []
    for h in human:
        ref = h.get("unitId") or None
        action = _text(h.get("action"))
        note = _text(h.get("note"))
        detail = " — ".join(part for part in (action, note) if part) or _text(h.get("kind"))
        entry: dict[str, Any] = {
            "at": _text(h.get("at")),
            "source": "human",
            "tenant": h.get("tenant"),
            "kind": _text(h.get("kind")),
            "detail": detail,
        }
        if ref is not None:
            entry["ref"] = ref
        out.append(entry)
    for t, rows in commands_by_tenant.items():
        for r in rows:
            kind = _text(r.get("kind"))
            action = _text(r.get("action"))
            evidence = r.get("evidence")
            target = "-"
            if isinstance(evidence, dict) and evidence.get("target"):
                target = json.dumps(evidence["target"], ensure_ascii=False, separators=(",", ":"))
            units = ""
            if isinstance(evidence, dict) and evidence.get("unitIds"):
                units = f" unit={len(evidence['unitIds'])}"
            entry = {
                "at": _norm_time(r),
                "source": "command",
                "tenant": t or None,
                "kind": kind or action or "command",
                "detail": (
                    f"{action or kind} → {target}{units} issuer={_text(r.get('issuer')) or '-'}"
                ),
            }
            if target != "-":
                entry["ref"] = target
            out.append(entry)
    for r in arbitrations:
        cell = _text(r.get("cell"))
        winner_value = r.get("winnerTenant")
        winner = "auto" if winner_value is None else _text(winner_value)
        note = _text(r.get("note"))
        entry = {
            "at": _norm_time(r),
            "source": "arbitration",
            "tenant": None,
            "kind": "arbitrate-clear" if winner == "auto" else "arbitrate",
            "detail": f"cell {cell} → winner {winner}{'（' + note + '）' if note else ''}",
        }
        if cell:
            entry["ref"] = cell
        out.append(entry)
    for r in supervisors:
        type_value = _text(r.get("type"))
        pid = f" pid={r['pid']}" if r.get("pid") is not None else ""
        code = f" code={r['exitCode']}" if r.get("exitCode") is not None else ""
        sig = f" sig={r['signal']}" if r.get("signal") is not None else ""
        out.append(
            {
                "at": _norm_time(r),
                "source": "supervisor",
                "tenant": _text(r.get("tenantId")) or None,
                "kind": type_value or "event",
                "detail": f"{type_value}{pid}{code}{sig}".strip(),
            }
        )
    return out


def merge_audit_trails(
    normalized: list[dict[str, Any]],
    *,
    tenant: str | None = None,
    source: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Filter and sort normalized entries time-descending (TS parity)."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError(f"limit must be an integer; actual={limit!r}")
    cap = min(max(limit, 1), MAX_LIMIT)
    filtered = [
        entry
        for entry in normalized
        if (tenant is None or entry.get("tenant") == tenant)
        and (source is None or entry.get("source") == source)
    ]
    filtered.sort(key=lambda entry: entry["at"], reverse=True)
    return filtered[:cap]


def load_audit_trail(
    data_root: str | os.PathLike[str],
    *,
    tenant: str | None = None,
    source: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Read the four audit sources and merge into the unified feed (``/api/audit/trail``)."""
    root = validate_data_root(data_root)
    if source is not None and source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}; actual={source!r}")
    human = load_jsonl_rows(root / "runtime" / "human-command-audit.jsonl")
    commands_by_tenant: dict[str, list[dict[str, Any]]] = {}
    for t in TENANTS:
        commands_by_tenant[t] = load_jsonl_rows(root / "runtime" / "command-audit" / f"{t}.jsonl")
    arbitrations = load_jsonl_rows(root / "runtime" / "survey" / "arbitration.jsonl")
    supervisors = load_jsonl_rows(root / "runtime" / "supervisor.jsonl")
    normalized = normalize_audit_trails(human, commands_by_tenant, arbitrations, supervisors)
    entries = merge_audit_trails(normalized, tenant=tenant, source=source, limit=limit)
    counts: dict[str, int] = {name: 0 for name in SOURCES}
    for entry in entries:
        entry_source = entry.get("source")
        if isinstance(entry_source, str) and entry_source in counts:
            counts[entry_source] += 1
    at = iso_utc(current_epoch_ms())
    return {
        "generatedAt": at,
        "entries": entries,
        "counts": counts,
        "filters": {"tenant": tenant, "source": source},
        "cachedAt": at,
    }
