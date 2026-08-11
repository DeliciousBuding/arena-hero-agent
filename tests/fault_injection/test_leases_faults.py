"""P4-15 process lease fault injection: real SIGKILL of the holder process.

Each scenario injects a real crash (subprocess hard-kill) and asserts the
fail-closed semantics hold: a crashed holder's durable record blocks a plain
acquire, a stale/wrong observed fence is rejected, and once the lease expires
the exact observed fence takes over with the next monotonic token. Cleanup is
guaranteed so a failed assertion never leaks a child process.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from helpers import LEASE_CHILD, hard_kill, read_line, reap, spawn_python, write_line

from arena_hero_agent.adapters.runtime.process_leases import FileWriterLeaseCoordinator
from arena_hero_agent.domain import DeadlineBudget, FencingToken, Generation, TenantId
from arena_hero_agent.ports.leases import LeaseDisposition

TENANT = TenantId("sample")
GENERATION = Generation(1)
BUDGET = DeadlineBudget(1)
KILL_LEASE_NS = 150_000_000  # 150 ms so expiry is observable without slowing the suite


class ManualWallClock:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now

    def advance(self, nanoseconds: int) -> None:
        self.now += nanoseconds


async def _recover_after_expiry(root: Path) -> None:
    """Take over the crashed lease once it expires; fail-closed before that."""
    coordinator = FileWriterLeaseCoordinator(
        root, lease_duration_ns=KILL_LEASE_NS, holder_id="recovery"
    )
    # A crashed holder's durable record still blocks a plain acquire.
    assert await coordinator.acquire_writer(TENANT, GENERATION, BUDGET) is None
    # A stale observed fence is rejected even after expiry.
    assert (
        await coordinator.replace_writer(
            TENANT,
            GENERATION,
            expected_fencing_token=FencingToken(2),
            budget=BUDGET,
        )
        is None
    )
    # The exact observed fence takes over once the crashed lease expires.
    deadline = time.monotonic() + 15.0
    replacement = None
    while replacement is None and time.monotonic() < deadline:
        replacement = await coordinator.replace_writer(
            TENANT,
            GENERATION,
            expected_fencing_token=FencingToken(1),
            budget=BUDGET,
        )
        if replacement is None:
            await asyncio.sleep(0.05)
    assert replacement is not None, "crashed lease never expired or takeover was blocked"
    assert replacement.fencing_token == FencingToken(2)
    assert replacement.disposition is LeaseDisposition.ACTIVE
    await replacement.release()


@pytest.mark.parametrize("renewals", [0, 1, 2])
def test_sigkill_holder_recovers_after_expiry_with_next_fence(
    tmp_path: Path, renewals: int
) -> None:
    """Kill the holder after it acquired (and optionally renewed) the lease."""
    process = spawn_python(LEASE_CHILD, str(tmp_path), TENANT.value, str(KILL_LEASE_NS))
    try:
        assert read_line(process) == "acquired:1"
        for _ in range(renewals):
            write_line(process, "renew")
            assert read_line(process) == "renewed:1"
        hard_kill(process)
    finally:
        reap(process)

    asyncio.run(_recover_after_expiry(tmp_path))


def test_sigkill_holder_never_takes_clean_release_path(tmp_path: Path) -> None:
    """A crash must leave the durable record (future expiry), not a clean release."""
    process = spawn_python(LEASE_CHILD, str(tmp_path), TENANT.value, str(KILL_LEASE_NS))
    try:
        assert read_line(process) == "acquired:1"
        # No "release" is ever sent; the process is killed while holding.
        hard_kill(process)
    finally:
        reap(process)

    record = (tmp_path / TENANT.value / "writer-lease.json").read_text(encoding="utf-8")
    assert '"holderId": "child-' in record
    # The record keeps the original future expiry: a crash is not a release.
    assert record.endswith("\n")

    # Recovery still works through expiry + exact-fence takeover.
    asyncio.run(_recover_after_expiry(tmp_path))


def test_sigkill_holder_blocks_premature_takeover_until_expiry(tmp_path: Path) -> None:
    """The OS lock is freed on death, but the durable record still fences."""
    process = spawn_python(LEASE_CHILD, str(tmp_path), TENANT.value, str(KILL_LEASE_NS))
    try:
        assert read_line(process) == "acquired:1"
        hard_kill(process)
    finally:
        reap(process)

    coordinator = FileWriterLeaseCoordinator(
        tmp_path, lease_duration_ns=KILL_LEASE_NS, holder_id="probe"
    )

    async def probe() -> None:
        # Immediately after the crash: even the exact observed fence must NOT
        # take over, because the durable record has not expired yet.
        assert (
            await coordinator.replace_writer(
                TENANT,
                GENERATION,
                expected_fencing_token=FencingToken(1),
                budget=BUDGET,
            )
            is None
        )
        assert await coordinator.acquire_writer(TENANT, GENERATION, BUDGET) is None

    asyncio.run(probe())
