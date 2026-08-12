# BLOCKED — W22 当前待办（2026-08-12）

## 明确 SKIP（W19–W21 遗留，未做）

- alliance advice / exploration / director / survey-mining 端点：依赖未移植的
  projection 或非领域读模型，Python Command Center 尚未实现（app 现仅
  stream / map / map-lod / alliance-snapshot / alliance-defense）。
- mapEngine / RedeemPanel / IntelPanel 调用方迁移：legacy 包装含 cookie 行为
  （X-Shop-Cookie / localStorage，`web/src/lib/shopApi.ts`）且实际提交 I/O 仍在
  mapEngine（~4300 行）内、无单测覆盖，迁移前需先建测试与 I/O 边界。

其余：无。
