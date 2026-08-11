# Wave 14 B=Agent P5-7 BLOCKED

## 1. OpenAPI 200 响应 schema 全为 `{}`（待裁决，不阻塞）
- 现象：P5-5 OpenAPI 的 200 application/json schema 全是空 `{}`，生成器按设计映射为 `unknown`，前端调用需显式泛型（`getShop<{ products?: ShopProduct[] }>()`）。
- 建议：后续 wave 在 `openapi.py` 补齐响应 schema（属「改 openapi.py 生成逻辑」顺手活，本 wave 不越界），生成响应类型将自动从 `unknown` 变具体，前端手写类型即可逐步删除。

## 2. 真实消费方迁移范围（待裁决，不阻塞）
- `mapEngine.ts`（getJSON/fetchJSONWithETag 调用方）与 `RedeemPanel`/`IntelPanel`（shopRequest 调用方）不在白名单，未改（只允许 engine/api.ts、engine/types.ts、lib/shopApi.ts）。
- 当前已完成：engine/api.ts 与 lib/shopApi.ts 数据访问层委托生成 client + re-export 生成函数/类型 + 手写类型 deprecation。后续 wave 可把 mapEngine/组件调用逐步切到生成函数（属「重构前端组件」顺手活）。

## 无
- 其余无阻塞项。
