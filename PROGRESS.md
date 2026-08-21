# PROGRESS — 当前状态（收口摘要）

> 本文件是入口摘要，不是变更日志。历史波次（W18–release-008 时代及以后）明细
> 以 `git log main` 为唯一权威；生产部署与租户状态以私有根仓
> `docs/progress/MASTER.md` §5.2 为准，不写入本公开仓。

## 版本口径

- 包版本以 `pyproject.toml` 的 `version` 字段为准（研究波次发版频繁，文档不
  硬编码版本号）。研究层（economy/raid/exploration/respawn/movement/stuck）
  默认开启。

## 当前能力基线

- 研究层默认开启：economy budget/expansion、raid quota、exploration v2、respawn
  recovery、movement guard、stuck guard、survey burst。
- 性能：exploration 与 worker 路由的 BFS flood 均为整数坐标元组实现（障碍 key
  只解码一次），无每邻居 `Coordinate`+string 分配。
- 防御/经济韧性（后续波次）：工人威胁规避、低产迁移触发、逼近储备等，详见
  `git log main`。

## 收口纪律

- 门禁（干净工作树）：`uv sync --locked --all-groups`；`ruff format --check .`；
  `ruff check .`；`ty check`；`pytest -q`。
- 新行为必须附带聚焦单测；如适用附 replay/契约 fixture。
