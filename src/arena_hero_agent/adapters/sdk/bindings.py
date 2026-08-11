"""Lazy loading and validation of the public ``arena-hero`` SDK surface."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import import_module, metadata
from types import ModuleType
from typing import Any

from .errors import SdkContractViolationError

_MINIMUM_VERSION = (0, 2, 9)
_NEXT_BREAKING_VERSION = (0, 3, 0)
# The fork's 0.3.0a3 migration baseline is additive over the 0.2.9 public
# surface and is the pinned dependency for this repository (pyproject.toml).
_MIGRATION_BASELINE = "0.3.0a3"
_ALLOWED_0_3_PRERELEASES = frozenset({_MIGRATION_BASELINE})
_RELEASE_PREFIX = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[a-zA-Z]|[-+]|$)")
_REQUIRED_PUBLIC_NAMES = (
    "APIError",
    "Accepted",
    "ArenaHeroError",
    "AsyncArenaHeroClient",
    "AsyncTurn",
    "AuthenticationError",
    "CommandPlan",
    "CommandSource",
    "ConfigurationError",
    "Direction",
    "InvalidActionError",
    "PolicyViolationError",
    "ProtocolError",
    "Received",
    "Tick",
    "TransportError",
    "TurnClosedError",
)


@dataclass(frozen=True, slots=True)
class SdkBindings:
    """Runtime references to the validated public SDK API.

    Keeping these references in one value makes normal package import lazy and lets
    contract tests inject a deterministic SDK-shaped implementation.
    """

    version: str
    async_client_type: type[Any]
    accepted_type: type[Any]
    command_plan_type: type[Any]
    command_source_type: type[Any]
    direction_type: type[Any]
    event_types: tuple[type[Any], ...]
    arena_error_type: type[BaseException]
    api_error_type: type[BaseException]
    authentication_error_type: type[BaseException]
    configuration_error_type: type[BaseException]
    invalid_action_error_type: type[BaseException]
    policy_violation_error_type: type[BaseException]
    protocol_error_type: type[BaseException]
    transport_error_type: type[BaseException]
    turn_closed_error_type: type[BaseException]


def _release_tuple(version: str) -> tuple[int, int, int]:
    match = _RELEASE_PREFIX.match(version)
    if match is None:
        raise SdkContractViolationError("load", f"unsupported SDK version syntax: {version!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _accepts_version(version: str, release: tuple[int, int, int]) -> bool:
    if _MINIMUM_VERSION <= release < _NEXT_BREAKING_VERSION:
        return True
    return version in _ALLOWED_0_3_PRERELEASES


def _require_public_api(module: ModuleType, name: str) -> Any:
    try:
        return getattr(module, name)
    except AttributeError as exc:
        raise SdkContractViolationError(
            "load", f"arena-hero is missing required public name {name!r}"
        ) from exc


def load_sdk_bindings() -> SdkBindings:
    """Import and validate the installed public ``arena-hero`` SDK on first use."""

    version = metadata.version("arena-hero")
    release = _release_tuple(version)
    if not _accepts_version(version, release):
        raise SdkContractViolationError(
            "load",
            f"arena-hero version must satisfy >=0.2.9,<0.3 or the pinned "
            f"{_MIGRATION_BASELINE} baseline (installed {version})",
        )

    module = import_module("arena_hero")
    public = {name: _require_public_api(module, name) for name in _REQUIRED_PUBLIC_NAMES}
    return SdkBindings(
        version=version,
        async_client_type=public["AsyncArenaHeroClient"],
        accepted_type=public["Accepted"],
        command_plan_type=public["CommandPlan"],
        command_source_type=public["CommandSource"],
        direction_type=public["Direction"],
        event_types=(public["Tick"], public["AsyncTurn"], public["Received"]),
        arena_error_type=public["ArenaHeroError"],
        api_error_type=public["APIError"],
        authentication_error_type=public["AuthenticationError"],
        configuration_error_type=public["ConfigurationError"],
        invalid_action_error_type=public["InvalidActionError"],
        policy_violation_error_type=public["PolicyViolationError"],
        protocol_error_type=public["ProtocolError"],
        transport_error_type=public["TransportError"],
        turn_closed_error_type=public["TurnClosedError"],
    )
