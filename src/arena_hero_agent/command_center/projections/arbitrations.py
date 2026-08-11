"""Shared-survey arbitration projection (port of legacy ``arbitration.ts``).

Humans may override the default same-cell mining arbitration
(last-seen newest wins, ties by tenant order) via ``arbitration.jsonl`` —
an append-only log where the last row for a cell wins (override or clear).
This read surface feeds the alliance-survey consensus aggregation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..jsonl import load_jsonl_rows
from ..paths import validate_data_root

__all__ = ["arbitration_file", "list_arbitrations", "load_arbitrations"]


def arbitration_file(data_root: str | os.PathLike[str]) -> Path:
    """Path of the shared arbitration log."""
    return validate_data_root(data_root) / "runtime" / "survey" / "arbitration.jsonl"


def load_arbitrations(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Last-effective entry per cell (append-only log: the last row wins).

    Rows with a missing/empty ``cell`` are skipped; malformed rows are already
    filtered by the JSONL base. Mirror of the TS ``Map`` semantics: an
    overwrite keeps the first-seen position.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        cell = row.get("cell")
        if not isinstance(cell, str) or cell == "":
            continue
        out[cell] = row
    return out


def list_arbitrations(
    data_root: str | os.PathLike[str],
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Effective arbitration list in first-seen cell order."""
    if rows is None:
        rows = load_jsonl_rows(arbitration_file(data_root))
    return list(load_arbitrations(rows).values())
