"""Regenerate TS-oracle golden outputs for the projection parity suite (P5-4).

Each ``fixtures/<name>.golden.json`` is produced by running the actual legacy
TypeScript oracle with Node on the paired ``fixtures/<name>.json`` input:

- requires Node >= 22 (type stripping) and the read-only TS checkout at
  ``arena-hero-agent-ts`` (HEAD ``8cf5cbb`` = P5-2 snapshot commit);
- never writes into the TS checkout (the oracle harness lives outside it);
- the harness is expected at ``<tmp>/cc-oracle.mjs`` (kept outside the repo
  because the TS checkout must stay clean).

Usage:

    uv run python tests/command_center/projections/tools/regenerate_goldens.py

Only run this after deliberately changing a fixture; goldens are committed so
the suite runs without Node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ORACLE_HARNESS = Path.home() / "tmp" / "cc-oracle.mjs"
# advice fixtures need a materialized data root, not the raw fixture JSON
ORACLE_ADVICE_HARNESS = Path.home() / "tmp" / "cc-oracle-advice.mjs"
# survey/mine fixtures need a materialized survey-db data root (W44 wave 5)
ORACLE_SURVEY_MINE_HARNESS = Path.home() / "tmp" / "cc-oracle-survey-mine.mjs"

# fixture base name -> oracle dispatch kind (see cc-oracle.mjs)
KIND_BY_FIXTURE: dict[str, str] = {
    "alliance_cluster_basic": "cluster",
    "alliance_defense_basic": "defense",
    "alliance_intel_basic": "allianceIntel",
    "alliance_mining_basic": "mining",
    "alliance_snapshot_basic": "allianceSnapshot",
    "decisions_audit_basic": "decisionAudit",
    "decisions_trend_basic": "decisionTrend",
    "workers_basic": "workerLiveness",
    "conflicts_basic": "humanConflict",
    "trail_normalize_basic": "normalizeTrails",
    "trail_merge_basic": "mergeTrails",
    "map_lod_basic": "mapLod",
    "mines_util_basic": "mineUtil",
    "mines_trend_basic": "mineTrend",
    "shop_history_agg_basic": "shopHistory",
    "shop_history_normalize_basic": "shopHistory",
    "shop_history_signature_basic": "shopHistory",
    "shop_history_should_append_basic": "shopHistory",
    "mining_effectiveness_basic": "miningEffectiveness",
    "arbitrations_basic": "arbitrations",
    "human_audit_basic": "humanAudit",
    "alliance_advice_basic": "advice",
    "alliance_advice_full": "advice",
    "exploration_coverage_basic": "explorationCoverage",
    "mine_patterns_predict_basic": "minePatternsPredict",
    "mine_patterns_predict_absences_basic": "minePatternsPredictAbsences",
    "mine_patterns_absent_stats_basic": "minePatternsAbsentStats",
    "mine_patterns_dead_mines_basic": "minePatternsDeadMines",
    "mine_patterns_accuracy_basic": "minePatternsAccuracy",
    "consensus_mining_basic": "consensusMining",
    "survey_mine_basic": "surveyMine",
    "survey_mine_default_cell": "surveyMine",
    "enemy_cores_basic": "enemyCores",
    "decision_input_basic": "decisionInput",
}


def main() -> int:
    if shutil.which("node") is None:
        print("node is required to regenerate goldens", file=sys.stderr)
        return 1
    if not ORACLE_HARNESS.exists():
        print(f"oracle harness missing: {ORACLE_HARNESS}", file=sys.stderr)
        return 1
    regenerated: list[str] = []
    for base, kind in KIND_BY_FIXTURE.items():
        fixture = FIXTURES / f"{base}.json"
        golden = FIXTURES / f"{base}.golden.json"
        if not fixture.exists():
            print(f"missing fixture: {fixture}", file=sys.stderr)
            return 1
        if kind == "advice":
            # advice oracle reads a materialized Command Center data root
            import json as _json
            import tempfile

            import advice_fixture

            spec = _json.loads(fixture.read_text(encoding="utf-8"))
            with tempfile.TemporaryDirectory(prefix="cc-advice-regen-") as root_dir:
                root = Path(root_dir)
                advice_fixture.materialize_advice_data_root(spec, root)
                result = subprocess.run(
                    ["node", str(ORACLE_ADVICE_HARNESS), str(root), str(spec["nowMs"])],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
        elif kind == "surveyMine":
            # survey/mine oracle reads a materialized survey-db data root
            import json as _json
            import tempfile

            import advice_fixture

            spec = _json.loads(fixture.read_text(encoding="utf-8"))
            with tempfile.TemporaryDirectory(prefix="cc-survey-mine-regen-") as root_dir:
                root = Path(root_dir)
                advice_fixture.materialize_advice_data_root(spec, root)
                result = subprocess.run(
                    [
                        "node",
                        str(ORACLE_SURVEY_MINE_HARNESS),
                        str(root),
                        str(spec.get("tenant", "t1")),
                        str(spec.get("cell", "")),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
        else:
            result = subprocess.run(
                ["node", str(ORACLE_HARNESS), str(fixture), kind],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        if result.returncode != 0:
            print(f"oracle failed for {base}: {result.stderr}", file=sys.stderr)
            return 1
        # validate the output parses as JSON before writing
        json.loads(result.stdout)
        golden.write_text(result.stdout, encoding="utf-8")
        regenerated.append(base)
    print(f"regenerated {len(regenerated)} goldens: {', '.join(regenerated)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
