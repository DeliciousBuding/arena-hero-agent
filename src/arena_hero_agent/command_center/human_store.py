"""Human command store (port of legacy ``store.ts``).

The store is the highest-priority human control surface: read/write/reconcile/
stuck-cancel for ``data/runtime/human-commands/<tenant>.json``. Only this
module writes the store file, and writes are atomic (tmp + rename) so a crash
mid-write never leaves a half-written JSON document.

Behavioral notes and registered differences from the TS oracle:

- Missing file and corrupt JSON both yield the empty store (TS parity).
- Structurally valid JSON whose ``commands``/``goals`` entries violate the
  documented shape raises ``CommandCenterError`` instead of being trusted as a
  cast (fail-closed schema check).
- ``write_human_store`` always writes ``version: 1`` and a fresh
  ``updatedAt`` (TS parity); the ``tenant`` field is never persisted.
- ``reconcile_human_store`` cleans satisfied/applied/unknown_unit entries with
  a timing guard (``createdAt <= processedAt``) and cancels goals stuck in
  ``WAIT`` for ``STUCK_TICKS`` ticks, exactly like the oracle.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from arena_hero_agent.domain import TenantId

from .errors import CommandCenterError
from .goal_store import GoalEntry, iso_utc
from .jsonl import latest_run_dir, list_cases, read_jsonl_tail
from .paths import (
    calibration_dir,
    human_commands_file,
    outcome_jsonl_path,
    validate_data_root,
)

STUCK_TICKS = 8
STUCK_RING_MAX = 6
HUMAN_OVERRIDE_TAIL = 4

CommandMode = Literal["override", "disabled"]


@dataclass(slots=True)
class HumanCommand:
    """One one-shot human command targeting a unit."""

    id: str
    unit_id: str
    action: dict[str, Any]
    created_at: str
    note: str | None = None


@dataclass(slots=True)
class HumanStore:
    """Per-tenant human command store document."""

    version: int
    mode: CommandMode
    commands: list[HumanCommand]
    goals: list[GoalEntry]
    updated_at: str | None
    tenant: str | None = None


@dataclass(slots=True)
class StuckEntry:
    """Record of an automatically cancelled stuck goal."""

    unit_id: str
    kind: str
    target: list[int]
    reason: str
    at: str


def empty_store(tenant: str) -> HumanStore:
    """Return the canonical empty store for a tenant."""
    return HumanStore(
        version=1, mode="override", commands=[], goals=[], updated_at=None, tenant=tenant
    )


def _parse_command(raw: object) -> HumanCommand:
    if not isinstance(raw, dict):
        raise CommandCenterError(
            f"human store command must be an object; actual={type(raw).__name__}"
        )
    try:
        return HumanCommand(
            id=_require_string(raw, "id"),
            unit_id=_require_string(raw, "unitId"),
            action=_require_dict(raw, "action"),
            created_at=_require_string(raw, "createdAt"),
            note=_optional_string(raw, "note"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CommandCenterError(f"malformed human store command: {exc}") from exc


def _parse_goal(raw: object) -> GoalEntry:
    if not isinstance(raw, dict):
        raise CommandCenterError(f"human store goal must be an object; actual={type(raw).__name__}")
    try:
        kind = _require_string(raw, "kind")
        if kind not in ("mine", "goto"):
            raise CommandCenterError(f"goal kind must be mine|goto; actual={kind!r}")
        target = _require_int_pair(raw, "target")
        return GoalEntry(
            id=_require_string(raw, "id"),
            unit_id=_require_string(raw, "unitId"),
            kind=kind,  # type: ignore[arg-type]
            target=target,
            created_at=_require_string(raw, "createdAt"),
            note=_optional_string(raw, "note"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CommandCenterError(f"malformed human store goal: {exc}") from exc


def _require_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise CommandCenterError(f"field {key!r} must be a string; actual={value!r}")
    return value


def _optional_string(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CommandCenterError(f"field {key!r} must be a string when present; actual={value!r}")
    return value


def _require_dict(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise CommandCenterError(f"field {key!r} must be an object; actual={value!r}")
    return value


def _require_int_pair(raw: dict[str, Any], key: str) -> tuple[int, int]:
    value = raw.get(key)
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    ):
        raise CommandCenterError(f"field {key!r} must be a [number, number] pair; actual={value!r}")
    return (value[0], value[1])


def read_human_store(data_root: str | os.PathLike[str], tenant: str | TenantId) -> HumanStore:
    """Read one tenant's human command store (missing/corrupt file -> empty store)."""
    root = validate_data_root(data_root)
    tenant_value = tenant.value if isinstance(tenant, TenantId) else tenant
    file = human_commands_file(root, tenant_value)
    if not file.exists():
        return empty_store(tenant_value)
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_store(tenant_value)
    if not isinstance(raw, dict):
        return empty_store(tenant_value)
    version = raw.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise CommandCenterError(f"human store version must be an integer; actual={version!r}")
    mode: CommandMode = "disabled" if raw.get("mode") == "disabled" else "override"
    commands_raw = raw.get("commands", [])
    goals_raw = raw.get("goals", [])
    if not isinstance(commands_raw, list) or not isinstance(goals_raw, list):
        raise CommandCenterError("human store commands/goals must be arrays")
    updated_at = raw.get("updatedAt")
    if updated_at is not None and not isinstance(updated_at, str):
        raise CommandCenterError(
            f"human store updatedAt must be a string or null; actual={updated_at!r}"
        )
    return HumanStore(
        version=version,
        mode=mode,
        commands=[_parse_command(item) for item in commands_raw],
        goals=[_parse_goal(item) for item in goals_raw],
        updated_at=updated_at,
        tenant=tenant_value,
    )


def _command_to_json(command: HumanCommand) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": command.id,
        "unitId": command.unit_id,
        "action": command.action,
        "createdAt": command.created_at,
    }
    if command.note is not None:
        out["note"] = command.note
    return out


def _goal_to_json(goal: GoalEntry) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": goal.id,
        "unitId": goal.unit_id,
        "kind": goal.kind,
        "target": [goal.target[0], goal.target[1]],
        "createdAt": goal.created_at,
    }
    if goal.note is not None:
        out["note"] = goal.note
    return out


def write_human_store(
    data_root: str | os.PathLike[str],
    tenant: str | TenantId,
    store: HumanStore,
    *,
    now_ms: int | None = None,
) -> HumanStore:
    """Atomically write a tenant's human command store (returns the persisted shape)."""
    root = validate_data_root(data_root)
    tenant_value = tenant.value if isinstance(tenant, TenantId) else tenant
    now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    payload: dict[str, Any] = {
        "version": 1,
        "mode": store.mode,
        "commands": [_command_to_json(command) for command in store.commands],
        "goals": [_goal_to_json(goal) for goal in store.goals],
        "updatedAt": iso_utc(now),
    }
    target = human_commands_file(root, tenant_value)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_name, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise CommandCenterError(f"failed to write human store {target}: {exc}") from exc
    return HumanStore(
        version=payload["version"],
        mode=payload["mode"],
        commands=store.commands,
        goals=store.goals,
        updated_at=payload["updatedAt"],
        tenant=None,
    )


def latest_human_override(
    data_root: str | os.PathLike[str], tenant: str | TenantId
) -> dict[str, Any] | None:
    """Latest humanOverride telemetry from the outcome tail (null when absent)."""
    root = validate_data_root(data_root)
    tenant_value = tenant.value if isinstance(tenant, TenantId) else tenant
    file = outcome_jsonl_path(root, tenant_value)
    if not file.exists():
        return None
    rows = read_jsonl_tail(file, HUMAN_OVERRIDE_TAIL)
    last = rows[-1] if rows else None
    if last is None or "humanOverride" not in last:
        return None
    override = last["humanOverride"]
    if not isinstance(override, dict):
        raise CommandCenterError(
            f"humanOverride must be an object; actual={type(override).__name__}"
        )
    applied = override.get("applied") or []
    rejected = override.get("rejected") or []
    satisfied = override.get("satisfied") or []
    if not override.get("active") and not applied and not rejected and not satisfied:
        return None
    return {"tick": last.get("tick"), **override}


def cancel_stuck_goals(
    data_root: str | os.PathLike[str],
    tenant: str | TenantId,
    store: HumanStore,
    override: dict[str, Any],
    *,
    now_ms: int | None = None,
) -> tuple[HumanStore, list[StuckEntry]]:
    """Cancel active goals whose units have only WAITed for STUCK_TICKS ticks."""
    root = validate_data_root(data_root)
    tenant_value = tenant.value if isinstance(tenant, TenantId) else tenant
    applied = set(_string_list(override.get("applied")))
    active_goals = [goal for goal in store.goals if goal.unit_id in applied]
    if not active_goals:
        return store, []
    run_dir = latest_run_dir(root, tenant_value)
    if run_dir is None:
        return store, []
    cases = list_cases(root, tenant_value, run_dir)
    if len(cases) < STUCK_TICKS:
        return store, []
    now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    stuck: list[StuckEntry] = []
    for goal in active_goals:
        wait_count = 0
        for index in range(STUCK_TICKS):
            case_name = cases[len(cases) - 1 - index]
            case_path = calibration_dir(root, tenant_value) / run_dir / "cases" / case_name
            action_type = _unit_action_type(case_path, goal.unit_id)
            if action_type is None:
                continue
            if action_type == "WAIT":
                wait_count += 1
            elif action_type in ("MOVE", "HARVEST", "DEPOSIT"):
                wait_count = 0
                break
        if wait_count >= STUCK_TICKS - 1:
            reason = f"连续 {wait_count} tick 无推进（路径被堵/目标不可达），已自动取消"
            stuck.append(
                StuckEntry(
                    unit_id=goal.unit_id,
                    kind=goal.kind,
                    target=[goal.target[0], goal.target[1]],
                    reason=reason,
                    at=iso_utc(now),
                )
            )
            store.goals = [entry for entry in store.goals if entry.id != goal.id]
    return store, stuck


def _unit_action_type(case_path: Path, unit_id: str) -> str | None:
    try:
        raw = json.loads(case_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    plan = raw.get("plan")
    if not isinstance(plan, dict):
        return None
    unit_actions = plan.get("unitActions")
    if not isinstance(unit_actions, dict):
        return None
    action = unit_actions.get(unit_id)
    if not isinstance(action, dict):
        return None
    action_type = action.get("type")
    return action_type if isinstance(action_type, str) else None


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def reconcile_human_store(
    data_root: str | os.PathLike[str],
    tenant: str | TenantId,
    *,
    now_ms: int | None = None,
) -> HumanStore:
    """Reconcile the store against latest telemetry and cancel stuck goals."""
    root = validate_data_root(data_root)
    tenant_value = tenant.value if isinstance(tenant, TenantId) else tenant
    store = read_human_store(root, tenant_value)
    if not store.commands and not store.goals:
        return store
    override = latest_human_override(root, tenant_value)
    if override is None:
        return store
    processed_at = override.get("updatedAt") if isinstance(override.get("updatedAt"), str) else None

    def within(created_at: object) -> bool:
        return processed_at is None or (isinstance(created_at, str) and created_at <= processed_at)

    satisfied = set(_string_list(override.get("satisfied")))
    applied = set(_string_list(override.get("applied")))
    unknown = {
        entry.get("unitId")
        for entry in _rejected_entries(override.get("rejected"))
        if entry.get("reason") == "unknown_unit" and isinstance(entry.get("unitId"), str)
    }
    if not satisfied and not applied and not unknown:
        return store
    goals_before = len(store.goals)
    commands_before = len(store.commands)
    if satisfied:
        store.goals = [
            g for g in store.goals if not (g.unit_id in satisfied and within(g.created_at))
        ]
    if applied:
        store.commands = [
            c for c in store.commands if not (c.unit_id in applied and within(c.created_at))
        ]
    if unknown:
        store.goals = [
            g for g in store.goals if not (g.unit_id in unknown and within(g.created_at))
        ]
        store.commands = [
            c for c in store.commands if not (c.unit_id in unknown and within(c.created_at))
        ]
    store, stuck = cancel_stuck_goals(root, tenant_value, store, override, now_ms=now_ms)
    if len(store.goals) != goals_before or len(store.commands) != commands_before:
        write_human_store(root, tenant_value, store, now_ms=now_ms)
    if stuck:
        ring = _stuck_ring.get(tenant_value) or []
        _stuck_ring[tenant_value] = (ring + stuck)[-STUCK_RING_MAX:]
    return store


def _rejected_entries(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def stuck_record(tenant: str | TenantId) -> list[StuckEntry]:
    """Recent stuck-cancel records for a tenant (display surface for /api/commands)."""
    tenant_value = tenant.value if isinstance(tenant, TenantId) else tenant
    return list(_stuck_ring.get(tenant_value, []))


_stuck_ring: dict[str, list[StuckEntry]] = {}


__all__ = [
    "HUMAN_OVERRIDE_TAIL",
    "STUCK_RING_MAX",
    "STUCK_TICKS",
    "HumanCommand",
    "HumanStore",
    "StuckEntry",
    "cancel_stuck_goals",
    "empty_store",
    "latest_human_override",
    "read_human_store",
    "reconcile_human_store",
    "stuck_record",
    "write_human_store",
]
