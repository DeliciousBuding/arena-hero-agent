"""Generic in-memory TTL cache (port of legacy ``cache.ts`` ``TtlCache``).

Semantics match the TypeScript oracle:

- ``get`` returns ``None`` for a missing or expired key and evicts expired
  entries lazily;
- ``set`` records the value with the current clock reading;
- ``get_or_load`` returns a live hit, otherwise loads, caches, and returns the
  value; a raising loader is never cached and the next call retries;
- ``invalidate`` clears one key or the whole cache; ``size`` reports the
  current entry count.

The clock is injectable for deterministic tests. The TS oracle uses
``Date.now()`` (epoch milliseconds); this port uses a monotonic seconds clock
by default, which preserves TTL semantics without depending on wall-clock
adjustments.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Generic, TypeVar

from .errors import CommandCenterError

T = TypeVar("T")


class TtlCache(Generic[T]):
    """Time-to-live key/value store with lazy expiry."""

    def __init__(self, ttl_ms: int, *, clock: Callable[[], float] | None = None) -> None:
        if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, int):
            raise CommandCenterError(f"ttl_ms must be an integer; actual={ttl_ms!r}")
        if ttl_ms < 0:
            raise CommandCenterError(f"ttl_ms cannot be negative; actual={ttl_ms}")
        self._ttl_ms = ttl_ms
        self._clock = clock if clock is not None else time.monotonic
        self._items: dict[str, tuple[T, float]] = {}

    def get(self, key: str) -> T | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        value, at = entry
        if (self._clock() - at) * 1000.0 > self._ttl_ms:
            del self._items[key]
            return None
        return value

    def set(self, key: str, value: T) -> None:
        self._items[key] = (value, self._clock())

    def get_or_load(self, key: str, load: Callable[[], T]) -> T:
        """Return a live hit, otherwise load and cache; loader errors propagate."""
        hit = self.get(key)
        if hit is not None:
            return hit
        value = load()
        self._items[key] = (value, self._clock())
        return value

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._items.clear()
        else:
            self._items.pop(key, None)

    def size(self) -> int:
        return len(self._items)


__all__ = ["TtlCache"]
