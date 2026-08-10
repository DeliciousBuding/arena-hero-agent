from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arena_hero_agent.domain import (
    BeaconObservation,
    BeaconStatus,
    Bounds,
    CellState,
    Coordinate,
    CoreObservation,
    CoreState,
    EntityId,
    EntityKind,
    EntityObservation,
    ResourceObservation,
    RulesVersion,
    TerrainObservation,
    TerrainState,
    UnitObservation,
    UnitRole,
    WorldProjection,
    canonicalize,
)


def _unit(identifier: str, x: int) -> UnitObservation:
    return UnitObservation(
        id=EntityId(identifier),
        position=Coordinate(x, 0),
        role=UnitRole.WORKER,
        health=2,
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


def _core(identifier: str = "core-a") -> CoreObservation:
    return CoreObservation(
        id=EntityId(identifier),
        position=Coordinate(0, 0),
        health=5,
        shield=4,
        state=CoreState.NORMAL,
        owner="player",
    )


def _projection(
    *,
    units: tuple[UnitObservation, ...] = (),
    entities: tuple[EntityObservation, ...] = (),
    resources: tuple[ResourceObservation, ...] = (),
    terrain: tuple[TerrainObservation, ...] = (),
) -> WorldProjection:
    return WorldProjection(
        tick=42,
        rules_version=RulesVersion.V0_14,
        core=_core(),
        units=units,
        entities=entities,
        resources=resources,
        terrain=terrain,
        beacon=BeaconObservation(Coordinate(8, 8), BeaconStatus.GROUND),
    )


@given(st.permutations(("unit-A", "unit-Z", "unit-a")))
def test_world_projection_digest_is_insertion_order_independent(
    identifiers: list[str],
) -> None:
    by_id = {
        "unit-A": _unit("unit-A", 1),
        "unit-Z": _unit("unit-Z", 2),
        "unit-a": _unit("unit-a", 3),
    }
    candidate = _projection(units=tuple(by_id[identifier] for identifier in identifiers))
    canonical = _projection(units=tuple(by_id[identifier] for identifier in sorted(by_id)))

    assert candidate.units == canonical.units
    assert candidate.state_digest == canonical.state_digest
    assert [unit.id.value for unit in candidate.units] == ["unit-A", "unit-Z", "unit-a"]


def test_world_projection_reuses_typed_canonical_compatibility() -> None:
    projection = _projection(
        units=(_unit("unit-b", 2), _unit("unit-a", 1)),
        entities=(_entity("enemy-b", 5), _entity("enemy-a", 4)),
        resources=(
            ResourceObservation(Coordinate(3, 0)),
            ResourceObservation(Coordinate(-1, 2), remaining=4),
        ),
        terrain=(
            TerrainObservation(Coordinate(1, 0), TerrainState.BLOCKED),
            TerrainObservation(Coordinate(0, 0), TerrainState.OPEN),
        ),
    )

    node = cast(list[object], canonicalize(projection))
    assert node[0] == "record"
    assert node[1] == "arena-hero.world-projection.v1"
    assert projection.state_digest.value == (
        "e025d561366db84299e0c9bdb86e7944f8973f9fbc3efbab637b3b0d3cbaf719"
    )
    assert (
        projection.state_digest
        != WorldProjection(
            tick=43,
            rules_version=projection.rules_version,
            core=projection.core,
            units=projection.units,
            entities=projection.entities,
            resources=projection.resources,
            terrain=projection.terrain,
            beacon=projection.beacon,
        ).state_digest
    )


def _assign_attribute(target: object, name: str, value: object) -> None:
    setattr(target, name, value)


def test_projection_and_nested_observations_are_immutable() -> None:
    projection = _projection(units=(_unit("unit-a", 1),))

    with pytest.raises(FrozenInstanceError):
        _assign_attribute(projection, "tick", 99)
    with pytest.raises(FrozenInstanceError):
        _assign_attribute(projection.units[0], "cargo", 9)
    assert isinstance(projection.units, tuple)


@pytest.mark.parametrize(
    "units,entities,core_id",
    [
        ((_unit("dup", 1), _unit("dup", 2)), (), "core-a"),
        ((_unit("dup", 1),), (_entity("dup", 2),), "core-a"),
        ((_unit("dup", 1),), (), "dup"),
    ],
)
def test_duplicate_ids_fail_loudly(
    units: tuple[UnitObservation, ...],
    entities: tuple[EntityObservation, ...],
    core_id: str,
) -> None:
    with pytest.raises(ValueError, match="duplicate world entity ids: dup"):
        WorldProjection(
            tick=1,
            rules_version=RulesVersion.V0_14,
            core=_core(core_id),
            units=units,
            entities=entities,
        )


def test_duplicate_resource_and_terrain_cells_fail_loudly() -> None:
    cell = Coordinate(1, 1)
    with pytest.raises(ValueError, match="duplicate resource cells"):
        _projection(resources=(ResourceObservation(cell), ResourceObservation(cell, remaining=2)))
    with pytest.raises(ValueError, match="duplicate terrain cells"):
        _projection(
            terrain=(
                TerrainObservation(cell, TerrainState.OPEN),
                TerrainObservation(cell, TerrainState.BLOCKED),
            )
        )


def test_observation_invariants_reject_ambiguous_semantics() -> None:
    with pytest.raises(ValueError, match="require carrier_id"):
        BeaconObservation(Coordinate(0, 0), BeaconStatus.CARRIED)
    with pytest.raises(ValueError, match="only carried"):
        BeaconObservation(
            Coordinate(0, 0),
            BeaconStatus.UNKNOWN,
            carrier_id=EntityId("unit-a"),
        )
    with pytest.raises(ValueError, match="require unit_role"):
        EntityObservation(
            EntityId("enemy-a"),
            EntityKind.UNIT,
            Coordinate(0, 0),
            health=1,
        )
    with pytest.raises(ValueError, match="require a destination"):
        CoreObservation(
            EntityId("core-a"),
            Coordinate(0, 0),
            health=1,
            shield=0,
            state=CoreState.MOVING,
            owner="player",
        )
    with pytest.raises(ValueError, match="cannot be negative"):
        ResourceObservation(Coordinate(0, 0), remaining=-1)


def test_navigation_grid_is_a_pure_projection_with_unknown_preserved() -> None:
    open_cell = Coordinate(0, 0)
    blocked_cell = Coordinate(1, 0)
    unknown_cell = Coordinate(2, 0)
    projection = _projection(
        terrain=(
            TerrainObservation(blocked_cell, TerrainState.BLOCKED),
            TerrainObservation(open_cell, TerrainState.OPEN),
        )
    )
    grid = projection.navigation_grid(bounds=Bounds(0, 2, 0, 0))

    assert grid.state_at(open_cell) is CellState.OPEN
    assert grid.state_at(blocked_cell) is CellState.BLOCKED
    assert grid.state_at(unknown_cell) is CellState.UNKNOWN


def test_world_values_enforce_cross_language_safe_integer_bounds() -> None:
    maximum = 2**53 - 1
    projection = WorldProjection(tick=maximum, rules_version=RulesVersion.V0_14)
    assert projection.tick == maximum
    with pytest.raises(ValueError, match="safe-integer"):
        WorldProjection(tick=maximum + 1, rules_version=RulesVersion.V0_14)
    with pytest.raises(ValueError, match="safe-integer"):
        UnitObservation(
            EntityId("unit-max"),
            Coordinate(0, 0),
            UnitRole.WORKER,
            health=maximum + 1,
        )


def test_world_canonical_enum_identity_is_stable_and_typed() -> None:
    enum_node = cast(list[object], canonicalize(RulesVersion.V0_14))
    assert enum_node[:2] == ["enum", "arena-hero.rules-version.v1"]
    assert canonicalize(RulesVersion.V0_14) != canonicalize("v0.14")
    assert cast(list[object], canonicalize(TerrainState.OPEN))[:2] == [
        "enum",
        "arena-hero.terrain-state.v1",
    ]


def test_resource_cannot_overlap_blocked_terrain() -> None:
    cell = Coordinate(4, -2)
    with pytest.raises(ValueError, match="resource cells cannot also be blocked terrain"):
        _projection(
            resources=(ResourceObservation(cell),),
            terrain=(TerrainObservation(cell, TerrainState.BLOCKED),),
        )
