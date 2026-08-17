"""Production strategy configuration and identity tests."""

from __future__ import annotations

import pytest

from arena_hero_agent.strategies import (
    ComposedDeciderConfig,
    config_from_mapping,
    config_hash,
    strategy_hash,
)


def test_configuration_mapping_changes_effective_hash() -> None:
    default_config = ComposedDeciderConfig()
    tuned_config = config_from_mapping(
        {
            "respawn_worker_target": 12,
            "movement_loop_window": 16,
            "safety": {"aggression": "aggressive"},
        }
    )

    assert tuned_config.respawn_worker_target == 12
    assert tuned_config.safety_config.aggression.value == "aggressive"
    assert config_hash(default_config) != config_hash(tuned_config)
    assert strategy_hash(default_config) != strategy_hash(tuned_config)


def test_configuration_hash_is_stable_for_equal_configs() -> None:
    first = config_from_mapping({"economy_expansion_enabled": False})
    second = config_from_mapping({"economy_expansion_enabled": False})

    assert config_hash(first) == config_hash(second)
    assert strategy_hash(first) == strategy_hash(second)


def test_configuration_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="unknown top-level"):
        config_from_mapping({"not_a_real_strategy_switch": True})


def test_configuration_rejects_boolean_as_integer() -> None:
    with pytest.raises(ValueError, match="must be integer"):
        config_from_mapping({"respawn_worker_target": True})
