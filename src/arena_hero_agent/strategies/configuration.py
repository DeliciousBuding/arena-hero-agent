"""Load and identify the small production strategy configuration surface.

The live process must be tunable without editing code, but configuration is
still deliberately narrow: only fields that have an effect in the composed
decider are accepted here.  The resulting document is canonical and hashed so
every runtime trace can be tied back to the exact strategy inputs.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from .composition import ComposedDeciderConfig
from .safety_planner_config import AggressionLevel

CONFIG_PATH_ENV = "ARENA_HERO_STRATEGY_CONFIG"
CONFIG_JSON_ENV = "ARENA_HERO_STRATEGY_CONFIG_JSON"

_BOOLEAN_FIELDS = frozenset(
    {
        "survey_burst_active",
        "stuck_guard_enabled",
        "movement_guard_enabled",
        "economy_budget_enabled",
        "economy_expansion_enabled",
        "raid_quota_enabled",
        "exploration_v2_enabled",
        "respawn_recovery_enabled",
    }
)
_INTEGER_FIELDS = frozenset(
    {
        "stuck_guard_ticks",
        "stuck_guard_radius",
        "movement_loop_window",
        "movement_loop_min_unique",
        "movement_deposit_stall_ticks",
        "movement_deposit_repath_streak",
        "movement_cargo_spin_ticks",
        "movement_cargo_spin_budget",
        "movement_cargo_core_distance",
        "respawn_worker_target",
        "respawn_detection_distance",
        "raid_min_observations",
        "raid_max_distance",
        "raid_min_fighters",
    }
)
_SAFETY_INTEGER_FIELDS = frozenset({"worker_target", "population_ceiling"})
_SAFETY_FIELDS = _SAFETY_INTEGER_FIELDS | {"aggression", "vanguard_ratio"}
_TOP_LEVEL_FIELDS = _BOOLEAN_FIELDS | _INTEGER_FIELDS | {"variants", "safety"}


def _reject_unknown_fields(values: Mapping[str, Any], allowed: frozenset[str], scope: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(f"unknown {scope} strategy configuration field(s): {', '.join(unknown)}")


def _require_boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"strategy field {name!r} must be boolean")
    return value


def _require_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"strategy field {name!r} must be integer")
    return value


def _config_document(config: ComposedDeciderConfig) -> dict[str, Any]:
    """Return a JSON-safe document containing every effective input."""

    document = _json_safe(config)
    document["variants"] = list(config.variants)
    safety = document["safety_config"]
    safety["aggression"] = config.safety_config.aggression.value
    document["safety"] = safety
    del document["safety_config"]
    return document


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _json_safe(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    return value


def config_hash(config: ComposedDeciderConfig) -> str:
    """Return the stable identity of the effective strategy configuration."""

    return _sha256_document(_config_document(config))


def strategy_hash(config: ComposedDeciderConfig) -> str:
    """Return a stable strategy identity distinct from its tunable inputs."""

    return _sha256_document(
        {"implementation": "composed-decider-v1", "configuration": _config_document(config)}
    )


def _sha256_document(document: Mapping[str, Any]) -> str:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def config_document(config: ComposedDeciderConfig) -> dict[str, Any]:
    """Return a defensive copy suitable for diagnostics and tests."""

    return json.loads(json.dumps(_config_document(config), sort_keys=True))


def config_from_mapping(values: Mapping[str, Any]) -> ComposedDeciderConfig:
    """Build a validated decider config from a strict JSON object."""

    if not isinstance(values, Mapping):
        raise ValueError("strategy configuration must be a JSON object")
    _reject_unknown_fields(values, _TOP_LEVEL_FIELDS, "top-level")

    updates: dict[str, Any] = {}
    for name in _BOOLEAN_FIELDS:
        if name in values:
            updates[name] = _require_boolean(values[name], name)
    for name in _INTEGER_FIELDS:
        if name in values:
            updates[name] = _require_integer(values[name], name)

    if "variants" in values:
        variants = values["variants"]
        if not isinstance(variants, list) or any(not isinstance(item, str) for item in variants):
            raise ValueError("strategy field 'variants' must be an array of strings")
        updates["variants"] = tuple(variants)

    safety_values = values.get("safety")
    if safety_values is not None:
        if not isinstance(safety_values, Mapping):
            raise ValueError("strategy field 'safety' must be an object")
        _reject_unknown_fields(safety_values, _SAFETY_FIELDS, "safety")
        safety_updates: dict[str, Any] = {}
        for name in _SAFETY_INTEGER_FIELDS:
            if name in safety_values:
                safety_updates[name] = _require_integer(safety_values[name], f"safety.{name}")
        if "aggression" in safety_values:
            try:
                safety_updates["aggression"] = AggressionLevel(safety_values["aggression"])
            except ValueError as exc:
                raise ValueError("safety.aggression must be 'defensive' or 'aggressive'") from exc
        if "vanguard_ratio" in safety_values:
            ratio = safety_values["vanguard_ratio"]
            if ratio is not None and (
                isinstance(ratio, bool) or not isinstance(ratio, (int, float))
            ):
                raise ValueError("safety.vanguard_ratio must be a number or null")
            safety_updates["vanguard_ratio"] = ratio
        updates["safety_config"] = replace(ComposedDeciderConfig().safety_config, **safety_updates)

    return ComposedDeciderConfig(**updates)


def load_config(path: str | os.PathLike[str] | None = None) -> ComposedDeciderConfig:
    """Load config from an explicit path or the documented environment inputs."""

    configured_path = path or os.environ.get(CONFIG_PATH_ENV)
    inline = os.environ.get(CONFIG_JSON_ENV)
    if configured_path is not None and inline is not None:
        raise ValueError(f"set only one of {CONFIG_PATH_ENV} and {CONFIG_JSON_ENV}")
    if configured_path is None and inline is None:
        return ComposedDeciderConfig()
    try:
        raw = (
            json.loads(inline)
            if inline is not None
            else json.loads(Path(configured_path).read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("strategy configuration could not be read as JSON") from exc
    return config_from_mapping(raw)


__all__ = [
    "CONFIG_JSON_ENV",
    "CONFIG_PATH_ENV",
    "config_document",
    "config_from_mapping",
    "config_hash",
    "load_config",
    "strategy_hash",
]
