from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import cast

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
    Generation,
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
from arena_hero_agent.ports import TenantStateStore, WriterLeaseHandle

TENANT = TenantId("sample")
DECISION = DecisionId("decision:abc")


def _unit(identifier: str, x: int, *, health: int = 2) -> UnitObservation:
    return UnitObservation(
        id=EntityId(identifier),
        position=Coordinate(x, 0),
        role=UnitRole.WORKER,
        health=health,
        cargo=1,
    )


def _entity(identifier: str, x: int) -> EntityObservation:
    return EntityObservation(
        id=EntityId(identifier),
        kind=EntityKind.UNIT,
        position=Coordinate(x, 1),
        health=3,
        owner="opponent",
        unit_role=UnitRole.VANGUARD,
    )


def _core() -> CoreObservation:
    return CoreObservation(
        id=EntityId("core-a"),
        position=Coordinate(0, 0),
        health=5,
        shield=4,
        state=CoreState.NORMAL,
        owner="player",
    )


def _world(*, tick: int = 42, unit_health: int = 2) -> WorldProjection:
    return WorldProjection(
        tick=tick,
        rules_version=RulesVersion.V0_14,
        core=_core(),
        units=(_unit("unit-a", 1, health=unit_health), _unit("unit-b", 2)),
        entities=(_entity("enemy-a", 4), _entity("enemy-b", 5)),
        resources=(
            ResourceObservation(Coordinate(3, 0)),
            ResourceObservation(Coordinate(-1, 2), remaining=4),
        ),
        terrain=(
            TerrainObservation(Coordinate(0, 0), TerrainState.OPEN),
            TerrainObservation(Coordinate(1, 0), TerrainState.BLOCKED),
        ),
        beacon=BeaconObservation(Coordinate(8, 8), BeaconStatus.GROUND),
    )


def _reordered_world() -> WorldProjection:
    """Same semantic content as ``_world()`` with every collection reordered."""
    return WorldProjection(
        tick=42,
        rules_version=RulesVersion.V0_14,
        core=_core(),
        units=(_unit("unit-b", 2), _unit("unit-a", 1)),
        entities=(_entity("enemy-b", 5), _entity("enemy-a", 4)),
        resources=(
            ResourceObservation(Coordinate(-1, 2), remaining=4),
            ResourceObservation(Coordinate(3, 0)),
        ),
        terrain=(
            TerrainObservation(Coordinate(1, 0), TerrainState.BLOCKED),
            TerrainObservation(Coordinate(0, 0), TerrainState.OPEN),
        ),
        beacon=BeaconObservation(Coordinate(8, 8), BeaconStatus.GROUND),
    )


def _economy_input(
    *,
    tick: int = 42,
    resources: int = 8,
    population: int = 2,
    seed: int = 0xA11CE,
) -> EconomyTurnInput:
    return EconomyTurnInput.observed(
        seed=seed,
        tick=tick,
        rules_version=RulesVersion.V0_14,
        resources=resources,
        population=population,
    )


def _economy_state(*, tick: int = 42, resources: int = 8) -> EconomyState:
    return EconomyState.initial(_economy_input(tick=tick, resources=resources))


def _initial() -> TenantState:
    return TenantState(tenant_id=TENANT, world=_world(), economy=_economy_state())


def _set_attribute(target: object, name: str, value: object) -> None:
    setattr(target, name, value)


def test_tenant_state_is_immutable_and_validates() -> None:
    state = _initial()
    with pytest.raises(FrozenInstanceError):
        _set_attribute(state, "decision_count", 1)
    with pytest.raises(TypeError, match="tenant_id must be a TenantId"):
        TenantState(
            tenant_id=cast(TenantId, "sample"),
            world=_world(),
            economy=_economy_state(),
        )  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="world must be a WorldProjection"):
        TenantState(
            tenant_id=TENANT,
            world=cast(WorldProjection, {}),
            economy=_economy_state(),
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="decision_count"):
        TenantState(
            tenant_id=TENANT,
            world=_world(),
            economy=_economy_state(),
            decision_count=-1,
        )
    with pytest.raises(ValueError, match="safe-integer"):
        TenantState(
            tenant_id=TENANT,
            world=_world(),
            economy=_economy_state(),
            decision_count=2**53,
        )
    with pytest.raises(TypeError, match="last_decision_id"):
        TenantState(
            tenant_id=TENANT,
            world=_world(),
            economy=_economy_state(),
            last_decision_id=cast(DecisionId, "decision:other"),
        )  # type: ignore[arg-type]


def test_tenant_state_digest_known_answers_are_frozen() -> None:
    initial = _initial()
    advanced = initial.observe(_world(tick=43), _economy_input(tick=43, resources=9))
    committed = advanced.record_decision(DECISION)

    assert initial.state_digest.value == (
        "b397ad48e1b4d6535d1ca511e0aaa01140f17ec799453fddc34d006b123c4a2a"
    )
    assert advanced.state_digest.value == (
        "67f37d4e4d7fec96ed7283f0c3074bc3a45e8af5c4f97148e89345f53fa98adc"
    )
    assert committed.state_digest.value == (
        "4d7f4e40b7043a898efe4e771532f6867df5ef209b7db50b0bbbedac5e248cf0"
    )


def test_observe_reducer_advances_world_without_mutating() -> None:
    initial = _initial()
    advanced = initial.observe(_world(tick=43), _economy_input(tick=43, resources=9))

    assert advanced is not initial
    assert advanced.tenant_id is initial.tenant_id
    assert advanced.world.tick == 43
    assert advanced.decision_count == initial.decision_count == 0
    assert advanced.last_decision_id is initial.last_decision_id is None
    assert initial.world.tick == 42
    assert initial.state_digest.value == (
        "b397ad48e1b4d6535d1ca511e0aaa01140f17ec799453fddc34d006b123c4a2a"
    )


def test_observe_rejects_tick_regression_and_same_tick_conflict() -> None:
    initial = _initial()
    with pytest.raises(ValueError, match="regresses below"):
        initial.observe(_world(tick=41), _economy_input(tick=41))
    with pytest.raises(ValueError, match="conflicting world observation"):
        initial.observe(_world(tick=42, unit_health=9), _economy_input())
    assert initial.observe(_world(tick=42), _economy_input()) is initial


def test_observe_rejects_historical_rules_version() -> None:
    historical = WorldProjection(
        tick=42,
        rules_version=RulesVersion.V0_11,
        core=_core(),
        units=(),
        entities=(),
        resources=(),
        terrain=(),
    )
    with pytest.raises(ValueError, match="recognized but current"):
        _initial().observe(historical, _economy_input())


def test_record_decision_advances_journal_and_identity() -> None:
    initial = _initial()
    committed = initial.record_decision(DECISION)

    assert committed.decision_count == 1
    assert committed.last_decision_id == DECISION
    assert committed.world is initial.world
    assert committed.state_digest != initial.state_digest


def test_record_decision_rejects_duplicate_and_non_decision() -> None:
    committed = _initial().record_decision(DECISION)
    with pytest.raises(ValueError, match="duplicate decision commit"):
        committed.record_decision(DECISION)
    with pytest.raises(TypeError, match="decision_id must be a DecisionId"):
        committed.record_decision(cast(DecisionId, "decision:other"))  # type: ignore[arg-type]


def test_identity_is_independent_of_input_ordering() -> None:
    canonical = TenantState(tenant_id=TENANT, world=_world(), economy=_economy_state())
    reordered = TenantState(tenant_id=TENANT, world=_reordered_world(), economy=_economy_state())

    assert canonical.world == reordered.world
    assert canonical.state_digest == reordered.state_digest


def test_semantic_changes_alter_identity() -> None:
    state = _initial()
    changed_health = TenantState(
        tenant_id=TENANT, world=_world(unit_health=3), economy=_economy_state()
    )
    changed_economy = TenantState(
        tenant_id=TENANT,
        world=_world(),
        economy=_economy_state(resources=9),
    )
    changed_tick = _initial().observe(
        _world(tick=43),
        _economy_input(tick=43, resources=9),
    )
    changed_journal = _initial().record_decision(DECISION)

    assert changed_health.state_digest != state.state_digest
    assert changed_economy.state_digest != state.state_digest
    assert changed_tick.state_digest != state.state_digest
    assert changed_journal.state_digest != state.state_digest
    assert changed_health.state_digest != changed_tick.state_digest
    assert changed_health.state_digest != changed_journal.state_digest


class _FakeStateStore:
    async def load(self, tenant_id: TenantId) -> tuple[Generation, StateDigest, TenantState] | None:
        state = _initial()
        return (Generation(1), state.state_digest, state)

    async def compare_and_set(
        self,
        tenant_id: TenantId,
        *,
        expected_generation: Generation,
        next_generation: Generation,
        state_digest: StateDigest,
        state: TenantState,
        lease: WriterLeaseHandle,
    ) -> bool:
        return next_generation.supersedes(expected_generation)

    async def restore(
        self,
        tenant_id: TenantId,
        *,
        generation: Generation,
        state_digest: StateDigest,
        state: TenantState,
        lease: WriterLeaseHandle,
    ) -> bool:
        return lease.generation == generation


def test_tenant_state_satisfies_cas_store_state_type() -> None:
    """P4-9 seam: TenantState is a valid StateT for the CAS tenant store."""
    store: TenantStateStore[TenantState] = _FakeStateStore()
    assert isinstance(store, TenantStateStore)


def test_turn_input_validates_round_identity() -> None:
    turn = TurnInput(
        tick=43,
        projection=_world(tick=43),
        economy=_economy_input(tick=43, resources=9),
    )
    assert turn.tick == 43
    assert turn.projection == _world(tick=43)
    with pytest.raises(ValueError, match="does not match projection tick"):
        TurnInput(
            tick=44,
            projection=_world(tick=43),
            economy=_economy_input(tick=44, resources=9),
        )
    with pytest.raises(TypeError, match="turn tick must be an integer"):
        TurnInput(
            tick=cast(int, "43"),
            projection=_world(tick=43),
            economy=_economy_input(tick=43, resources=9),
        )  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="projection must be a WorldProjection"):
        TurnInput(
            tick=43,
            projection=cast(WorldProjection, {}),
            economy=_economy_input(tick=43, resources=9),
        )  # type: ignore[arg-type]


def test_state_owner_identity_and_require_owner_gate() -> None:
    state = _initial()
    assert state.owner is TENANT

    assert state.require_owner(TENANT) is state
    with pytest.raises(StateOwnershipError, match="does not own state"):
        state.require_owner(TenantId("other"))
    with pytest.raises(TypeError, match="actor must be a TenantId"):
        state.require_owner(cast(TenantId, "sample"))  # type: ignore[arg-type]


def test_advances_fail_closed_for_non_owner_actors() -> None:
    state = _initial()
    other = TenantId("other")
    turn = TurnInput(
        tick=43,
        projection=_world(tick=43),
        economy=_economy_input(tick=43, resources=9),
    )

    with pytest.raises(StateOwnershipError, match="does not own state"):
        state.observe(_world(tick=43), _economy_input(tick=43, resources=9), actor=other)
    with pytest.raises(StateOwnershipError, match="does not own state"):
        state.record_decision(DECISION, actor=other)
    with pytest.raises(StateOwnershipError, match="does not own state"):
        state.reduce_turn(turn, DECISION, actor=other)


def test_reduce_turn_applies_round_and_decision_in_one_step() -> None:
    initial = _initial()
    turn = TurnInput(
        tick=43,
        projection=_world(tick=43),
        economy=_economy_input(tick=43, resources=9),
    )
    reduced = initial.reduce_turn(turn, DECISION, actor=TENANT)

    composed = initial.observe(
        _world(tick=43), _economy_input(tick=43, resources=9)
    ).record_decision(DECISION)
    assert reduced == composed
    assert reduced.tenant_id is initial.tenant_id
    assert reduced.world.tick == 43
    assert reduced.decision_count == 1
    assert reduced.last_decision_id == DECISION
    assert initial.world.tick == 42
    assert initial.decision_count == 0


def test_reduce_turn_is_deterministic_for_same_input() -> None:
    initial = _initial()
    turn = TurnInput(
        tick=43,
        projection=_world(tick=43),
        economy=_economy_input(tick=43, resources=9),
    )
    first = initial.reduce_turn(turn, DECISION, actor=TENANT)
    second = initial.reduce_turn(turn, DECISION, actor=TENANT)

    assert first == second
    assert first.state_digest == second.state_digest
    assert first.state_digest.value == second.state_digest.value


def test_multi_turn_replay_matches_economy_digest_and_every_field() -> None:
    initial = _initial()
    transcript = (
        (
            TurnInput(
                tick=43,
                projection=_world(tick=43),
                economy=_economy_input(tick=43, resources=9),
            ),
            DecisionId("decision:43"),
        ),
        (
            TurnInput(
                tick=44,
                projection=_world(tick=44),
                economy=_economy_input(tick=44, resources=6),
            ),
            DecisionId("decision:44"),
        ),
        (
            TurnInput(
                tick=45,
                projection=_world(tick=45),
                economy=_economy_input(tick=45, resources=10),
            ),
            DecisionId("decision:45"),
        ),
    )

    def replay() -> tuple[TenantState, ...]:
        states = [initial]
        current = initial
        for turn, decision in transcript:
            current = current.reduce_turn(turn, decision, actor=TENANT)
            states.append(current)
        return tuple(states)

    first = replay()
    second = replay()

    assert first == second
    assert tuple(state.economy for state in first) == tuple(state.economy for state in second)
    assert tuple(state.economy.economy_digest for state in first) == tuple(
        state.economy.economy_digest for state in second
    )
    assert tuple(state.economy_input for state in first) == tuple(
        state.economy_input for state in second
    )


def test_reduce_turn_fails_closed_on_invalid_inputs() -> None:
    initial = _initial()
    turn = TurnInput(
        tick=43,
        projection=_world(tick=43),
        economy=_economy_input(tick=43, resources=9),
    )

    with pytest.raises(ValueError, match="regresses below"):
        initial.reduce_turn(
            TurnInput(
                tick=41,
                projection=_world(tick=41),
                economy=_economy_input(tick=41),
            ),
            DECISION,
            actor=TENANT,
        )
    with pytest.raises(ValueError, match="conflicting world observation"):
        initial.reduce_turn(
            TurnInput(
                tick=42,
                projection=_world(tick=42, unit_health=9),
                economy=_economy_input(),
            ),
            DECISION,
            actor=TENANT,
        )
    committed = initial.reduce_turn(turn, DECISION, actor=TENANT)
    with pytest.raises(ValueError, match="duplicate decision commit"):
        committed.reduce_turn(turn, DECISION, actor=TENANT)
    with pytest.raises(TypeError, match="turn must be a TurnInput"):
        initial.reduce_turn(cast(TurnInput, _world(tick=43)), DECISION, actor=TENANT)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="decision must be a DecisionId"):
        initial.reduce_turn(turn, cast(DecisionId, "decision:other"), actor=TENANT)  # type: ignore[arg-type]


def test_state_sequence_digest_is_deterministic_and_field_sensitive() -> None:
    initial = _initial()
    advanced = initial.observe(_world(tick=43), _economy_input(tick=43, resources=9))
    committed = advanced.record_decision(DECISION)
    sequence = (initial, advanced, committed)

    assert canonical_sha256(sequence) == canonical_sha256(sequence)
    assert canonical_sha256(sequence) != canonical_sha256((initial, advanced))
    assert canonical_sha256(sequence) != canonical_sha256((initial, committed, advanced))

    different_owner = TenantState(
        tenant_id=TenantId("other"),
        world=_world(),
        economy=_economy_state(),
    )
    different_count = TenantState(
        tenant_id=TENANT,
        world=_world(),
        economy=_economy_state(),
        decision_count=1,
    )
    different_last = TenantState(
        tenant_id=TENANT,
        world=_world(),
        economy=_economy_state(),
        last_decision_id=DECISION,
    )
    different_world = TenantState(
        tenant_id=TENANT, world=_world(unit_health=3), economy=_economy_state()
    )
    for changed in (different_owner, different_count, different_last, different_world):
        assert canonical_sha256((initial, changed)) != canonical_sha256((initial, initial))


def test_state_sequence_digest_ignores_nonsemantic_ordering() -> None:
    initial = _initial()
    equivalent = TenantState(tenant_id=TENANT, world=_reordered_world(), economy=_economy_state())

    assert equivalent == initial
    assert canonical_sha256((initial, equivalent)) == canonical_sha256((initial, initial))
