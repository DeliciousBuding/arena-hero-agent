"""Shared oracle fixture access for P4-11 differential tests.

The fixture is a pinned capture from the legacy TypeScript oracle
(arena-hero-agent-ts at 8cf5cbb). Every section is either fixture-matched
(MATCH), registered as an ALLOWED_DIFFERENCE, or explicitly listed as
EXPECTED_UNKNOWN in the behavior-difference registry; see
docs/planning-differences.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "strategies" / "fixtures" / "known_answers_v1.json"
)


def load_oracle_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
