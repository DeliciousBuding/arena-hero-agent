from __future__ import annotations

from collections.abc import Iterable
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from arena_hero_agent.domain import (
    EconomyDecisionInput,
    EconomyState,
    EconomyTurnInput,
    RulesVersion,
    StateDigest,
    TenantId,
    UnitPrices,
    UnitRole,
    core_resource_capacity,
    replay_economy,
    unit_price,
)

SEED = 0xA11CE
RULES = RulesVersion.V0_14
TENANT = TenantId("sample")


def _input(
    tick: int,
    *,
    seed: int = SEED,
    resources: int = 10,
    population: int = 2,
) -> EconomyTurnInput:
    return EconomyTurnInput.observed(
        seed=seed,
        tick=tick,
        rules_version=RULES,
        resources=resources,
        population=population,
    )


def _set_attribute(target: object, name: str, value: object) -> None:
    setattr(target, name, value)


@pytest.mark.parametrize(
    ("population", "expected"),
    [
        (0, UnitPrices(5, 10, 12)),
        (19, UnitPrices(5, 10, 12)),
        (20, UnitPrices(7, 13, 16)),
        (24, UnitPrices(7, 13, 16)),
        (25, UnitPrices(8, 17, 20)),
        (30, UnitPrices(11, 22, 26)),
        (100, UnitPrices(433, 865, 1038)),
    ],
)
def test_dynamic_unit_prices_match_v014_known_answers(
    population: int,
    expected: UnitPrices,
) -> None:
    assert UnitPrices.for_population(population, RULES) == expected
    assert unit_price(UnitRole.WORKER, population, RULES) == expected.worker
    assert unit_price(UnitRole.VANGUARD, population, RULES) == expected.vanguard
    assert unit_price(UnitRole.RANGER, population, RULES) == expected.ranger


@pytest.mark.parametrize(
    ("population", "capacity"),
    [(0, 10), (1, 10), (2, 10), (3, 15), (20, 100)],
)
def test_resource_capacity_is_exact_and_bounded(population: int, capacity: int) -> None:
    assert core_resource_capacity(population) == capacity


def test_economy_input_is_immutable_and_derives_prices() -> None:
    source = _input(4, resources=9, population=20)

    assert source.unit_prices == UnitPrices(worker=7, vanguard=13, ranger=16)
    assert source.input_digest == source.input_digest
    with pytest.raises(FrozenInstanceError):
        _set_attribute(source, "resources", 8)


def test_economy_inputs_fail_closed_on_invalid_balances_rules_and_prices() -> None:
    with pytest.raises(ValueError, match="resources cannot be negative"):
        _input(1, resources=-1)
    with pytest.raises(ValueError, match="population cannot be negative"):
        _input(1, population=-1)
    with pytest.raises(ValueError, match="exceed Core capacity"):
        _input(1, resources=11, population=2)
    with pytest.raises(ValueError, match="recognized but current"):
        EconomyTurnInput.observed(
            seed=SEED,
            tick=1,
            rules_version=RulesVersion.V0_11,
            resources=1,
            population=1,
        )
    with pytest.raises(TypeError, match="rules_version must be a RulesVersion"):
        EconomyTurnInput(
            seed=SEED,
            tick=1,
            rules_version=cast(RulesVersion, "v9.9"),
            resources=1,
            population=1,
            unit_prices=UnitPrices.base(),
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="safe-integer"):
        UnitPrices(worker=2**53, vanguard=10, ranger=12)
    with pytest.raises(ValueError, match="do not match"):
        EconomyTurnInput(
            seed=SEED,
            tick=20,
            rules_version=RULES,
            resources=20,
            population=20,
            unit_prices=UnitPrices.base(),
        )
    with pytest.raises(ValueError, match="unit price exceeds"):
        UnitPrices.for_population(10_000, RULES)


def test_economy_state_advances_resources_population_capacity_and_prices() -> None:
    initial = EconomyState.initial(_input(10, resources=8, population=2))
    advanced = initial.advance(_input(11, resources=37, population=20))

    assert advanced.seed == SEED
    assert advanced.tick == 11
    assert advanced.resources == 37
    assert advanced.resource_delta == 29
    assert advanced.population == 20
    assert advanced.population_delta == 18
    assert advanced.resource_capacity == 100
    assert advanced.resource_space == 63
    assert advanced.base_costs == UnitPrices.base()
    assert advanced.unit_prices == UnitPrices(worker=7, vanguard=13, ranger=16)
    assert initial.resources == 8
    assert initial.population == 2


def test_same_seed_same_inputs_replay_to_identical_states_and_decision_inputs() -> None:
    initial = EconomyState.initial(_input(10, resources=8, population=2))
    inputs = (
        _input(11, resources=10, population=3),
        _input(12, resources=14, population=20),
        _input(13, resources=7, population=21),
    )

    first = replay_economy(initial, inputs)
    second = replay_economy(initial, inputs)

    assert first == second
    assert [state.economy_digest for state in first] == [state.economy_digest for state in second]
    assert [state.decision_input(TENANT) for state in first] == [
        state.decision_input(TENANT) for state in second
    ]
    assert (
        first[-1].decision_input(TENANT).selection_seed
        == second[-1].decision_input(TENANT).selection_seed
    )


@pytest.mark.parametrize(
    "changed",
    [
        _input(10, seed=SEED + 1, resources=8, population=2),
        _input(11, resources=8, population=2),
        _input(10, resources=9, population=2),
        _input(10, resources=8, population=3),
    ],
)
def test_changing_any_economy_input_changes_digest(changed: EconomyTurnInput) -> None:
    baseline = EconomyState.initial(_input(10, resources=8, population=2))
    candidate = EconomyState.initial(changed)

    assert candidate.economy_digest != baseline.economy_digest
    assert candidate.input_digest != baseline.input_digest


def test_reducer_rejects_seed_tick_and_same_tick_conflicts() -> None:
    state = EconomyState.initial(_input(10, resources=8, population=2))

    with pytest.raises(ValueError, match="seed cannot change"):
        state.advance(_input(11, seed=SEED + 1, resources=8, population=2))
    with pytest.raises(ValueError, match="regresses below"):
        state.advance(_input(9, resources=8, population=2))
    with pytest.raises(ValueError, match="conflicting economy observation"):
        state.advance(_input(10, resources=9, population=2))


def test_decision_input_rejects_digest_and_selection_seed_tampering() -> None:
    economy = EconomyState.initial(_input(10, resources=8, population=2))
    decision_input = economy.decision_input(TENANT)

    assert decision_input.economy is economy
    assert decision_input.economy_digest == economy.economy_digest
    assert 0 <= decision_input.selection_seed < 2**52
    with pytest.raises(ValueError, match="economy_digest"):
        EconomyDecisionInput(
            tenant_id=TENANT,
            economy=economy,
            economy_digest=StateDigest("0" * 64),
            selection_seed=decision_input.selection_seed,
        )
    with pytest.raises(ValueError, match="selection_seed"):
        EconomyDecisionInput(
            tenant_id=TENANT,
            economy=economy,
            economy_digest=economy.economy_digest,
            selection_seed=decision_input.selection_seed + 1,
        )


def test_decision_input_seed_is_tenant_scoped() -> None:
    economy = EconomyState.initial(_input(10, resources=8, population=2))

    first = economy.decision_input(TenantId("t1"))
    second = economy.decision_input(TenantId("t2"))

    assert first.economy == second.economy
    assert first.economy_digest == second.economy_digest
    assert first.selection_seed != second.selection_seed


def test_replay_rejects_non_economy_inputs() -> None:
    initial = EconomyState.initial(_input(10, resources=8, population=2))
    with pytest.raises(TypeError, match="iterable"):
        replay_economy(initial, cast(Iterable[EconomyTurnInput], "not-inputs"))
    with pytest.raises(TypeError, match="only EconomyTurnInput"):
        replay_economy(initial, cast(list[EconomyTurnInput], [object()]))
