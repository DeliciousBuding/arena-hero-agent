# PROGRESS — W18–W21 收口（main@b85cd81）

> 状态：W18–W21 已全部合入 main 并 push（2026-08-12，origin/main 与本地同步）。
> 本文档为收口摘要；各 wave 明细见 `git log main`。范围仅 `arena-hero-agent` 本仓，
> 未动根仓 docs / Lab / SDK / production / 真实数据 / secrets。

## W18 — P4-20 deterministic offline contestant seam（branch `w18/p4-20`）

- Commits：`02a5aba`（canonical digests + manifests）、`8191a8b`（batch runner +
  fail-closed run id）、`e254b40`（docs: batch usage）。
- 内容要点：
  - `cli/canonical.py`：strip_nonsemantic（`recordedAtNs`/`updatedAtNs`/`startedAtNs`）
    + 排序键紧凑重编码 → 稳定 SHA-256；每次成功 `run` 写 `manifest.json`
    （schemaVersion/tenantId/runId/processRunId/digests）。
  - `batch` 子命令：每文件一场景，稳定 run id `scenario-<stem>-seed-<n>`，
    输出 `<data-root>/<run-id>/<tenant>/`；全量预校验，坏批次不产生部分产物。
  - Fail-closed：非法 run id / tenant / 输入 exit 2；同 tenant 重复 run id 冲突
    不追加；错误输出无绝对路径/secret。
- 门禁：`pytest -q` = **1410 passed**（基线 1379 + 31），skipped 0；
  ruff format/check、ty check、`git diff --check` 全 PASS。

## W19 — 硬化（branch `w19/hardening`）

- Commits：`20befd2`（W19-1 recorder）、`a84e018`（W19-2 telemetry）、
  `d8bdeb3` + `f0b5144`（W19-3 OpenAPI）、`ea40d20`（W19-5 空壳清理）。
- 内容要点：
  - recorder：非空既有库打开时 PRAGMA quick_check + 期望表检查，撕裂/截断
    SQLite 显式 raise 而非静默重建；零字节文件仍可初始化。
  - telemetry：JsonlWriter.flush OSError 计入 `dropped_count` + `last_error` 并
    re-raise（持久化边界失败既计数又对调用方可见）。
  - OpenAPI：主读端点 200 响应 schema 非空（字段名对齐 P5-4 投影），其余路由
    回退非空 object；regenerate `openapi-v1.json` + generated TS。
  - 删除无引用的空壳 `intelligence/` 包。
- 门禁：`pytest -q` = **1417 passed**（在 `ea40d20` 复跑验证），skipped 0；
  ruff/ty 全绿。

## W20 — alliance snapshot 读模型（branch `w20/alliance-readmodel`）

- Commits：`8ee5929`（shared-intel fusion）、`5ebbb88`（/api/alliance/snapshot +
  OpenAPI）、`16c19f6`（parity/loader/API 测试）。
- 内容要点：
  - `alliance/shared_intel.py`：跨租户目击融合 + 新鲜度窗口，TS oracle 对拍。
  - `command_center/projections/alliance_snapshot.py`：快照投影 + `/api/alliance/snapshot`
    端点 + OpenAPI 契约 + generated TS；golden parity 测试。
- 门禁：`pytest -q` = **1441 passed**（在 `16c19f6` 复跑验证），skipped 0；ruff/ty 全绿。

## W21 — alliance defense 读模型（branch `w21/alliance-defense`，当前 HEAD）

- Commits：`bd3aea6`（alliance-defense 联合协调读模型）、`375b6b1`
  （/api/alliance/defense + OpenAPI）、`b85cd81`（parity/loader/API 测试）。
- 内容要点：
  - `alliance/defense.py`：legacy TS `lib/alliance-defense.ts` 移植——endangered /
    reinforce / formation / pocket advice 纯函数，`generatedAtMs` 可注入；
    JS String(number)/Math.round 语义保留。
  - `command_center/projections/alliance_defense.py`：W20 snapshot 载荷之上的薄投影
    + `/api/alliance/defense` 端点 + OpenAPI 契约；golden parity 测试。
- 门禁（main@b85cd81 复跑验证）：`uv sync --locked --all-groups` PASS；
  `ruff format --check .` PASS（251 files）；`ruff check .` PASS；`ty check` PASS；
  `pytest -q` = **1469 passed**，skipped 0。

## 说明

- W19–W21 未单独写 delivery docs（PROGRESS/BLOCKED 停留在 W18 状态），本次为文档收口。
- 明确 SKIP 的遗留项见 `BLOCKED.md`。
