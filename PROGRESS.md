# Wave 15 B=Agent P5-8 浏览器规则投影处置 — PROGRESS

## 理解（任务 0，2026-08-12）
- 目标：盘点 `apps/command-center-web/src/engine/` 中属于"规则投影"的计算（地图 LOD/聚合/规则判定），
  对照后端 P5-4 projections 与领域规则模块已覆盖项；保留前端的写差分测试锚定，应迁后端的标延迟验证。
- 顺序：任务 0 基线 + 盘点清单（本文件）→ 任务 1 保留项差分测试（≥2 项）→ 任务 2 迁移判定清单（PROGRESS 交付 + 代码标注）。
- 死规矩：不改前端渲染逻辑；不碰测试配置/CI/pyproject tool 段；后端 projections 只加测试/标注不改核心逻辑。

## 任务 0 基线（2026-08-12 已核对）
- worktree：`arena-hero-agent/.worktrees/w15-p5-8`，分支 `w15/p5-8`，基线 `main@a307760`（1268 tests），工作树干净。
- `uv sync --locked --all-groups` ✓；`uv run python -m pytest -q` → **1268 passed**（skipped 0）✓。
- `npm install`（83 包，package-lock.json 未变，fresh worktree 无 node_modules，属环境准备非新增依赖）→ `npm run typecheck` ✓ + `npm run test:unit` → **48/48**（skipped 0）✓。

## 盘点清单：前端 engine/ 规则投影（任务 0，2026-08-12）

| ID | 计算 | 位置 | 类别 | 后端对照（实测/读过） | 处置分类 |
|---|---|---|---|---|---|
| FE-1 | 单位最大 HP（VANGUARD 4，其余 2） | `utils.ts:maxUnitHp` | 规则判定 | `planning/plan_validator.py` `UNIT_MAX_HP` {WORKER 2, VANGUARD 4, RANGER 2}（实跑确认） | **保留前端** + 差分测试（后端已覆盖） |
| FE-2 | 单位成本（基准×1.3^exp，pop<20 基准价） | `tactical.ts:tactUnitCost` | 规则判定 | `domain/economy.py` `unit_price`/`BASE_UNIT_COSTS`（实跑确认） | **保留前端** + 差分测试（后端已覆盖） |
| FE-3 | 核心容量 max(10, pop×5) | `tactical.ts:tactCoreCapacity` | 规则判定 | `domain/economy.py` `core_resource_capacity`（实跑确认） | **保留前端** + 差分测试（后端已覆盖） |
| FE-4 | 缩放桶 2^(round(log2(s)*2)/2) | `utils.ts:bucketScale` | 地图 LOD（视口） | 无后端等价（`projections/map_lod.py` 是 16×16 数据分块，非视口缩放桶；读过） | 保留前端（视口参数，无迁移面） |
| FE-5 | 网格步长 2 的幂 | `utils.ts:gridStepFor` | 地图 LOD（视口） | 无后端等价 | 保留前端（视口参数） |
| FE-6 | 编队多选摘要（count/parts/hpAvg/hpMin） | `commands.ts:squadSummary` | 聚合 | 无后端等价（交互多选瞬时派生） | 保留前端 |
| FE-7 | 战术动作可用性判定 | `tactical.ts:tactAvailability` | 规则判定 | 服务端动作校验 `planning/plan_validator.py`（读过；交互即时反馈需前端） | 保留前端 + 标注（服务端校验已覆盖动作合法性） |
| FE-8 | 移动可达/敌情/地形/近邻命中 | `tactical.ts:tactMoveTargets/tactTerrain/tactHostileAt/tactObjectAt/tactObjectNear` | 规则判定/命中 | 规划器动作校验覆盖部分语义（读过）；无 projections 等价 | 保留前端（交互即时反馈） |
| FE-9 | 游侠射程/目标（8 向 3 格 / 切比雪夫 1-3） | `tactical.ts:tactRangerRange/tactRangerTargets` | 规则判定 | `RANGER_SHOOT_RANGE = 3`（`plan_validator.py` + `strategies/safety_helpers.py`，实跑确认） | 保留前端 + 标注（后端射程常量已覆盖） |
| FE-10 | 视野半径（CORE 5/WORKER 3/VANGUARD 4/RANGER 5） | `tactical.ts:tactVisibility` | 规则判定 | 后端无视野半径常量（rg 确认）；`domain/navigation.py` 是射击视线阻挡，语义不同 | 保留前端（渲染层即时） |
| FE-11 | 意图/事件/动作中文标签与图标 | `tactical.ts:intentLabelCn` + 常量表 | 标签映射 | 无 projections 等价 | 保留前端（展示层） |
| FE-12 | 人类指令遥测差分/状态标签 | `commands.ts:commandTelemetryDeltas/commandStatusText/unitTelemetryOf/unitCommandLabel` | 聚合/规则判定 | `command_center/human_override.py`（服务端指令状态；读过） | 保留前端（展示派生） |
| FE-13 | HUD 测绘/生命周期内联聚合（activeMines/minedOut/spend/units） | `mapEngine.ts:tactRenderHud` 内联 | 聚合（渲染内联） | `projections/mines.py`（harvested/neverHarvested/utilization）、`mining_effectiveness.py`（读过） | 保留前端（渲染内联，不入迁移范围）；数据级聚合已由后端覆盖 |

**结论**：前端 engine/ 的规则投影分两类——
1. **保留前端 + 差分/锚定测试（本 wave 任务 1）**：FE-1/FE-2/FE-3（后端领域规则已覆盖，写同输入输出对比）；FE-9 射程常量锚定。
2. **保留前端（视口/交互/展示派生，无迁移面）**：FE-4/5/6/7/8/10/11/12/13——均为即时交互或渲染参数，后端无等价 projections 或语义不同；数据级聚合（FE-13）已由后端覆盖，前端保留的是渲染内联展示派生，不迁移。
3. **应迁后端（延迟验证）**：当前盘点未发现"前端计算应整体迁后端"的投影项——FE-1/2/3 后端已有规范实现（属"已覆盖"而非"待覆盖"），FE-4/5 视口参数不可迁，其余为交互/展示派生。FE-7/8/9 的动作合法性与射程常量在后端有权威实现（plan_validator），前端保留即时反馈副本，标注延迟验证指向该模块。

## 执行记录（按日追加）
- 2026-08-12 任务 0：见上。开始任务 1 前基线全绿。

## 执行记录（任务 1，2026-08-12）保留项差分测试
- 新增 `apps/command-center-web/test/projections-diff.test.ts`（4 例，skipped 0）：
  - `diff FE-2: tactUnitCost 与后端 domain.economy.unit_price 同输入输出`——Node 侧 `uv run python -c`（仓库标准工具链，CI 同用）实时计算后端权威值，与前端函数对拍（3 角色 × 9 人口）。
  - `diff FE-3: tactCoreCapacity 与后端 domain.economy.core_resource_capacity 同输入输出`（5 人口）。
  - `diff FE-1: maxUnitHp 与后端 planning.plan_validator.UNIT_MAX_HP 同输入输出`（3 角色）。
  - `anchor FE-2/FE-3/FE-1: 前端规则函数锚定后端实测值`——内嵌 2026-08-12 实跑后端输出（见下），含前端 clamp 兜底行为。
- 选择理由：任务允许"Node 侧差分测试（同输入输出对比）或前端单测锚定"——本 wave 两种都做：实时差分（最强证据）＋ 后端实测锚定（无 python 环境的回归网），互不 skip。
- 后端实测输出（2026-08-12，`uv run python -c`，锚定值来源）：
  `{"core_capacity": {"0": 10, "1": 10, "2": 10, "10": 50, "100": 500}, "unit_max_hp": {"ranger": 2, "vanguard": 4, "worker": 2}, "unit_price": {"ranger": {"0": 12, "5": 12, "19": 12, "20": 16, "24": 16, "25": 20, "30": 26, "45": 58, "100": 1038}, "vanguard": {"0": 10, "5": 10, "19": 10, "20": 13, "24": 13, "25": 17, "30": 22, "45": 48, "100": 865}, "worker": {"0": 5, "5": 5, "19": 5, "20": 7, "24": 7, "25": 8, "30": 11, "45": 24, "100": 433}}}`
- 差分输出（`node --test test/projections-diff.test.ts`）：
  `✔ diff FE-2: ... (125.4ms)  ✔ diff FE-3: ... (128.3ms)  ✔ diff FE-1: ... (123.1ms)  ✔ anchor ... (0.1ms)  ℹ tests 4 pass 4 skipped 0`

## 执行记录（任务 2，2026-08-12）迁移判定与延迟验证
- 判定（基于实跑/读过，非凭空）：前端 engine/ 无"应整体迁后端"的投影项——
  - **已覆盖（后端权威实现存在，前端保留即时副本 + 差分锚定）**：FE-1（UNIT_MAX_HP）、FE-2（unit_price）、FE-3（core_resource_capacity）、FE-9 射程常量（RANGER_SHOOT_RANGE=3）、FE-7 动作合法性（plan_validator 服务端校验）。
  - **保留前端（视口/交互/展示派生，无迁移面）**：FE-4/FE-5（视口参数，map_lod 是数据分块语义不同）、FE-6/FE-11/FE-12（交互/展示派生）、FE-8/FE-10（交互即时反馈；后端无视野半径等价，navigation.py 是射击视线阻挡语义不同）。
  - **渲染内联聚合（不入迁移范围）**：FE-13 tactRenderHud 内联测绘/生命周期聚合——数据级聚合已由 `mines.py`/`mining_effectiveness.py` 覆盖，前端保留的是渲染内联展示派生。
- 标注落点：
  - `src/arena_hero_agent/command_center/projections/README.md` 新增 "Frontend rule-projection ownership map (P5-8)" 段（8 行对照表，指向权威模块）。
  - `apps/command-center-web/src/engine/{tactical,utils,commands}.ts` 对应函数处加 P5-8 分类注释（纯注释，零行为改动）。
- 后端 projections 核心逻辑未动（git diff 仅 README.md 新增标注段，无 .py 改动）。

## 执行记录（最终验收，2026-08-12）
- `cd apps/command-center-web && npm run typecheck` ✓（tsc --noEmit 0 错）。
- `cd apps/command-center-web && npm run test:unit`：**52/52**（≥48，+4 差分/锚定），skipped 0，todo 0。
- `uv run python -m pytest -q`：**1268 passed**（≥1268），skipped 0。
- `git diff --check` ✓；git status 仅白名单内文件 + 交付文档。
- 后端 projections 核心逻辑零改动（README 标注除外）。
