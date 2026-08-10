# arena-hero-agent

Python-first 主实现仓（Spec v4，2026-08-10）。**当前为骨架，无业务实现。**

- 范围：决策栈 / 联盟 / 迁移 / 控制 / Command Center Python API + React 前端。
- 契约：官方 wire/Turn/Action/telemetry/Agent I/O 由 `arena-hero-sdk-py` 唯一
  拥有；本仓不重复实现协议。
- 依赖方向：本仓不 import 模拟器（arena-hero-lab）；lab 经 `arena.agent.io.v1`
  运行本仓参赛者。
- 实施按 root workspace `docs/design/python-first/01-phase-plan.md`（P4/P5）
  推进；生产切换永久用户授权。
