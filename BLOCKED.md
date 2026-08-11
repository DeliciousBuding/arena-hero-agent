# BLOCKED.md — 待裁决清单

## 阻塞
无。

## 顺手活（按 wave 裁决范围外，待后续 wave / 管理员裁决）
- 路线走廊审计（§4）、战术小队（§5）、节奏模型（§3）：后续 wave，本次不做。
- `migration_cancel` 命令经 P4-16 命令总线接线：§6.2 取消语义，本次只做状态机 CANCEL 事件，不做 command-plane 接线。
- 报告 JSONL（`<tenant>-report.jsonl`）：§8 KPI，live 阶梯相关，本次不做。
- 跨进程 conductor 文件锁：当前 fencing 基于进程内 `WriterLease`（P4-15，MemoryLeaseCoordinator）；多进程共存需文件级独占锁，属 live 部署决策。
- 计划 parse 数值字段严格 int（TS 接受有限 float）：Python 侧收紧，需管理员确认与 TS 对拍是否要求逐位兼容。
