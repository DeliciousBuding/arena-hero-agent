"""Shared fault-injection helpers: real subprocess kills and child scripts.

The fault suite deliberately injects real faults:

- SIGKILL scenarios spawn a real Python child (``sys.executable -c``) that holds
  a subsystem resource (writer lease, migration plan writer, recorder lock) and
  hard-kill it with ``Popen.kill()`` (TerminateProcess on Windows, SIGKILL on
  POSIX). Mocking a signal would not prove crash recovery, so the suite never
  mocks the unit under test; the child is the real production code.
- Disk/IO faults corrupt on-disk state (garbage SQLite bytes, half-written tmp
  files, torn JSONL tails) or inject an ``OSError`` at the exact IO boundary
  (``append_jsonl_line``) the production path already guards.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

LEASE_CHILD = textwrap.dedent(
    """\
    import asyncio
    import os
    import sys

    from arena_hero_agent.adapters.runtime.process_leases import FileWriterLeaseCoordinator
    from arena_hero_agent.domain import DeadlineBudget, Generation, TenantId

    async def main() -> None:
        root, tenant, duration_ns = sys.argv[1], sys.argv[2], int(sys.argv[3])
        coordinator = FileWriterLeaseCoordinator(
            root,
            lease_duration_ns=duration_ns,
            holder_id=f"child-{os.getpid()}",
        )
        handle = await coordinator.acquire_writer(
            TenantId(tenant), Generation(1), DeadlineBudget(1)
        )
        if handle is None:
            print("none")
            sys.stdout.flush()
            return
        print(f"acquired:{handle.fencing_token.value}")
        sys.stdout.flush()
        for line in sys.stdin:
            action = line.strip()
            if action == "renew":
                ok = await handle.renew(DeadlineBudget(1))
                print(f"renewed:{int(ok)}")
                sys.stdout.flush()
            elif action == "release":
                await handle.release()
                print("released")
                sys.stdout.flush()
                return

    asyncio.run(main())
    """
)


STORE_WRITER_CHILD = textwrap.dedent(
    """\
    import asyncio
    import dataclasses
    import os
    import sys
    from pathlib import Path

    from arena_hero_agent.adapters.runtime.process_leases import FileWriterLeaseCoordinator
    from arena_hero_agent.domain import DeadlineBudget, FencingToken, Generation, TenantId
    from arena_hero_agent.migration.store import MigrationPlanStore

    async def main() -> None:
        root, tenant, duration_ns, observed_fence = (
            sys.argv[1],
            sys.argv[2],
            int(sys.argv[3]),
            int(sys.argv[4]),
        )
        coordinator = FileWriterLeaseCoordinator(
            root,
            lease_duration_ns=duration_ns,
            holder_id=f"child-{os.getpid()}",
        )
        store = MigrationPlanStore(Path(root))
        handle = await coordinator.replace_writer(
            TenantId(tenant),
            Generation(1),
            expected_fencing_token=FencingToken(observed_fence),
            budget=DeadlineBudget(1),
        )
        if handle is None:
            print("no-lease")
            sys.stdout.flush()
            return
        plan = store.read_plan(tenant)
        if plan is None:
            print("no-plan")
            sys.stdout.flush()
            return
        revision = plan.revision
        while True:
            revision += 1
            updated = dataclasses.replace(
                plan,
                revision=revision,
                conductor_epoch=handle.fencing_token.value,
            )
            store.write_plan(updated, lease=handle)
            print(f"wrote:{revision}")
            sys.stdout.flush()
            await asyncio.sleep(0.005)

    asyncio.run(main())
    """
)


RECORDER_LOCK_CHILD = textwrap.dedent(
    """\
    import sys

    from arena_hero_agent.adapters.recorder import RecorderConfig, SqliteTickRecorder
    from arena_hero_agent.domain import TenantId

    def main() -> None:
        root, tenant = sys.argv[1], sys.argv[2]
        recorder = SqliteTickRecorder(
            RecorderConfig(data_root=root, tenant_id=TenantId(tenant))
        )
        print("opened")
        sys.stdout.flush()
        for line in sys.stdin:
            if line.strip() == "close":
                recorder.close()
                print("closed")
                sys.stdout.flush()
                return

    main()
    """
)


def spawn_python(script: str, *args: str) -> subprocess.Popen[str]:
    """Start a real Python child running ``script`` with ``args``."""
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def read_line(process: subprocess.Popen[str]) -> str:
    """Read one child stdout line; fails loudly if the child exited early."""
    assert process.stdout is not None
    line = process.stdout.readline()
    assert line, "child process exited without expected output"
    return line.strip()


def write_line(process: subprocess.Popen[str], text: str) -> None:
    """Write one stdin line to the child."""
    assert process.stdin is not None
    process.stdin.write(f"{text}\n")
    process.stdin.flush()


def hard_kill(process: subprocess.Popen[str]) -> None:
    """SIGKILL-equivalent hard kill: TerminateProcess on Windows, SIGKILL elsewhere."""
    process.kill()
    process.wait(timeout=15)


def reap(process: subprocess.Popen[str]) -> None:
    """Ensure a child is gone so tests never leak processes on failure."""
    if process.poll() is None:
        process.kill()
        process.wait(timeout=15)
