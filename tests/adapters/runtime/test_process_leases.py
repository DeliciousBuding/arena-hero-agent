from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from arena_hero_agent.adapters.runtime.process_leases import (
    FileWriterLeaseCoordinator,
    WriterLeaseError,
)
from arena_hero_agent.domain import DeadlineBudget, FencingToken, Generation, TenantId
from arena_hero_agent.ports import LeaseDisposition, WriterLease, WriterLeaseHandle

TENANT = TenantId("sample")
BUDGET = DeadlineBudget(1)
LEASE_DURATION_NS = 100


class ManualWallClock:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now

    def advance(self, nanoseconds: int) -> None:
        self.now += nanoseconds


def _coord(
    root: Path,
    clock: ManualWallClock,
    *,
    holder_id: str = "holder",
) -> FileWriterLeaseCoordinator:
    return FileWriterLeaseCoordinator(
        root,
        lease_duration_ns=LEASE_DURATION_NS,
        holder_id=holder_id,
        wall_clock=clock,
    )


def _record_path(root: Path) -> Path:
    return root / TENANT.value / "writer-lease.json"


# Static conformance exercised by `ty check`.
_writer_lease: WriterLease = FileWriterLeaseCoordinator(Path("unused"), lease_duration_ns=1)


async def test_two_contenders_only_one_writer(tmp_path: Path) -> None:
    clock = ManualWallClock()
    coordinator = _coord(tmp_path, clock)

    first, second = await asyncio.gather(
        coordinator.acquire_writer(TENANT, Generation(1), BUDGET),
        coordinator.acquire_writer(TENANT, Generation(1), BUDGET),
    )

    assert (first is None) != (second is None)
    winner = first if first is not None else second
    assert winner is not None
    assert isinstance(winner, WriterLeaseHandle)
    assert isinstance(coordinator, WriterLease)
    assert winner.disposition is LeaseDisposition.ACTIVE
    assert winner.fencing_token == FencingToken(1)
    assert winner.generation == Generation(1)
    await winner.release()


async def test_released_holder_allows_immediate_takeover_with_next_fence(tmp_path: Path) -> None:
    clock = ManualWallClock()
    coordinator = _coord(tmp_path, clock)

    first = await coordinator.acquire_writer(TENANT, Generation(1), BUDGET)
    assert first is not None
    await first.release()
    assert first.disposition is LeaseDisposition.RELEASED
    assert not await first.renew(BUDGET)

    assert await coordinator.acquire_writer(TENANT, Generation(2), BUDGET) is None
    second = await coordinator.replace_writer(
        TENANT,
        Generation(2),
        expected_fencing_token=first.fencing_token,
        budget=BUDGET,
    )
    assert second is not None
    assert second.fencing_token == FencingToken(2)
    assert second.generation == Generation(2)
    await second.release()


async def test_expired_lease_fails_closed_until_exact_fence_replacement(tmp_path: Path) -> None:
    clock = ManualWallClock()
    coordinator = _coord(tmp_path, clock)

    old = await coordinator.acquire_writer(TENANT, Generation(3), BUDGET)
    assert old is not None

    clock.advance(LEASE_DURATION_NS)
    assert old.disposition is LeaseDisposition.EXPIRED
    assert not await old.renew(BUDGET)

    # A live (even expired) holder owns the OS lock, so it cannot be replaced.
    assert (
        await coordinator.replace_writer(
            TENANT,
            Generation(4),
            expected_fencing_token=old.fencing_token,
            budget=BUDGET,
        )
        is None
    )
    assert await coordinator.acquire_writer(TENANT, Generation(4), BUDGET) is None

    # After the holder releases (or crashes), only the exact observed fence wins.
    await old.release()
    assert (
        await coordinator.replace_writer(
            TENANT,
            Generation(4),
            expected_fencing_token=FencingToken(99),
            budget=BUDGET,
        )
        is None
    )
    replacement = await coordinator.replace_writer(
        TENANT,
        Generation(4),
        expected_fencing_token=old.fencing_token,
        budget=BUDGET,
    )
    assert replacement is not None
    assert replacement.fencing_token == FencingToken(2)
    assert replacement.generation == Generation(4)
    await replacement.release()


async def test_replace_rejects_live_or_wrong_fence(tmp_path: Path) -> None:
    clock = ManualWallClock()
    coordinator = _coord(tmp_path, clock)

    current = await coordinator.acquire_writer(TENANT, Generation(1), BUDGET)
    assert current is not None

    clock.advance(50)
    too_early = await coordinator.replace_writer(
        TENANT,
        Generation(2),
        expected_fencing_token=current.fencing_token,
        budget=BUDGET,
    )
    assert too_early is None

    await current.release()
    clock.advance(50)
    wrong_fence = await coordinator.replace_writer(
        TENANT,
        Generation(2),
        expected_fencing_token=FencingToken(99),
        budget=BUDGET,
    )
    assert wrong_fence is None


async def test_fencing_token_monotonic_across_coordinator_instances(tmp_path: Path) -> None:
    clock = ManualWallClock()

    first = await _coord(tmp_path, clock, holder_id="instance-a").acquire_writer(
        TENANT, Generation(1), BUDGET
    )
    assert first is not None
    assert first.fencing_token == FencingToken(1)
    await first.release()

    second_coordinator = _coord(tmp_path, clock, holder_id="instance-b")
    second = await second_coordinator.replace_writer(
        TENANT,
        Generation(2),
        expected_fencing_token=first.fencing_token,
        budget=BUDGET,
    )
    assert second is not None
    assert second.fencing_token == FencingToken(2)
    await second.release()

    third = await _coord(tmp_path, clock, holder_id="instance-c").replace_writer(
        TENANT,
        Generation(3),
        expected_fencing_token=second.fencing_token,
        budget=BUDGET,
    )
    assert third is not None
    assert third.fencing_token == FencingToken(3)
    await third.release()


async def test_renew_extends_expiry_and_stale_handle_fails_closed(tmp_path: Path) -> None:
    clock = ManualWallClock()
    coordinator = _coord(tmp_path, clock)

    handle = await coordinator.acquire_writer(TENANT, Generation(1), BUDGET)
    assert handle is not None

    clock.advance(50)
    assert handle.disposition is LeaseDisposition.ACTIVE
    assert await handle.renew(BUDGET)

    clock.advance(50)
    assert handle.disposition is LeaseDisposition.ACTIVE
    assert await handle.renew(BUDGET)

    clock.advance(100)
    assert handle.disposition is LeaseDisposition.EXPIRED
    assert not await handle.renew(BUDGET)

    await handle.release()
    assert handle.disposition is LeaseDisposition.RELEASED
    assert not await handle.renew(BUDGET)


async def test_release_is_idempotent(tmp_path: Path) -> None:
    clock = ManualWallClock()
    coordinator = _coord(tmp_path, clock)

    handle = await coordinator.acquire_writer(TENANT, Generation(1), BUDGET)
    assert handle is not None
    await handle.release()
    await handle.release()

    successor = await coordinator.replace_writer(
        TENANT,
        Generation(2),
        expected_fencing_token=handle.fencing_token,
        budget=BUDGET,
    )
    assert successor is not None
    assert successor.fencing_token == FencingToken(2)
    await successor.release()


async def test_exhausted_budget_returns_none(tmp_path: Path) -> None:
    clock = ManualWallClock()
    coordinator = _coord(tmp_path, clock)
    exhausted = DeadlineBudget(0)

    assert await coordinator.acquire_writer(TENANT, Generation(1), exhausted) is None
    assert (
        await coordinator.replace_writer(
            TENANT,
            Generation(1),
            expected_fencing_token=FencingToken(1),
            budget=exhausted,
        )
        is None
    )


async def test_tenant_partitions_acquire_independently(tmp_path: Path) -> None:
    clock = ManualWallClock()
    coordinator = _coord(tmp_path, clock)

    first, second = await asyncio.gather(
        coordinator.acquire_writer(TenantId("t1"), Generation(1), BUDGET),
        coordinator.acquire_writer(TenantId("t2"), Generation(1), BUDGET),
    )

    assert first is not None
    assert second is not None
    await first.release()
    await second.release()


async def test_lease_record_is_versioned_and_preserves_fence_evidence(tmp_path: Path) -> None:
    clock = ManualWallClock()
    coordinator = _coord(tmp_path, clock, holder_id="holder-42")

    handle = await coordinator.acquire_writer(TENANT, Generation(5), BUDGET)
    assert handle is not None

    active = json.loads(_record_path(tmp_path).read_text(encoding="utf-8"))
    assert active["schemaVersion"] == 1
    assert active["tenantId"] == "sample"
    assert active["generation"] == 5
    assert active["fencingToken"] == 1
    assert active["holderId"] == "holder-42"
    assert active["expiresAtNs"] == LEASE_DURATION_NS

    clock.advance(10)
    await handle.release()
    released = json.loads(_record_path(tmp_path).read_text(encoding="utf-8"))
    assert released["fencingToken"] == 1
    assert released["expiresAtNs"] == 10


async def test_corrupt_record_fails_closed(tmp_path: Path) -> None:
    clock = ManualWallClock()
    coordinator = _coord(tmp_path, clock)
    record_path = _record_path(tmp_path)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(WriterLeaseError, match="malformed"):
        await coordinator.acquire_writer(TENANT, Generation(1), BUDGET)


def test_invalid_lease_duration_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lease_duration_ns"):
        FileWriterLeaseCoordinator(tmp_path, lease_duration_ns=0)


_CHILD_SCRIPT = textwrap.dedent(
    """\
    import asyncio
    import json
    import os
    import sys
    import time
    from pathlib import Path

    from arena_hero_agent.adapters.runtime.process_leases import FileWriterLeaseCoordinator
    from arena_hero_agent.domain import DeadlineBudget, FencingToken, Generation, TenantId

    async def main() -> None:
        root, tenant, duration_ns, mode = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
        coordinator = FileWriterLeaseCoordinator(
            root,
            lease_duration_ns=duration_ns,
            holder_id=f"child-{os.getpid()}",
        )
        tenant_id = TenantId(tenant)
        if mode == "replace":
            handle = None
            deadline = time.monotonic() + 10.0
            while handle is None and time.monotonic() < deadline:
                record = json.loads(
                    (Path(root) / tenant / "writer-lease.json").read_text(encoding="utf-8")
                )
                handle = await coordinator.replace_writer(
                    tenant_id,
                    Generation(1),
                    expected_fencing_token=FencingToken(record["fencingToken"]),
                    budget=DeadlineBudget(1),
                )
                if handle is None:
                    await asyncio.sleep(0.05)
            if handle is None:
                print("none")
                sys.stdout.flush()
                return
        else:
            handle = await coordinator.acquire_writer(
                tenant_id, Generation(1), DeadlineBudget(1)
            )
            if handle is None:
                print("none")
                sys.stdout.flush()
                return
        print(f"acquired:{handle.fencing_token.value}")
        sys.stdout.flush()
        if sys.stdin.readline().strip() == "release":
            await handle.release()

    asyncio.run(main())
    """
)


def _spawn_child(
    root: Path,
    tenant: str,
    duration_ns: int,
    mode: str,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_SCRIPT, str(root), tenant, str(duration_ns), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_line(process: subprocess.Popen[str]) -> str:
    assert process.stdout is not None
    line = process.stdout.readline()
    assert line, "child process exited without a lease outcome"
    return line.strip()


def test_two_os_processes_contend_for_one_tenant(tmp_path: Path) -> None:
    duration_ns = 30_000_000_000

    first = _spawn_child(tmp_path, "sample", duration_ns, "acquire")
    assert _read_line(first) == "acquired:1"

    second = _spawn_child(tmp_path, "sample", duration_ns, "acquire")
    assert _read_line(second) == "none"
    assert second.wait(timeout=15) == 0

    assert first.stdin is not None
    first.stdin.write("release\n")
    first.stdin.flush()
    assert first.wait(timeout=15) == 0

    third = _spawn_child(tmp_path, "sample", duration_ns, "replace")
    assert _read_line(third) == "acquired:2"
    assert third.stdin is not None
    third.stdin.write("release\n")
    third.stdin.flush()
    assert third.wait(timeout=15) == 0


def test_crashed_holder_fails_closed_then_exact_fence_replacement(tmp_path: Path) -> None:
    short_duration_ns = 150_000_000

    winner = _spawn_child(tmp_path, "sample", short_duration_ns, "acquire")
    assert _read_line(winner) == "acquired:1"
    winner.kill()
    winner.wait(timeout=15)

    coordinator = FileWriterLeaseCoordinator(
        tmp_path,
        lease_duration_ns=short_duration_ns,
        holder_id="recovery",
    )

    async def probe() -> None:
        # The durable active record blocks a plain acquire after a crash.
        assert await coordinator.acquire_writer(TENANT, Generation(1), BUDGET) is None
        # A stale observed fence is rejected even after expiry.
        assert (
            await coordinator.replace_writer(
                TENANT,
                Generation(1),
                expected_fencing_token=FencingToken(2),
                budget=BUDGET,
            )
            is None
        )
        # Once the crashed lease expires, the exact observed fence takes over.
        deadline = time.monotonic() + 10.0
        replacement = None
        while replacement is None and time.monotonic() < deadline:
            replacement = await coordinator.replace_writer(
                TENANT,
                Generation(1),
                expected_fencing_token=FencingToken(1),
                budget=BUDGET,
            )
            if replacement is None:
                await asyncio.sleep(0.05)
        assert replacement is not None, "crashed lease never expired"
        assert replacement.fencing_token == FencingToken(2)
        await replacement.release()

    asyncio.run(probe())
