# BLOCKED.md — 待裁决清单

## 阻塞
无。

## 顺手活（按 wave 裁决范围外，待后续 wave / 管理员裁决）
- SQLite 截断自愈：实测对 `ticks.sqlite3` 截尾或尾部追加垃圾，SQLite 会静默重建 schema（self-heal），recorder 不会 fail-closed 也不报错——若真实 torn-write 场景丢数据会被掩盖。本次用整文件垃圾覆盖注入（open 即失败）证明损坏 fail-closed；是否需要对截断/尾损做显式检测（如行数/schema 校验）超出本 wave 范围，待裁决。
- `JsonlWriter.flush()` 的 IOError 直接上抛（不计数 dropped_count）：与 write() 的 best-effort 语义不同，属显式 fsync 边界，未在本次改；是否需要计数/兜底待裁决。
- src/ 本次零改动：现有 fail-closed 语义已覆盖全部注入点，未加 failpoint 钩子。
