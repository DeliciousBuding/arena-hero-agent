"""Redeem-request history (port of legacy ``redeem-log.ts``).

Ports ``loadRedeemHistory`` from the TypeScript oracle: a bounded tail of the
``data/runtime/redeem-log.jsonl`` audit (masked codes only — full redemption
codes never touch disk or logs). Pure read. ``/api/redeem/history``.

Registered differences from the TS oracle:

- The Python Command Center has no redeem write path (``POST /api/redeem`` is
  a 501 write-gated route), so no process ever appends to the log; the loader
  still reads a persisted tail for a shared data root that carries legacy
  TS-era rows (fail-open, never guessed).
- TS ``count`` is the in-memory process array length (empty at boot; Python
  has no in-memory log). Python reports the persisted tail count
  (``len(records)``) so the envelope is self-consistent.
"""

from __future__ import annotations

import os
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import load_jsonl_rows
from ..paths import redeem_log_path, validate_data_root
from ._common import current_epoch_ms

__all__ = ["MAX_KEEP", "load_redeem_history"]

MAX_KEEP = 200


def load_redeem_history(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Read the redeem-log tail (``/api/redeem/history`` source)."""
    at = iso_utc(now_ms if now_ms is not None else current_epoch_ms())
    root = validate_data_root(data_root)
    records = load_jsonl_rows(redeem_log_path(root), max_lines=MAX_KEEP)
    return {"generatedAt": at, "records": records, "count": len(records)}
