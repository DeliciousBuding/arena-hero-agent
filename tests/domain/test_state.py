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
    EntityId,
    EntityKind,
    EntityObservation,
    Generation,
    ResourceObservation,
    RulesVersion,
    StateDigest,
    TenantId,
    TenantState,
    TerrainObservation,
    TerrainState,
    UnitObservation,
    UnitRole,
    WorldProjection,
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


def _initial() -> TenantState:
    return TenantState(tenant_id=TENANT, world=_world())


def _set_attribute(target: object, name: str, value: object) -> None:
    setattr(target, name, value)


def test_tenant_state_is_immutable_and_validates() -> None:
    state = _initial()
    with pytest.raises(FrozenInstanceError):
        _set_attribute(state, "decision_count", 1)
    with pytest.raises(TypeError, match="tenant_id must be a TenantId"):
        TenantState(tenant_id=cast(TenantId, "sample"), world=_world())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="world must be a WorldProjection"):
        TenantState(tenant_id=TENANT, world=cast(WorldProjection, {}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="decision_count"):
        TenantState(tenant_id=TENANT, world=_world(), decision_count=-1)
    with pytest.raises(ValueError, match="safe-integer"):
        TenantState(tenant_id=TENANT, world=_world(), decision_count=2**53)
    with pytest.raises(TypeError, match="last_decision_id"):
        TenantState(
            tenant_id=TENANT,
            world=_world(),
            last_decision_id=cast(DecisionId, "decision:other"),
        )  # type: ignore[arg-type]


def test_tenant_state_digest_known_answers_are_frozen() -> None:
    initial = _initial()
    advanced = initial.observe(_world(tick=43))
    committed = advanced.record_decision(DECISION)

    assert initial.state_digest.value == (
        "02ab1a943103c0ece99007eb10bdc90576a5fda13818e3acfb3ac4d08d7065d6"
    )
    assert advanced.state_digest.value == (
        "da77015b67a9390ee3a537d582080148257f820a7b9ba1aa8475f6356bc29aaf"
    )
    assert committed.state_digest.value == (
        "571b94f5ccec03116831f375e55979009af6b685c5aae54b747898dd21871646"
    )


def test_observe_reducer_advances_world_without_mutating() -> None:
    initial = _initial()
    advanced = initial.observe(_world(tick=43))

    assert advanced is not initial
    assert advanced.tenant_id is initial.tenant_id
    assert advanced.world.tick == 43
    assert advanced.decision_count == initial.decision_count == 0
    assert advanced.last_decision_id is initial.last_decision_id is None
    assert initial.world.tick == 42
    assert initial.state_digest.value == (
        "02ab1a943103c0ece99007eb10bdc90576a5fda13818e3acfb3ac4d08d7065d6"
    )


def test_observe_rejects_tick_regression_and_same_tick_conflict() -> None:
    initial = _initial()
    with pytest.raises(ValueError, match="regresses below"):
        initial.observe(_world(tick=41))
    with pytest.raises(ValueError, match="conflicting world observation"):
        initial.observe(_world(tick=42, unit_health=9))
    assert initial.observe(_world(tick=42)) is initial


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
        _initial().observe(historical)


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
    canonical = TenantState(tenant_id=TENANT, world=_world())
    reordered = TenantState(tenant_id=TENANT, world=_reordered_world())

    assert canonical.world == reordered.world
    assert canonical.state_digest == reordered.state_digest


def test_semantic_changes_alter_identity() -> None:
    state = _initial()
    changed_health = TenantState(tenant_id=TENANT, world=_world(unit_health=3))
    changed_tick = _initial().observe(_world(tick=43))
    changed_journal = _initial().record_decision(DECISION)

    assert changed_health.state_digest != state.state_digest
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


def test_tenant_state_satisfies_cas_store_state_type() -> None:
    """P4-9 seam: TenantState is a valid StateT for the CAS tenant store."""
    store: TenantStateStore[TenantState] = _FakeStateStore()
    assert isinstance(store, TenantStateStore)
