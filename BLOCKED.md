# BLOCKED — W22 当前待办（2026-08-12）

## SKIP / 已解除（W44 更新，2026-08-12）

- **alliance exploration / survey-mining → 已解除（W44，w44/cc-wiring 已合入）**：
  `GET /api/exploration`、`GET /api/survey/mine-patterns`、`GET /api/alliance/survey/mining`
  已接线（投影已移植；consensus-mining 新写纯 join），OpenAPI 200 schema + 28 测试，
  main 门禁 1565 passed。
- **director 端点仍 SKIP**：依赖外部 arena-agent supervisor Debug API
  `http://127.0.0.1:8120/alliance-director`（非投影移植项，运维接线，Python 部署无 8120）。
- **W44 遗留**：三端点 Node golden 对拍（`run-arena-report` + `.golden.json`）待做；
  现为纯 Python fixture 级验证（`fixtures/cc_wiring/`，W25-A now_ms 注入 + fail-open 钉死）。
- **W44 只读接线第三波（w44/cc-wave3，2026-08-12）**：详见下方第三波小节；
  本小节原 leaderboard / audit/human SKIP 已按 captain 预裁决更新。
  - `GET /api/audit/human` **已接线（w44/cc-wave3）**：按 Python manifest fail-closed 语义
    （`tenant_param=tN`，默认 t1，tenant=all/非法 → 400）；与 TS 的 ALLOWED divergence 已记录
    （TS 宽松：默认全租户、非法租户降级不 400）。Python-first fail-closed 纪律优先（2026-08-12 captain 裁决）。
  - `GET /api/leaderboard` 仍 SKIP（缺口已精确记录）：服务端富化两部分——
    `loadOurUsernames`（各租户 calibration 受控 CORE owner_username）Python 可用小 loader 补；
    `buildEncounteredIndex`（`loadAllianceIntel().enemies`，含 raid-risk/信标载者推断/enemyUnitMemory，
    intel.ts 整仓未移植，Python 只有 alliance_snapshot/shared_intel 融合模型，无 TS enemies 形状）——
    缺该数据源，profiles `encountered`/`encounteredCount`/`encountered` 无法 1:1 补出，不硬凑。
- **路由映射裁决（2026-08-12 captain）**：联盟探索覆盖 canonical 路由是
  `/api/alliance/exploration`（TS `loadAllianceExploration`）；`/api/exploration`
  （TS `loadTenantSurveyCached`：per-tenant survey+lifecycle+current）Python 未移植，
  保持 501（已注册+校验，非 404），待后续 port。
- **mapEngine / RedeemPanel / IntelPanel 调用方迁移：继续 SKIP**（2026-08-12 Meitner 调查）：
  目标写端点（`/api/command*`、`/api/shop/order`、`/api/redeem`）Python 后端全部 501
  （P5-9 写门就绪但无实现），现在迁移会指向死 API；正确顺序 = 后端写端点落地 →
  抽 mapEngine I/O 边界 + 补测试 → 前端调用方换生成 client。


## W44 第三波只读接线（w44/cc-wave3，2026-08-12）

30 条 GET 路由逐一 triage（TS oracle `server.ts` + `lib/*.ts` vs Python 现有投影）。接线 4 条，
其余如实分类（wireable / needs-small-loader / needs-new-data-source / contract-decision）。

### 已接线（501 → 200，4 条）

| 路由 | 数据源 | 说明 |
|---|---|---|
| `GET /api/audit/human` | `runtime/human-command-audit.jsonl`（`human.py:read_human_audit`） | captain 裁决 fail-closed tN（默认 t1，all/非法→400）；envelope `{generatedAt, tenant, count, records}`，limit clamp 1..500 |
| `GET /api/alliance/cluster` | `alliance_snapshot` members → `build_alliance_cluster_view`（新 loader `cluster_input_of_members`） | 纯组合小 loader；空根 fail-open 200（0 members/0 groups） |
| `GET /api/alliance/mining` | snapshot(cores/workers) + alliance_survey(observers/conflicts) + mine_utilization(candidates) + mine_patterns(预测；Python 目前恒空) + enemy_heat(16×16 桶) | 新 loader `load_alliance_mining`；`predictedNextTick`/`dueInTicks` 恒 null 为已注册缺口（mine-patterns predictions 未移植），fail-open |
| `GET /api/registry/agents` | `runtime/registry.db`（`registry.py:RegistryStore.list_agents`，已有 store 移植） | 小 loader；key 序列化（asdict）；空根 fail-open 200（空 agents）；TS 同样 open 即建库 |

### Triage 分类（未接线 26 条）

| 分类 | 路由 | 缺口/说明 |
|---|---|---|
| needs-small-loader | `/api/events` | calibration case `after.state.events` + EVENT_KINDS 过滤；Python jsonl 基座齐备，未在本波接线（新 loader + 大 payload） |
| needs-small-loader | `/api/plan` | 最新 case `plan` 字段直读；极小 loader，未接线 |
| needs-small-loader | `/api/world` | 最新 case `before/after.state` 直读；极小 loader，未接线（world state payload 大，需先契约） |
| needs-small-loader | `/api/survey/mine` | survey-db resources + `resource_events` timeline；Python schema 无 resource_events 时 timeline 空（同 mines.py ALLOWED divergence），可接但需新 loader |
| needs-small-loader | `/api/survey/enemy-cores` | `enemy-core-state.ts` 聚合（core_hunts → ACTIVE/RELOCATED/STALE + 威胁级）未移植；Python 有 core_hunts 表 + core_trails 投影，聚合逻辑 ~100 行待移植 |
| needs-small-loader | `/api/redeem/history` | `redeem-log.jsonl` 尾读极小 loader，但 Python 侧无 redeem 写路径（POST /api/redeem 501，属 shop 外部集成），接了恒空——等写路径落地再接 |
| needs-new-projection | `/api/leaderboard` | `ours`（calibration 受控 CORE owner_username）小 loader 可补；`encountered` 需 `intel.ts loadAllianceIntel` 移植（enemies 形状未移植）→ 整体不接线（缺数据源，不硬凑），见上 SKIP 精确缺口 |
| needs-new-projection | `/api/audit/alignment` | 输入全齐（decision-audit / mine-utilization / mining-effectiveness / trend / snapshot），但 alignment 聚合（grade/reasons）未移植 |
| needs-new-projection | `/api/survey/decision-input` | `decision-input.ts`（矿刷新预测 dueInTicks + chunk 覆盖）——依赖 mine-patterns predictions（Python 恒空）+ chunks 表 |
| needs-new-data-source | `/api/audit/lifecycle` | `loadLifecycleAudit` 依赖 survey-db `unit_lifecycle`/`core_spends`/`resource_events` 表——Python AGENT_SCHEMA 无这些表 |
| needs-new-data-source | `/api/audit/overview` | 组合含 lifecycle + pipeline 两个未移植输入 |
| needs-new-data-source | `/api/deeds` | 事迹扫描（★3-4 稀有 + ★2 survey-db 里程碑 + 联盟事迹）+ 45s 缓存，全新产品能力 |
| needs-new-data-source | `/api/deeds/journal` | 事迹流日记聚合 + category/minStar 筛选 |
| needs-new-data-source | `/api/health/pipeline` | survey-db 水位 vs live tick + `surveySync` 桥状态；且 TS 请求路径会触发惰性 survey:sync（写副作用，Python P5-9 已 gate 该路由） |
| needs-new-data-source | `/api/intel` | `intel.ts loadAllianceIntel`（enemy core 扫描 + raid-risk + 信标载者 + enemyUnitMemory）整仓未移植 |
| needs-new-data-source | `/api/overview` | supervisor 8120 + agents 台账 + outcome.jsonl 双源合并，未移植 |
| needs-new-data-source | `/api/replay` | 回放轨迹重建（calibration cases → replay 序列）未移植 |
| needs-new-data-source | `/api/tenants` | supervisor /ready 探活（8120）；Python 无 supervisor 桥，接了一律 live=false |
| needs-new-data-source | `/api/agents` | supervisor + overview + agent db + world 合并（`/api/agents` 大组合） |
| needs-new-data-source | `/api/survey` | per-tenant SurveyData + lifecycle/spendsTrend/unitsDetail/chunks（缺 lifecycle/chunks/spends 表） |
| needs-new-data-source | `/api/exploration` | `loadTenantSurveyCached` 的 survey+lifecycle+current：survey-db `sync_meta`/`resource_events`/`unit_lifecycle`/`core_spends`/`chunks` 表缺；current world state 无 loadWorld 移植——**缺口精确记录，不接线**（2026-08-12 captain 预裁决：现有投影补不出则记录） |
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
