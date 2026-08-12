# BLOCKED — W22 当前待办（2026-08-12）

## 明确 SKIP（W19–W21 遗留，未做）

- alliance exploration / director / survey-mining 端点：依赖未移植的
  projection 或非领域读模型，Python Command Center 尚未实现（app 现仅
  stream / map / map-lod / alliance-snapshot / alliance-defense /
  alliance-advice；advice 已于 W25-A DONE）。
- mapEngine / RedeemPanel / IntelPanel 调用方迁移：legacy 包装含 cookie 行为
  （X-Shop-Cookie / localStorage，`web/src/lib/shopApi.ts`）且实际提交 I/O 仍在
  mapEngine（~4300 行）内、无单测覆盖，迁移前需先建测试与 I/O 边界。

## P3 观察（release-004 待修，2026-08-12）

- telemetry `processRunId` 恒 "unknown"（sink 默认值，offline/live 两路径一致；
  health 快照有真实 run id）——不影响数据完整性，随 release-004 修复。

其余：无。
