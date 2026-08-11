# PROGRESS — Wave 16 / Line A: P4-14 candidate runtime (learning/runtime/)

## 任务 0（2026-08-12）：理解、目标、顺序、最大风险（≤10 行）

- 理解：strategies/ 是确定性候选生成面（SafetyPlanner→Plan、variant_registry id→配置覆盖、tactical squads），planning/ 做任务/分配约束；P4-11 SafetyPlannerConfig 是候选约束表面。现有代码无独立 "candidate 集" 结构 → P4-14 在 learning/runtime/ 新建有界选择核心，候选 = 策略配置/变体描述（production 标记 + SafetyPlannerConfig 约束）。
- 目标：selector.py 纯函数（production 过滤 fail-closed、bound 截断记录、稳定 tie 确定性排序、无 RNG、损坏输入 raise）；variant_registry 加 production 标记最小增量（registered variants 默认非生产）；接线：registry 标记 → 运行时候选集 → 有界选择，集成测试。
- 顺序：任务0 PROGRESS → selector.py + tests/learning/test_selector.py（红→绿）→ strategies 标记增量 + 接线 + 集成测试 → 全量验证（3.14/3.11/ruff/ty/diff）→ 交付文档 + commit。
- 最大风险：不改变既有 strategies/planning 行为（标记必须 additive、默认值保守、不改既有测试）；"候选流" 无既有结构，接线需自洽且不越白名单；3.11 全绿。

## 2026-08-12 基线确认

- `uv sync --locked --all-groups` OK（CPython 3.14.6，25 packages）。
- `uv run python -m pytest -q` = 1366 passed，skipped 0。
- `uv run --python 3.11 python -m pytest -q` = 1366 passed。
- 已读 strategies/（safety_planner.py/config、variant_registry.py、tactical_squads.py）与 planning/（plan/mission/task/worker_assignment/plan_validator）：候选流 = strategies 生成（SafetyPlanner 出 Plan、variant registry 出配置覆盖）+ planning 约束（任务/分配）；SafetyPlannerConfig 为约束面。
- 对拍无误：与任务书描述一致（strategies 候选生成 + P4-11 SafetyPlanner 约束），无既有 candidate 结构需兼容 → 动工。

## 2026-08-12 实现与验收（两任务完成）

### 交付内容
- `src/arena_hero_agent/learning/runtime/selector.py`：有界候选选择核心（纯函数）——
  `StrategyCandidate`（id / production 标记 / priority / SafetyPlannerConfig 约束面）、
  `RejectedCandidate`、`SelectionResult`（selected/rejected/truncated/bound）、
  `select_candidates(candidates, bound)`、`select_runtime_candidates(bound)`（接线）。
- 死规矩落实：非生产候选一律 reject（reason="non-production"，永不进 selected，fail-closed）；
  结果 ≤ bound，超出的生产候选记入 truncated（按选择序）；无 RNG；同输入同输出
  （稳定全序：priority desc → id asc）；损坏输入 raise（重复 id / 非候选项 / 坏 bound / 坏字段）。
- `src/arena_hero_agent/strategies/variant_registry.py`：production 标记最小增量
  （`VARIANT_PRODUCTION` 空集 + `is_production_variant`，未标记 = 非生产，fail-closed）；
  不改既有注册表行为。`strategies/__init__.py` 增导出。
- 接线：`select_runtime_candidates` 把 strategies 候选面接进选择器——生产基线
  （default-v1/aggressive-v1，production=True）+ 每个 registered variant
  （production=is_production_variant(id)，当前全部非生产 → 全部 reject）。
- 测试 `tests/learning/test_selector.py`：13 例表驱动（production 过滤、bound 截断、
  tie 稳定/确定性、空输入、损坏输入 fail-closed、标记保守默认、接线集成）。

### 选择策略（PROGRESS 记录）
- 排序：priority desc → id asc（与 P4-17 task market 同风格，无 RNG 的稳定全序）。
- 标记默认：`StrategyCandidate.production=False`、`VARIANT_PRODUCTION=∅`——未标记即非生产。
- 基线候选 id：default-v1 / aggressive-v1（`PRODUCTION_BASELINE_IDS`，导出为稳定契约）；
  variant 带 `apply_variant_overrides(DEFAULT_SAFETY_CONFIG, [id])` 解析的约束面。
- 刻意差异/说明：`learning/runtime/` 无 TS oracle（TS offline-learning/runtime 不在
  只读边界内）→ 按 P4-14 规格实现纯选择核心；"只保留生产候选"= 选择器只接受
  production=True 的候选，研究变体由标记层显式排除，不实现任何学习/进化。
- 顺手活（未做，待裁决）：learning 算法、planning/alliance 改造——见 BLOCKED.md。

### 验证（贴输出）
- `uv run python -m pytest -q` → 1379 passed（基线 1366 + 新增 13），skipped 0。
- `uv run --python 3.11 python -m pytest -q` → 1379 passed。
- `uv run ruff format --check src/arena_hero_agent/learning src/arena_hero_agent/strategies tests/learning` → 10 files already formatted。
- `uv run ruff check src/arena_hero_agent/learning src/arena_hero_agent/strategies tests/learning` → All checks passed。
- `uv run ty check`（全仓）→ All checks passed。
- `git diff --check` → 干净。
- 提交：`c43192b` feat(strategies) production 标记；`246078a` feat(runtime) 有界选择核心 + 接线 + 测试。
