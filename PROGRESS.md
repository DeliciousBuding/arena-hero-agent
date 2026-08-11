# Wave 14 B=Agent P5-7 TS client/types 生成 — PROGRESS

## 理解（任务 0，2026-08-12）
- 目标：`tsgen.py`（Python stdlib only）从 `build_openapi(...)` 输出生成 `command_center/generated/ts/{types,client}.ts`；前端至少一个真实模块改引用生成产物，手写类型标 deprecation 留迁移路径。
- 顺序：任务 0 基线（已核对：后端 1217 passed、前端 typecheck ✓ + unit 48/48）→ 任务 1 tsgen + golden（hash 固定）→ 任务 2 前端接线 → 全量验收（py3.11/ruff/ty/diff-check/前端）。
- 最大风险：① OpenAPI 200 响应 schema 全是 `{}` → 响应类型恒 `unknown`（保留 response 泛型兜底），tsgen 对真正未知构造（oneOf/$ref 缺失/未知关键字）显式 fail-fast；② 前端只许改 3 个白名单文件，真实调用方（mapEngine/组件）不可改 → 迁移落在 api.ts/shopApi.ts 自身数据访问层；③ 生成产物在 Python 包内，前端跨目录相对导入，tsc + node --test（.js→.ts 解析）必须同时过。

## 执行记录（按日追加）
- 2026-08-12 任务 0：uv sync --locked --all-groups ✓；`uv run python -m pytest -q` 1217 passed ✓；`npm ci`（83 包，锁文件已有，非新增依赖）→ typecheck ✓ + test:unit 48/48 ✓。

## 执行记录（任务 1，2026-08-12）
- `tsgen.py`（stdlib only，纯函数）：`generate_types_ts` / `generate_client_ts` / `generate(doc)` 从 `build_openapi(RouteTable())` 输出生成 `command_center/generated/ts/{types,client}.ts`；LF 固定、排序遍历确定性；`TsGenError` fail-fast（未知关键字/`not`/缺 `items`/未知 type/缺 `$ref` 目标/循环 `$ref`/空 enum/enum 类型不匹配）；空 schema `{}` → `unknown`（设计，非降级）。
- types.ts：66 个 `<Op>Params` 接口 + `<Op>Response` 别名；租户枚举值集确定性命名 `Tenant`/`TenantWithAll`；`client.ts`：fetch 封装（no-store+超时+弱 ETag 304+错误映射）＋66 个按 tag 分组的类型化函数（路径/查询/body 类型化，response 保留 `<T = XxxResponse>` 泛型）；`getMap` 走 ccGetEtag（304→null）。
- `tests/command_center/test_tsgen.py` 23 例：golden sha256 固定 `9d6d6ce...`、确定性、产物与再生成一致、66 路由全覆盖抽查、关键类型/函数形状、fail-fast 参数化、组件/`$ref` 支持、空 schema→unknown。
- 验证：pytest 1240（+23）✓；py3.11 1240 ✓；ruff/ty ✓；`tsgen --check` ✓；`uv build` ✓。

## 执行记录（任务 2，2026-08-12）
- `engine/api.ts`：re-export 全部 66 个生成函数 + Params/Response/租户类型；`getJSON`/`fetchJSONWithETag` 标 `@deprecated` 并委托生成 `ccGet`/`ccGetEtag`（签名不变，mapEngine 无需改动）。
- `lib/shopApi.ts`：`ShopProduct`/`ShopMe`/`ShopOrder`/`shopRequest` 标 `@deprecated` 指向生成版；`shopRequest` 委托生成 `ccGet`/`ccSend`（X-Shop-Cookie 头/错误映射/缓存语义不变）；re-export 商店/兑换生成函数与类型。
- 前端相对导入接入：`../../../../src/arena_hero_agent/command_center/generated/ts/{client,types}.ts`（显式 .ts 后缀，Node type-stripping 需要；tsconfig 已开 allowImportingTsExtensions）。渲染/交互逻辑零改动。
- 验证：typecheck ✓；test:unit 48/48（shop-api 3 例含委托后行为）✓；`npm run build`（Vite 产物含生成的 shopApi chunk）✓。
- 说明：`npm ci` 安装锁定依赖（83 包，锁文件已有，非新增依赖）——fresh worktree 无 node_modules，基线 typecheck 必需。

## 执行记录（最终验收，2026-08-12）
- `uv run python -m pytest -q`：1240 passed（≥1217，skipped 0）。
- `uv run --python 3.11 python -m pytest -q`：1240 passed。
- `ruff format --check .` / `ruff check .` / `ty check` / `git diff --check`：全过。
- `cd apps/command-center-web && npm run typecheck` ✓；`npm run test:unit`：48/48，skipped 0。
- `tsgen --check`：生成产物与再生成一致（hash 固定）。
