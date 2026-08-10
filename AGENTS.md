# AGENTS.md — arena-hero-agent

最后更新：2026-08-10。本仓是 Python-first Spec v4 的新建主实现仓（骨架期）。

## 权威层级
1. 管理员即时指令
2. `C:\Users\Ding\AGENTS.md`（全局规则）
3. `D:\Code\Projects\arena\AGENTS.md`（root 治理：仓库边界、单 writer、防删库）
4. root `docs/design/python-first/`（Spec v4 计划）与 `docs/adr/`
5. 本文件

## 本仓边界
- 只实现 Agent 决策/联盟/迁移/控制/Command Center（Python）+ 前端（React/TS）。
- 不实现模拟器引擎（属 arena-hero-lab）；不 import lab。
- 不重复实现官方 wire/telemetry 协议（属 arena-hero-sdk-py）。
- 真实数据写入仅限授权生产进程；本仓开发期不得写 `data/runtime/`。

## 纪律
- 小步提交；默认分支 main；不破坏性 git；不覆盖其他会话 WIP。
- 生产部署/重启/回滚、canary 切换永久需要用户授权。
- 代码质量门禁：uv sync / ruff check / ruff format --check / ty check / pytest -q。
