"""Schema and value-object tests for the telemetry package."""

from __future__ import annotations

import json
from typing import cast

import pytest

from arena_hero_agent.telemetry import (
    SCHEMA_VERSION,
    UNSET,
    FailedEventTrace,
    RuntimeTraceRecord,
    decision_trace,
    outcome_trace,
    runtime_trace,
    to_json,
    to_json_object,
    validate_trace_record,
)

RT = {
    "tick": 1000,
    "runId": "run-1",
    "deadlineOutcome": "candidate",
    "agentLatencyMs": 100,
    "selectionLatencyMs": 150,
    "abortRequested": False,
    "rotationGeneration": 0,
    "submitResult": "accepted",
}

DT = {
    "tick": 1000,
    "runId": "run-1",
    "decisionSource": "hybrid",
    "agentActionCount": 2,
    "safetyReplacementCount": 1,
    "invalidAgentActionCount": 0,
    "repairCount": 0,
    "intentCounts": {"patrol": 2, "return_home": 1},
    "planHash": "sha256:abc",
}

OT = {
    "tick": 1000,
    "coreResourcesBefore": 5,
    "coreResourcesAfter": 7,
    "coreResourceDelta": 2,
    "uniqueWorkerCellCount": 3,
    "workerMaxDistanceFromCore": 8,
    "workerMeanDistanceFromCore": 4.5,
    "failedEvents": [
        {
            "eventType": "UNIT_MOVE_FAILED",
            "reasonCode": "blocked",
            "actorId": "w1",
            "targetId": None,
            "position": [2, 3],
            "priorAction": '{"type":"MOVE","direction":"RIGHT"}',
            "priorIntent": "return_home",
        }
    ],
    "events": ["DEPOSIT 2"],
}


def test_schema_version_is_stable() -> None:
    assert SCHEMA_VERSION == 1


def test_runtime_trace_factory_defaults() -> None:
    record = runtime_trace(RT)
    assert isinstance(record, RuntimeTraceRecord)
    assert record.processRunId == "unknown"
    assert record.tenantId == "unknown"
    assert record.tick == 1000
    assert record.deadlineOutcome == "candidate"
    assert record.submitResult == "accepted"


def test_runtime_trace_keeps_submit_error_field() -> None:
    record = runtime_trace(
        {**RT, "submitResult": "rejected", "submitError": "HTTP 409 tick already closed"}
    )
    assert record.submitError == "HTTP 409 tick already closed"


def test_decision_trace_fields() -> None:
    record = decision_trace(DT)
    assert record.decisionSource == "hybrid"
    assert record.planHash == "sha256:abc"
    assert record.intentCounts == {"patrol": 2, "return_home": 1}


def test_decision_trace_human_source_passes() -> None:
    record = decision_trace({**DT, "decisionSource": "human"})
    assert record.decisionSource == "human"
    validate_trace_record(record)


def test_outcome_trace_fields() -> None:
    record = outcome_trace(OT)
    assert record.coreResourceDelta == 2
    assert record.events == ["DEPOSIT 2"]
    failed_events = cast(list[FailedEventTrace] | tuple[FailedEventTrace, ...], record.failedEvents)
    assert failed_events[0].reasonCode == "blocked"
    assert failed_events[0].priorIntent == "return_home"
    assert record.uniqueWorkerCellCount == 3
    assert record.workerMeanDistanceFromCore == 4.5


def test_factory_missing_required_field_raises() -> None:
    with pytest.raises(TypeError):
        runtime_trace({k: v for k, v in RT.items() if k != "runId"})
    with pytest.raises(TypeError):
        decision_trace({k: v for k, v in DT.items() if k != "planHash"})


def test_factory_rejects_unknown_fields() -> None:
    with pytest.raises(TypeError):
        runtime_trace({**RT, "bogusField": 1})


def test_unknown_enum_rejected_with_field_path() -> None:
    with pytest.raises(ValueError) as exc:
        decision_trace({**DT, "decisionSource": "bogus"})
    message = str(exc.value)
    assert "invalid trace record" in message
    assert "decision(/decisionSource" in message


def test_wrong_type_rejected() -> None:
    with pytest.raises(ValueError) as exc:
        runtime_trace({**RT, "tick": "1000"})
    assert "/tick Expected integer" in str(exc.value)


def test_agent_latency_null_allowed() -> None:
    record = runtime_trace({**RT, "agentLatencyMs": None})
    validate_trace_record(record)
    assert "agentLatencyMs" in to_json_object(record)
    assert to_json_object(record)["agentLatencyMs"] is None


def test_optional_absent_is_omitted_explicit_null_is_kept() -> None:
    absent = decision_trace(DT)
    assert "threatReason" not in to_json_object(absent)
    explicit = decision_trace({**DT, "threatReason": None})
    assert to_json_object(explicit)["threatReason"] is None
    assert "threatReason" in to_json_object(explicit)


def test_extra_fields_tolerated_by_validator() -> None:
    # TypeBox Type.Object allows additional properties by default; the Python
    # validator keeps that behavior so extended records (stall telemetry, ...)
    # pass validation exactly like the TypeScript oracle. Records validated
    # directly must carry their process/tenant identity (as factories do).
    extended = {
        "processRunId": "unknown",
        "tenantId": "unknown",
        **RT,
        "telemetryType": "stall_warning",
        "stallKind": "decision",
        "stallStreak": 3,
    }
    assert validate_trace_record(extended) is extended
    random_extra = {
        "processRunId": "unknown",
        "tenantId": "unknown",
        **DT,
        "someFutureField": {"nested": True},
    }
    assert validate_trace_record(random_extra) is random_extra


def test_validate_accepts_dict_and_dataclass() -> None:
    validate_trace_record({"processRunId": "unknown", "tenantId": "unknown", **RT})
    validate_trace_record(runtime_trace(RT))


def test_beacon_invalid_status_rejected() -> None:
    with pytest.raises(ValueError) as exc:
        decision_trace({**DT, "beacon": {"position": [2, 3], "status": "FLYING"}})
    assert "/beacon/status" in str(exc.value)


def test_beacon_null_status_and_carrier_allowed() -> None:
    record = decision_trace(
        {**DT, "beacon": {"position": [2, 3], "status": None, "carrierId": None}}
    )
    validate_trace_record(record)
    obj = to_json_object(record)
    beacon = cast(dict[str, object], obj["beacon"])
    assert beacon["status"] is None
    assert beacon["carrierId"] is None


def test_intent_counts_must_be_integer_values() -> None:
    with pytest.raises(ValueError) as exc:
        decision_trace({**DT, "intentCounts": {"patrol": 2.5}})
    assert "/intentCounts" in str(exc.value)


def test_known_answer_runtime_json_matches_typescript() -> None:
    # Byte-for-byte equal to JSON.stringify(runtimeTrace(RT)) from the TS oracle.
    expected = (
        '{"processRunId":"unknown","tenantId":"unknown","tick":1000,"runId":"run-1",'
        '"deadlineOutcome":"candidate","agentLatencyMs":100,"selectionLatencyMs":150,'
        '"abortRequested":false,"rotationGeneration":0,"submitResult":"accepted"}'
    )
    assert to_json(runtime_trace(RT)) == expected


def test_known_answer_round_trip() -> None:
    lines = [to_json(runtime_trace(RT)), to_json(decision_trace(DT)), to_json(outcome_trace(OT))]
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["runId"] == "run-1"
    assert parsed[1]["decisionSource"] == "hybrid"
    assert parsed[2]["coreResourceDelta"] == 2
    assert parsed[2]["failedEvents"][0]["position"] == [2, 3]


def test_serialization_is_order_stable() -> None:
    first = to_json(runtime_trace(RT))
    second = to_json(runtime_trace(dict(reversed(list(RT.items())))))
    assert first == second


def test_unset_is_not_serialized() -> None:
    obj = to_json_object(runtime_trace(RT))
    assert "configHash" not in obj
    assert "strategyHash" not in obj
    assert UNSET not in obj.values()


def test_factory_accepts_interface_only_stall_fields() -> None:
    record = runtime_trace(
        {
            **RT,
            "telemetryType": "stall_warning",
            "stallKind": "decision",
            "stallStreak": 2,
            "recoveryState": "recovering",
            "escalated": False,
            "outcome": "recovered",
        }
    )
    validate_trace_record(record)
    obj = to_json_object(record)
    assert obj["telemetryType"] == "stall_warning"
    assert obj["outcome"] == "recovered"
