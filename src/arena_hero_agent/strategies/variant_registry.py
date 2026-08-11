"""Strategy variant selection (port of legacy ``variant-registry.ts``).

The legacy TypeScript oracle maps production variant ids to ``SafetyPlanner``
config overrides through one frozen registry; production configs declare
enabled variants by id and unknown ids fail fast at startup ("variant enablement
moves from a code boolean to a config declaration").

This port registers only the variants whose full effect is expressible on the
Python ``SafetyPlannerConfig`` surface. Every other oracle variant controls
``SafetyPlanner`` switches that are not migrated (EXPECTED_UNKNOWN in the
behavior-difference registry); enabling one in a Python config fails fast
instead of silently running with weakened behavior.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields
from typing import Final

from .safety_planner_config import SafetyPlannerConfig

# Variant id -> SafetyPlannerConfig field overrides. Field names use the Python
# config surface (snake_case). Only variants whose oracle override is entirely
# expressible here are registered; see docs/planning-differences.md for the
# EXPECTED_UNKNOWN set and the fail-closed contract for unmigrated ids.
VARIANT_SAFETY_CONFIG: Final[Mapping[str, Mapping[str, object]]] = {
    "population-ceiling-30-v1": {"population_ceiling": 30},
    "population-ceiling-35-v1": {"population_ceiling": 35},
    "population-ceiling-40-v1": {"population_ceiling": 40},
}

_KNOWN_FIELDS: Final[frozenset[str]] = frozenset(
    field.name for field in fields(SafetyPlannerConfig)
)


def is_safety_variant(id: str) -> bool:
    """Return whether an id is registered as a safety variant."""

    if not isinstance(id, str):
        raise TypeError("variant id must be a string")
    return id in VARIANT_SAFETY_CONFIG


def resolve_safety_variant_config(id: str) -> Mapping[str, object]:
    """Resolve one variant id to its config override; unknown ids fail fast."""

    if not isinstance(id, str):
        raise TypeError("variant id must be a string")
    config = VARIANT_SAFETY_CONFIG.get(id)
    if config is None:
        registered = ", ".join(sorted(VARIANT_SAFETY_CONFIG))
        raise ValueError(f"unknown safety variant: {id} (registered: {registered})")
    return config


def resolve_variants_config(ids: Sequence[str] | None) -> dict[str, object]:
    """Merge the overrides of the enabled variant ids (empty input = zero override).

    Later ids win on field conflicts, matching the oracle's ``Object.assign``
    merge order. Duplicate ids are tolerated here (again oracle parity); the
    selection boundary rejects them in :func:`apply_variant_overrides`.
    """

    if ids is None:
        return {}
    if isinstance(ids, str) or not isinstance(ids, Sequence):
        raise TypeError("variant ids must be a sequence of strings")
    merged: dict[str, object] = {}
    for id in ids:
        merged.update(resolve_safety_variant_config(id))
    return merged


def apply_variant_overrides(config: SafetyPlannerConfig, ids: Sequence[str]) -> SafetyPlannerConfig:
    """Select the effective config for the enabled variant ids.

    The selection boundary mirrors the oracle's ``compileRuntimeStrategy``
    duplicate gate: an ambiguous (duplicate) declaration fails fast. Unknown
    ids fail fast through :func:`resolve_variants_config`. The resulting
    config is rebuilt through the dataclass constructor so field invariants
    are re-validated.
    """

    if not isinstance(config, SafetyPlannerConfig):
        raise TypeError("config must be a SafetyPlannerConfig")
    if isinstance(ids, str) or not isinstance(ids, Sequence):
        raise TypeError("variant ids must be a sequence of strings")
    seen: set[str] = set()
    for id in ids:
        if not isinstance(id, str):
            raise TypeError("variant ids must be strings")
        if id in seen:
            raise ValueError(f"duplicate strategy variant: {id}")
        seen.add(id)
    merged = resolve_variants_config(ids)
    if not merged:
        return config
    values = {field.name: getattr(config, field.name) for field in fields(SafetyPlannerConfig)}
    values.update(merged)
    return SafetyPlannerConfig(**values)
