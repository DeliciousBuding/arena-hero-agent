"""Latest calibration plan/world snapshots (port of legacy ``streams.ts``).

Ports ``loadPlan`` / ``loadWorld`` from the TypeScript oracle: the most recent
calibration run's newest case is the live plan (``after.plan``) and world
state (``after.state`` / ``before.state`` + tick). Pure read; nothing is
written. ``/api/plan`` and ``/api/world``.

Registered difference from the TS oracle: ``generatedAt`` is injectable via
``now_ms``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import calibration_dir, latest_run_dir, list_cases, parse_tick
from ..paths import validate_data_root
from ._common import current_epoch_ms

__all__ = ["load_plan", "load_world"]


def _latest_case_path(
    data_root: str | os.PathLike[str], tenant: str
) -> tuple[str | None, str | None]:
    """Latest run dir + newest case file name (``(None, None)`` when no data)."""
    root = validate_data_root(data_root)
    run_dir = latest_run_dir(root, tenant)
    if run_dir is None:
        return None, None
    files = list_cases(root, tenant, run_dir)
    if not files:
        return run_dir, None
    return run_dir, files[-1]


def load_plan(
    data_root: str | os.PathLike[str],
    tenant: str,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Latest case decision plan (TS ``loadPlan``)."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    run_dir, case_file = _latest_case_path(data_root, tenant)
    if run_dir is None or case_file is None:
        return {"tenant": tenant, "generatedAt": at, "plan": None, "tick": None}
    path = calibration_dir(data_root, tenant) / run_dir / "cases" / case_file
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "tenant": tenant,
            "generatedAt": at,
            "plan": None,
            "tick": None,
            "error": str(exc),
        }
    plan = raw.get("plan") if isinstance(raw, dict) else None
    return {
        "tenant": tenant,
        "generatedAt": at,
        "plan": plan,
        "tick": parse_tick(case_file),
    }


def load_world(
    data_root: str | os.PathLike[str],
    tenant: str,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Latest calibration world snapshot (TS ``loadWorld``)."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    run_dir, case_file = _latest_case_path(data_root, tenant)
    if run_dir is None or case_file is None:
        return {"tenant": tenant, "generatedAt": at, "state": None, "caseFile": None}
    path = calibration_dir(data_root, tenant) / run_dir / "cases" / case_file
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "tenant": tenant,
            "generatedAt": at,
            "state": None,
            "caseFile": None,
            "error": str(exc),
        }
    after = raw.get("after") if isinstance(raw, dict) else None
    before = raw.get("before") if isinstance(raw, dict) else None
    after_state = after.get("state") if isinstance(after, dict) else None
    before_state = before.get("state") if isinstance(before, dict) else None
    tick = None
    if isinstance(after, dict) and after.get("tick") is not None:
        tick = after.get("tick")
    elif isinstance(before, dict) and before.get("tick") is not None:
        tick = before.get("tick")
    return {
        "tenant": tenant,
        "generatedAt": at,
        "runId": run_dir,
        "caseFile": case_file,
        "tick": tick,
        "state": after_state if after_state is not None else before_state,
    }
