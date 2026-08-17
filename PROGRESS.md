# PROGRESS — 当前状态（收口摘要）

## Lane 1 receipt (2026-08-17)

- Scope: live configuration injection, stable hashes, measured latency, and budget safety fallback.
- Compatibility target: preserve release-008 defaults and submit/lease semantics.
- Validation and implementation are pending; no cross-repository changes or deployment are planned.

> 本仓当前版本 0.1.7（release-008）。研究层
> （economy/raid/exploration/respawn/movement/stuck）默认开启。
> 历史 wave（W18–W44）明细以 `git log main` 为唯一权威；本文件只保留当前状态
> 与最近收口指针，不再重复变更流水。生产部署与租户状态以私有根仓
> `docs/progress/MASTER.md` §5.2 为准，不写入本公开仓。

## 当前基线（release-008 / agent 0.1.7）

- 研究层默认开启：economy budget/expansion、raid quota、exploration v2、respawn
  recovery、movement guard、stuck guard、survey burst。
- 性能：exploration 与 worker 路由两处 BFS flood 均改为整数坐标元组（障碍 key
  只解码一次），消除每邻居 `Coordinate`+string 分配。
- 测试基线：`pytest -q` = 1927 passed；ruff format/check、ty check 全绿。

## 最近收口（commit 指针）

- `eda5577` perf：worker routing BFS 整数元组化（与 `8b9dcc4` exploration 同型）。
- `8b9dcc4` perf：exploration BFS frontier flood 整数元组化。
- `e6f3bd6` feat：研究层默认开启。
- `77fe30d` feat：live_status.json 持久化。

## 收口纪律

- 门禁（干净工作树）：`uv sync --locked --all-groups`；`ruff format --check .`；
  `ruff check .`；`ty check`；`pytest -q`。
- 本文件是入口摘要，不是变更日志；历史 wave 明细与 commit 指针以 `git log main`
  为准。
