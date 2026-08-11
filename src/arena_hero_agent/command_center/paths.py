"""Command Center shared data paths and tenant validation (P5-3).

Mirrors the legacy TypeScript ``fs-jsonl.ts`` path helpers and tenant set. The
shared data root is resolved through ``ARENA_DATA_ROOT`` (default: the
coordination repository ``data/`` directory) and is never hard-coded per
machine; concrete paths stay under the injected root.

Fail-closed rules:

- The data root must be non-empty, NUL-free, and free of ``..`` traversal
  components. Both ``/`` and ``\\`` are treated as separators for validation on
  every OS so a Windows-spelled path cannot hide traversal from a POSIX host
  (or vice versa), matching the recorder's path hardening.
- Runtime tenants are the production set ``t1..t4`` (TS ``TENANTS``). Survey
  databases additionally accept the ``sim-*`` simulation namespace used by
  ``/api/ingest/agents``. Anything else raises instead of guessing.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from arena_hero_agent.domain import TenantId

from .errors import CommandCenterError

TENANTS: tuple[str, ...] = ("t1", "t2", "t3", "t4")
SIM_TENANT_PREFIX = "sim-"
DATA_ROOT_ENV = "ARENA_DATA_ROOT"

# Relative fallback from the package directory to the coordination repo root.
_REPO_MARKER = "pyproject.toml"
_REPO_DATA_DIR = "data"


def validate_data_root(data_root: str | os.PathLike[str]) -> Path:
    """Validate a caller-supplied data root with portable input semantics."""
    if not isinstance(data_root, (str, os.PathLike)):
        raise CommandCenterError(
            f"data_root must be str or PathLike; actual={type(data_root).__name__}"
        )
    text = os.fspath(data_root)
    if not isinstance(text, str):
        raise CommandCenterError(f"data_root must resolve to str; actual={type(text).__name__}")
    if not text:
        raise CommandCenterError("data_root must not be empty")
    if "\x00" in text:
        raise CommandCenterError("data_root must not contain NUL bytes")
    windows_view = PureWindowsPath(text)
    posix_view = PurePosixPath(text)
    if any(part == ".." for part in windows_view.parts) or any(
        part == ".." for part in posix_view.parts
    ):
        raise CommandCenterError("data_root must not contain '..' traversal components")
    return Path(text)


def _default_data_root() -> Path:
    """Resolve the coordination repo ``data/`` directory for a checkout."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / _REPO_MARKER).is_file():
            return candidate / _REPO_DATA_DIR
    raise CommandCenterError(
        f"unable to resolve the coordination data/ directory from {here}; "
        f"set {DATA_ROOT_ENV} explicitly"
    )


def resolve_data_root(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the shared data root: explicit override, env, then repo default."""
    if override is not None:
        return validate_data_root(override)
    env_value = os.environ.get(DATA_ROOT_ENV)
    if env_value:
        return validate_data_root(env_value)
    return validate_data_root(_default_data_root())


def normalize_tenant(tenant: str | TenantId) -> str:
    """Validate and normalize a runtime tenant to its string form."""
    if isinstance(tenant, TenantId):
        value = tenant.value
    elif isinstance(tenant, str):
        value = tenant
    else:
        raise CommandCenterError(f"tenant must be str or TenantId; actual={type(tenant).__name__}")
    try:
        TenantId(value)
    except ValueError as exc:
        raise CommandCenterError(f"invalid tenant {value!r}: {exc}") from exc
    return value


def validate_tenant(tenant: str | TenantId) -> str:
    """Fail-closed runtime tenant check: must be one of the production tenants."""
    value = normalize_tenant(tenant)
    if value not in TENANTS:
        raise CommandCenterError(
            f"tenant {value!r} is not a runtime tenant; expected one of {TENANTS}"
        )
    return value


def validate_survey_tenant(tenant: str | TenantId) -> str:
    """Fail-closed survey tenant check: production tenants or the sim-* namespace."""
    value = normalize_tenant(tenant)
    if value not in TENANTS and not value.startswith(SIM_TENANT_PREFIX):
        raise CommandCenterError(
            f"tenant {value!r} is not a survey tenant; expected t1..t4 or {SIM_TENANT_PREFIX}*"
        )
    return value


def runtime_dir(data_root: str | os.PathLike[str], tenant: str | TenantId) -> Path:
    """Per-tenant runtime directory: ``<data_root>/runtime/<tenant>``."""
    root = validate_data_root(data_root)
    return root / "runtime" / validate_tenant(tenant)


def calibration_dir(data_root: str | os.PathLike[str], tenant: str | TenantId) -> Path:
    """Per-tenant calibration directory: ``<data_root>/runtime/<tenant>/calibration``."""
    return runtime_dir(data_root, tenant) / "calibration"


def telemetry_dir(data_root: str | os.PathLike[str], tenant: str | TenantId) -> Path:
    """Per-tenant telemetry directory: ``<data_root>/runtime/<tenant>/telemetry``."""
    return runtime_dir(data_root, tenant) / "telemetry"


def human_commands_dir(data_root: str | os.PathLike[str]) -> Path:
    """Human command store directory: ``<data_root>/runtime/human-commands``."""
    return validate_data_root(data_root) / "runtime" / "human-commands"


def human_commands_file(data_root: str | os.PathLike[str], tenant: str | TenantId) -> Path:
    """Human command store file for one tenant."""
    return human_commands_dir(data_root) / f"{validate_tenant(tenant)}.json"


def survey_db_path(data_root: str | os.PathLike[str], tenant: str | TenantId) -> Path:
    """Survey database file for one tenant: ``<data_root>/runtime/survey/<tenant>.db``."""
    root = validate_data_root(data_root)
    return root / "runtime" / "survey" / f"{validate_survey_tenant(tenant)}.db"


def registry_db_path(data_root: str | os.PathLike[str]) -> Path:
    """Agent registry database: ``<data_root>/runtime/registry.db``."""
    return validate_data_root(data_root) / "runtime" / "registry.db"


def redeem_log_path(data_root: str | os.PathLike[str]) -> Path:
    """Redeem request log: ``<data_root>/runtime/redeem-log.jsonl``."""
    return validate_data_root(data_root) / "runtime" / "redeem-log.jsonl"


def outcome_jsonl_path(data_root: str | os.PathLike[str], tenant: str | TenantId) -> Path:
    """Per-tenant outcome telemetry JSONL file under the telemetry directory."""
    return telemetry_dir(data_root, tenant) / "outcome.jsonl"


def write_api_audit_path(data_root: str | os.PathLike[str]) -> Path:
    """Write API security gate audit log: ``<data_root>/runtime/write-api-audit.jsonl``."""
    return validate_data_root(data_root) / "runtime" / "write-api-audit.jsonl"


__all__ = [
    "DATA_ROOT_ENV",
    "SIM_TENANT_PREFIX",
    "TENANTS",
    "CommandCenterError",
    "calibration_dir",
    "human_commands_dir",
    "human_commands_file",
    "normalize_tenant",
    "outcome_jsonl_path",
    "redeem_log_path",
    "registry_db_path",
    "resolve_data_root",
    "runtime_dir",
    "survey_db_path",
    "telemetry_dir",
    "validate_data_root",
    "validate_survey_tenant",
    "validate_tenant",
    "write_api_audit_path",
]
