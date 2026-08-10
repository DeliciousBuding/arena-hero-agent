from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from arena_hero_agent.domain import (
    CURRENT_RULES_VERSION,
    DEFAULT_PHASE_CONFIG,
    SUPPORTED_RULES_VERSIONS,
    GamePhase,
    PhaseConfig,
    PhaseInputs,
    RulesVersion,
    assert_current_rules_version,
    parse_rules_version,
    transition_phase,
)


def test_rules_versions_are_explicit_and_current_is_v014() -> None:
    assert SUPPORTED_RULES_VERSIONS == (RulesVersion.V0_11, RulesVersion.V0_14)
    assert CURRENT_RULES_VERSION is RulesVersion.V0_14
    assert parse_rules_version("v0.11") is RulesVersion.V0_11
    assert parse_rules_version("v0.14") is RulesVersion.V0_14


@pytest.mark.parametrize("value", ["0.14", "latest", "V0.14", "v0.15", ""])
def test_rules_version_parser_rejects_inference_and_unknown_values(value: str) -> None:
    with pytest.raises(ValueError, match="unsupported rules version"):
        parse_rules_version(value)


def test_current_rules_assertion_recognizes_but_rejects_historical_version() -> None:
    assert_current_rules_version(RulesVersion.V0_14)
    with pytest.raises(ValueError, match="recognized but current"):
        assert_current_rules_version(RulesVersion.V0_11)


def test_phase_transition_is_pure_and_matches_pinned_thresholds() -> None:
    early = PhaseInputs(population=4, resources=100, enemies_near_core=0)
    balanced = PhaseInputs(population=5, resources=20, enemies_near_core=0)
    military_population = PhaseInputs(population=18, resources=0, enemies_near_core=0)
    military_threat = PhaseInputs(population=0, resources=0, enemies_near_core=2)

    assert transition_phase(GamePhase.MILITARY, early) is GamePhase.EARLY_EXPANSION
    assert transition_phase(GamePhase.EARLY_EXPANSION, balanced) is GamePhase.BALANCED
    assert transition_phase(GamePhase.BALANCED, military_population) is GamePhase.MILITARY
    assert transition_phase(GamePhase.EARLY_EXPANSION, military_threat) is GamePhase.MILITARY
    assert (
        transition_phase(GamePhase.EARLY_EXPANSION, early, forced=GamePhase.MILITARY)
        is GamePhase.MILITARY
    )
    assert PhaseConfig(18, 2, 5, 20) == DEFAULT_PHASE_CONFIG


def _set_attribute(target: object, name: str, value: object) -> None:
    setattr(target, name, value)


def test_phase_values_are_immutable_and_validate_thresholds() -> None:
    inputs = PhaseInputs(population=1, resources=2, enemies_near_core=0)
    with pytest.raises(FrozenInstanceError):
        _set_attribute(inputs, "population", 9)
    with pytest.raises(ValueError, match="cannot exceed"):
        PhaseConfig(military_population=4, balanced_population=5)
    with pytest.raises(ValueError, match="cannot be negative"):
        PhaseInputs(population=-1, resources=0, enemies_near_core=0)


def test_phase_thresholds_enforce_cross_language_safe_integer_bounds() -> None:
    maximum = 2**53 - 1
    assert PhaseInputs(maximum, maximum, maximum).population == maximum
    with pytest.raises(ValueError, match="safe-integer"):
        PhaseInputs(maximum + 1, 0, 0)
    with pytest.raises(ValueError, match="safe-integer"):
        PhaseConfig(military_population=maximum + 1)
