# Wave 14 (P4-19) fault injection suite — progress log

分支 `w14/p4-19`，基线 main@ba8a8da（1217 tests）。任务来源：Wave 14 三线并行 A 线任务书（P4-19 故障注入）。

## 理解（目标／顺序／最大风险）
- 目标：`tests/fault_injection/` 新建参数化故障套件，用真实注入（SIGKILL 子进程、磁盘/文件损坏、IOError failpoint）证明 P4-15 lease、P4-16 command ledger/applier、P4-18 migration store、P4-5 recorder、telemetry JsonlWriter 的 fail-closed 与崩溃恢复语义；证据进 CI（全量 ≥1217、skipped 0、3.11 全绿）。
- 顺序：任务 0 基线 → 任务 1 骨架 + lease(SIGKILL) + store(半写/损坏/无 lease) → 任务 2 command bus(半写重放/断线重放/幂等) + recorder(SQLite 损坏/锁/坏尾) + telemetry(磁盘写满 IOError + 半行 torn tail) → 收口全门禁 + 至少 2 组红→绿反向验证。
- 只写 `tests/fault_injection/**`；src/ 只允许最小可测试性改动（无则不加）；测试配置/CI/pyproject 不碰。
- 最大风险：子进程 kill 在 Windows 的锁释放语义（TerminateProcess 后 OS 释放文件锁，需实测）；SQLite 截断可能被 CREATE TABLE 自愈而非 fail-closed（需实测后选注入形态）；`ty check` 对测试文件严格类型；共享 docs/schema/registry 不可写。

## 日志
- 2026-08-12 任务 0 完成：`uv sync --locked --all-groups`；`pytest -q` 1217 passed（skipped 0）；`uv run --python 3.11 python -m pytest -q` 1217 passed；`ruff format --check .` / `ruff check .` / `ty check` / `git diff --check` 全过。基线核对无误，开始动工。
