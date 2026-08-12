# BLOCKED — W22 当前待办（2026-08-12）

## Captain 终裁（2026-08-13）：CC 只读可移植面收口（41/66）

剩余 10 条 GET 501 均为**非纯投影**，不属于 CC 只读可移植范围，本轮不迁；对应
里程碑单独做。CC 只读计数钉在 **41/66**，后续不再按“501 缺失”追。

- **8120 supervisor 类（Python 部署无 8120）**：/api/tenants、/api/agents、
  /api/overview、/api/audit/overview、/api/health/pipeline、
  /api/alliance/director → 归运维桥/遥测管线里程碑，不归只读投影。
- **外部官方商店代理（敏感面，X-Shop-Cookie）**：/api/shop、/api/shop/me、
  /api/shop/orders → 单独 shop 集成里程碑，不在只读面。
- **写回契约**：/api/commands（read + reconcile 会写回 cleanup，P5-9 is_write_route 未 gate）→ 归写侧里程碑；只读面不得暴露无门禁写回，**维持 501**。
## SKIP / 已解除（W44 更新，2026-08-12）

- **alliance exploration / survey-mining → 已解除（W44，w44/cc-wiring 已合入）**：
  `GET /api/exploration`、`GET /api/survey/mine-patterns`、`GET /api/alliance/survey/mining`
  已接线（投影已移植；consensus-mining 新写纯 join），OpenAPI 200 schema + 28 测试，
  main 门禁 1565 passed。
- **director 端点仍 SKIP**：依赖外部 arena-agent supervisor Debug API
  `http://127.0.0.1:8120/alliance-director`（非投影移植项，运维接线，Python 部署无 8120）。
- **W44 遗留 → 已关闭（w44/cc-wave5，2026-08-12）**：wave-3/4 已接线投影核的 Node golden
  对拍已完成——exploration coverage / mine-patterns predictions（5 个纯函数）/
  consensus-mining / survey/mine（物化 survey-db 跑真实 `loadSurveyDb`+`loadResourceTimeline`）/
  enemy-cores / decision-input，共 12 个新 fixture 全部 MATCH（现 35 个 golden case，
  `tests/command_center/projections/test_golden_parity.py` 38 passed）。Oracle harness 在
  `~/tmp/cc-oracle.mjs` / `~/tmp/cc-oracle-survey-mine.mjs`（仓外，TS checkout 保持只读）。
- **W44 只读接线第三波（w44/cc-wave3，2026-08-12）**：详见下方第三波小节；
  本小节原 leaderboard / audit/human SKIP 已按 captain 预裁决更新。
  - `GET /api/audit/human` **已接线（w44/cc-wave3）**：按 Python manifest fail-closed 语义
    （`tenant_param=tN`，默认 t1，tenant=all/非法 → 400）；与 TS 的 ALLOWED divergence 已记录
    （TS 宽松：默认全租户、非法租户降级不 400）。Python-first fail-closed 纪律优先（2026-08-12 captain 裁决）。
  - `GET /api/leaderboard` **已解除（w44/cc-wave7，2026-08-12）**：服务端富化两部分已移植——
    `loadOurUsernames`（各租户 calibration 受控 CORE owner_username）与
    `buildEncounteredIndex`（`intel.py:load_alliance_intel` 移植后的 enemies 形状，含
    raid-risk/信标载者推断/enemyUnitMemory）已 1:1 补出；501→200，OpenAPI 200 + golden MATCH
    （详见下方第七波小节）。
- **路由映射裁决（2026-08-12 captain）**：联盟探索覆盖 canonical 路由是
  `/api/alliance/exploration`（TS `loadAllianceExploration`）；`/api/exploration`
  （TS `loadTenantSurveyCached`：per-tenant survey+lifecycle+current）**已接线**
  （w44/cc-wave6，2026-08-12）——wave-6 survey-db 数据源落地后按 pre-ruling 解除。
- **mapEngine / RedeemPanel / IntelPanel 调用方迁移：继续 SKIP**（2026-08-12 Meitner 调查）：
  目标写端点（`/api/command*`、`/api/shop/order`、`/api/redeem`）Python 后端全部 501
  （P5-9 写门就绪但无实现），现在迁移会指向死 API；正确顺序 = 后端写端点落地 →
  抽 mapEngine I/O 边界 + 补测试 → 前端调用方换生成 client。



## W44 第八波只读接线（w44/cc-wave8，2026-08-13）

replay + deeds + deeds/journal 三路由接线（needs-new-data-source 关闭），
并补齐四个投影核的 Node golden 对拍：

- `GET /api/replay` **501 → 200**：`replay.py` 移植 `loadReplay`（最新 run 的
  单位/核心每 tick 轨迹 + 事件帧）；空根 fail-open 200 `{tenant, generatedAt,
  replay: null}`（TS 同形状，不 500）。OpenAPI 200 schema + golden MATCH。
- `GET /api/deeds` **501 → 200**：`deeds.py` 移植 `loadDeeds`（★3-4 稀有事件
  扫描 + ★2 survey-db 里程碑 + ★1 常规限流）+ `alliance_deeds.py` 移植
  `loadAllianceDeeds`（新敌核/热区/抢矿/资源濒危）；tenant=all 合并联盟事迹；
  `?limit` clamp 1..200。空根 fail-open。OpenAPI 200 schema + golden MATCH。
- `GET /api/deeds/journal` **501 → 200**：`deeds_journal.py` 移植 `loadDeedsJournal`
  核心（tick 窗口头条/分类计数/分租户统计/中文叙事/上一窗口 delta +
  window/category/minStar 筛选）；空根 fail-open。OpenAPI 200 schema + golden MATCH。
- **Node golden 对拍（4 新 case 全 MATCH）**：`replay_basic` / `deeds_basic` /
  `alliance_deeds_basic` / `deeds_journal_basic` 物化 calibration runs + survey-db，
  跑真实 TS `loadReplay` / `loadDeeds` / `loadAllianceDeeds` / `loadDeedsJournal`；
  oracle harness 在 `~/tmp/cc-oracle-replay.mjs` / `cc-oracle-deeds.mjs` /
  `cc-oracle-alliance-deeds.mjs` / `cc-oracle-deeds-journal.mjs`（仓外，TS checkout 只读）。
  现 47 个 golden case。
- **注册 divergence**：
  - deeds / deeds-journal 的 45s / 30s 内存缓存不移植（Python 每次重算，输出同形状）；
  - `buildAuditDeeds`（AUDIT_INSIGHT 日记层）不移植——依赖 `loadAuditOverview`
    （8120 supervisor pipeline，仍 501），日记不含审计洞察；
  - deeds-journal `tenant=all` 的叙事富化行（商店/测绘覆盖/决策健康/威胁/采矿执行/
    管线健康）不移植——依赖外部商店 fetch 与 8120/写副作用管线投影；
  - replay 45s 缓存不移植（每次重算，输出同形状）。
- 维持 SKIP（不变，10 条）：`/api/audit/overview`、`/api/health/pipeline`、
  `/api/overview`、`/api/tenants`、`/api/agents`、`/api/shop`、`/api/shop/me`、
  `/api/shop/orders`、`/api/commands`、`/api/alliance/director`。

## W44 第六波只读接线（w44/cc-wave6，2026-08-12）

survey-db 数据源扩展 + 三路由接线（needs-new-data-source 关闭）：

- **AGENT_SCHEMA 扩展（additive only）**：补齐 TS survey-db 缺失 8 表
  （`sync_meta`/`unit_lifecycle`/`core_spends`/`resource_events`/`chunks`/
  `heat_archive`/`resource_absences`/`notable_events`，列类型/default 与
  `advice_fixture.SURVEY_SCHEMA` 逐列一致）+ `idx_units_seen_controlled_tick`；
  `CREATE TABLE IF NOT EXISTS` 非破坏，存量库照常打开；新增 schema-parity 测试。
- `GET /api/audit/lifecycle` **501 → 200**（needs-new-data-source 关闭）：
  `lifecycle.py` 移植 `aggregateLifecycle` + `loadLifecycleAudit`
  （calibration case 事件聚合 + survey-db `unit_lifecycle`/`core_spends`/
  `notable_events` 回填）；`?tenant=all|tN` 默认 all；空根 fail-open 200
  （TS 空 payload，不 500）；OpenAPI 200 oneOf schema + 8 测试。
- `GET /api/survey` **501 → 200**（needs-new-data-source 关闭）：
  `survey.py` 移植 `loadSurveyDb`/`loadChunksDb`/`loadLifecycleDb`/
  `loadSpendTrend`/`loadUnitLifecycleDb` + `loadTenantSurveyCached` 与逐租户
  组合（`?tenant=all|tN` 默认 all、`?states=visible,stale` 过滤、colors）；
  缺库 = 逐租户 `{error: "survey db missing"}`（TS parity）；OpenAPI 200 + 13 测试。
- `GET /api/exploration` **501 → 200**（2026-08-12 captain pre-ruling 解除：
  “数据源存在即接线”）：`exploration.py` = `loadTenantSurveyCached` + 缺库
  calibration-scan 回退（TS `loadSurvey`）+ `loadWorld` current 子集；
  全缺 = `{tenant, generatedAt, survey: null}` fail-open；OpenAPI 200 + 路由测试。
- `GET /api/audit/overview` **仍不接线**：lifecycle 输入现已可用，但 8120
  supervisor pipeline 输入仍未移植（`loadAuditOverview` 组合含管线健康），
  维持 SKIP，缺口精确记录于下表。
- `advice_fixture.py` 物化器补 `unit_lifecycle`/`core_spends`/`notable_events`/
  `agent_events` 表写入（TABLE_MAP 扩展），供 fixture/golden 物化。
- OpenAPI 200 schema 三路由 + `openapi-v1.json` + 生成 TS client/types
  （tsgen hash pin）已同步；baseline 1732 → 1753 passed。


## W44 第七波只读接线（w44/cc-wave7，2026-08-12）

intel + leaderboard 两路由接线（needs-new-data-source / needs-new-projection 关闭）：

- **`projections/intel.py`（新）**：移植 `intel.ts` `loadAllianceIntel`（enemy core 扫描
  `seenCores`/`enemyUnitById`/`combatNearCore` + `assessRaidRisk` 级联 + 30-run×8-case 扫描 +
  survey-db `core_hunts` 贴脸记忆合并 + leaderboard 威胁画像 join + 信标载者推断 +
  `enemyUnitMemory` 上限 100）+ `trails.ts` `loadBeaconTrail`（跨 run 合并/同格去重/96 点上限/
  2000 tick 时间窗）+ `buildEncounteredIndex`。空根 fail-open 200（4 租户 `runId: null` 占位，
  TS 同形状）。
- **`projections/leaderboard.py` 扩展**：`load_our_usernames`（TS `loadOurUsernames`，最新 run
  受控 CORE owner_username）+ `build_leaderboard_payload`（TS server.ts 组合：profiles 富化
  `ours`/`encountered` + `snapshotAtMs` 透传 + `encounteredCount`/`encountered`）。快照缺失 →
  **200 空成功形状 + TS 404 error 文案**（注册 divergence：TS 返回 404，wave-7 fail-open 纪律
  不 500）。
- `GET /api/intel`、`GET /api/leaderboard` **501 → 200**；OpenAPI 200 schema（intel 全形状 +
  leaderboard 全形状）+ `openapi-v1.json` + 生成 TS client/types（tsgen hash pin 已更新）。
- **Node golden 对拍（2 新 case 全 MATCH）**：`intel_basic` / `leaderboard_basic` 物化多 run
  calibration + survey-db core_hunts + leaderboard 快照，跑真实 TS `loadAllianceIntel` /
  server.ts leaderboard 组合；oracle harness 在 `~/tmp/cc-oracle-intel.mjs` /
  `~/tmp/cc-oracle-leaderboard.mjs`（仓外，TS checkout 只读）。`advice_fixture.py` 补
  `calibrationRuns` 多 run 物化。现 43 个 golden case。
- **注册 divergence**：
  - leaderboard 快照缺失 200（TS 404）；
  - `maybeRefreshLeaderboardLazy` 不移植（官方 API 外部 fetch 写副作用，P5-9 gate 路由）；
  - `ageSeconds`/`stale` 依赖墙钟 `Date.now()`（Python 注入 `now_ms`），golden 对拍时
    `ageSeconds` 剥离、`stale` 按 case 剥离（ALLOWED_DIFFERENCES 已登记）；
  - intel/leaderboard 30s 内存缓存不移植（Python 每次重算，输出同形状，无后台刷新）；
  - `history.jsonl` 仅由 POST `/api/leaderboard/refresh` 追加，不在 GET payload 内（server.ts
    核实），GET 不读。
- 维持 SKIP（不变）：`/api/audit/overview`（8120 pipeline）、
  `/api/health/pipeline`（surveySync 写副作用）、`/api/tenants`、`/api/agents`、
  `/api/shop`、`/api/shop/me`、`/api/shop/orders`（外部商店 cookie 契约）、`/api/commands`
  （reconcile 写回契约）、`/api/alliance/director`（8120）；mapEngine 调用方迁移顺序不变。

## W44 第五波只读接线（w44/cc-wave5，2026-08-12）

- `/api/audit/alignment` **501 → 200**（needs-new-projection 关闭）：输入投影已全齐
  （decision-audit / mine-utilization(+trend) / mining-effectiveness / alliance snapshot），
  新 port `alignment.py`（`aggregate_alignment` 纯函数 + `load_alignment_audit` loader），
  grade/reasons 1:1 对齐 TS `alignment-audit.ts`（含 `(rate*100).toFixed(0)` 百分比渲染与
  `Math.round` 速率四舍五入）；无 tenant 参数（manifest tenant_param=null，TS 默认行为）；
  空根 fail-open 全租户 data_gap。OpenAPI 200 schema + 生成 TS client/types 已同步；
  8 个 fixture 级测试 + alignment Node golden 对拍（MATCH）。
- **wave-3/4 投影核 Node golden 对拍（W44 遗留关闭）**：见上方 SKIP 小节 W44 遗留条目——
  6 个投影核（exploration coverage / mine-patterns predictions×5 / consensus-mining /
  survey/mine / enemy-cores / decision-input）共 12 个新 fixture 全部 MATCH（35 个 golden case），
  oracle harness 在 `~/tmp/cc-oracle.mjs` / `~/tmp/cc-oracle-survey-mine.mjs`（仓外）。

## W44 第四波只读接线（w44/cc-wave4，2026-08-12）

wave-3 标为 needs-small-loader 的 6 条 + decision-input 全部接线（501→200，7 条），
并移植 mine-patterns predictions（wave-3 注册缺口关闭）。

### 已接线（501 → 200，7 条）

| 路由 | 数据源 | 说明 |
|---|---|---|
| `GET /api/events` | 最新 run calibration cases `after.state.events`（before 恒空，2026-08-08 修复）+ EVENT_KINDS 过滤 | `events.py:load_events`；n clamp 1..200、tick 倒序、扫最近 20 case；空根 fail-open 200 `{tenant, generatedAt, events: []}` |
| `GET /api/plan` | 最新 case 顶层 `plan` + `parse_tick` | `snapshots.py:load_plan`；无 run/case → plan null；解析失败带 `error` 字段（TS parity） |
| `GET /api/world` | 最新 case `after.state ?? before.state` + tick | `snapshots.py:load_world`；无 run/case → state null、caseFile null |
| `GET /api/survey/mine` | survey-db `resources`（TS loadSurveyDb shape：derived fresh/stale + persisted harvested/empty 负态优先 + harvest 聚合）+ `resource_events` timeline（tick 升序 cap 500） | `survey_mine.py:load_survey_mine`；cell=x,y，缺省取最近活跃矿；缺库 → 200 `{tenant, error: "survey db missing"}`（TS parity）；P5-3 无 resource_events → timeline 空（注册 divergence） |
| `GET /api/survey/enemy-cores` | 跨租户 `core_hunts` → `build_enemy_core_states`（ACTIVE/RELOCATED/STALE + threat high≤60/medium≤200，STALE 不高威胁）+ alliance_snapshot 友核 | `enemy_cores.py`；无 tenant 参数（TS parity）；currentTick = max last_seen；空根 fail-open 200 |
| `GET /api/redeem/history` | `runtime/redeem-log.jsonl` 尾读（MAX_KEEP 200） | `redeem.py:load_redeem_history`；Python 无 redeem 写路径（POST /api/redeem 501）恒空 fail-open；`count=len(records)` 为注册 divergence（TS count=进程内数组长度） |
| `GET /api/survey/decision-input` | mine-patterns predictions + survey-db chunks + exploration resurveyTargets + core-threats（core_trails × 友核）+ mine-utilization candidates + consensus-mining threat | `decision_input.py:load_decision_input`；tenant fail-closed tN（all/非法→400）；各输入 fail-open（TS try/catch parity），不阻断 refill/chunk |

### mine-patterns predictions 已移植（wave-3 注册缺口关闭）

- `mine_patterns.py` 完整移植 TS A15/A16 refill 模型：`compute_refill_stats(_from_absences)`、
  `compute_refill_predictions(_from_absences)`、`compute_absent_stats`、`compute_dead_mines`、
  `compute_prediction_accuracy`（REFILL_GAP_TICKS=5、DEAD_ABSENT_TICKS=200；缺席段→重见优先于出现窗口）。
- `alliance/mining` 的 `predictedNextTick`/`dueInTicks` 现在从 predictions 填充（wave-3 恒 null 缺口关闭）。
- P5-3 watermark fallback：`sync_meta MAX(last_tick)` → `agents MAX(tick)`；缺表逐表降级空输入。

### 跳过的（wave-4 范围内）

无。6 条 needs-small-loader + decision-input 全部接线。其余 SKIP 维持 wave-3 分类不变
（缺数据源/契约裁决，见第三波小节）；wave-6 已接线 audit-lifecycle / survey / exploration（见上方第六波小节），
剩 leaderboard / deeds / journal / audit-overview / intel / health-pipeline / replay / tenants /
agents / shop / me / orders / commands / director。



## W44 第三波只读接线（w44/cc-wave3，2026-08-12）

30 条 GET 路由逐一 triage（TS oracle `server.ts` + `lib/*.ts` vs Python 现有投影）。接线 4 条，
其余如实分类（wireable / needs-small-loader / needs-new-data-source / contract-decision）。

### 已接线（501 → 200，4 条）

| 路由 | 数据源 | 说明 |
|---|---|---|
| `GET /api/audit/human` | `runtime/human-command-audit.jsonl`（`human.py:read_human_audit`） | captain 裁决 fail-closed tN（默认 t1，all/非法→400）；envelope `{generatedAt, tenant, count, records}`，limit clamp 1..500 |
| `GET /api/alliance/cluster` | `alliance_snapshot` members → `build_alliance_cluster_view`（新 loader `cluster_input_of_members`） | 纯组合小 loader；空根 fail-open 200（0 members/0 groups） |
| `GET /api/alliance/mining` | snapshot(cores/workers) + alliance_survey(observers/conflicts) + mine_utilization(candidates) + mine_patterns(预测；Python 目前恒空) + enemy_heat(16×16 桶) | 新 loader `load_alliance_mining`；`predictedNextTick`/`dueInTicks` 恒 null 为已注册缺口（mine-patterns predictions 未移植），fail-open——**wave-4 已补 predictions（缺口关闭）** |
| `GET /api/registry/agents` | `runtime/registry.db`（`registry.py:RegistryStore.list_agents`，已有 store 移植） | 小 loader；key 序列化（asdict）；空根 fail-open 200（空 agents）；TS 同样 open 即建库 |

### Triage 分类（未接线 26 条）

| 分类 | 路由 | 缺口/说明 |
|---|---|---|
| needs-small-loader | `/api/events` | （wave-4 已接线） calibration case `after.state.events` + EVENT_KINDS 过滤；Python jsonl 基座齐备，未在本波接线（新 loader + 大 payload） |
| needs-small-loader | `/api/plan` | （wave-4 已接线） 最新 case `plan` 字段直读；极小 loader，未接线 |
| needs-small-loader | `/api/world` | （wave-4 已接线） 最新 case `before/after.state` 直读；极小 loader，未接线（world state payload 大，需先契约） |
| needs-small-loader | `/api/survey/mine` | （wave-4 已接线） survey-db resources + `resource_events` timeline；Python schema 无 resource_events 时 timeline 空（同 mines.py ALLOWED divergence），可接但需新 loader |
| needs-small-loader | `/api/survey/enemy-cores` | （wave-4 已接线） `enemy-core-state.ts` 聚合（core_hunts → ACTIVE/RELOCATED/STALE + 威胁级）未移植；Python 有 core_hunts 表 + core_trails 投影，聚合逻辑 ~100 行待移植 |
| needs-small-loader | `/api/redeem/history` | （wave-4 已接线，恒空 fail-open） `redeem-log.jsonl` 尾读极小 loader，但 Python 侧无 redeem 写路径（POST /api/redeem 501，属 shop 外部集成），接了恒空——等写路径落地再接 |
| ~~needs-new-projection~~ → **已接线（w44/cc-wave7）** | `/api/leaderboard` | `load_our_usernames`（calibration 受控 CORE owner_username）+ `build_leaderboard_payload`（encountered 来自 `intel.py` enemies 索引），501→200；OpenAPI 200 + golden MATCH；快照缺失 200（TS 404）为注册 divergence |
| ~~needs-new-projection~~ → **已接线（w44/cc-wave5）** | `/api/audit/alignment` | `alignment.py` 移植完成（aggregate_alignment + load_alignment_audit），501→200；OpenAPI 200 + golden MATCH |
| needs-new-projection | `/api/survey/decision-input` | （wave-4 已接线） `decision-input.ts`（矿刷新预测 dueInTicks + chunk 覆盖）——依赖 mine-patterns predictions（Python 恒空）+ chunks 表 |
| ~~needs-new-data-source~~ → **已接线（w44/cc-wave6）** | `/api/audit/lifecycle` | `lifecycle.py` 移植（AGENT_SCHEMA 已补 `unit_lifecycle`/`core_spends`/`notable_events`），501→200；OpenAPI 200 + 8 测试 |
| needs-new-data-source | `/api/audit/overview` | 组合含 lifecycle（wave-6 已可用）+ pipeline（8120 supervisor 输入）两个输入——pipeline 仍未移植，**维持 501 不接线** |
| ~~needs-new-data-source~~ → **已接线（w44/cc-wave8）** | `/api/deeds` | `deeds.py` 移植 `loadDeeds`（★3-4 稀有 + ★2 survey-db 里程碑 + 联盟事迹扫描），501→200；OpenAPI 200 + golden MATCH |
| ~~needs-new-data-source~~ → **已接线（w44/cc-wave8）** | `/api/deeds/journal` | `deeds_journal.py` 移植 `loadDeedsJournal`（事迹流日记聚合 + window/category/minStar 筛选），501→200；OpenAPI 200 + golden MATCH |
| needs-new-data-source | `/api/health/pipeline` | survey-db 水位 vs live tick + `surveySync` 桥状态；且 TS 请求路径会触发惰性 survey:sync（写副作用，Python P5-9 已 gate 该路由） |
| ~~needs-new-data-source~~ → **已接线（w44/cc-wave7）** | `/api/intel` | `intel.py` 移植 `loadAllianceIntel`（enemy core 扫描 + raid-risk + 信标载者 + enemyUnitMemory）+ `loadBeaconTrail` + `buildEncounteredIndex`，501→200；OpenAPI 200 + golden MATCH |
| needs-new-data-source | `/api/overview` | supervisor 8120 + agents 台账 + outcome.jsonl 双源合并，未移植 |
| ~~needs-new-data-source~~ → **已接线（w44/cc-wave8）** | `/api/replay` | `replay.py` 移植 `loadReplay`（calibration cases → 单位/核心轨迹 + 事件帧），501→200；OpenAPI 200 + golden MATCH |
| needs-new-data-source | `/api/tenants` | supervisor /ready 探活（8120）；Python 无 supervisor 桥，接了一律 live=false |
| needs-new-data-source | `/api/agents` | supervisor + overview + agent db + world 合并（`/api/agents` 大组合） |
| ~~needs-new-data-source~~ → **已接线（w44/cc-wave6）** | `/api/survey` | `survey.py` 移植（AGENT_SCHEMA 已补 `lifecycle`/`chunks`/`spends`/`resource_events` 表），501→200；OpenAPI 200 + 13 测试 |
| ~~needs-new-data-source~~ → **已接线（w44/cc-wave6）** | `/api/exploration` | `exploration.py` 移植（survey-db 表已补 + loadWorld 已有 `snapshots.py`），501→200；缺库 calibration-scan 回退；全缺 survey null fail-open |
| needs-new-data-source | `/api/shop` | 官方商店外部 API 代理（无 cookie）；Python 部署无该代理，契约裁决 |
| needs-new-data-source | `/api/shop/me` `/api/shop/orders` | 外部商店 API + X-Shop-Cookie 头转发（登录 Cookie 敏感面），契约裁决 |
| contract-decision | `/api/commands` | manifest `write_semantics=read + reconcile (may write-back cleanup)`：GET 会写回（reconcile 清 satisfed/applied + 取消 stuck goal），Python P5-9 `is_write_route` 不含它（不 gate）——只读面带无门禁写回，需 captain 裁决 |
| contract-decision | `/api/alliance/director` | 仍 SKIP：外部 arena-agent supervisor Debug API `127.0.0.1:8120/alliance-director`（非投影移植项，Python 部署无 8120） |


## P3 观察（release-005 待修，2026-08-12 W43）

- **submit 超时护栏已实现（main@3a482b5）**：`TickLoopConfig.submit_timeout_seconds`
  （默认 None 离线不变，live 10s = 2x SDK 5s 超时），submit 超时记
  `REJECTED("submit timed out")` 并受 `submit_error_policy` 控制——挂起的网络提交
  不再可能永久阻塞 tick loop。decide/submit 非 SDK 异常 fail-closed 已用测试钉死。
  架构约束与"为何不用外部 agent 框架"决策记录已写入 docs/architecture.md。

- telemetry `processRunId` 恒 "unknown"（sink 默认值，offline/live 两路径一致；
  health 快照有真实 run id）——不影响数据完整性，随 release-005 修复。
- telemetry `agentLatencyMs`/`selectionLatencyMs`：根因已查明，tick_loop 只追踪
  budget 消耗不追踪墙钟延迟（P4-4 设计使然），非 bug；随 release-005/P3 补墙钟字段。
- **live `stream_ended` 硬化（F5，已实现 main@3272009，随 release-005 部署）**：
  SDK `events()` 对 websocket code 1000 正常关闭直接 return（不重连），tick loop 以
  `stream_ended` 结束、进程干净退出（exit 0，systemd 不拉起）。2026-08-12 观测到一次
  （catch-up 会话关闭，一次性）；F5 = `continue_on_stream_ended`（默认 False 离线不变，
  live 置 True）在 clean stream end 时重开 source 继续（max_reconnects 有界），重放尾
  去重。1531 passed。

其余：无。
