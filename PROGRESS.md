# Wave 13 (P4-18) migration conductor core — progress log

分支 `w13/p4-18`，基线 main@92c55e2。任务来源：`docs/design/migration-system-v1.md` §2/§6/§8（根仓 SSOT）+ TS 行为 oracle（`arena-hero-agent-ts/packages/arena-agent/src/migration/`）。

## 理解（目标／顺序／最大风险）
- 目标：Python 侧 conductor 核心状态机（§2）+ 计划 schema（§6.1）+ 生效契约/fencing（§6.2）+ 原子持久化与重启恢复；仅离线可证部分，不做走廊审计/战术小队/节奏模型（后续 wave）。
- 顺序：任务 0 基线 → 任务 1 状态机+计划 → 任务 2 契约+fencing → 任务 3 存储+恢复；每项带测试、全量 pytest 绿后更新本文件。
- 复用：fencing 直接复用 P4-15 `WriterLease`（MemoryLeaseCoordinator），`conductorEpoch` = fencing token 数值（单调）；不另造租约、不加第三方依赖（stdlib only）。
- 最大风险：与 TS oracle 语义偏差（untilTick>=currentTick 才算新鲜、心跳 TTL 边界、非法事件 no-op vs 拒绝）；`ty check .` 全仓约束对测试的严格类型要求；Windows 上原子 rename 语义。

## 日志
- 2026-08-12 任务 0 完成：`uv sync --locked --all-groups`；`pytest -q` 1094 passed（skipped 0）；`ruff format --check .`、`ruff check .`、`ty check .`、`git diff --check` 全过。
- 2026-08-12 任务 1 完成：`migration/state_machine.py`（8 状态全转移，非法转移 no-op，未知 state/event ValueError 拒绝；`MigrationEvent.__post_init__` 校验——StrEnum 对未知字符串会静默造成员，必须显式拒绝）+ `migration/plan.py`（§6.1 全字段 frozen dataclass + 严格 fail-closed parse）。测试 86 个（test_state_machine 全转移表驱动 sweep + test_plan 解析拒绝矩阵）。全量 1180 passed（基线 1094），ruff/ty/format 全过。决定：`MigrationState` 放 migration 模块而非 domain/value_objects.py（避免触碰共享层）；计划数值字段严格 int（TS 允许有限 float，Python 侧收紧更安全）；`conductorEpoch` 起点 = fencing token 1（FencingToken 要求正数，TS lock 起点 0，单调性一致）。
- 2026-08-12 任务 2 完成：`migration/enactment.py`——§6.2 runtime 前置门（leaseFresh && epoch 匹配 && coreId 同代际，任一不满足 → 拒发 START_MOVE）+ conductor fencing（复用 P4-15 WriterLease；conductorEpoch = fencing token；stale takeover 只替换"已过期且 token 精确匹配"的持有者，epoch 单调 +1，旧订单被拒）。红→绿证据：test_enactment 先写后实现（ModuleNotFoundError → 17 passed）；故障注入 `test_stale_takeover_rejects_old_conductor_order_fault_injection` 与 lease 过期 fail-closed 分支均覆盖。全量 1197 passed（+17），ruff/ty/format 全过。决定：不做独立文件锁——进程内 WriterLease 即租户级 exclusive lock（符合"复用 P4-15 lease 或文件锁"）。
