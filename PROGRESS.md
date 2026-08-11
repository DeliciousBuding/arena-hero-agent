# PROGRESS — Wave 15 / Line A: alliance snapshot + threat + task market (P4-17)

## 任务 0（2026-08-12）：理解、目标、顺序、最大风险（≤10 行）

- 理解：TS lib/alliance 语义已核对（Node 24 可直接跑 oracle 对拍）。sightings 按
  mergeKey 去重（id / owner-Core spatial gate 8 / ephemeral unit），confidence =
  max(floor, exp(-age/tau))；counts 四口径；threat field 投影（radius 12/24，
  proximity 1/(1+d)）；summary 八扇区；shared-intel 新鲜度 LIVE/RECENT/HISTORICAL
  （liveWindow 1 / freshnessWindow 8）。
- 过期策略（所选）：snapshot 用 shared-intel 新鲜窗口分类；HISTORICAL 标记为 stale
  （stale_sighting_keys，fail-closed），不进入 current/recent 兵力与威胁放大；counts
  保留 TS counts.ts 语义以对拍。
- 目标：snapshot.py（合并+过期识别+counts）、threat.py（field+summary）、
  task_market.py（确定性分配）；stdlib only；复用 domain（TenantId/Coordinate）。
- 顺序：snapshot → threat → task_market → __init__ 导出 → 对拍 → 3.11/ruff/ty/校验 → 交付。
- 最大风险：浮点与 TS 精确对拍（同公式同顺序，round 用 half-away-from-zero 对齐 JS）；
  task market 在 lib/alliance/ 无 oracle → 按 P4-17 规格实现并记录差异；不新增依赖。

## 2026-08-12 基线确认

- `uv sync --locked --all-groups` OK；`uv run python -m pytest -q` = 1268 passed；
  `uv run --python 3.11 python -m pytest -q` = 1268 passed；skipped 0。
- TS oracle 8 文件已读（snapshot/threat-field/threat-summary/roster/counts/sightings/
  shared-intel/types）；task-market.ts 仅在 arena-agent/src（lib/alliance 之外，遵守只读边界未读）。
- task market 无 oracle：按 P4-17 规格实现（确定性排序 + 稳定 tie + fail-closed 拒绝），
  与 TS 的差异即"无对齐基准，按规格实现"。

## 2026-08-12 实现与验收（三功能完成）

### 交付内容
- `src/arena_hero_agent/alliance/snapshot.py`：跨租户只读快照（TS snapshot/sightings/counts/
  shared-intel 语义）+ 过期识别（FreshnessWindow LIVE/RECENT/HISTORICAL，默认 1/8 tick）。
- `src/arena_hero_agent/alliance/threat.py`：威胁场（threat-field：direct/projected/coreRaid/
  uncertainty，radius 12/24，proximity 1/(1+d)）+ 威胁摘要（threat-summary：八扇区）+
  leaderboard 先验。过期威胁数据不放大威胁等级（HISTORICAL 默认排除；include_historical=True
  复现 TS 原样）。
- `src/arena_hero_agent/alliance/task_market.py`：确定性任务市场（无 RNG，稳定全序：
  priority desc → deadline asc（无 deadline 排最后，_NO_DEADLINE=2**63-1）→ task_id asc；
  tenant 按 tenant_id；不可分配显式 reject 带原因；损坏输入 fail-closed raise）。
- `__init__.py` 导出三模块；测试 3 个新文件（tests/alliance/test_{snapshot,threat,task_market}.py）。

### 选定策略（PROGRESS 记录）
- 过期识别：shared-intel 新鲜窗口（liveWindow=1/freshnessWindow=8），HISTORICAL 标记进
  stale_sighting_keys（fail-closed），不进入 currentVisibleCombat，不投影/不放大威胁；
  counts 其余口径保留 TS counts.ts（recent 300 窗口、confidence 衰减地板——均为有界非放大）。
- 对拍方式：Node 24 type stripping 直接跑 TS lib/alliance 为 oracle；共享 fixture JSON
  （C:\Users\Ding\tmp\w15-fixture.json），TS/Python 各自输出 w15-ts-out.json / w15-py-out.json，
  逐字段比对（容差 1e-9）：sightings/counts/tickWindow/2904 威胁格/summaries 全部一致；
  stale case 展示刻意差异（TS 投影衰减 0.18887560283756183 vs Python 0 格，fail-closed）。
- 与 TS 的刻意差异（均记录）：
  1. generated_at_ms 默认 0（TS 默认 Date.now() 非确定）——纯函数确定性优先；
  2. maxDirect tie 打破按 cell key 升序（TS 取首个插入，未定义 tie）——确定性优先；
  3. 威胁场/摘要默认排除 HISTORICAL（TS snapshot 变体含衰减历史；intel 路径本就排除）；
  4. treasury_tenant 用 TenantId|None（TS 空串）；
  5. task market 在 lib/alliance 无 oracle → 按 P4-17 规格实现并记录差异。

### 验证（贴输出）
- `uv run python -m pytest -q` → 1366 passed（基线 1268 + 新增 98），skipped 0。
- `uv run --python 3.11 python -m pytest -q` → 1366 passed。
- `uv run ruff format --check src/arena_hero_agent/alliance tests/alliance` → 13 files already formatted。
- `uv run ruff check src/arena_hero_agent/alliance tests/alliance` → All checks passed。
- `uv run ty check`（全仓）→ All checks passed。
- `git diff --check` → 干净。
