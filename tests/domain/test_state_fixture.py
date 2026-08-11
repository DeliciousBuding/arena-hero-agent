"""Versioned offline known-answer fixture for the P4-8 tenant state reducer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arena_hero_agent.domain import (
    BeaconObservation,
    BeaconStatus,
    Coordinate,
    CoreObservation,
    CoreState,
    DecisionId,
    EntityId,
    EntityKind,
    EntityObservation,
    ResourceObservation,
    RulesVersion,
    StateDigest,
    StateOwnershipError,
    TenantId,
    TenantState,
    TerrainObservation,
    TerrainState,
    TurnInput,
    UnitObservation,
    UnitRole,
    WorldProjection,
    canonical_sha256,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tenant_state_reducer_known_answers_v1.json"


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _coordinate(payload: list[int]) -> Coordinate:
    return Coordinate(payload[0], payload[1])


def _entity_id(payload: str) -> EntityId:
    return EntityId(payload)


def _unit_observation(payload: dict[str, Any]) -> UnitObservation:
    return UnitObservation(
        id=_entity_id(payload["id"]),
        position=_coordinate(payload["position"]),
        role=UnitRole(payload["role"]),
        health=payload["health"],
        cargo=payload["cargo"],
    )


def _entity_observation(payload: dict[str, Any]) -> EntityObservation:
    return EntityObservation(
        id=_entity_id(payload["id"]),
        kind=EntityKind(payload["kind"]),
        position=_coordinate(payload["position"]),
        health=payload["health"],
        owner=payload["owner"],
        unit_role=None if payload["unit_role"] is None else UnitRole(payload["unit_role"]),
    )


def _resource_observation(payload: dict[str, Any]) -> ResourceObservation:
    return ResourceObservation(
        position=_coordinate(payload["position"]),
        remaining=payload["remaining"],
    )


def _terrain_observation(payload: dict[str, Any]) -> TerrainObservation:
    return TerrainObservation(
        position=_coordinate(payload["position"]),
        state=TerrainState(payload["state"]),
    )


def _core_observation(payload: dict[str, Any]) -> CoreObservation:
    return CoreObservation(
        id=_entity_id(payload["id"]),
        position=_coordinate(payload["position"]),
        health=payload["health"],
        shield=payload["shield"],
        state=CoreState(payload["state"]),
        owner=payload["owner"],
        destination=None if payload["destination"] is None else _coordinate(payload["destination"]),
    )


def _beacon_observation(payload: dict[str, Any]) -> BeaconObservation:
    return BeaconObservation(
        position=_coordinate(payload["position"]),
        status=BeaconStatus(payload["status"]),
        carrier_id=None if payload["carrier_id"] is None else _entity_id(payload["carrier_id"]),
    )


def _world_projection(payload: dict[str, Any]) -> WorldProjection:
    return WorldProjection(
        tick=payload["tick"],
        rules_version=RulesVersion(payload["rules_version"]),
        core=None if payload["core"] is None else _core_observation(payload["core"]),
        units=tuple(_unit_observation(item) for item in payload["units"]),
        entities=tuple(_entity_observation(item) for item in payload["entities"]),
        resources=tuple(_resource_observation(item) for item in payload["resources"]),
        terrain=tuple(_terrain_observation(item) for item in payload["terrain"]),
        beacon=None if payload["beacon"] is None else _beacon_observation(payload["beacon"]),
    )


def _turn_input(payload: dict[str, Any]) -> TurnInput:
    return TurnInput(tick=payload["tick"], projection=_world_projection(payload["projection"]))


def _tenant_state(payload: dict[str, Any]) -> TenantState:
    return TenantState(
        tenant_id=TenantId(payload["tenant_id"]["value"]),
        world=_world_projection(payload["world"]),
        decision_count=payload["decision_count"],
        last_decision_id=(
            None
            if payload["last_decision_id"] is None
            else DecisionId(payload["last_decision_id"]["value"])
        ),
    )


def test_fixture_round_trip_pins_reducer_chain() -> None:
    fixture = _load_fixture()
    assert fixture["metadata"]["version"] == 1
    assert fixture["tenant"] == "sample"

    initial = _tenant_state(fixture["initial"])
    turn = _turn_input(fixture["turn"])
    decision = DecisionId(fixture["decision"])
    tenant = TenantId(fixture["tenant"])

    assert initial.state_digest.value == fixture["digests"]["initial"]

    observed = initial.observe(turn.projection)
    assert observed.state_digest.value == fixture["digests"]["after_observe"]

    reduced = initial.reduce_turn(turn, decision, actor=tenant)
    assert reduced == _tenant_state(fixture["expected"])
    assert reduced.state_digest.value == fixture["digests"]["after_decision"]
    assert canonical_sha256((initial, observed, reduced)) == fixture["digests"]["sequence"]


def test_fixture_digests_are_reproducible_without_reloading() -> None:
    fixture = _load_fixture()
    initial = _tenant_state(fixture["initial"])
    turn = _turn_input(fixture["turn"])
    decision = DecisionId(fixture["decision"])
    tenant = TenantId(fixture["tenant"])

    first = initial.reduce_turn(turn, decision, actor=tenant)
    second = initial.reduce_turn(turn, decision, actor=tenant)
    assert first == second
    assert first.state_digest == second.state_digest
    assert first.state_digest == StateDigest(fixture["digests"]["after_decision"])


def test_fixture_ownership_fails_closed_for_cross_tenant_actor() -> None:
    fixture = _load_fixture()
    initial = _tenant_state(fixture["initial"])
    turn = _turn_input(fixture["turn"])
    decision = DecisionId(fixture["decision"])

    with pytest.raises(StateOwnershipError, match="does not own state"):
        initial.reduce_turn(turn, decision, actor=TenantId("other"))


def test_fixture_is_environment_neutral() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    for forbidden in ("D:\\", "C:\\", "/mnt/", "/home/", "Users\\Ding", "Users/Ding"):
        assert forbidden not in text
