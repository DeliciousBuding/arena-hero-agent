"""Cross-process fenced writer leases: OS process lock plus a versioned durable record.

P4-15 extends the P4-9 in-process writer lease to multiple processes sharing one
tenant. Mutual exclusion comes from the P4-5 ``acquire_process_lock`` pattern: a
per-tenant sidecar lock file is held for the whole lease lifetime, so a second
instance can neither acquire nor replace while the holder lives. Fencing
monotonicity survives restarts because every acquisition writes a versioned
lease record carrying the last issued fence; the record is never deleted, so a
takeover always issues ``observed + 1``.

Semantics:

- ``acquire_writer`` succeeds only for a tenant with no lease record yet (fence
  starts at one). Any existing record — live, expired, or cleanly released —
  blocks acquire so a caller cannot bypass fencing evidence.
- ``replace_writer`` takes over only the exact expired holder: the caller must
  present the observed fencing token and the record must be past expiry. Live
  leases, wrong fences, and fresh tenants all return ``None``.
- A clean ``release`` keeps the fencing token and marks the record immediately
  expired (``expiresAtNs = now``), so the next instance can take over at once
  with the next fence. A crash leaves the original expiry: the next instance
  waits out the lease and then replaces with the exact observed fence.
- A lost lease is fail-closed: expiry, release, or replacement invalidates the
  handle, and every downstream write path refuses a non-``ACTIVE`` handle.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from arena_hero_agent.adapters.recorder._common import (
    RecorderError,
    acquire_process_lock,
    release_process_lock,
    validate_data_root,
)
from arena_hero_agent.domain import DeadlineBudget, FencingToken, Generation, TenantId
from arena_hero_agent.ports.leases import LeaseDisposition

_LEASE_SCHEMA_VERSION = 1
_RECORD_FILENAME = "writer-lease.json"
_LOCK_FILENAME = "writer-lease.lock"
_UNKNOWN_HOLDER = "unknown"


class WriterLeaseError(RuntimeError):
    """Fail-closed failure when a versioned writer lease record is unreadable."""


@dataclass(frozen=True, slots=True)
class _LeaseRecord:
    """Durable evidence for one tenant writer lease; never deleted once written."""

    schema_version: int
    tenant_id: str
    generation: int
    fencing_token: int
    holder_id: str
    expires_at_ns: int

    def to_json_object(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "tenantId": self.tenant_id,
            "generation": self.generation,
            "fencingToken": self.fencing_token,
            "holderId": self.holder_id,
            "expiresAtNs": self.expires_at_ns,
        }


def _read_record(path: Path) -> _LeaseRecord | None:
    """Read the versioned record, or ``None`` when the tenant has no record yet.

    A missing file is a fresh tenant. Any present-but-unreadable or unsupported
    record fails closed instead of guessing a fencing token.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise WriterLeaseError(f"writer lease record is unreadable: {path}") from error
    try:
        payload = json.loads(text)
    except ValueError as error:
        raise WriterLeaseError(f"writer lease record is malformed: {path}") from error
    if not isinstance(payload, dict):
        raise WriterLeaseError(f"writer lease record is malformed: {path}")
    try:
        if payload.get("schemaVersion") != _LEASE_SCHEMA_VERSION:
            raise WriterLeaseError(f"writer lease record has an unsupported schema: {path}")
        tenant_id = payload["tenantId"]
        generation = payload["generation"]
        fencing_token = payload["fencingToken"]
        holder_id = payload["holderId"]
        expires_at_ns = payload["expiresAtNs"]
        if (
            not isinstance(tenant_id, str)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or not isinstance(holder_id, str)
            or isinstance(expires_at_ns, bool)
            or not isinstance(expires_at_ns, int)
        ):
            raise WriterLeaseError(f"writer lease record is malformed: {path}")
    except KeyError as error:
        raise WriterLeaseError(f"writer lease record is malformed: {path}") from error
    return _LeaseRecord(
        schema_version=_LEASE_SCHEMA_VERSION,
        tenant_id=tenant_id,
        generation=generation,
        fencing_token=fencing_token,
        holder_id=holder_id,
        expires_at_ns=expires_at_ns,
    )


def _write_record(path: Path, record: _LeaseRecord) -> None:
    """Atomically persist a record: write a temp file, then replace the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(record.to_json_object(), sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


class FileWriterLeaseHandle:
    """Writer handle backed by a held OS process lock and a durable record."""

    def __init__(
        self,
        coordinator: FileWriterLeaseCoordinator,
        *,
        tenant_id: TenantId,
        generation: Generation,
        fencing_token: FencingToken,
        holder_id: str,
        expires_at_ns: int,
        lock: BinaryIO,
    ) -> None:
        self._coordinator = coordinator
        self._tenant_id = tenant_id
        self._generation = generation
        self._fencing_token = fencing_token
        self._holder_id = holder_id
        self._expires_at_ns = expires_at_ns
        self._lock: BinaryIO | None = lock
        self._disposition = LeaseDisposition.ACTIVE

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id

    @property
    def generation(self) -> Generation:
        return self._generation

    @property
    def fencing_token(self) -> FencingToken:
        return self._fencing_token

    @property
    def disposition(self) -> LeaseDisposition:
        if self._disposition is not LeaseDisposition.ACTIVE:
            return self._disposition
        if self._coordinator._wall_clock() >= self._expires_at_ns:
            return LeaseDisposition.EXPIRED
        return LeaseDisposition.ACTIVE

    async def renew(self, budget: DeadlineBudget) -> bool:
        return await self._coordinator._renew(self, budget)

    async def release(self) -> None:
        await self._coordinator._release(self)


class FileWriterLeaseCoordinator:
    """Cross-process tenant writer lease: an OS lock plus a versioned record.

    All record mutations happen while holding the per-tenant OS process lock, so
    at most one process can own a tenant's write authority at any time. Fencing
    tokens are monotonic across restarts because the durable record is the only
    source of the next token and it is never deleted.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        lease_duration_ns: int,
        holder_id: str = "",
        wall_clock: Callable[[], int] | None = None,
    ) -> None:
        if isinstance(lease_duration_ns, bool) or lease_duration_ns < 1:
            raise ValueError("lease_duration_ns must be positive")
        self._root = validate_data_root(root)
        self._lease_duration_ns = lease_duration_ns
        self._holder_id = holder_id if holder_id else _UNKNOWN_HOLDER
        self._wall_clock = wall_clock if wall_clock is not None else time.time_ns
        self._lock = asyncio.Lock()

    async def acquire_writer(
        self,
        tenant_id: TenantId,
        generation: Generation,
        budget: DeadlineBudget,
    ) -> FileWriterLeaseHandle | None:
        if budget.exhausted:
            return None
        async with self._lock:
            lock = self._try_lock(tenant_id)
            if lock is None:
                return None
            handle: FileWriterLeaseHandle | None = None
            try:
                record = _read_record(self._record_path(tenant_id))
                if record is not None:
                    return None
                handle = self._issue(lock, tenant_id, generation, FencingToken(1))
                return handle
            finally:
                if handle is None:
                    release_process_lock(lock)

    async def replace_writer(
        self,
        tenant_id: TenantId,
        generation: Generation,
        *,
        expected_fencing_token: FencingToken,
        budget: DeadlineBudget,
    ) -> FileWriterLeaseHandle | None:
        if budget.exhausted:
            return None
        async with self._lock:
            lock = self._try_lock(tenant_id)
            if lock is None:
                return None
            handle: FileWriterLeaseHandle | None = None
            try:
                record = _read_record(self._record_path(tenant_id))
                if (
                    record is None
                    or record.fencing_token != expected_fencing_token.value
                    or self._wall_clock() < record.expires_at_ns
                ):
                    return None
                handle = self._issue(lock, tenant_id, generation, expected_fencing_token.next())
                return handle
            finally:
                if handle is None:
                    release_process_lock(lock)

    def _try_lock(self, tenant_id: TenantId) -> BinaryIO | None:
        """Acquire the per-tenant OS process lock, fail-closed on contention."""
        lock_path = self._lock_path(tenant_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return acquire_process_lock(lock_path)
        except RecorderError:
            return None

    def _issue(
        self,
        lock: BinaryIO,
        tenant_id: TenantId,
        generation: Generation,
        fencing_token: FencingToken,
    ) -> FileWriterLeaseHandle:
        expires_at_ns = self._wall_clock() + self._lease_duration_ns
        _write_record(
            self._record_path(tenant_id),
            _LeaseRecord(
                schema_version=_LEASE_SCHEMA_VERSION,
                tenant_id=tenant_id.value,
                generation=generation.value,
                fencing_token=fencing_token.value,
                holder_id=self._holder_id,
                expires_at_ns=expires_at_ns,
            ),
        )
        return FileWriterLeaseHandle(
            self,
            tenant_id=tenant_id,
            generation=generation,
            fencing_token=fencing_token,
            holder_id=self._holder_id,
            expires_at_ns=expires_at_ns,
            lock=lock,
        )

    async def _renew(self, handle: FileWriterLeaseHandle, budget: DeadlineBudget) -> bool:
        if budget.exhausted:
            return False
        async with self._lock:
            if handle._disposition is not LeaseDisposition.ACTIVE:
                return False
            if self._wall_clock() >= handle._expires_at_ns:
                handle._disposition = LeaseDisposition.EXPIRED
                return False
            expires_at_ns = self._wall_clock() + self._lease_duration_ns
            handle._expires_at_ns = expires_at_ns
            _write_record(
                self._record_path(handle.tenant_id),
                _LeaseRecord(
                    schema_version=_LEASE_SCHEMA_VERSION,
                    tenant_id=handle.tenant_id.value,
                    generation=handle.generation.value,
                    fencing_token=handle.fencing_token.value,
                    holder_id=handle._holder_id,
                    expires_at_ns=expires_at_ns,
                ),
            )
            return True

    async def _release(self, handle: FileWriterLeaseHandle) -> None:
        async with self._lock:
            if handle._disposition is LeaseDisposition.RELEASED:
                return
            # Keep the fencing token and expire immediately so the next instance
            # can take over (via replace with this exact fence) without waiting.
            _write_record(
                self._record_path(handle.tenant_id),
                _LeaseRecord(
                    schema_version=_LEASE_SCHEMA_VERSION,
                    tenant_id=handle.tenant_id.value,
                    generation=handle.generation.value,
                    fencing_token=handle.fencing_token.value,
                    holder_id=handle._holder_id,
                    expires_at_ns=self._wall_clock(),
                ),
            )
            handle._disposition = LeaseDisposition.RELEASED
            if handle._lock is not None:
                release_process_lock(handle._lock)
                handle._lock = None

    def _record_path(self, tenant_id: TenantId) -> Path:
        return self._root / tenant_id.value / _RECORD_FILENAME

    def _lock_path(self, tenant_id: TenantId) -> Path:
        return self._root / tenant_id.value / _LOCK_FILENAME


__all__ = [
    "FileWriterLeaseCoordinator",
    "FileWriterLeaseHandle",
    "WriterLeaseError",
]
