"""Shared recorder configuration, errors, and single-writer enforcement."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO

from arena_hero_agent.domain import TenantId


class RecorderError(Exception):
    """Recorder configuration, contention, corruption, or IO failure."""


def validate_data_root(data_root: str | os.PathLike[str]) -> Path:
    r"""Validate a caller-supplied data root with portable input semantics.

    Mirrors the telemetry writer's path hardening: both ``/`` and ``\`` are
    treated as separators for validation on every OS so a Windows-spelled
    path cannot hide traversal from a POSIX host (or vice versa). Directories
    are allowed here; concrete target files under the root are hardened by
    the JSONL append primitive and the SQLite layer.
    """
    if not isinstance(data_root, (str, os.PathLike)):
        raise RecorderError(f"data_root must be str or PathLike; actual={type(data_root).__name__}")
    text = os.fspath(data_root)
    if not isinstance(text, str):
        raise RecorderError(f"data_root must resolve to str; actual={type(text).__name__}")
    if not text:
        raise RecorderError("data_root must not be empty")
    if "\x00" in text:
        raise RecorderError("data_root must not contain NUL bytes")
    windows_view = PureWindowsPath(text)
    posix_view = PurePosixPath(text)
    if any(part == ".." for part in windows_view.parts) or any(
        part == ".." for part in posix_view.parts
    ):
        raise RecorderError("data_root must not contain '..' traversal components")
    return Path(text)


@dataclass(frozen=True, slots=True)
class RecorderConfig:
    """Offline recorder configuration; paths are injected, never hard-coded."""

    data_root: str | os.PathLike[str]
    tenant_id: TenantId
    busy_timeout_ms: int = 2000

    def __post_init__(self) -> None:
        validate_data_root(self.data_root)
        if not isinstance(self.tenant_id, TenantId):
            raise RecorderError(
                f"tenant_id must be a TenantId; actual={type(self.tenant_id).__name__}"
            )
        if isinstance(self.busy_timeout_ms, bool) or not isinstance(self.busy_timeout_ms, int):
            raise RecorderError(
                f"busy_timeout_ms must be an integer; actual={self.busy_timeout_ms!r}"
            )
        if self.busy_timeout_ms < 0:
            raise RecorderError("busy_timeout_ms cannot be negative")


def jsonl_target_path(config: RecorderConfig) -> Path:
    """Resolve the JSONL target file for one tenant under the data root."""
    return validate_data_root(config.data_root) / config.tenant_id.value / "ticks.jsonl"


def sqlite_target_path(config: RecorderConfig) -> Path:
    """Resolve the SQLite target file for one tenant under the data root."""
    return validate_data_root(config.data_root) / config.tenant_id.value / "ticks.sqlite3"


# ---------------------------------------------------------------------------
# Single-writer enforcement
# ---------------------------------------------------------------------------

_active_targets: dict[str, object] = {}
_registry_lock = threading.Lock()


def register_target(key: str, owner: object) -> None:
    """Claim a resolved recorder target; a live claim raises loudly."""
    with _registry_lock:
        existing = _active_targets.get(key)
        if existing is not None:
            raise RecorderError(f"another active recorder already owns {key}")
        _active_targets[key] = owner


def unregister_target(key: str, owner: object) -> None:
    """Release a recorder target claimed by ``owner`` (idempotent)."""
    with _registry_lock:
        if _active_targets.get(key) is owner:
            del _active_targets[key]


def acquire_process_lock(path: Path) -> BinaryIO:
    """Acquire an advisory cross-process lock on a sidecar lock file.

    The lock file is a zero-byte regular file next to the recorder target. A
    second process (or a second open handle in this process) fails loudly
    with ``RecorderError`` instead of interleaving appends. The returned
    handle must be passed to :func:`release_process_lock` on close.
    """
    handle: BinaryIO = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RecorderError(f"another process owns recorder target {path}") from exc
    return handle


def release_process_lock(handle: BinaryIO) -> None:
    """Release and close a lock handle from :func:`acquire_process_lock`."""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
