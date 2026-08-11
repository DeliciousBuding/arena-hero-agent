"""Shared coercion helpers for the Command Center projections (P5-4).

The TypeScript oracle repeats a small ``num`` coercion in every projection
file (finite numbers pass through, numeric strings parse, anything else is
``0``) so a missing or unknown field never poisons an aggregate. The Python
ports share one implementation instead of duplicating it per module.
"""

from __future__ import annotations

import math
import time

__all__ = ["current_epoch_ms", "finite_number", "num"]


def num(value: object) -> int | float:
    """Coerce a runtime artifact value to a finite number (TS ``num`` helper).

    A finite number passes through unchanged (integral floats become ints so
    JSON output matches the TS ``JSON.stringify`` shape), a non-empty numeric
    string is parsed, and anything else becomes ``0``.
    """
    parsed = finite_number(value)
    return 0 if parsed is None else parsed


def finite_number(value: object) -> int | float | None:
    """Like ``num`` but returns ``None`` for non-finite input (TS ``Number`` + isFinite)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str) and value.strip():
        try:
            parsed = float(value)
        except ValueError:
            return None
        if not math.isfinite(parsed):
            return None
        return int(parsed) if parsed.is_integer() else parsed
    return None


def current_epoch_ms() -> int:
    """Current wall clock in epoch milliseconds (TS ``Date.now()``)."""
    return time.time_ns() // 1_000_000
