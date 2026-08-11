"""Test-only SDK drift fixtures.

These helpers build a module-shaped copy of the installed ``arena_hero``
package with deliberate drift (missing exports or added enum members) so the
adapter's fail-closed contract checks can be exercised without a network or a
second SDK install.
"""

from __future__ import annotations

import types
from enum import StrEnum


def fake_arena_hero_module(
    *,
    override: dict[str, object] | None = None,
    remove: set[str] | None = None,
) -> types.ModuleType:
    """Return an ``arena_hero``-shaped module with optional drift applied."""

    import arena_hero

    fake = types.ModuleType("arena_hero")
    all_names = list(getattr(arena_hero, "__all__", ()))
    vars(fake)["__all__"] = all_names
    for name in all_names:
        setattr(fake, name, getattr(arena_hero, name))
    if remove:
        for name in remove:
            if hasattr(fake, name):
                delattr(fake, name)
    if override:
        for name, value in override.items():
            setattr(fake, name, value)
    return fake


class FuturePlayerStatus(StrEnum):
    """A future PlayerStatus with a member the current adapter does not recognize."""

    ACTIVE = "ACTIVE"
    RESPAWNING = "RESPAWNING"
    FUTURE = "FUTURE"


class FutureUnitType(StrEnum):
    """A future UnitType with a member the current adapter does not recognize."""

    WORKER = "WORKER"
    VANGUARD = "VANGUARD"
    RANGER = "RANGER"
    TITAN = "TITAN"
