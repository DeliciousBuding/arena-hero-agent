"""Strategy variant selection (P4-13): deterministic registry + oracle parity.

The registry is the config-declared variant surface: enabled ids resolve to
``SafetyPlannerConfig`` overrides, unknown ids fail fast, and selection rebuilds
the immutable config so field invariants are re-validated. The ``variant_config``
oracle fixture section pins the registered overrides to the TS oracle at the
pinned commit (camelCase -> snake_case field translation only).
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from arena_hero_agent.strategies import (
    DEFAULT_SAFETY_CONFIG,
    VARIANT_SAFETY_CONFIG,
    SafetyPlannerConfig,
    apply_variant_overrides,
    is_safety_variant,
    resolve_safety_variant_config,
    resolve_variants_config,
)
from tests.strategies.fixture_loader import load_oracle_fixture

# Oracle camelCase field names -> Python snake_case config fields.
_ORACLE_FIELD_MAP = {
    "populationCeiling": "population_ceiling",
}


def _to_python(overrides: dict[str, object]) -> dict[str, object]:
    return {_ORACLE_FIELD_MAP.get(key, key): value for key, value in overrides.items()}


def test_registered_overrides_target_real_config_fields() -> None:
    config_names = {field.name for field in fields(SafetyPlannerConfig)}
    assert VARIANT_SAFETY_CONFIG
    for variant_id, overrides in VARIANT_SAFETY_CONFIG.items():
        assert variant_id
        assert set(overrides) <= config_names, f"{variant_id} maps an unknown config field"


def test_known_ids_resolve_to_oracle_overrides() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["variant_config"]:
        if "ids" in case:
            continue
        actual = dict(resolve_safety_variant_config(case["name"]))
        assert actual == _to_python(case["expected"]), case["name"]


def test_unknown_id_fails_fast() -> None:
    with pytest.raises(ValueError, match="unknown safety variant: no-such-variant"):
        resolve_safety_variant_config("no-such-variant")
    assert is_safety_variant("no-such-variant") is False
    assert is_safety_variant("population-ceiling-35-v1") is True


def test_empty_or_none_variant_list_is_zero_override() -> None:
    assert resolve_variants_config(None) == {}
    assert resolve_variants_config([]) == {}


def test_merge_matches_oracle_order() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["variant_config"]:
        if "ids" not in case:
            continue
        actual = resolve_variants_config(case["ids"])
        assert actual == _to_python(case["expected"]), case["name"]


def test_apply_variant_overrides_rebuilds_validated_config() -> None:
    selected = apply_variant_overrides(
        DEFAULT_SAFETY_CONFIG, ["population-ceiling-30-v1", "population-ceiling-35-v1"]
    )
    assert selected.population_ceiling == 35  # later id wins, like the oracle merge
    assert selected.worker_target == DEFAULT_SAFETY_CONFIG.worker_target
    unchanged = apply_variant_overrides(DEFAULT_SAFETY_CONFIG, [])
    assert unchanged is DEFAULT_SAFETY_CONFIG


def test_apply_variant_overrides_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="duplicate strategy variant: population-ceiling-35-v1"):
        apply_variant_overrides(
            DEFAULT_SAFETY_CONFIG, ["population-ceiling-35-v1", "population-ceiling-35-v1"]
        )


def test_apply_variant_overrides_unknown_id_fails_fast() -> None:
    with pytest.raises(ValueError, match="unknown safety variant"):
        apply_variant_overrides(DEFAULT_SAFETY_CONFIG, ["not-a-variant"])
