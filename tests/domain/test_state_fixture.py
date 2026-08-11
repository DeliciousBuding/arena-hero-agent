"""Versioned offline known-answer fixture for the P4-10 tenant/economy reducer."""

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
    EconomyState,
    EconomyTurnInput,
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
    UnitPrices,
    UnitRole,
    WorldProjection,
    canonical_sha256,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tenant_state_reducer_known_answers_v2.json"


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


def _unit_prices(payload: dict[str, Any]) -> UnitPrices:
    return UnitPrices(
        worker=payload["worker"],
        vanguard=payload["vanguard"],
        ranger=payload["ranger"],
    )


def _economy_input(payload: dict[str, Any]) -> EconomyTurnInput:
    return EconomyTurnInput(
        seed=payload["seed"],
        tick=payload["tick"],
        rules_version=RulesVersion(payload["rules_version"]),
        resources=payload["resources"],
        population=payload["population"],
        unit_prices=_unit_prices(payload["unit_prices"]),
    )


def _economy_state(payload: dict[str, Any]) -> EconomyState:
    return EconomyState(
        seed=payload["seed"],
        tick=payload["tick"],
        rules_version=RulesVersion(payload["rules_version"]),
        resources=payload["resources"],
        population=payload["population"],
        resource_capacity=payload["resource_capacity"],
        resource_space=payload["resource_space"],
        base_costs=_unit_prices(payload["base_costs"]),
        unit_prices=_unit_prices(payload["unit_prices"]),
        resource_delta=payload["resource_delta"],
        population_delta=payload["population_delta"],
        input_digest=StateDigest(payload["input_digest"]),
    )


def _turn_input(payload: dict[str, Any]) -> TurnInput:
    return TurnInput(
        tick=payload["tick"],
        projection=_world_projection(payload["projection"]),
        economy=_economy_input(payload["economy"]),
    )


def _tenant_state(payload: dict[str, Any]) -> TenantState:
    return TenantState(
        tenant_id=TenantId(payload["tenant_id"]["value"]),
        world=_world_projection(payload["world"]),
        economy=_economy_state(payload["economy"]),
        decision_count=payload["decision_count"],
        last_decision_id=(
            None
            if payload["last_decision_id"] is None
            else DecisionId(payload["last_decision_id"]["value"])
        ),
    )


def test_fixture_round_trip_pins_reducer_chain() -> None:
    fixture = _load_fixture()
    assert fixture["metadata"]["version"] == 2
    assert fixture["tenant"] == "sample"

    initial = _tenant_state(fixture["initial"])
    turn = _turn_input(fixture["turn"])
    decision = DecisionId(fixture["decision"])
    tenant = TenantId(fixture["tenant"])

    assert initial.state_digest.value == fixture["digests"]["initial"]
    assert initial.economy.economy_digest.value == fixture["digests"]["economy_initial"]

    observed = initial.observe(turn.projection, turn.economy)
    assert observed.state_digest.value == fixture["digests"]["after_observe"]
    assert observed.economy.economy_digest.value == fixture["digests"]["economy_after"]
    assert observed.economy_input.selection_seed == fixture["decision_input"]["selection_seed"]

    reduced = initial.reduce_turn(turn, decision, actor=tenant)
    assert reduced == _tenant_state(fixture["expected"])
    assert reduced.state_digest.value == fixture["digests"]["after_decision"]
    assert reduced.economy_input.economy_digest.value == fixture["decision_input"]["economy_digest"]
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
