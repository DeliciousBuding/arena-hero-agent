# P4-20 PROGRESS — deterministic offline contestant seam (W18-B)

- Branch: `w18/p4-20` (worktree `.worktrees/w18-p4-20`, based on `main@b30556f`)
- Scope: `arena-hero-agent` only. No root-arena docs, Lab/SDK/production repos, real data, or secrets touched.
- Seam: `agent-run-v1` JSONL artifacts (health.json / telemetry.jsonl / ticks.jsonl under `<data-root>/<tenant>/`). No existing JSONL field names or `recordType` values changed, so Lab `import_agent_run` keeps consuming the same wire contract.

## Task 0 re-verification (fresh roots, same input + run id `seam-fixed`)

Command (per task sheet):
`uv run python -m arena_hero_agent.cli run --tenant t1 --input tests/cli/fixtures/replay_turns_v1.json --data-root <tmp> --run-id seam-fixed`

Run twice into two fresh data roots:

| Check | Result |
|---|---|
| `telemetry.jsonl` byte-identical across runs | PASS |
| `ticks.jsonl` differs only in `recordedAtNs` (4 lines each) | PASS |
| `health.json` contains `updatedAtNs` (and `startedAtNs`) | PASS |
| records under `<data-root>/<tenant>/` (`t1/health.json`, `t1/telemetry.jsonl`, `t1/ticks.jsonl`) | PASS |

## What was implemented

1. **Determinism / content-addressing (no schema change)**
   - New `src/arena_hero_agent/cli/canonical.py`: `strip_nonsemantic` removes
     `recordedAtNs` / `updatedAtNs` / `startedAtNs`; `canonical_record_bytes`
     re-encodes with sorted keys + compact separators; `jsonl_file_digest` /
     `json_document_digest` / `run_artifacts_digest` produce stable SHA-256.
   - Every successful `run` now writes `manifest.json` next to health:
     `{schemaVersion:1, tenantId, runId, processRunId, digests:{health,telemetry,ticks,run}}`.
   - `run` digest binds `runId` (carried by health + telemetry): same input +
     same run id → same digest; different run id → different `run` digest.
     Per-record decision content (`ticks` digest) is identical across run ids —
     run id is declared not to affect decision content (covered by tests).
   - SQLite backend: `ticks` digest is `null` (no `ticks.jsonl`); combined
     digest covers present artifacts.

2. **Batch entry** — new subcommand:
   `arena-hero-agent batch --input-dir <dir> --tenant <id> --data-root <root> [--seed N] [--backend jsonl|sqlite]`
   - Each regular file in the directory is one scenario; stable run id
     `scenario-<sanitized-stem>-seed-<n>` satisfying `_SAFE_RUN_ID`
     (`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`; over-long stems truncated with an
     8-char sha256 tail).
   - Output layout: `<data-root>/<run-id>/<tenant>/{health.json,telemetry.jsonl,ticks.jsonl,manifest.json}` —
     stable, collision-free, and each scenario dir is itself readable by
     `health --tenant <id> --data-root <root>/<run-id>`.
   - Scenarios run in sorted filename order; execution aborts on first failure.

3. **Fail-closed**
   - Illegal run id → `invalid run id` (exit 2, pre-existing check retained).
   - Unknown/invalid tenant → `invalid tenant id` (exit 2).
   - Bad/unloadable input → `replay input could not be loaded` (exit 2).
   - Explicit duplicate run id already recorded in the same tenant dir →
     `run id conflict: run id already recorded for this tenant` (exit 2),
     checked before the recorder opens, so no artifact is appended or created.
   - Batch pre-validates everything (tenant, seed, backend, budgets, input dir,
     per-file inputs, run-id derivation, collisions, existing manifests) before
     any scenario runs → a bad batch leaves no partial artifacts.
   - Batch run-id collisions (`same.json` + `same.jsonl`) → clear error.
   - Unsafe scenario names (sanitize to empty) → clear error.
   - No absolute paths or secrets in error output (privacy scan extended to batch).

## Tests (added 31, baseline intact)

- `tests/cli/test_canonical.py` (11): stripping, stable bytes, per-file digests,
  run digest semantics (same input+run-id stable; run-id bound; ticks content
  independent), manifest build/read round-trips, missing/malformed manifests.
- `tests/cli/test_batch.py` (20): parser defaults, run-id derivation (shape,
  sanitization, stability, seed sensitivity, rejects), happy path sorted order,
  determinism same-seed, different-seed semantics, fail-closed (bad input,
  collision, unsafe name, missing/empty dir, invalid tenant), no path leaks,
  duplicate run-id conflict (no append), manifest content, end-to-end digest
  stability across fresh roots, sqlite backend ticks-digest null.
- Existing assertions untouched: baseline `1379 passed` → now `1410 passed`, `skipped 0`.

## Gates (clean worktree)

```
uv sync --locked --all-groups        PASS (resolved 25 packages, lock unchanged)
uv run ruff format --check .         PASS (240 files)
uv run ruff check .                  PASS
uv run ty check                      PASS
uv run pytest -q                     1410 passed, 0 skipped
git diff --check                     PASS
git status --short                   clean after commits (only expected files)
```

## Reverse verification (亲手制造失败)

1. **Canonicalizer is what stabilizes digests**: raw (unstripped) SHA-256 of
   `ticks.jsonl` from the two task-0 runs differ
   (`f5737534e655...` vs `c4e00496a06c...`), while the canonical digest is
   identical. Removing `strip_nonsemantic` is caught by
   `test_jsonl_file_digest_ignores_recorded_at_ns`,
   `test_json_document_digest_strips_health_timestamps`, and the end-to-end
   `test_run_deterministic_canonical_digest_across_fresh_roots`.
2. **Illegal run id rejected**: `--run-id "bad run id!"` → exit 2,
   `invalid run id` (verified in acceptance script and `test_parser_*` suite).
3. **Duplicate run id rejected**: second `run` with the same explicit run id
   into the same tenant dir → exit 2, `run id conflict`; `ticks.jsonl` bytes
   unchanged (no append) — caught by
   `test_run_duplicate_run_id_conflict_fails_closed`.

## Lab consumption — exact CLI usage

```bash
# One scenario (deterministic seam):
arena-hero-agent run --tenant t1 \
  --input tests/cli/fixtures/replay_turns_v1.json \
  --data-root /tmp/arena-runs/scenario-001 \
  --run-id scenario-001

# Batch: one scenario per file in a directory, stable run ids, per-scenario output:
arena-hero-agent batch --tenant t1 \
  --input-dir /tmp/arena-scenarios \
  --data-root /tmp/arena-runs \
  --seed 0

# Outputs per scenario (and per single run):
#   <data-root>/<run-id>/<tenant>/health.json
#   <data-root>/<run-id>/<tenant>/telemetry.jsonl
#   <data-root>/<run-id>/<tenant>/ticks.jsonl
#   <data-root>/<run-id>/<tenant>/manifest.json   <- content-addressed digests
# Exit code 0 = complete; 2 = fail-closed error (no publishable artifact for that run).
```

- Determinism contract: identical input + identical `--run-id` → identical
  `manifest.json` digests across fresh data roots. Different run id → different
  `run` digest; `ticks` decision-content digest unchanged.
- Re-running the same explicit run id into the same tenant directory is
  rejected (use a fresh data root or a new run id); batch re-run into the same
  data root with the same seed is rejected the same way.

## Commits (branch `w18/p4-20`, not pushed)

1. `feat(cli): deterministic canonical record digests and manifests (P4-20)` — `src/arena_hero_agent/cli/canonical.py` + `tests/cli/test_canonical.py`
2. `feat(cli): batch offline runner and fail-closed run id checks (P4-20)` — `src/arena_hero_agent/cli/main.py` + `tests/cli/test_batch.py`
3. `docs(cli): batch usage and deterministic offline records (P4-20)` — `README.md` + `PROGRESS.md` + `BLOCKED.md`
