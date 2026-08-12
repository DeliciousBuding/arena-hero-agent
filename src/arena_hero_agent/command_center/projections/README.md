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
| `alignment.py` | `/api/audit/alignment` | decision-audit + mine-utilization (+ trend) + mining-effectiveness + alliance snapshot |
| `decisions.py` | `/api/audit/decisions` (+ trend) | `telemetry/<t>/decision.jsonl`, `outcome.jsonl` |
| `workers.py` | `/api/audit/workers` | `telemetry/<t>/runtime.jsonl` |
| `human.py` | `/api/audit/human` | `runtime/human-command-audit.jsonl` |
| `conflicts.py` | `/api/audit/human/conflicts` | `outcome.jsonl` + human audit |
| `trail.py` | `/api/audit/trail` | human/command/arbitration/supervisor JSONL |
| `mines.py` | `/api/audit/mines` (+ trend) | survey `resources` (+ `resource_events` when present) |
| `mining_effectiveness.py` | `/api/audit/mining-effectiveness` | alliance mining assignments + harvest stats |
| `map_lod.py` | `/api/map/lod` | survey `resources` / `obstacles` / `core_hunts` |
| `alliance_survey.py` | `/api/alliance/survey` | survey tables + arbitration log |
| `alliance_snapshot.py` | `/api/alliance/snapshot` | calibration world cases + survey-db `core_hunts` + leaderboard snapshot (canonical alliance domain model: members/sightings/counts/intel/threat/threatSummaries; W20) |
| `alliance_defense.py` | `/api/alliance/defense` | W20 snapshot payload (members/sightings/threatSummaries) -> endangered/reinforce/formation/pocket advice (TS `alliance-defense.ts`; W21) |
| `alliance_mining.py` | `/api/alliance/mining` | alliance survey/snapshot/mines inputs |
| `consensus_mining.py` | `/api/alliance/survey/mining` | alliance survey + mining-effectiveness + mine utilization + enemy heat |

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

## Frontend rule-projection ownership map (P5-8, 2026-08-12)

P5-8 处置结论：前端 `apps/command-center-web/src/engine/` 的规则投影计算与后端对照如下
（完整盘点见 wave 15 PROGRESS.md 任务 0）。本表为延迟验证标注：前端保留项若与后端
语义相关，在此指向权威模块；后端核心逻辑未改动。

| 前端计算 | 后端权威实现（已覆盖） | 处置 |
|---|---|---|
| `tactical.ts:tactUnitCost` | `domain/economy.py:unit_price`（v0.14 定价） | 保留前端 + 差分测试锚定 |
| `tactical.ts:tactCoreCapacity` | `domain/economy.py:core_resource_capacity` | 保留前端 + 差分测试锚定（负人口后端拒绝，前端 clamp，文档化差异） |
| `utils.ts:maxUnitHp` | `planning/plan_validator.py:UNIT_MAX_HP` | 保留前端 + 差分测试锚定 |
| `tactical.ts:tactRangerRange/tactRangerTargets` | `planning/plan_validator.py:RANGER_SHOOT_RANGE`（=3） | 保留前端（即时反馈）；射程常量后端已覆盖 |
| `tactical.ts:tactAvailability`（动作可用性） | `planning/plan_validator.py`（服务端动作校验） | 保留前端（交互即时反馈）；合法性校验以后端为准 |
| `mapEngine.ts:tactRenderHud` 内联测绘/生命周期聚合 | `mines.py` / `mining_effectiveness.py` | 渲染内联展示派生，不迁移；数据级聚合已由本模块覆盖 |
| `utils.ts:bucketScale/gridStepFor`（视口 LOD） | 无等价（`map_lod.py` 是数据 16×16 分块） | 保留前端（视口参数，无迁移面） |
| `commands.ts:squadSummary` 等交互聚合 | 无等价 | 保留前端（交互多选瞬时派生） |

