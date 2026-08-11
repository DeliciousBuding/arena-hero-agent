# Wave 14 B=Agent P5-7 TS client/types 生成 — PROGRESS

## 理解（任务 0，2026-08-12）
- 目标：`tsgen.py`（Python stdlib only）从 `build_openapi(...)` 输出生成 `command_center/generated/ts/{types,client}.ts`；前端至少一个真实模块改引用生成产物，手写类型标 deprecation 留迁移路径。
- 顺序：任务 0 基线（已核对：后端 1217 passed、前端 typecheck ✓ + unit 48/48）→ 任务 1 tsgen + golden（hash 固定）→ 任务 2 前端接线 → 全量验收（py3.11/ruff/ty/diff-check/前端）。
- 最大风险：① OpenAPI 200 响应 schema 全是 `{}` → 响应类型恒 `unknown`（保留 response 泛型兜底），tsgen 对真正未知构造（oneOf/$ref 缺失/未知关键字）显式 fail-fast；② 前端只许改 3 个白名单文件，真实调用方（mapEngine/组件）不可改 → 迁移落在 api.ts/shopApi.ts 自身数据访问层；③ 生成产物在 Python 包内，前端跨目录相对导入，tsc + node --test（.js→.ts 解析）必须同时过。

## 执行记录（按日追加）
- 2026-08-12 任务 0：uv sync --locked --all-groups ✓；`uv run python -m pytest -q` 1217 passed ✓；`npm ci`（83 包，锁文件已有，非新增依赖）→ typecheck ✓ + test:unit 48/48 ✓。
