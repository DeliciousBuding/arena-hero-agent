"""Human command audit projection (port of legacy ``human-audit.ts``).

Every manual operation (command / goal / goal dedupe / mode / clear / delete)
appends one row to ``runtime/human-command-audit.jsonl``; this read path
returns the most recent ``MAX_KEEP`` entries, optionally filtered by tenant,
newest first. ``/api/audit/human``.
"""

from __future__ import annotations

import os
from typing import Any

from ..jsonl import load_jsonl_rows
from ..paths import validate_data_root

MAX_KEEP = 500
DEFAULT_LIMIT = 100

__all__ = ["DEFAULT_LIMIT", "MAX_KEEP", "load_human_audit", "read_human_audit"]


def load_human_audit(
    rows: list[dict[str, Any]],
    tenant: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Filter parsed audit rows to the most recent ``limit``, newest first.

    Mirrors the TS oracle: only the last ``MAX_KEEP`` rows are considered, a
    tenant filter is applied when given, and the result is reversed so the
    newest entry comes first. The limit is clamped to ``1..500``.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError(f"limit must be an integer; actual={limit!r}")
    cap = min(max(limit, 1), MAX_KEEP)
    recent = rows[-MAX_KEEP:]
    if tenant is not None:
        recent = [row for row in recent if row.get("tenant") == tenant]
    return list(reversed(recent[-cap:]))


def read_human_audit(
    data_root: str | os.PathLike[str],
    tenant: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Read the human command audit JSONL and return the recent window."""
    root = validate_data_root(data_root)
    path = root / "runtime" / "human-command-audit.jsonl"
    rows = load_jsonl_rows(path)
    return load_human_audit(rows, tenant=tenant, limit=limit)
