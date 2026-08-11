# Wave 14 (P4-19) fault injection suite — progress log

分支 `w14/p4-19`，基线 main@ba8a8da（1217 tests）。任务来源：Wave 14 三线并行 A 线任务书（P4-19 故障注入）。

## 理解（目标／顺序／最大风险）
- 目标：`tests/fault_injection/` 新建参数化故障套件，用真实注入（SIGKILL 子进程、磁盘/文件损坏、IOError failpoint）证明 P4-15 lease、P4-16 command ledger/applier、P4-18 migration store、P4-5 recorder、telemetry JsonlWriter 的 fail-closed 与崩溃恢复语义；证据进 CI（全量 ≥1217、skipped 0、3.11 全绿）。
- 顺序：任务 0 基线 → 任务 1 骨架 + lease(SIGKILL) + store(半写/损坏/无 lease) → 任务 2 command bus(半写重放/断线重放/幂等) + recorder(SQLite 损坏/锁/坏尾) + telemetry(磁盘写满 IOError + 半行 torn tail) → 收口全门禁 + 至少 2 组红→绿反向验证。
- 只写 `tests/fault_injection/**`；src/ 只允许最小可测试性改动（无则不加）；测试配置/CI/pyproject 不碰。
- 最大风险：子进程 kill 在 Windows 的锁释放语义（TerminateProcess 后 OS 释放文件锁，需实测）；SQLite 截断可能被 CREATE TABLE 自愈而非 fail-closed（需实测后选注入形态）；`ty check` 对测试文件严格类型；共享 docs/schema/registry 不可写。

## 日志
- 2026-08-12 任务 2 完成：command bus/recorder/telemetry 故障注入。P4-16：半写 ledger torn tail 跳过（不提交）→ 补全整行后重放仅 APPLIED 一次、相同行 DUPLICATE（幂等实证）；断线重放（新 bus/applier + 精确 fence 接管）空重放 ()、新命令只 APPLIED 一次（audit 3 条）；applied.json 半写 tmp 不提升；malformed 行 3 种 payload fail-closed。P4-5 recorder：SQLite 整文件垃圾 → open 即 RecorderError 且文件原样（实测截断/尾垃圾会被 SQLite 自愈，故用整文件覆盖）；坏尾行（非法枚举值直插 DB）→ read RecorderError 不丢好行；第二进程持锁 → RecorderError；JSONL 半行 torn tail 恢复（recovered_partial=1）。telemetry：ENOSPC failpoint 注入 append 边界 → dropped_count=1、last_error=OSError、不 raise（决策路径不阻塞），故障清除后下一记录落盘；半行 torn tail 默认 TornTailError fail-closed、开启 recovery 截断恢复计数。红→绿（磁盘类）：临时禁用 failpoint → `dropped_count == 1` 失败（assert 0 == 1，红）→ 还原 → 3 passed 绿。决定：磁盘注入选 append 边界 IOError failpoint 而非只读目录（Windows 只读属性不拦已有文件写入 + admin 绕过，不可靠）；src/ 零改动（现有 fail-closed 语义已完备，无需加 failpoint 钩子）。
- 2026-08-12 任务 1 完成：`tests/fault_injection/` 参数化骨架 + P4-15 lease SIGKILL + P4-18 store 故障。子进程真杀（`sys.executable -c` 起真实进程 → `Popen.kill()`，Windows=TerminateProcess/POSIX=SIGKILL；不 mock 被测对象）。lease：SIGKILL 后 acquire 被拒、错 fence 被拒、过期后精确 fence 接管（fence+1），参数化 renewals 0/1/2；store：半写 tmp 不提升（3 种残留）、损坏 5 种 payload fail-closed 且文件原样保留、无 lease/过期 lease/错 epoch/错租户写全拒、SIGKILL 写循环后 plan 恒有效。红→绿（SIGKILL）：临时禁用 kill（子进程存活持 OS 锁）→ `assert replacement is not None` 失败（"crashed lease never expired or takeover was blocked"，15.75s 红）→ 还原 kill → 5 passed 绿。全量 fault 套件 15 passed；ruff/format/ty 全过。决定：磁盘注入选 failpoint/文件损坏而非只读目录——Windows 只读属性不拦已有文件写入且 admin 绕过，不可靠；SQLite 截断/尾垃圾会自愈（实测 self-heal），故损坏注入用整文件垃圾字节（open 即 DatabaseError→RecorderError）。
- 2026-08-12 任务 0 完成：`uv sync --locked --all-groups`；`pytest -q` 1217 passed（skipped 0）；`uv run --python 3.11 python -m pytest -q` 1217 passed；`ruff format --check .` / `ruff check .` / `ty check` / `git diff --check` 全过。基线核对无误，开始动工。


