"""Differential and fail-closed tests for the deterministic planning snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from arena_hero_agent.domain import (
    CURRENT_RULES_VERSION,
    BeaconObservation,
    BeaconStatus,
    Coordinate,
    CoreObservation,
    CoreState,
    EconomyState,
    EconomyTurnInput,
    EntityId,
    EntityKind,
    EntityObservation,
    ResourceObservation,
    TerrainObservation,
    UnitObservation,
    UnitRole,
    WorldProjection,
)
from arena_hero_agent.planning import (
    BeaconInfo,
    EnemyUnit,
    PlanningSnapshot,
    PlanningUnit,
    ResourceCellInfo,
    build_threat_map,
    extract_planning_snapshot,
    threat_contribution,
)
from tests.strategies.fixture_loader import load_oracle_fixture

RULES = CURRENT_RULES_VERSION
SEED = 0xA11CE


def _coordinate(value: list[int]) -> Coordinate:
    return Coordinate(value[0], value[1])


def _role(name: str) -> UnitRole:
    return {"WORKER": UnitRole.WORKER, "VANGUARD": UnitRole.VANGUARD, "RANGER": UnitRole.RANGER}[
        name
    ]


def _enemy(record: dict[str, object]) -> EnemyUnit:
    position = record["position"]
    assert isinstance(position, list)
    return EnemyUnit(
        id=EntityId(cast(str, record["id"])),
        position=_coordinate(position),
        unit_role=_role(cast(str, record["unitType"])),
    )


def _economy(tick: int, *, resources: int = 0, population: int = 0) -> EconomyState:
    return EconomyState.initial(
        EconomyTurnInput.observed(
            seed=SEED,
            tick=tick,
            rules_version=RULES,
            resources=resources,
            population=population,
        )
    )


def _projection(
    *,
    tick: int = 1,
    units: tuple[PlanningUnit, ...] = (),
    enemies: tuple[EnemyUnit, ...] = (),
    core: CoreObservation | None = None,
    beacon: BeaconObservation | None = None,
    resources: tuple[ResourceObservation, ...] = (),
    terrain: tuple[TerrainObservation, ...] = (),
) -> WorldProjection:
    return WorldProjection(
        tick=tick,
        rules_version=RULES,
        core=core,
        units=tuple(
            UnitObservation(
                id=unit.id,
                position=unit.position,
                role=unit.unit_role,
                health=unit.health,
                cargo=unit.cargo,
            )
            for unit in units
        ),
        entities=tuple(
            EntityObservation(
                id=enemy.id,
                kind=EntityKind.UNIT,
                position=enemy.position,
                health=1,
                unit_role=enemy.unit_role,
            )
            for enemy in enemies
        ),
        resources=resources,
        terrain=terrain,
        beacon=beacon,
    )


def _set_attribute(target: object, name: str, value: object) -> None:
    setattr(target, name, value)


def test_threat_map_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    metadata = fixture["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["oracle_commit"] == "8cf5cbbcccf396a8feee94404af44969c5388e15"
    for case in fixture["threat_maps"]:
        enemies = tuple(_enemy(enemy) for enemy in case["enemies"])
        assert build_threat_map(enemies) == case["expected"], case["name"]


def test_threat_map_is_deterministic() -> None:
    enemies = (
        EnemyUnit(id=EntityId("e1"), position=Coordinate(0, 0), unit_role=UnitRole.RANGER),
        EnemyUnit(id=EntityId("e2"), position=Coordinate(1, 0), unit_role=UnitRole.VANGUARD),
    )
    assert build_threat_map(enemies) == build_threat_map(enemies)


def test_threat_contribution_decays_inverse_distance() -> None:
    assert threat_contribution(0) == 1.0
    assert threat_contribution(1) == 0.5
    assert threat_contribution(3) == 0.25


def test_threat_map_rejects_invalid_inputs() -> None:
    with pytest.raises(TypeError):
        build_threat_map(cast(tuple[EnemyUnit, ...], []))
    with pytest.raises(TypeError):
        build_threat_map(cast(tuple[EnemyUnit, ...], ({"id": "e1"},)))
    with pytest.raises(ValueError):
        threat_contribution(-1)


def test_extract_planning_snapshot_projects_world_and_economy() -> None:
    projection = _projection(
        tick=7,
        units=(
            PlanningUnit(
                id=EntityId("w1"),
                unit_role=UnitRole.WORKER,
                position=Coordinate(2, 3),
                health=2,
                cargo=1,
            ),
        ),
        enemies=(_enemy({"id": "e1", "kind": "UNIT", "position": [0, 0], "unitType": "RANGER"}),),
        core=CoreObservation(
            id=EntityId("core"),
            position=Coordinate(0, 0),
            health=5,
            shield=3,
            state=CoreState.NORMAL,
            owner="u",
        ),
    )
    snapshot = extract_planning_snapshot(projection, _economy(7))
    assert isinstance(snapshot, PlanningSnapshot)
    assert snapshot.tick == 7
    assert snapshot.core_id == "core"
    assert snapshot.core_shield == 3
    assert snapshot.core_state == "normal"
    assert snapshot.enemy_cells == frozenset({"0,0"})
    assert [unit.cargo for unit in snapshot.units] == [1]
    # no beacon observation -> status None sentinel, fail closed
    assert snapshot.beacon.status is None
    assert snapshot.beacon.position == Coordinate(0, 0)


def test_extract_planning_snapshot_includes_ground_beacon_status() -> None:
    projection = _projection(
        beacon=BeaconObservation(
            position=Coordinate(3, 3),
            status=BeaconStatus.GROUND,
            carrier_id=None,
        )
    )
    snapshot = extract_planning_snapshot(projection, _economy(1))
    assert snapshot.beacon.status == "ground"
    assert snapshot.beacon.position == Coordinate(3, 3)
    assert snapshot.beacon.carrier_id is None


def test_extract_planning_snapshot_rejects_tick_or_rules_mismatch() -> None:
    projection = _projection()
    with pytest.raises(ValueError):
        extract_planning_snapshot(projection, _economy(2))


def test_snapshot_immutability_and_validation() -> None:
    snapshot = PlanningSnapshot(
        tick=1,
        rules_version=RULES,
        resources=0,
        resource_capacity=10,
        resource_space=10,
        population=0,
        units=(),
        resource_cells={},
        obstacle_cells=frozenset(),
        enemy_cells=frozenset(),
        enemy_units=(),
        core_id=None,
        core_position=None,
        core_health=None,
        core_shield=None,
        core_state=None,
        beacon=BeaconInfo(position=Coordinate(0, 0), status=None),
        threat_map={},
    )
    with pytest.raises(FrozenInstanceError):
        _set_attribute(snapshot, "units", ())
    with pytest.raises(ValueError):
        PlanningSnapshot(
            tick=0,
            rules_version=RULES,
            resources=0,
            resource_capacity=10,
            resource_space=10,
            population=0,
            units=(),
            resource_cells={},
            obstacle_cells=frozenset(),
            enemy_cells=frozenset(),
            enemy_units=(),
            core_id=None,
            core_position=None,
            core_health=None,
            core_shield=None,
            core_state=None,
            beacon=BeaconInfo(position=Coordinate(0, 0)),
            threat_map={},
        )
    with pytest.raises(TypeError):
        PlanningSnapshot(
            tick=1,
            rules_version=RULES,
            resources=0,
            resource_capacity=10,
            resource_space=10,
            population=0,
            units=(),
            resource_cells=cast(Mapping[str, ResourceCellInfo], []),
            obstacle_cells=frozenset(),
            enemy_cells=frozenset(),
            enemy_units=(),
            core_id=None,
            core_position=None,
            core_health=None,
            core_shield=None,
            core_state=None,
            beacon=BeaconInfo(position=Coordinate(0, 0)),
            threat_map={},
        )


def test_resource_cell_info_requires_positive_last_seen() -> None:
    with pytest.raises(ValueError):
        ResourceCellInfo(position=Coordinate(0, 0), visible=True, last_seen_tick=0)
