"""Stable record schema shared by offline recorder backends.

Both backends persist the same logical records so storage can change without
changing the persisted contract:

- ``tick`` records hold one processed tick: the full ``TickResult`` fields,
  the owning tenant, and a wall-clock timestamp.
- ``loop`` records hold one completed tick-loop run summary; per-tick detail
  lives in ``tick`` records.

Field names are stable camelCase wire keys. ``RECORD_SCHEMA_VERSION`` is the
single version source: writers always stamp it and readers fail loudly on
unknown versions instead of guessing.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from arena_hero_agent.application import (
    DeadlineOutcome,
    StoppedReason,
    SubmitResult,
    TickLoopResult,
    TickResult,
)
from arena_hero_agent.domain import DecisionId, TenantId

from ._common import RecorderError

RECORD_SCHEMA_VERSION: Final = 1

RECORD_TYPE_TICK: Final = "tick"
RECORD_TYPE_LOOP: Final = "loop"


def _required_int(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecorderError(f"{key} must be an integer; actual={value!r}")
    return value


def _require_enum(data: Mapping[str, object], key: str, enum_type: type[StrEnum]) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise RecorderError(f"{key} must be a string; actual={value!r}")
    allowed = {member.value for member in enum_type.__members__.values()}
    if value not in allowed:
        raise RecorderError(f"invalid {key} value {value!r}; expected one of {sorted(allowed)}")
    return value


def _check_common(data: Mapping[str, object], expected_tenant: TenantId, record_type: str) -> None:
    schema_version = data.get("schemaVersion")
    if schema_version != RECORD_SCHEMA_VERSION:
        raise RecorderError(
            "unsupported recorder schemaVersion "
            f"{schema_version!r}; expected {RECORD_SCHEMA_VERSION}"
        )
    actual_type = data.get("recordType")
    if actual_type != record_type:
        raise RecorderError(f"expected recordType {record_type!r}; actual={actual_type!r}")
    tenant = data.get("tenantId")
    if tenant != expected_tenant.value:
        raise RecorderError(
            f"record tenantId {tenant!r} does not match recorder tenant {expected_tenant.value!r}"
        )


def serialize_tick(
    result: TickResult, *, tenant_id: TenantId, recorded_at_ns: int
) -> dict[str, object]:
    """Serialize one tick outcome into a stable JSON object."""
    return {
        "schemaVersion": RECORD_SCHEMA_VERSION,
        "recordType": RECORD_TYPE_TICK,
        "tenantId": tenant_id.value,
        "recordedAtNs": recorded_at_ns,
        "tick": result.tick,
        "decisionId": result.decision_id.value,
        "deadlineOutcome": result.deadline_outcome.value,
        "submitResult": result.submit_result.value,
        "submitError": result.submit_error,
    }


def parse_tick(data: Mapping[str, object], *, expected_tenant: TenantId) -> TickResult:
    """Parse and validate one tick record, failing loudly on corruption."""
    _check_common(data, expected_tenant, RECORD_TYPE_TICK)
    tick = _required_int(data, "tick")
    decision_id = data.get("decisionId")
    if not isinstance(decision_id, str):
        raise RecorderError(f"decisionId must be a string; actual={decision_id!r}")
    deadline_outcome = DeadlineOutcome(_require_enum(data, "deadlineOutcome", DeadlineOutcome))
    submit_result = SubmitResult(_require_enum(data, "submitResult", SubmitResult))
    submit_error = data.get("submitError")
    if submit_error is not None and not isinstance(submit_error, str):
        raise RecorderError(f"submitError must be a string or null; actual={submit_error!r}")
    try:
        return TickResult(
            tick=tick,
            decision_id=DecisionId(decision_id),
            deadline_outcome=deadline_outcome,
            submit_result=submit_result,
            submit_error=submit_error,
        )
    except (TypeError, ValueError) as exc:
        raise RecorderError(f"invalid tick record: {exc}") from exc


def serialize_loop(result: TickLoopResult, *, recorded_at_ns: int) -> dict[str, object]:
    """Serialize one tick-loop run summary into a stable JSON object."""
    return {
        "schemaVersion": RECORD_SCHEMA_VERSION,
        "recordType": RECORD_TYPE_LOOP,
        "tenantId": result.tenant_id.value,
        "recordedAtNs": recorded_at_ns,
        "lastTick": result.last_tick,
        "ticksProcessed": result.ticks_processed,
        "duplicateTicks": result.duplicate_ticks,
        "outOfOrderTicks": result.out_of_order_ticks,
        "gapTicks": result.gap_ticks,
        "reconnectCount": result.reconnect_count,
        "stoppedReason": result.stopped_reason.value,
        "outcomeCount": len(result.outcomes),
    }


def parse_loop(data: Mapping[str, object], *, expected_tenant: TenantId) -> TickLoopResult:
    """Parse and validate one loop record, failing loudly on corruption."""
    _check_common(data, expected_tenant, RECORD_TYPE_LOOP)
    last_tick = _required_int(data, "lastTick")
    ticks_processed = _required_int(data, "ticksProcessed")
    duplicate_ticks = _required_int(data, "duplicateTicks")
    out_of_order_ticks = _required_int(data, "outOfOrderTicks")
    gap_ticks = _required_int(data, "gapTicks")
    reconnect_count = _required_int(data, "reconnectCount")
    stopped_reason = StoppedReason(_require_enum(data, "stoppedReason", StoppedReason))
    _required_int(data, "outcomeCount")  # informational; per-tick detail lives in tick records
    try:
        return TickLoopResult(
            tenant_id=expected_tenant,
            last_tick=last_tick,
            ticks_processed=ticks_processed,
            duplicate_ticks=duplicate_ticks,
            out_of_order_ticks=out_of_order_ticks,
            gap_ticks=gap_ticks,
            reconnect_count=reconnect_count,
            stopped_reason=stopped_reason,
            outcomes=(),
        )
    except (TypeError, ValueError) as exc:
        raise RecorderError(f"invalid loop record: {exc}") from exc
