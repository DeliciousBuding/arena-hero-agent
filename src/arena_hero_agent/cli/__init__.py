"""Console entrypoints and composition roots; no domain rules."""

from arena_hero_agent.cli.main import (
    DEFAULT_DATA_ROOT,
    HealthSnapshot,
    RunState,
    console_entrypoint,
    main,
)

__all__ = [
    "DEFAULT_DATA_ROOT",
    "HealthSnapshot",
    "RunState",
    "console_entrypoint",
    "main",
]
