"""Versioned telemetry record schemas and value objects.

This module mirrors the TypeScript oracle at
``packages/arena-agent/src/telemetry/schema.ts`` and
``packages/arena-agent/src/telemetry/decision-trace.ts``. It defines the
three local trace record families (runtime / decision / outcome) as immutable
value objects, plus a TypeBox-compatible validator.

Behavior notes (recorded in ``docs/telemetry.md``):

- Records are strict value objects: factories reject unknown fields at
  construction time.
- ``validate_trace_record`` accepts plain mappings as well as record objects.
  Unknown/extra fields are tolerated, matching TypeBox's default behavior
  (``Type.Object`` without ``additionalProperties: false``). This is what lets
  extended records such as stall telemetry pass validation.
- Optional fields that were not provided use the ``UNSET`` sentinel and are
  omitted from canonical JSON output; explicitly set ``None`` is emitted as
  ``null`` where the schema permits it.
- The schema is versioned through ``SCHEMA_VERSION``. Field names stay
  camelCase because they are wire-level JSON keys.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from typing import Any, Final, Literal, TypeAlias, cast

SCHEMA_VERSION: Final = 1


class _Unset:
    """Sentinel marking an optional field that was not provided."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"


UNSET: Final = _Unset()

# Optional telemetry fields are either absent (UNSET), an explicit value, or an
# explicit null where the schema allows it.
UnsetOr: TypeAlias = object


# ---------------------------------------------------------------------------
# Trace record value objects
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class BeaconTrace:
    """Per-tick beacon snapshot: position is always public; status and carrier
    id are only populated when the beacon cell is visible."""

    position: tuple[int, int]
    status: Literal["GROUND", "CARRIED"] | None | _Unset = UNSET
    carrierId: str | None | _Unset = UNSET


@dataclasses.dataclass(frozen=True, slots=True)
class RejectedOverrideTrace:
    unitId: str
    reason: str


@dataclasses.dataclass(frozen=True, slots=True)
class HumanOverrideTrace:
    """Human-command merge summary for the command plane echo."""

    active: bool
    applied: tuple[str, ...] | list[str]
    rejected: tuple[RejectedOverrideTrace, ...] | list[RejectedOverrideTrace]
    satisfied: tuple[str, ...] | list[str]
    updatedAt: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rejected", _coerce(self.rejected, RejectedOverrideTrace))


@dataclasses.dataclass(frozen=True, slots=True)
class FailedEventTrace:
    """Server-side failed event plus the action actually submitted."""

    eventType: str
    reasonCode: str | None
    actorId: str | None
    targetId: str | None
    position: tuple[int, int] | _Unset = UNSET
    priorAction: str | _Unset = UNSET
    priorIntent: str | _Unset = UNSET


@dataclasses.dataclass(frozen=True, slots=True)
class RuntimeTraceRecord:
    """Per-tick runtime correctness trace (deadline, latency, submit)."""

    processRunId: str
    tenantId: str
    tick: int
    runId: str
    deadlineOutcome: Literal[
        "candidate", "soft_deadline", "selection_timeout", "not_applicable", "error"
    ]
    agentLatencyMs: int | float | None
    selectionLatencyMs: int | float
    abortRequested: bool
    rotationGeneration: int
    submitResult: Literal["accepted", "rejected", "not_submitted"]
    configGeneration: int | _Unset = UNSET
    configHash: str | _Unset = UNSET
    strategyHash: str | _Unset = UNSET
    submitError: str | _Unset = UNSET
    notSubmittedReason: Literal["disabled", "startup_sync", "outcome_drain"] | _Unset = UNSET
    leaseRejectionCode: str | _Unset = UNSET
    telemetryType: Literal["stall_warning", "stall_recovery"] | _Unset = UNSET
    stallKind: str | _Unset = UNSET
    stallStreak: int | _Unset = UNSET
    recoveryState: str | _Unset = UNSET
    escalated: bool | _Unset = UNSET
    outcome: Literal["recovered", "failed", "expired"] | _Unset = UNSET


@dataclasses.dataclass(frozen=True, slots=True)
class DecisionTraceRecord:
    """Why the agent chose this plan (source, arbitration counts, plan hash)."""

    processRunId: str
    tenantId: str
    tick: int
    runId: str
    decisionSource: Literal[
        "agent", "hybrid", "deterministic", "safety", "emergency", "repaired-agent", "human"
    ]
    agentActionCount: int
    safetyReplacementCount: int
    invalidAgentActionCount: int
    repairCount: int
    planHash: str
    moveCount: int | _Unset = UNSET
    harvestCount: int | _Unset = UNSET
    depositCount: int | _Unset = UNSET
    waitCount: int | _Unset = UNSET
    intentCounts: Mapping[str, int] | _Unset = UNSET
    reason: str | _Unset = UNSET
    threatLevel: Literal["NORMAL", "ALERT", "ENGAGED", "BREAKOUT"] | _Unset = UNSET
    threatReason: str | None | _Unset = UNSET
    threatClosingEnemies: int | _Unset = UNSET
    threatMovingEnemies: int | _Unset = UNSET
    threatAxes: int | _Unset = UNSET
    beacon: BeaconTrace | _Unset = UNSET
    failedCooldownEscalationCount: int | _Unset = UNSET

    def __post_init__(self) -> None:
        object.__setattr__(self, "beacon", _coerce(self.beacon, BeaconTrace))


@dataclasses.dataclass(frozen=True, slots=True)
class OutcomeTraceRecord:
    """What happened after execution (resource deltas, production, losses)."""

    processRunId: str
    tenantId: str
    tick: int
    coreResourcesBefore: int | float
    coreResourcesAfter: int | float
    coreResourceDelta: int | float
    events: tuple[str, ...] | list[str]
    coreState: Literal["NORMAL", "MOVING"] | None | _Unset = UNSET
    visibleResourceCellCount: int | _Unset = UNSET
    workerCount: int | _Unset = UNSET
    workersWithCargo: int | _Unset = UNSET
    workerCargoTotal: int | _Unset = UNSET
    uniqueWorkerCellCount: int | _Unset = UNSET
    workerMaxDistanceFromCore: int | float | _Unset = UNSET
    workerMeanDistanceFromCore: int | float | _Unset = UNSET
    failedEvents: tuple[FailedEventTrace, ...] | list[FailedEventTrace] | _Unset = UNSET
    grossDeposit: int | float | _Unset = UNSET
    spawnCount: int | _Unset = UNSET
    healCount: int | _Unset = UNSET
    unitLossCount: int | _Unset = UNSET
    humanOverride: HumanOverrideTrace | _Unset = UNSET

    def __post_init__(self) -> None:
        object.__setattr__(self, "failedEvents", _coerce(self.failedEvents, FailedEventTrace))
        object.__setattr__(self, "humanOverride", _coerce(self.humanOverride, HumanOverrideTrace))


TraceRecord = RuntimeTraceRecord | DecisionTraceRecord | OutcomeTraceRecord


# ---------------------------------------------------------------------------


def _coerce(value: object, value_type: type) -> object:
    """Convert plain mappings/lists into nested frozen value objects."""
    if value is UNSET or value is None or isinstance(value, value_type):
        return value
    if isinstance(value, Mapping):
        return value_type(**value)
    if isinstance(value, (list, tuple)):
        return tuple(_coerce(item, value_type) for item in value)
    return value


# Canonical JSON serialization
# ---------------------------------------------------------------------------


def _json_value(value: object) -> object:
    if isinstance(value, _Unset):
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _record_items(value)
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _record_items(record: object) -> dict[str, object]:
    items: dict[str, object] = {}
    for field in dataclasses.fields(cast(Any, record)):
        value = getattr(record, field.name)
        if value is UNSET:
            continue
        items[field.name] = _json_value(value)
    return items


def to_json_object(record: TraceRecord | Mapping[str, object]) -> dict[str, object]:
    """Convert a record (or plain mapping) to a plain JSON-ready dict.

    Absent optional fields are omitted; explicit ``None`` values are kept so
    that ``null`` reaches the wire exactly where the caller set it.
    """
    if dataclasses.is_dataclass(record) and not isinstance(record, type):
        return _record_items(record)
    if isinstance(record, Mapping):
        return {key: _json_value(value) for key, value in record.items()}
    raise TypeError(f"expected a trace record or mapping, got {type(record).__name__}")


def to_json(record: TraceRecord | Mapping[str, object]) -> str:
    """Serialize a record to a single canonical JSON line (no trailing newline)."""
    return json.dumps(
        to_json_object(record), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


# ---------------------------------------------------------------------------
# TypeBox-compatible validation
# ---------------------------------------------------------------------------

_DEADLINE_OUTCOMES = frozenset(
    {"candidate", "soft_deadline", "selection_timeout", "not_applicable", "error"}
)
_DECISION_SOURCES = frozenset(
    {"agent", "hybrid", "deterministic", "safety", "emergency", "repaired-agent", "human"}
)
_SUBMIT_RESULTS = frozenset({"accepted", "rejected", "not_submitted"})
_NOT_SUBMITTED_REASONS = frozenset({"disabled", "startup_sync", "outcome_drain"})
_THREAT_LEVELS = frozenset({"NORMAL", "ALERT", "ENGAGED", "BREAKOUT"})
_BEACON_STATUSES = frozenset({"GROUND", "CARRIED"})
_CORE_STATES = frozenset({"NORMAL", "MOVING"})
_STALL_TYPES = frozenset({"stall_warning", "stall_recovery"})
_STALL_OUTCOMES = frozenset({"recovered", "failed", "expired"})


def _present(record: Mapping[str, object], key: str) -> bool:
    return key in record and record[key] is not UNSET


def _integer(record: Mapping[str, object], key: str, *, required: bool = True) -> str | None:
    if not _present(record, key):
        return None if not required else f"/{key} Required property"
    value = record[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return f"/{key} Expected integer"
    return None


def _number(record: Mapping[str, object], key: str, *, required: bool = True) -> str | None:
    if not _present(record, key):
        return None if not required else f"/{key} Required property"
    value = record[key]
    if isinstance(value, bool):
        return f"/{key} Expected number"
    if isinstance(value, int):
        return None
    if isinstance(value, float) and value == value and value not in (float("inf"), float("-inf")):
        return None
    return f"/{key} Expected number"


def _string(record: Mapping[str, object], key: str, *, required: bool = True) -> str | None:
    if not _present(record, key):
        return None if not required else f"/{key} Required property"
    if not isinstance(record[key], str):
        return f"/{key} Expected string"
    return None


def _boolean(record: Mapping[str, object], key: str, *, required: bool = True) -> str | None:
    if not _present(record, key):
        return None if not required else f"/{key} Required property"
    if not isinstance(record[key], bool):
        return f"/{key} Expected boolean"
    return None


def _nullable_number(
    record: Mapping[str, object], key: str, *, required: bool = True
) -> str | None:
    if not _present(record, key):
        return None if not required else f"/{key} Required property"
    value = record[key]
    if value is None:
        return None
    if isinstance(value, bool):
        return f"/{key} Expected number"
    if isinstance(value, int):
        return None
    if isinstance(value, float) and value == value and value not in (float("inf"), float("-inf")):
        return None
    return f"/{key} Expected number"


def _nullable_string(
    record: Mapping[str, object], key: str, *, required: bool = True
) -> str | None:
    if not _present(record, key):
        return None if not required else f"/{key} Required property"
    value = record[key]
    if value is None or isinstance(value, str):
        return None
    return f"/{key} Expected string"


def _literal(
    record: Mapping[str, object], key: str, allowed: frozenset[str], *, required: bool = True
) -> str | None:
    if not _present(record, key):
        return None if not required else f"/{key} Required property"
    if record[key] not in allowed:
        return f"/{key} must be equal to constant"
    return None


def _nullable_literal(
    record: Mapping[str, object], key: str, allowed: frozenset[str], *, required: bool = True
) -> str | None:
    if not _present(record, key):
        return None if not required else f"/{key} Required property"
    value = record[key]
    if value is None or value in allowed:
        return None
    return f"/{key} must be equal to constant"


def _int_tuple2(record: Mapping[str, object], key: str, *, required: bool = True) -> str | None:
    if not _present(record, key):
        return None if not required else f"/{key} Required property"
    value = record[key]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return f"/{key} Expected tuple of 2 integers"
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        return f"/{key} Expected tuple of 2 integers"
    return None


def _string_array(record: Mapping[str, object], key: str, *, required: bool = True) -> str | None:
    if not _present(record, key):
        return None if not required else f"/{key} Required property"
    value = record[key]
    if not isinstance(value, (list, tuple)):
        return f"/{key} Expected array"
    for item in value:
        if not isinstance(item, str):
            return f"/{key} Expected array of strings"
    return None


def _string_int_record(record: Mapping[str, object], key: str) -> str | None:
    if not _present(record, key):
        return None
    value = record[key]
    if not isinstance(value, Mapping):
        return f"/{key} Expected object"
    for item_key, item in value.items():
        if not isinstance(item_key, str):
            return f"/{key} Expected string keys"
        if isinstance(item, bool) or not isinstance(item, int):
            return f"/{key} Expected integer values"
    return None


def _check_beacon(record: Mapping[str, object]) -> str | None:
    if not _present(record, "beacon"):
        return None
    beacon = record["beacon"]
    if not isinstance(beacon, Mapping):
        return "/beacon Expected object"
    error = _int_tuple2(beacon, "position", required=True)
    if error is not None:
        return f"/beacon{error}"
    error = _nullable_literal(beacon, "status", _BEACON_STATUSES, required=False)
    if error is not None:
        return f"/beacon{error}"
    error = _nullable_string(beacon, "carrierId", required=False)
    if error is not None:
        return f"/beacon{error}"
    return None


def _check_failed_events(record: Mapping[str, object]) -> str | None:
    if not _present(record, "failedEvents"):
        return None
    events = record["failedEvents"]
    if not isinstance(events, (list, tuple)):
        return "/failedEvents Expected array"
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            return f"/failedEvents/{index} Expected object"
        for key, required in (
            ("eventType", True),
            ("reasonCode", True),
            ("actorId", True),
            ("targetId", True),
        ):
            error = _string(event, key, required=required)
            if error is not None:
                error = _nullable_string(event, key, required=required)
            if error is not None:
                return f"/failedEvents/{index}{error}"
        error = _int_tuple2(event, "position", required=False)
        if error is not None:
            return f"/failedEvents/{index}{error}"
        error = _string(event, "priorAction", required=False)
        if error is not None:
            return f"/failedEvents/{index}{error}"
        error = _string(event, "priorIntent", required=False)
        if error is not None:
            return f"/failedEvents/{index}{error}"
    return None


def _check_human_override(record: Mapping[str, object]) -> str | None:
    if not _present(record, "humanOverride"):
        return None
    override = record["humanOverride"]
    if not isinstance(override, Mapping):
        return "/humanOverride Expected object"
    error = _boolean(override, "active", required=True)
    if error is not None:
        return f"/humanOverride{error}"
    error = _string_array(override, "applied", required=True)
    if error is not None:
        return f"/humanOverride{error}"
    if not _present(override, "rejected"):
        return "/humanOverride/rejected Required property"
    rejected = override["rejected"]
    if not isinstance(rejected, (list, tuple)):
        return "/humanOverride/rejected Expected array"
    for index, item in enumerate(rejected):
        if not isinstance(item, Mapping):
            return f"/humanOverride/rejected/{index} Expected object"
        error = _string(item, "unitId", required=True)
        if error is not None:
            return f"/humanOverride/rejected/{index}{error}"
        error = _string(item, "reason", required=True)
        if error is not None:
            return f"/humanOverride/rejected/{index}{error}"
    error = _string_array(override, "satisfied", required=True)
    if error is not None:
        return f"/humanOverride{error}"
    error = _nullable_string(override, "updatedAt", required=True)
    if error is not None:
        return f"/humanOverride{error}"
    return None


def _check_runtime(record: Mapping[str, object]) -> str | None:
    for key in ("processRunId", "tenantId", "runId"):
        error = _string(record, key, required=True)
        if error is not None:
            return error
    error = _integer(record, "tick", required=True)
    if error is not None:
        return error
    error = _literal(record, "deadlineOutcome", _DEADLINE_OUTCOMES, required=True)
    if error is not None:
        return error
    error = _nullable_number(record, "agentLatencyMs", required=True)
    if error is not None:
        return error
    error = _number(record, "selectionLatencyMs", required=True)
    if error is not None:
        return error
    error = _boolean(record, "abortRequested", required=True)
    if error is not None:
        return error
    error = _integer(record, "rotationGeneration", required=True)
    if error is not None:
        return error
    error = _integer(record, "configGeneration", required=False)
    if error is not None:
        return error
    error = _string(record, "configHash", required=False)
    if error is not None:
        return error
    error = _string(record, "strategyHash", required=False)
    if error is not None:
        return error
    error = _literal(record, "submitResult", _SUBMIT_RESULTS, required=True)
    if error is not None:
        return error
    error = _string(record, "submitError", required=False)
    if error is not None:
        return error
    error = _literal(record, "notSubmittedReason", _NOT_SUBMITTED_REASONS, required=False)
    if error is not None:
        return error
    error = _string(record, "leaseRejectionCode", required=False)
    if error is not None:
        return error
    error = _literal(record, "telemetryType", _STALL_TYPES, required=False)
    if error is not None:
        return error
    error = _string(record, "stallKind", required=False)
    if error is not None:
        return error
    error = _integer(record, "stallStreak", required=False)
    if error is not None:
        return error
    error = _string(record, "recoveryState", required=False)
    if error is not None:
        return error
    error = _boolean(record, "escalated", required=False)
    if error is not None:
        return error
    error = _literal(record, "outcome", _STALL_OUTCOMES, required=False)
    if error is not None:
        return error
    return None


def _check_decision(record: Mapping[str, object]) -> str | None:
    for key in ("processRunId", "tenantId", "runId"):
        error = _string(record, key, required=True)
        if error is not None:
            return error
    error = _integer(record, "tick", required=True)
    if error is not None:
        return error
    error = _literal(record, "decisionSource", _DECISION_SOURCES, required=True)
    if error is not None:
        return error
    for key in (
        "agentActionCount",
        "safetyReplacementCount",
        "invalidAgentActionCount",
        "repairCount",
    ):
        error = _integer(record, key, required=True)
        if error is not None:
            return error
    for key in ("moveCount", "harvestCount", "depositCount", "waitCount"):
        error = _integer(record, key, required=False)
        if error is not None:
            return error
    error = _string_int_record(record, "intentCounts")
    if error is not None:
        return error
    error = _string(record, "planHash", required=True)
    if error is not None:
        return error
    error = _string(record, "reason", required=False)
    if error is not None:
        return error
    error = _literal(record, "threatLevel", _THREAT_LEVELS, required=False)
    if error is not None:
        return error
    error = _nullable_string(record, "threatReason", required=False)
    if error is not None:
        return error
    for key in ("threatClosingEnemies", "threatMovingEnemies", "threatAxes"):
        error = _integer(record, key, required=False)
        if error is not None:
            return error
    error = _check_beacon(record)
    if error is not None:
        return error
    error = _integer(record, "failedCooldownEscalationCount", required=False)
    if error is not None:
        return error
    return None


def _check_outcome(record: Mapping[str, object]) -> str | None:
    for key in ("processRunId", "tenantId"):
        error = _string(record, key, required=True)
        if error is not None:
            return error
    error = _integer(record, "tick", required=True)
    if error is not None:
        return error
    for key in ("coreResourcesBefore", "coreResourcesAfter", "coreResourceDelta"):
        error = _number(record, key, required=True)
        if error is not None:
            return error
    error = _nullable_literal(record, "coreState", _CORE_STATES, required=False)
    if error is not None:
        return error
    for key in (
        "visibleResourceCellCount",
        "workerCount",
        "workersWithCargo",
        "workerCargoTotal",
        "uniqueWorkerCellCount",
        "spawnCount",
        "healCount",
        "unitLossCount",
    ):
        error = _integer(record, key, required=False)
        if error is not None:
            return error
    for key in ("workerMaxDistanceFromCore", "workerMeanDistanceFromCore", "grossDeposit"):
        error = _number(record, key, required=False)
        if error is not None:
            return error
    error = _check_failed_events(record)
    if error is not None:
        return error
    error = _string_array(record, "events", required=True)
    if error is not None:
        return error
    error = _check_human_override(record)
    if error is not None:
        return error
    return None


_CHECKERS = (
    ("runtime", _check_runtime),
    ("decision", _check_decision),
    ("outcome", _check_outcome),
)


def validate_trace_record(
    record: TraceRecord | Mapping[str, object],
) -> TraceRecord | Mapping[str, object]:
    """Validate a record against the runtime/decision/outcome schemas.

    Returns the record unchanged when any schema passes. When all three fail,
    raises ``ValueError`` whose message includes the first field error of each
    schema, mirroring the TypeScript ``validateTraceRecord`` behavior.
    """
    if dataclasses.is_dataclass(record) and not isinstance(record, type):
        data = _record_items(record)
    elif isinstance(record, Mapping):
        data = dict(record)
    else:
        raise TypeError(f"expected a trace record or mapping, got {type(record).__name__}")

    failures: list[str] = []
    for name, checker in _CHECKERS:
        error = checker(data)
        if error is None:
            return record
        failures.append(f"{name}({error})")
    raise ValueError(f"invalid trace record: {'; '.join(failures)}")
