"""JSONL data base for the Python Command Center (P5-3).

Ports the legacy TypeScript ``fs-jsonl.ts`` data-access primitives:

- ``read_jsonl_tail``: bounded seek-based tail read (64 KiB..2 MiB window),
  tolerant of bad lines and of a partial first line when the window starts
  mid-line, matching the TS oracle;
- ``append_jsonl``: plain append of one JSON object per line (TS
  ``appendFileSync`` semantics), fail-closed on invalid targets or IO errors;
- calibration run/case enumeration: ``latest_run_dir`` and ``list_cases`` with
  the same 1.5 s TTL memoization as the oracle, ``runs_by_max_tick``, and
  ``parse_tick``.

Deliberate, registered differences from the TS oracle:

- A valid-JSON line that is not an object raises ``CommandCenterError``
  (fail-closed schema check) instead of being cast downstream as an object;
- ``allow_nan=False``: non-finite numbers raise instead of being serialized as
  ``null`` by the writer.

The TTL memo caches are injectable for deterministic tests; the module-level
defaults are keyed by data root and tenant so independent roots never share
stale entries.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from arena_hero_agent.domain import TenantId

from .cache import TtlCache
from .errors import CommandCenterError
from .paths import calibration_dir, validate_data_root

TAIL_WINDOW_MIN_BYTES = 64 * 1024
TAIL_WINDOW_MAX_BYTES = 2 * 1024 * 1024
TAIL_BYTES_PER_LINE = 1024
RUN_CACHE_TTL_MS = 1500

_LATEST_RUN_CACHE_KEY = "command-center:latest-run"
_LIST_CASES_CACHE_KEY = "command-center:list-cases"


def _cache_key(root: Path, tenant: str) -> str:
    return f"{root}:{tenant}"


def _validate_target_path(path: str | os.PathLike[str]) -> Path:
    """Portable NUL/traversal hardening for a concrete target file."""
    target = os.fspath(path)
    if not isinstance(target, str) or not target:
        raise CommandCenterError("target path must be a non-empty string")
    if "\x00" in target:
        raise CommandCenterError("target path must not contain NUL bytes")
    from pathlib import PurePosixPath, PureWindowsPath

    windows_view = PureWindowsPath(target)
    posix_view = PurePosixPath(target)
    if any(part == ".." for part in windows_view.parts) or any(
        part == ".." for part in posix_view.parts
    ):
        raise CommandCenterError("target path must not contain '..' traversal components")
    return Path(target)


def read_jsonl_tail(path: str | os.PathLike[str], max_lines: int) -> list[dict[str, Any]]:
    """Read the trailing ``max_lines`` JSON objects from a JSONL file.

    Mirrors the TS oracle: a bounded tail window is read with seek (64 KiB
    floor, 2 MiB ceiling, ``max_lines * 1024`` bytes per line), a partial first
    line created by the window boundary is discarded, blank lines are skipped,
    and lines that fail to parse are skipped. A missing file yields ``[]``.
    """
    if isinstance(max_lines, bool) or not isinstance(max_lines, int):
        raise CommandCenterError(f"max_lines must be an integer; actual={max_lines!r}")
    if max_lines < 0:
        raise CommandCenterError(f"max_lines cannot be negative; actual={max_lines}")
    target = _validate_target_path(path)
    if not target.exists() or not target.is_file():
        return []
    size = target.stat().st_size
    want = max(TAIL_WINDOW_MIN_BYTES, min(TAIL_WINDOW_MAX_BYTES, max_lines * TAIL_BYTES_PER_LINE))
    start = max(0, size - want)
    if start == 0:
        text = target.read_text(encoding="utf-8", errors="replace")
    else:
        with target.open("rb") as handle:
            handle.seek(start)
            raw = handle.read(size - start)
        text = raw.decode("utf-8", errors="replace")
        newline = text.find("\n")
        if newline >= 0:
            text = text[newline + 1 :]
    lines = [line for line in re.split(r"\r?\n", text) if line.strip()]
    rows: list[dict[str, Any]] = []
    for line in lines[-max_lines:]:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            raise CommandCenterError(
                f"non-object JSON row in {target}: expected an object, got {type(parsed).__name__}"
            )
        rows.append(parsed)
    return rows


def load_jsonl_rows(
    path: str | os.PathLike[str], *, max_lines: int | None = None
) -> list[dict[str, Any]]:
    """Read JSON objects from a JSONL file, tolerating bad lines.

    A missing file yields ``[]``. When ``max_lines`` is given, only the last
    ``max_lines`` rows are returned (used by the redeem-log history window).
    """
    if max_lines is not None and (
        isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines < 0
    ):
        raise CommandCenterError(f"invalid max_lines: {max_lines!r}")
    target = _validate_target_path(path)
    if not target.exists() or not target.is_file():
        return []
    text = target.read_text(encoding="utf-8", errors="replace")
    lines = [line for line in re.split(r"\r?\n", text) if line.strip()]
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            raise CommandCenterError(
                f"non-object JSON row in {target}: expected an object, got {type(parsed).__name__}"
            )
        rows.append(parsed)
    if max_lines is not None:
        return rows[-max_lines:]
    return rows


def append_jsonl(path: str | os.PathLike[str], record: dict[str, Any]) -> None:
    """Append one JSON object as a newline-terminated line (TS appendFileSync)."""
    if not isinstance(record, dict):
        raise CommandCenterError(
            f"append_jsonl expects a JSON object; actual={type(record).__name__}"
        )
    target = _validate_target_path(path)
    try:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CommandCenterError(f"unserializable record for {target}: {exc}") from exc
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.is_dir():
            raise CommandCenterError(f"append target is a directory: {target}")
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
    except OSError as exc:
        raise CommandCenterError(f"failed to append JSONL row to {target}: {exc}") from exc


def parse_tick(file_name: str) -> int:
    """Extract the leading tick number from a case file name (``0`` when absent)."""
    match_start = 0
    while match_start < len(file_name) and file_name[match_start].isdigit():
        match_start += 1
    if match_start == 0:
        return 0
    return int(file_name[:match_start])


def _default_latest_run_cache() -> TtlCache[str | None]:
    return TtlCache[str | None](RUN_CACHE_TTL_MS)


def _default_list_cases_cache() -> TtlCache[list[str]]:
    return TtlCache[list[str]](RUN_CACHE_TTL_MS)


def latest_run_dir(
    data_root: str | os.PathLike[str],
    tenant: str | TenantId,
    *,
    cache: TtlCache[str | None] | None = None,
) -> str | None:
    """Most recent run directory with calibration cases for a tenant.

    The oracle selects by run-directory mtime (case writes bump mtime and the
    agent only writes the newest run) and verifies the ``cases/`` directory is
    non-empty. Results are memoized for 1.5 s.
    """
    root = validate_data_root(data_root)
    base = calibration_dir(root, tenant)
    tenant_value = tenant.value if isinstance(tenant, TenantId) else tenant
    memo = cache if cache is not None else _default_latest_run_cache()
    key = _cache_key(root, tenant_value)
    return memo.get_or_load(key, lambda: _latest_run_dir_inner(base))


def _latest_run_dir_inner(base: Path) -> str | None:
    if not base.exists() or not base.is_dir():
        return None
    runs = [entry for entry in base.iterdir() if entry.is_dir()]
    runs.sort(key=lambda entry: entry.stat().st_mtime_ns, reverse=True)
    for run in runs:
        cases_dir = run / "cases"
        if cases_dir.is_dir() and any(cases_dir.iterdir()):
            return run.name
    return None


def list_cases(
    data_root: str | os.PathLike[str],
    tenant: str | TenantId,
    run_dir: str,
    *,
    cache: TtlCache[list[str]] | None = None,
) -> list[str]:
    """Sorted ``*.json`` case file names for a run (memoized for 1.5 s)."""
    root = validate_data_root(data_root)
    cases_dir = calibration_dir(root, tenant) / run_dir / "cases"
    tenant_value = tenant.value if isinstance(tenant, TenantId) else tenant
    memo = cache if cache is not None else _default_list_cases_cache()
    key = f"{_cache_key(root, tenant_value)}/{run_dir}"
    return memo.get_or_load(key, lambda: _list_cases_inner(cases_dir))


def _list_cases_inner(cases_dir: Path) -> list[str]:
    if not cases_dir.exists() or not cases_dir.is_dir():
        return []
    return sorted(name for name in os.listdir(cases_dir) if name.endswith(".json"))


def runs_by_max_tick(
    data_root: str | os.PathLike[str], tenant: str | TenantId
) -> list[dict[str, str | int]]:
    """Run directories ordered by their highest case tick, descending."""
    root = validate_data_root(data_root)
    base = calibration_dir(root, tenant)
    if not base.exists() or not base.is_dir():
        return []
    out: list[dict[str, str | int]] = []
    for name in sorted(entry.name for entry in base.iterdir() if entry.is_dir()):
        cases_dir = base / name / "cases"
        if not cases_dir.is_dir():
            continue
        max_tick = -1
        for case_file in cases_dir.iterdir():
            tick = parse_tick(case_file.name)
            if tick > max_tick:
                max_tick = tick
        if max_tick >= 0:
            out.append({"run": name, "maxTick": max_tick})
    out.sort(key=lambda item: item["maxTick"], reverse=True)
    return out


__all__ = [
    "RUN_CACHE_TTL_MS",
    "append_jsonl",
    "latest_run_dir",
    "list_cases",
    "load_jsonl_rows",
    "parse_tick",
    "read_jsonl_tail",
    "runs_by_max_tick",
]
