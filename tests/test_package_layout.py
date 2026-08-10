from __future__ import annotations

import importlib

MODULES = (
    "domain",
    "ports",
    "application",
    "adapters",
    "strategies",
    "planning",
    "alliance",
    "migration",
    "intelligence",
    "learning.runtime",
    "control",
    "telemetry",
    "command_center.api",
    "command_center.services",
    "command_center.projections",
    "command_center.streams",
    "cli",
)


def test_architecture_packages_are_importable() -> None:
    for module in MODULES:
        importlib.import_module(f"arena_hero_agent.{module}")
