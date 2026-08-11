"""Deterministic rectangular minimum-cost assignment (legacy Hungarian solver).

Port of the legacy TypeScript ``minimumCostAssignment``
(``packages/arena-agent/src/algorithms/min-cost-assignment.ts`` at the pinned
oracle commit) used by the worker-task-planner cost matrix.  The solver is the
classic O(rows^2 * columns) potential/slack Hungarian formulation with the
oracle's tie-break (equal slack picks the lower column index) so cross-run
output is stable.

Contract (mirrors the oracle exactly):

- the matrix must be rectangular with ``rows <= columns`` (workers <= resource
  cells plus one dummy WAIT column per worker);
- every cost must be finite; forbidden combinations use large sentinel values
  chosen by the caller, never ``inf``;
- returns one selected column per row minimizing total finite cost.

This module is pure and deterministic; it is fixture-compared against the
oracle in ``tests/planning/test_min_cost_assignment.py``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Final

# Number.EPSILON = 2^-52; the oracle compares slacks within this tolerance.
_EPSILON: Final = 2.220446049250313e-16


def minimum_cost_assignment(costs: Sequence[Sequence[float]]) -> list[int]:
    """Return one selected column per row for the minimum-cost assignment.

    Raises ``ValueError`` when the matrix is not rectangular, has more rows
    than columns, contains non-finite costs, or the assignment is incomplete.
    """

    if len(costs) == 0:
        return []
    rows = len(costs)
    cols = len(costs[0])
    if cols < rows or any(len(row) != cols for row in costs):
        raise ValueError("assignment matrix must be rectangular with rows <= columns")
    for row in costs:
        for cost in row:
            if isinstance(cost, bool) or not isinstance(cost, (int, float)):
                raise TypeError("assignment costs must be numbers")
            if not math.isfinite(cost):
                raise ValueError("assignment matrix costs must be finite")

    # 1-indexed Hungarian arrays: u = row potential, v = column potential,
    # p = matched row for column, way = augmenting predecessor column.
    u = [0.0] * (rows + 1)
    v = [0.0] * (cols + 1)
    p = [0] * (cols + 1)
    way = [0] * (cols + 1)

    for i in range(1, rows + 1):
        p[0] = i
        j0 = 0
        minv = [float("inf")] * (cols + 1)
        used = [False] * (cols + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = 0
            for j in range(1, cols + 1):
                if used[j]:
                    continue
                cur = costs[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j] - _EPSILON:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta - _EPSILON or (abs(minv[j] - delta) <= _EPSILON and j < j1):
                    delta = minv[j]
                    j1 = j
            for j in range(0, cols + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    assignment = [-1] * rows
    for j in range(1, cols + 1):
        row = p[j]
        if row != 0:
            assignment[row - 1] = j - 1
    if any(column < 0 for column in assignment):
        raise ValueError("assignment incomplete")
    return assignment
