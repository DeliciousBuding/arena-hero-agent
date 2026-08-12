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
- **mapEngine / RedeemPanel / IntelPanel 调用方迁移：继续 SKIP**（2026-08-12 Meitner 调查）：
  目标写端点（`/api/command*`、`/api/shop/order`、`/api/redeem`）Python 后端全部 501
  （P5-9 写门就绪但无实现），现在迁移会指向死 API；正确顺序 = 后端写端点落地 →
  抽 mapEngine I/O 边界 + 补测试 → 前端调用方换生成 client。

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
