"""Decision trace factories, outcome counters, and deterministic plan hashing.

Port of the TypeScript oracle ``packages/arena-agent/src/telemetry/decision-trace.ts``.
Field names stay camelCase because they are wire-level JSON keys.

Deterministic hashing notes:

- ``plan_hash_of`` replicates the TypeScript FNV-1a 32-bit hash over a stable,
  key-sorted JSON string. The TypeScript implementation iterates UTF-16 code
  units (``charCodeAt``), so this port iterates the same code units, including
  surrogate pairs for non-BMP characters.
- Wall-clock timestamps are never part of deterministic identifiers or hashes.
  A timestamp may be recorded as evidence elsewhere, but it must not influence
  ``plan_hash_of`` output.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

from arena_hero_agent.telemetry.schema import (
    DecisionTraceRecord,
    OutcomeTraceRecord,
    RuntimeTraceRecord,
    validate_trace_record,
)

DEFAULT_PROCESS_RUN_ID: Final = "unknown"
DEFAULT_TENANT_ID: Final = "unknown"

# ---------------------------------------------------------------------------
# Outcome event counter constants (single source, mirrors TS)
# ---------------------------------------------------------------------------


class OutcomeCountEventTypes:
    DEPOSIT_SUCCEEDED = "DEPOSIT_SUCCEEDED"
    CORE_SPAWN_SUCCEEDED = "CORE_SPAWN_SUCCEEDED"
    UNIT_HEAL_SUCCEEDED = "UNIT_HEAL_SUCCEEDED"
    CORE_HEAL_SUCCEEDED = "CORE_HEAL_SUCCEEDED"
    UNIT_DESTROYED = "UNIT_DESTROYED"
    UNIT_SELF_DESTRUCTED = "UNIT_SELF_DESTRUCTED"


OUTCOME_COUNT_EVENT_TYPES = OutcomeCountEventTypes


@dataclasses.dataclass(frozen=True, slots=True)
class OutcomeCountEvent:
    """Minimal structural contract for outcome counting.

    Deliberately reads only ``eventType`` / ``actorId`` / ``targetId`` /
    ``values`` so simulator-internal types never leak into the telemetry layer.
    """

    eventType: str
    actorId: str | None = None
    targetId: str | None = None
    values: Mapping[str, object] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class OutcomeCountOwnershipContext:
    """Unit/core identity context for ownership-filtered outcome counts."""

    priorUnitIds: frozenset[str]
    currentUnitIds: frozenset[str] | None = None
    priorCoreId: str | None = None
    currentCoreId: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class OutcomeEventCounts:
    grossDeposit: int | float
    spawnCount: int
    healCount: int
    unitLossCount: int


def _normalize_event(
    event: OutcomeCountEvent | Mapping[str, object],
) -> tuple[object, object | None, object | None, Mapping[str, object] | None]:
    if isinstance(event, OutcomeCountEvent):
        return event.eventType, event.actorId, event.targetId, event.values
    if isinstance(event, Mapping):
        values = event.get("values")
        return (
            event.get("eventType"),
            event.get("actorId"),
            event.get("targetId"),
            cast(Mapping[str, object] | None, values if isinstance(values, Mapping) else None),
        )
    return event.eventType, event.actorId, event.targetId, event.values  # type: ignore[attr-defined]


def count_outcome_events(
    events: Sequence[OutcomeCountEvent | Mapping[str, object]],
    ownership: OutcomeCountOwnershipContext | None = None,
) -> OutcomeEventCounts:
    """Aggregate the four outcome.jsonl counters from a settlement event stream.

    - ``grossDeposit`` = sum of ``DEPOSIT_SUCCEEDED.values.amount``
      (missing / non-finite amount contributes 0; negative amounts are only
      counted when no ownership context is supplied, mirroring TypeScript).
    - ``spawnCount`` = ``CORE_SPAWN_SUCCEEDED`` count.
    - ``healCount`` = ``UNIT_HEAL_SUCCEEDED`` + ``CORE_HEAL_SUCCEEDED`` count
      (``REPAIR`` is not a heal).
    - ``unitLossCount`` = ``UNIT_DESTROYED`` + ``UNIT_SELF_DESTRUCTED`` count.
      ``CORE_RESOURCE_OVERFLOW_DESTROYED`` and ``CORE_DESTROYED`` are excluded
      by design (resource overflow and core destruction are not unit losses).
    """
    gross_deposit: int | float = 0
    spawn_count = 0
    heal_count = 0
    unit_loss_count = 0

    def owned_actor(actor_id: object | None) -> bool:
        if ownership is None:
            return True
        if actor_id is None:
            return False
        return (
            actor_id == ownership.priorCoreId
            or actor_id == ownership.currentCoreId
            or actor_id in ownership.priorUnitIds
            or (ownership.currentUnitIds is not None and actor_id in ownership.currentUnitIds)
        )

    for raw in events:
        event_type, actor_id, _, values = _normalize_event(raw)
        if event_type == OUTCOME_COUNT_EVENT_TYPES.DEPOSIT_SUCCEEDED:
            if not owned_actor(actor_id):
                continue
            if values is None:
                continue
            amount = values.get("amount")
            if isinstance(amount, bool):
                continue
            if not isinstance(amount, (int, float)):
                continue
            if isinstance(amount, float) and not math.isfinite(amount):
                continue
            if ownership is None or amount >= 0:
                gross_deposit += amount
        elif event_type == OUTCOME_COUNT_EVENT_TYPES.CORE_SPAWN_SUCCEEDED:
            if owned_actor(actor_id):
                spawn_count += 1
        elif event_type in (
            OUTCOME_COUNT_EVENT_TYPES.UNIT_HEAL_SUCCEEDED,
            OUTCOME_COUNT_EVENT_TYPES.CORE_HEAL_SUCCEEDED,
        ):
            if owned_actor(actor_id):
                heal_count += 1
        elif event_type in (
            OUTCOME_COUNT_EVENT_TYPES.UNIT_DESTROYED,
            OUTCOME_COUNT_EVENT_TYPES.UNIT_SELF_DESTRUCTED,
        ):
            if ownership is None or (actor_id is not None and actor_id in ownership.priorUnitIds):
                unit_loss_count += 1

    return OutcomeEventCounts(
        grossDeposit=gross_deposit,
        spawnCount=spawn_count,
        healCount=heal_count,
        unitLossCount=unit_loss_count,
    )


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _merged(defaults: Mapping[str, object], partial: Mapping[str, object]) -> dict[str, object]:
    fields = dict(defaults)
    fields.update(partial)
    return fields


def runtime_trace(partial: Mapping[str, object]) -> RuntimeTraceRecord:
    """Build a runtime trace record with process/tenant defaults.

    ``tick`` and the other required fields must be supplied by the caller; the
    record is validated immediately (fail fast).
    """
    fields = _merged(
        {"processRunId": DEFAULT_PROCESS_RUN_ID, "tenantId": DEFAULT_TENANT_ID}, partial
    )
    record = RuntimeTraceRecord(**cast(dict[str, Any], fields))
    validate_trace_record(record)
    return record


def decision_trace(partial: Mapping[str, object]) -> DecisionTraceRecord:
    fields = _merged(
        {"processRunId": DEFAULT_PROCESS_RUN_ID, "tenantId": DEFAULT_TENANT_ID}, partial
    )
    record = DecisionTraceRecord(**cast(dict[str, Any], fields))
    validate_trace_record(record)
    return record


def outcome_trace(partial: Mapping[str, object]) -> OutcomeTraceRecord:
    fields = _merged(
        {"processRunId": DEFAULT_PROCESS_RUN_ID, "tenantId": DEFAULT_TENANT_ID}, partial
    )
    record = OutcomeTraceRecord(**cast(dict[str, Any], fields))
    validate_trace_record(record)
    return record


# ---------------------------------------------------------------------------
# Deterministic plan hash (FNV-1a 32-bit, UTF-16 code units)
# ---------------------------------------------------------------------------


def _utf16_code_units(text: str):
    for char in text:
        codepoint = ord(char)
        if codepoint < 0x10000:
            yield codepoint
        else:
            codepoint -= 0x10000
            yield 0xD800 + (codepoint >> 10)
            yield 0xDC00 + (codepoint & 0x3FF)


def _js_json_string(value: str) -> str:
    """Replicate JavaScript ``JSON.stringify`` for a string value."""
    out: list[str] = ['"']
    for char in value:
        codepoint = ord(char)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\b":
            out.append("\\b")
        elif char == "\t":
            out.append("\\t")
        elif char == "\n":
            out.append("\\n")
        elif char == "\f":
            out.append("\\f")
        elif char == "\r":
            out.append("\\r")
        elif codepoint < 0x20 or 0xD800 <= codepoint <= 0xDFFF:
            out.append(f"\\u{codepoint:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _js_number_string(value: int | float) -> str:
    """Serialize a number like JavaScript ``JSON.stringify`` (shortest repr)."""
    if isinstance(value, int):
        return str(value)
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return "null"
    if number == 0:
        return "0"
    rendered = repr(number)
    if "e" not in rendered and "E" not in rendered:
        if rendered.endswith(".0"):
            rendered = rendered[:-2]
        return rendered
    mantissa, _, exponent_text = rendered.partition("e")
    exponent = int(exponent_text)
    negative = mantissa.startswith("-")
    if negative:
        mantissa = mantissa[1:]
    digits = mantissa.replace(".", "")
    if -7 < exponent < 21:
        if exponent >= 0:
            if exponent + 1 >= len(digits):
                fixed = digits + "0" * (exponent + 1 - len(digits))
            else:
                fixed = digits[: exponent + 1] + "." + digits[exponent + 1 :]
        else:
            fixed = "0." + "0" * (-exponent - 1) + digits
        return ("-" if negative else "") + fixed
    return (
        ("-" if negative else "")
        + mantissa
        + ("e+" if exponent >= 0 else "e-")
        + str(abs(exponent))
    )


def _stable_stringify(value: object) -> str:
    """Key-sorted deterministic JSON (arrays keep order; keys sorted)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _js_number_string(value)
    if isinstance(value, str):
        return _js_json_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_stable_stringify(item) for item in value) + "]"
    if isinstance(value, Mapping):
        entries = sorted(value.items(), key=lambda item: item[0])
        return (
            "{"
            + ",".join(
                _js_json_string(key) + ":" + _stable_stringify(item) for key, item in entries
            )
            + "}"
        )
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        entries = sorted(
            ((field.name, getattr(value, field.name)) for field in dataclasses.fields(value)),
            key=lambda item: item[0],
        )
        return (
            "{"
            + ",".join(
                _js_json_string(key) + ":" + _stable_stringify(item) for key, item in entries
            )
            + "}"
        )
    raise TypeError(f"cannot stably stringify {type(value).__name__}")


def plan_hash_of(value: object) -> str:
    """Stable FNV-1a 32-bit plan hash (8 lowercase hex digits).

    Matches the TypeScript ``planHashOf`` for identical inputs. Never depends on
    wall-clock time or insertion order.
    """
    text = _stable_stringify(value)
    hash_value = 0x811C9DC5
    for code_unit in _utf16_code_units(text):
        hash_value ^= code_unit
        hash_value = (hash_value * 0x01000193) & 0xFFFFFFFF
    return f"{hash_value:08x}"
