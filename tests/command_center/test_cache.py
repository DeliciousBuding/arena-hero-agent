"""TTL cache port semantics (legacy cache.ts TtlCache)."""

from __future__ import annotations

from typing import cast

import pytest

from arena_hero_agent.command_center import CommandCenterError, TtlCache


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_get_missing_returns_none() -> None:
    assert TtlCache(1000).get("missing") is None


def test_set_and_get_within_ttl() -> None:
    clock = _FakeClock()
    cache = TtlCache[int](1000, clock=clock)
    cache.set("k", 7)
    assert cache.get("k") == 7
    clock.now = 0.999
    assert cache.get("k") == 7


def test_get_evicts_expired_entry() -> None:
    clock = _FakeClock()
    cache = TtlCache[int](1000, clock=clock)
    cache.set("k", 7)
    clock.now = 1.001
    assert cache.get("k") is None
    assert cache.size() == 0


def test_get_or_load_caches_loader_result() -> None:
    clock = _FakeClock()
    cache = TtlCache[int](1000, clock=clock)
    loads: list[int] = []
    first = cache.get_or_load("k", lambda: (loads.append(1), 42)[1])
    second = cache.get_or_load("k", lambda: (loads.append(2), 99)[1])
    assert (first, second) == (42, 42)
    assert loads == [1]


def test_get_or_load_retries_after_expiry() -> None:
    clock = _FakeClock()
    cache = TtlCache[int](1000, clock=clock)
    cache.get_or_load("k", lambda: 1)
    clock.now = 2.0
    assert cache.get_or_load("k", lambda: 2) == 2


def test_loader_error_is_not_cached() -> None:
    cache = TtlCache[int](1000)

    def boom() -> int:
        raise RuntimeError("load failed")

    with pytest.raises(RuntimeError, match="load failed"):
        cache.get_or_load("k", boom)
    assert cache.size() == 0
    with pytest.raises(RuntimeError, match="load failed"):
        cache.get_or_load("k", boom)


def test_invalidate_single_key_and_all() -> None:
    clock = _FakeClock()
    cache = TtlCache[int](1000, clock=clock)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.invalidate("a")
    assert cache.get("a") is None
    assert cache.get("b") == 2
    cache.invalidate()
    assert cache.size() == 0


def test_size_reflects_live_entries() -> None:
    cache = TtlCache[int](1000)
    assert cache.size() == 0
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.size() == 2


@pytest.mark.parametrize("bad", [True, -1, 1.5, "1000"])
def test_invalid_ttl_raises(bad: object) -> None:
    with pytest.raises(CommandCenterError, match="ttl_ms"):
        TtlCache(cast(int, bad))
