# Command Center projections (P5-4)

Ports the legacy TypeScript Command Center **audit / map / alliance / shop**
projections to Python on top of the P5-3 data base (`paths` / `jsonl` /
`cache` / `registry` / `survey_db`). Each module exposes:

- a deterministic **aggregation core** (pure function, golden-tested);
- a thin **loader** that reads the same runtime artifacts the TS oracle reads
  (jsonl tails via `read_jsonl_tail` / `load_jsonl_rows`, survey databases
  read-only via `survey_db_path`).

## Modules

| Module | Endpoint family | Source artifacts |
|--------|-----------------|------------------|
| `decisions.py` | `/api/audit/decisions` (+ trend) | `telemetry/<t>/decision.jsonl`, `outcome.jsonl` |
| `workers.py` | `/api/audit/workers` | `telemetry/<t>/runtime.jsonl` |
| `human.py` | `/api/audit/human` | `runtime/human-command-audit.jsonl` |
| `conflicts.py` | `/api/audit/human/conflicts` | `outcome.jsonl` + human audit |
| `trail.py` | `/api/audit/trail` | human/command/arbitration/supervisor JSONL |
| `mines.py` | `/api/audit/mines` (+ trend) | survey `resources` (+ `resource_events` when present) |
| `mining_effectiveness.py` | `/api/audit/mining-effectiveness` | alliance mining assignments + harvest stats |
| `map_lod.py` | `/api/map/lod` | survey `resources` / `obstacles` / `core_hunts` |
| `alliance_survey.py` | `/api/alliance/survey` | survey tables + arbitration log |
| `alliance_mining.py` | `/api/alliance/mining` | alliance survey/snapshot/mines inputs |
| `alliance_cluster.py` | `/api/alliance/cluster` | none (pure member input) |
| `arbitrations.py` | `/api/alliance/survey/arbitrations` | `runtime/survey/arbitration.jsonl` |
| `shop_history.py` | `/api/shop/history` | `runtime/shop-history.jsonl` |

## Acceptance classification (B5C)

Golden fixtures pair synthetic runtime artifacts with outputs produced by
running the **actual** legacy TS oracle (Node, read-only TS checkout at the
P5-2 snapshot commit `8cf5cbb`). Every case must classify:

- **MATCH** — Python aggregation equals the TS oracle output field-for-field
  (after stripping injectable wall-clock timestamps).
- **ALLOWED** — a registered, documented divergence (see `conftest.py`
  `ALLOWED_DIFFERENCES`): injectable timestamps, parsed-rows input, missing
  TS survey tables (`sync_meta` / `resource_events` / `chunks`) degrading to
  empty inputs, explicit `refreshedAt`, ordered arbitration pairs.
- **UNKNOWN** — anything else; fails the suite (fail-closed).

Regenerate goldens with
`uv run python tests/command_center/projections/tools/regenerate_goldens.py`
(after deliberately changing a fixture; goldens are committed so the suite
runs without Node).
