"""Deterministic Arena Hero economy state and v0.14 pricing reducers.

The server turn remains authoritative for resource and population balances. This
module validates those balances, derives capacity and exact dynamic prices with
integer arithmetic, and folds them into one immutable replayable state. No
filesystem, SDK, wall-clock, or random-number-generator state enters the
reducer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Self

from .rules import RulesVersion, assert_current_rules_version
from .value_objects import StateDigest, _require_int
from .world import UnitRole

_MAX_SAFE_INTEGER = 2**53 - 1
_CORE_RESOURCE_CAPACITY_PER_UNIT = 5
_CORE_RESOURCE_MINIMUM_CAPACITY = 10
_PRICING_BASE_POPULATION = 20
_PRICING_TIER_SIZE = 5
_PRICING_GROWTH_NUMERATOR = 13
_PRICING_GROWTH_DENOMINATOR = 10

BASE_UNIT_COSTS: Final = MappingProxyType(
    {
        UnitRole.WORKER: 5,
        UnitRole.VANGUARD: 10,
        UnitRole.RANGER: 12,
    }
)


def _nonnegative_safe_int(name: str, value: object) -> int:
    checked = _require_int(name, value)
    if checked < 0:
        raise ValueError(f"{name} cannot be negative")
    if checked > _MAX_SAFE_INTEGER:
        raise ValueError(f"{name} exceeds the cross-language safe-integer range")
    return checked


def _positive_safe_int(name: str, value: object) -> int:
    checked = _nonnegative_safe_int(name, value)
    if checked == 0:
        raise ValueError(f"{name} must be positive")
    return checked


def _signed_safe_int(name: str, value: object) -> int:
    checked = _require_int(name, value)
    if not -_MAX_SAFE_INTEGER <= checked <= _MAX_SAFE_INTEGER:
        raise ValueError(f"{name} exceeds the cross-language safe-integer range")
    return checked


def _pricing_exponent(population: int) -> int:
    if population < _PRICING_BASE_POPULATION:
        return 0
    return (population - _PRICING_BASE_POPULATION) // _PRICING_TIER_SIZE + 1


def _maximum_safe_exponent(base_cost: int) -> int:
    exponent = 0
    numerator = base_cost
    denominator = 1
    while True:
        next_numerator = numerator * _PRICING_GROWTH_NUMERATOR
        next_denominator = denominator * _PRICING_GROWTH_DENOMINATOR
        next_price = (2 * next_numerator + next_denominator) // (2 * next_denominator)
        if next_price > _MAX_SAFE_INTEGER:
            return exponent
        exponent += 1
        numerator = next_numerator
        denominator = next_denominator


_MAX_SAFE_PRICE_EXPONENT: Final = min(
    _maximum_safe_exponent(base_cost) for base_cost in BASE_UNIT_COSTS.values()
)


def core_resource_capacity(population: int) -> int:
    """Return the v0.14 Core capacity, rejecting cross-language overflow."""

    checked = _nonnegative_safe_int("population", population)
    if checked > _MAX_SAFE_INTEGER // _CORE_RESOURCE_CAPACITY_PER_UNIT:
        raise ValueError("resource capacity exceeds the cross-language safe-integer range")
    return max(_CORE_RESOURCE_MINIMUM_CAPACITY, checked * _CORE_RESOURCE_CAPACITY_PER_UNIT)


def unit_price(role: UnitRole, population: int, rules_version: RulesVersion) -> int:
    """Return the exact current price using one final round-half-up operation."""

    if not isinstance(role, UnitRole):
        raise TypeError("role must be a UnitRole")
    if not isinstance(rules_version, RulesVersion):
        raise TypeError("rules_version must be a RulesVersion")
    assert_current_rules_version(rules_version)
    checked_population = _nonnegative_safe_int("population", population)
    exponent = _pricing_exponent(checked_population)
    if exponent > _MAX_SAFE_PRICE_EXPONENT:
        raise ValueError("unit price exceeds the cross-language safe-integer range")
    numerator = BASE_UNIT_COSTS[role] * _PRICING_GROWTH_NUMERATOR**exponent
    denominator = _PRICING_GROWTH_DENOMINATOR**exponent
    price = (2 * numerator + denominator) // (2 * denominator)
    return _positive_safe_int(f"{role.value} unit price", price)


@dataclass(frozen=True, slots=True)
class UnitPrices:
    """Canonical price vector for the three current unit roles."""

    __canonical_name__ = "arena-hero.unit-prices.v1"

    worker: int
    vanguard: int
    ranger: int

    def __post_init__(self) -> None:
        _positive_safe_int("worker unit price", self.worker)
        _positive_safe_int("vanguard unit price", self.vanguard)
        _positive_safe_int("ranger unit price", self.ranger)

    @classmethod
    def base(cls) -> Self:
        return cls(
            worker=BASE_UNIT_COSTS[UnitRole.WORKER],
            vanguard=BASE_UNIT_COSTS[UnitRole.VANGUARD],
            ranger=BASE_UNIT_COSTS[UnitRole.RANGER],
        )

    @classmethod
    def for_population(cls, population: int, rules_version: RulesVersion) -> Self:
        return cls(
            worker=unit_price(UnitRole.WORKER, population, rules_version),
            vanguard=unit_price(UnitRole.VANGUARD, population, rules_version),
            ranger=unit_price(UnitRole.RANGER, population, rules_version),
        )

    def for_role(self, role: UnitRole) -> int:
        if not isinstance(role, UnitRole):
            raise TypeError("role must be a UnitRole")
        return {
            UnitRole.WORKER: self.worker,
            UnitRole.VANGUARD: self.vanguard,
            UnitRole.RANGER: self.ranger,
        }[role]


@dataclass(frozen=True, slots=True)
class EconomyTurnInput:
    """Authoritative economic observation for one deterministic tenant turn."""

    __canonical_name__ = "arena-hero.economy-turn-input.v1"

    seed: int
    tick: int
    rules_version: RulesVersion
    resources: int
    population: int
    unit_prices: UnitPrices

    def __post_init__(self) -> None:
        _nonnegative_safe_int("economy seed", self.seed)
        _nonnegative_safe_int("economy tick", self.tick)
        if not isinstance(self.rules_version, RulesVersion):
            raise TypeError("rules_version must be a RulesVersion")
        assert_current_rules_version(self.rules_version)
        resources = _nonnegative_safe_int("economy resources", self.resources)
        population = _nonnegative_safe_int("economy population", self.population)
        if not isinstance(self.unit_prices, UnitPrices):
            raise TypeError("unit_prices must be UnitPrices")
        capacity = core_resource_capacity(population)
        if resources > capacity:
            raise ValueError(f"economy resources {resources} exceed Core capacity {capacity}")
        expected_prices = UnitPrices.for_population(population, self.rules_version)
        if self.unit_prices != expected_prices:
            raise ValueError("unit_prices do not match rules_version and population")

    @classmethod
    def observed(
        cls,
        *,
        seed: int,
        tick: int,
        rules_version: RulesVersion,
        resources: int,
        population: int,
    ) -> Self:
        """Build a validated input while deriving the exact canonical price vector."""

        return cls(
            seed=seed,
            tick=tick,
            rules_version=rules_version,
            resources=resources,
            population=population,
            unit_prices=UnitPrices.for_population(population, rules_version),
        )

    @property
    def input_digest(self) -> StateDigest:
        return StateDigest.from_state(self)


@dataclass(frozen=True, slots=True)
class EconomyState:
    """Immutable economic state embedded in the authoritative tenant state."""

    __canonical_name__ = "arena-hero.economy-state.v1"

    seed: int
    tick: int
    rules_version: RulesVersion
    resources: int
    population: int
    resource_capacity: int
    resource_space: int
    base_costs: UnitPrices
    unit_prices: UnitPrices
    resource_delta: int
    population_delta: int
    input_digest: StateDigest

    def __post_init__(self) -> None:
        _nonnegative_safe_int("economy seed", self.seed)
        _nonnegative_safe_int("economy tick", self.tick)
        if not isinstance(self.rules_version, RulesVersion):
            raise TypeError("rules_version must be a RulesVersion")
        assert_current_rules_version(self.rules_version)
        resources = _nonnegative_safe_int("economy resources", self.resources)
        population = _nonnegative_safe_int("economy population", self.population)
        capacity = _nonnegative_safe_int("economy resource_capacity", self.resource_capacity)
        space = _nonnegative_safe_int("economy resource_space", self.resource_space)
        _signed_safe_int("economy resource_delta", self.resource_delta)
        _signed_safe_int("economy population_delta", self.population_delta)
        if not isinstance(self.base_costs, UnitPrices):
            raise TypeError("base_costs must be UnitPrices")
        if not isinstance(self.unit_prices, UnitPrices):
            raise TypeError("unit_prices must be UnitPrices")
        if not isinstance(self.input_digest, StateDigest):
            raise TypeError("input_digest must be a StateDigest")
        expected_capacity = core_resource_capacity(population)
        if capacity != expected_capacity:
            raise ValueError("resource_capacity does not match population")
        if resources > capacity:
            raise ValueError("economy resources exceed Core capacity")
        if space != capacity - resources:
            raise ValueError("resource_space does not match capacity minus resources")
        if self.base_costs != UnitPrices.base():
            raise ValueError("base_costs do not match the current unit cost table")
        expected_prices = UnitPrices.for_population(population, self.rules_version)
        if self.unit_prices != expected_prices:
            raise ValueError("unit_prices do not match rules_version and population")
        source = EconomyTurnInput(
            seed=self.seed,
            tick=self.tick,
            rules_version=self.rules_version,
            resources=self.resources,
            population=self.population,
            unit_prices=self.unit_prices,
        )
        if source.input_digest != self.input_digest:
            raise ValueError("input_digest does not match economy input")

    @classmethod
    def initial(cls, source: EconomyTurnInput) -> Self:
        if not isinstance(source, EconomyTurnInput):
            raise TypeError("source must be an EconomyTurnInput")
        capacity = core_resource_capacity(source.population)
        return cls(
            seed=source.seed,
            tick=source.tick,
            rules_version=source.rules_version,
            resources=source.resources,
            population=source.population,
            resource_capacity=capacity,
            resource_space=capacity - source.resources,
            base_costs=UnitPrices.base(),
            unit_prices=source.unit_prices,
            resource_delta=0,
            population_delta=0,
            input_digest=source.input_digest,
        )

    def advance(self, source: EconomyTurnInput) -> Self:
        """Fold one authoritative turn input into the economic state."""

        if not isinstance(source, EconomyTurnInput):
            raise TypeError("source must be an EconomyTurnInput")
        if source.seed != self.seed:
            raise ValueError("economy seed cannot change during replay")
        if source.rules_version is not self.rules_version:
            raise ValueError("economy rules_version cannot change during replay")
        if source.tick < self.tick:
            raise ValueError(f"economy tick {source.tick} regresses below current tick {self.tick}")
        if source.tick == self.tick:
            if source.input_digest == self.input_digest:
                return self
            raise ValueError(f"conflicting economy observation for tick {source.tick}")
        capacity = core_resource_capacity(source.population)
        return type(self)(
            seed=self.seed,
            tick=source.tick,
            rules_version=self.rules_version,
            resources=source.resources,
            population=source.population,
            resource_capacity=capacity,
            resource_space=capacity - source.resources,
            base_costs=self.base_costs,
            unit_prices=source.unit_prices,
            resource_delta=source.resources - self.resources,
            population_delta=source.population - self.population,
            input_digest=source.input_digest,
        )

    @property
    def economy_digest(self) -> StateDigest:
        return StateDigest.from_state(self)

    @property
    def decision_input(self) -> EconomyDecisionInput:
        return EconomyDecisionInput.from_state(self)


@dataclass(frozen=True, slots=True)
class EconomyDecisionInput:
    """Stable P4-11 input seam for safety and tactical policy layers."""

    __canonical_name__ = "arena-hero.economy-decision-input.v1"

    economy: EconomyState
    economy_digest: StateDigest
    selection_seed: int

    def __post_init__(self) -> None:
        if not isinstance(self.economy, EconomyState):
            raise TypeError("economy must be an EconomyState")
        if not isinstance(self.economy_digest, StateDigest):
            raise TypeError("economy_digest must be a StateDigest")
        if self.economy.economy_digest != self.economy_digest:
            raise ValueError("economy_digest does not match economy state")
        expected_seed = self._selection_seed(self.economy)
        if self.selection_seed != expected_seed:
            raise ValueError("selection_seed does not match economy state")

    @classmethod
    def from_state(cls, economy: EconomyState) -> Self:
        if not isinstance(economy, EconomyState):
            raise TypeError("economy must be an EconomyState")
        return cls(
            economy=economy,
            economy_digest=economy.economy_digest,
            selection_seed=cls._selection_seed(economy),
        )

    @staticmethod
    def _selection_seed(economy: EconomyState) -> int:
        digest = StateDigest.from_state(
            (
                "arena-hero.economy-selection-seed.v1",
                economy.seed,
                economy.economy_digest,
            )
        )
        return int(digest.value[:13], 16)


def replay_economy(
    initial: EconomyState,
    inputs: Iterable[EconomyTurnInput],
) -> tuple[EconomyState, ...]:
    """Replay an input sequence and return every state, including the initial state."""

    if not isinstance(initial, EconomyState):
        raise TypeError("initial must be an EconomyState")
    if isinstance(inputs, str | bytes) or not isinstance(inputs, Iterable):
        raise TypeError("inputs must be an iterable of EconomyTurnInput")
    states = [initial]
    current = initial
    for source in inputs:
        if not isinstance(source, EconomyTurnInput):
            raise TypeError("inputs must contain only EconomyTurnInput values")
        current = current.advance(source)
        states.append(current)
    return tuple(states)
