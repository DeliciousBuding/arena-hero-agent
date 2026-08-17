"""Offline tick-loop recorder adapters: JSONL and SQLite backends."""

from __future__ import annotations

from enum import StrEnum

from arena_hero_agent.application import TickRecorder

from ._common import RecorderConfig, RecorderError
from .jsonl import JsonlTickRecorder
from .records import (
    RECORD_SCHEMA_VERSION,
    RECORD_TYPE_LOOP,
    RECORD_TYPE_TICK,
    RECORD_TYPE_TICK_STATE,
    parse_loop,
    parse_tick,
    parse_tick_state,
    serialize_loop,
    serialize_tick,
    serialize_tick_state,
)
from .sqlite import SqliteTickRecorder


class RecorderBackend(StrEnum):
    """Supported offline recorder storage backends."""

    JSONL = "jsonl"
    SQLITE = "sqlite"


def open_tick_recorder(
    config: RecorderConfig,
    *,
    backend: RecorderBackend | str = RecorderBackend.JSONL,
) -> TickRecorder:
    """Open an offline per-tenant recorder on the requested backend."""
    if backend == RecorderBackend.JSONL:
        return JsonlTickRecorder(config)
    if backend == RecorderBackend.SQLITE:
        return SqliteTickRecorder(config)
    raise RecorderError(f"unknown recorder backend: {backend!r}")


__all__ = [
    "RECORD_SCHEMA_VERSION",
    "RECORD_TYPE_LOOP",
    "RECORD_TYPE_TICK",
    "RECORD_TYPE_TICK_STATE",
    "JsonlTickRecorder",
    "RecorderBackend",
    "RecorderConfig",
    "RecorderError",
    "SqliteTickRecorder",
    "open_tick_recorder",
    "parse_loop",
    "parse_tick",
    "parse_tick_state",
    "serialize_loop",
    "serialize_tick",
    "serialize_tick_state",
]
