# PROGRESS — W18–W25 收口（w25/advice 基线 main@f4ba3eb）

> 状态：W18–W21 已合入 main 并 push；W25-A alliance advice 读模型移植在
> `.worktrees/w25-advice`（分支 `w25/advice`）完成，待合入。本文档为收口摘要；
> 各 wave 明细见 `git log main`。范围仅 `arena-hero-agent` 本仓，未动根仓 docs /
> Lab / SDK / production / 真实数据 / secrets。

## W18 — P4-20 deterministic offline contestant seam（branch `w18/p4-20`）

- Commits：`02a5aba`（canonical digests + manifests）、`8191a8b`（batch runner +
  fail-closed run id）、`e254b40`（docs: batch usage）。
- 内容要点：
  - `cli/canonical.py`：strip_nonsemantic（`recordedAtNs`/`updatedAtNs`/`startedAtNs`）
    + 排序键紧凑重编码 → 稳定 SHA-256；每次成功 `run` 写 `manifest.json`
    （schemaVersion/tenantId/runId/processRunId/digests）。
  - `batch` 子命令：每文件一场景，稳定 run id `scenario-<stem>-seed-<n>`，
    输出 `<data-root>/<run-id>/<tenant>/`；全量预校验，坏批次不产生部分产物。
  - Fail-closed：非法 run id / tenant / 输入 exit 2；同 tenant 重复 run id 冲突
    不追加；错误输出无绝对路径/secret。
- 门禁：`pytest -q` = **1410 passed**（基线 1379 + 31），skipped 0；
  ruff format/check、ty check、`git diff --check` 全 PASS。

## W19 — 硬化（branch `w19/hardening`）

- Commits：`20befd2`（W19-1 recorder）、`a84e018`（W19-2 telemetry）、
  `d8bdeb3` + `f0b5144`（W19-3 OpenAPI）、`ea40d20`（W19-5 空壳清理）。
- 内容要点：
  - recorder：非空既有库打开时 PRAGMA quick_check + 期望表检查，撕裂/截断
    SQLite 显式 raise 而非静默重建；零字节文件仍可初始化。
  - telemetry：JsonlWriter.flush OSError 计入 `dropped_count` + `last_error` 并
    re-raise（持久化边界失败既计数又对调用方可见）。
  - OpenAPI：主读端点 200 响应 schema 非空（字段名对齐 P5-4 投影），其余路由
    回退非空 object；regenerate `openapi-v1.json` + generated TS。
  - 删除无引用的空壳 `intelligence/` 包。
- 门禁：`pytest -q` = **1417 passed**（在 `ea40d20` 复跑验证），skipped 0；
  ruff/ty 全绿。

## W20 — alliance snapshot 读模型（branch `w20/alliance-readmodel`）

- Commits：`8ee5929`（shared-intel fusion）、`5ebbb88`（/api/alliance/snapshot +
  OpenAPI）、`16c19f6`（parity/loader/API 测试）。
- 内容要点：
  - `alliance/shared_intel.py`：跨租户目击融合 + 新鲜度窗口，TS oracle 对拍。
  - `command_center/projections/alliance_snapshot.py`：快照投影 + `/api/alliance/snapshot`
    端点 + OpenAPI 契约 + generated TS；golden parity 测试。
- 门禁：`pytest -q` = **1441 passed**（在 `16c19f6` 复跑验证），skipped 0；ruff/ty 全绿。

## W21 — alliance defense 读模型（branch `w21/alliance-defense`，当前 HEAD）

- Commits：`bd3aea6`（alliance-defense 联合协调读模型）、`375b6b1`
  （/api/alliance/defense + OpenAPI）、`b85cd81`（parity/loader/API 测试）。
- 内容要点：
  - `alliance/defense.py`：legacy TS `lib/alliance-defense.ts` 移植——endangered /
    reinforce / formation / pocket advice 纯函数，`generatedAtMs` 可注入；
    JS String(number)/Math.round 语义保留。
  - `command_center/projections/alliance_defense.py`：W20 snapshot 载荷之上的薄投影
    + `/api/alliance/defense` 端点 + OpenAPI 契约；golden parity 测试。
- 门禁（main@b85cd81 复跑验证）：`uv sync --locked --all-groups` PASS；
  `ruff format --check .` PASS（251 files）；`ruff check .` PASS；`ty check` PASS；
  `pytest -q` = **1469 passed**，skipped 0。

## W25-A — alliance advice 读模型移植（branch `w25/advice`，基线 main@f4ba3eb）

> BLOCKED.md 原 SKIP 项 "alliance advice" 端点 → **DONE**（本 wave）。exploration /
> director / survey-mining 仍 SKIP，未动。

- Commits：`275e881`（feat(alliance)）、`2b5f13a`（feat(cc)）、`cef2436`（test(cc)）。
- 内容要点：
  - `alliance/advice.py`：legacy TS `lib/alliance-advice.ts`（`loadAllianceAdvice`）
    完整移植——11 段建议（经济濒危 / 零战斗敌核邻近 / 敌情热区近核 / 威胁扇区 /
    排行榜高威胁目击 / 抢矿冲突 / 排行榜基线 / 数据层审计（visibleNever /
    决策负增长 / 决策空转 / 手操拒绝率 / 分工兑现）/ 补测 / 金牌矿 / 敌核逼近与近距），
    纯领域无 IO，`now_ms` 注入，JS `String(number)` / `toFixed` / `Math.round`
    半入语义逐项保留；severity+weight 稳定排序、`(category, tenant|all, title)`
    去重、15 条切片、avgConfidence。
  - `command_center/projections/` 新增薄投影：`leaderboard`（mtime 龄/stale）、
    `enemy_heat`（units_seen + heat_archive 16×16）、`mine_patterns`（visible /
    topActive）、`exploration_coverage`（chunks + 世界核 → resurveyTargets）、
    `core_trails`（core_hunts 轨迹）、`alliance_advice`（组合 loader）、
    `mining_effectiveness` 补 `load_mining_effectiveness`（snapshot+survey+mines+
    patterns+heat → assign → aggregate）。
  - `/api/alliance/advice` 端点 + OpenAPI 200 schema + generated TS
    （`GetAllianceAdviceResponse`）；golden parity 测试（真实 Node TS oracle
    生成，逐字段 MATCH）。
- ALLOWED 明细（域文档已注册，golden 逐字段一致，无 strip）：
  - `generatedAt`/`cachedAt` 与每条建议 `at` 由 `now_ms` 注入；oracle 侧用固定
    Date 子类使 `new Date().toISOString()` 确定，golden 可逐位比较。
  - mine-pattern refill 预测字段非 advice 消费项，loader 返回 TS 空数据默认值。
  - 分工兑现审计的 stale / harvested 分支需"分配后时间演进"（静态快照不可达），
    open 分支已在 golden 覆盖；与 TS oracle 行为一致。
- 门禁：`pytest -q` = **1493 passed**（基线 1469 + 24）；ruff format/check、
  ty check、前端 `tsc --noEmit`、前端 `test:unit` **52/52**、`git diff --check` 全 PASS。

## 说明

- W19–W21 未单独写 delivery docs（PROGRESS/BLOCKED 停留在 W18 状态），本次为文档收口。
- 明确 SKIP 的遗留项见 `BLOCKED.md`。
## W25 — live writer 接线（branch `w25/live`）

- Commits：`1f56c30`（组合决策栈）、`272c2a3`（live 适配器 + observed lease）、
  `8b07525`（live 子命令 + fake-client E2E）。
- 内容要点（P4-21 live 关键路径，t4 canary 已授权）：
  - `cli/main.py` 新增 `live` 子命令：
    `arena-hero-agent live --tenant ID --data-root PATH [--base-url URL]
    [--tick-budget-ms N] [--max-reconnects N]`；api key 从环境
    `ARENA_HERO_API_KEY` 读取（不落仓/日志）。流程：acquire/replace fenced
    writer lease（P4-15）→ `create_sdk_game_client(api_key, base_url)` →
    `LiveTurnSource`（events() 流 → `adapt_async_turn`）→ `TenantRuntime`
    tick loop（decide=composed，submit=`LiveSubmitter`，DecisionId 幂等键）→
    复用 recorder/telemetry/health 观察器 → SIGTERM/SIGINT 优雅关闭
    （release lease + close client）。lease renew 失败 → fail-closed 停止。
  - `strategies/composition.py`：`compose_decider()` 组合根。
    组合顺序 = SafetyPlanner baseline → `assign_worker_tasks` override →
    task→action 转换 → `Plan`→`Decision`；显式持有跨 tick claims/previous
    assignments，`snapshot_from_turn` 用固定 zero seed 派生 EconomyState。
  - `adapters/sdk/live.py`：`LiveTurnSource`（可重开流，非 turn 事件跳过）、
    `LiveSubmitter`（SDK 错误 → rejected outcome，fail-closed）。
  - `adapters/runtime/process_leases.py`：新增 `WriterLeaseRecord` +
    `observed_writer_lease`（读取 fencing 记录供接管，fence 单调）。
- 对拍证据：composed decider 的 worker 动作转换对
  `known_answers_v1.json`（8cf5cbb）worker_assignments fixture——
  single_worker_single_cell / cargo_worker_forced_deposit /
  worker_on_visible_cell_forced_harvest / beacon_on_worker_cell_forced_pickup /
  two_workers_one_cell / no_candidate_cells_all_workers_wait /
  claim_keeps_cell_two_ticks / survey_burst_pre_reserve 共 8 例；底层
  `assign_worker_tasks` fixture 33 cases + safety_helpers 差分测试既有。
  组合/转换本身注册为 `planner_composition` ALLOWED_DIFFERENCE
  （docs/planning-differences.md，措辞已覆盖 live composition）。
- 门禁（w25/live 复跑验证）：`pytest -q` = **1499 passed**（基线 1469 + 30 新增：
  composition 9 + sdk/live 8 + lease observed 4 + cli/live 9），skipped 0；
  ruff format/check、ty check、`git diff --check` 全 PASS；
  `arena-hero-agent live --help` 可用。
- 遗留：
  - 未部署、未换 writer、未做 canary——t4 canary 阶梯需用户授权后另起
    （24h soak 后台运行中，勿碰）。
  - `arena-hero-agent-ts` 只读 oracle（有 WIP 未碰）；sdk-py / Lab / 根仓 /
    production 未动。

## W25-B live-fix (2026-08-12)

- t4 canary smoke (release-002, agent 0.1.1) 抓到一个真实集成缺陷：live 服务器在
  提交后的下一个 turn 里携带上一 tick 的 resolution 事件，adapt_async_turn 的
  严格校验 `event.tick == turn.tick` 直接抛 SdkContractViolationError → 流连续
  失败 → ReconnectLimitExceeded → live 会话失败。
- 修复：turns.py 改为只拒绝 `event.tick > turn.tick`（防未来），历史 resolution
  保留自身 tick 进入 TurnEvent（投影/记录语义不变，决策不消费 events）。
- 测试：`test_rejects_future_resolution_event_tick` + 新增
  `test_accepts_historical_resolution_event_tick`；1524 passed / ruff / ty 绿。
- 版本 bump 0.1.1 → 0.1.2；openapi-v1.json 重新生成（版本字段同步）。
