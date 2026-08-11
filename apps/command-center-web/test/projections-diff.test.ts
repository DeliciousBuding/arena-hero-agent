/**
 * P5-8 保留前端规则投影差分测试（2026-08-12）：
 * 前端 engine 规则函数 vs 后端 Python 权威实现——同输入输出对比。
 *
 * 覆盖（盘点清单 PROGRESS.md 任务 0）：
 *   FE-1 maxUnitHp        ↔ planning/plan_validator.UNIT_MAX_HP
 *   FE-2 tactUnitCost     ↔ domain.economy.unit_price（v0.14 定价）
 *   FE-3 tactCoreCapacity ↔ domain.economy.core_resource_capacity
 *
 * 差分方式：Node 侧以仓库标准工具链 `uv run python -c`（CI 同用 uv）实时计算后端
 * 权威值，与前端函数同输入输出对比；同时内嵌后端实测锚定值（2026-08-12 实跑输出，
 * 证据见 PROGRESS.md 任务 1），双保险且无 skip/放松。已知差异（文档化）：
 *   - tactCoreCapacity 对负人口 clamp 到 0（前端 Math.max(0, pop) 兜底）；
 *     后端 core_resource_capacity 拒绝负人口（ValueError，fail-closed）——差分矩阵
 *     只含非负人口，前端 clamp 行为由锚定用例覆盖。
 *   - 极大人人口时前端浮点 Math.pow 与后端整数精确舍入可能存在 1ulp 差（展示层
 *     可接受）；本差分矩阵覆盖实测人口范围 [0,100]，已对拍一致。
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { tactUnitCost, tactCoreCapacity } from "../src/engine/tactical.ts";
import { maxUnitHp } from "../src/engine/utils.ts";

const REPO_ROOT = fileURLToPath(new URL("../../", import.meta.url));
const POPS = [0, 5, 19, 20, 24, 25, 30, 45, 100];
const CAP_POPS = [0, 1, 2, 10, 100];
const ROLES = ["WORKER", "VANGUARD", "RANGER"];

/** 后端权威值（2026-08-12 实跑 `uv run python -c ...` 输出，作为锚定基线）。 */
const BACKEND_ANCHOR: {
  unitPrice: Record<string, Record<string, number>>;
  coreCapacity: Record<string, number>;
  unitMaxHp: Record<string, number>;
} = {
  unitPrice: {
    WORKER: { 0: 5, 5: 5, 19: 5, 20: 7, 24: 7, 25: 8, 30: 11, 45: 24, 100: 433 },
    VANGUARD: { 0: 10, 5: 10, 19: 10, 20: 13, 24: 13, 25: 17, 30: 22, 45: 48, 100: 865 },
    RANGER: { 0: 12, 5: 12, 19: 12, 20: 16, 24: 16, 25: 20, 30: 26, 45: 58, 100: 1038 },
  },
  coreCapacity: { 0: 10, 1: 10, 2: 10, 10: 50, 100: 500 },
  unitMaxHp: { WORKER: 2, VANGUARD: 4, RANGER: 2 },
};

/** 实时调用后端 Python 权威实现，返回与前端同构的数值表。 */
function backendLive(): typeof BACKEND_ANCHOR {
  const py = `
import json
from arena_hero_agent.domain import UnitRole, unit_price, core_resource_capacity
from arena_hero_agent.domain import CURRENT_RULES_VERSION
from arena_hero_agent.planning.plan_validator import UNIT_MAX_HP
pops = [0, 5, 19, 20, 24, 25, 30, 45, 100]
cap_pops = [0, 1, 2, 10, 100]
out = {
  "unitPrice": {r.value.upper(): {str(p): unit_price(r, p, CURRENT_RULES_VERSION) for p in pops} for r in UnitRole},
  "coreCapacity": {str(p): core_resource_capacity(p) for p in cap_pops},
  "unitMaxHp": {r.value.upper(): UNIT_MAX_HP[r] for r in UnitRole},
}
print("__P5_8_DIFF__" + json.dumps(out, sort_keys=True))
`;
  const raw = execFileSync("uv", ["run", "python", "-c", py], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    timeout: 90_000,
    windowsHide: true,
  });
  const line = raw.split(/\r?\n/).find((l) => l.startsWith("__P5_8_DIFF__"));
  assert.ok(line, `后端差分输出缺失：${raw.slice(0, 400)}`);
  return JSON.parse(line.slice("__P5_8_DIFF__".length));
}

test("diff FE-2: tactUnitCost 与后端 domain.economy.unit_price 同输入输出", () => {
  const backend = backendLive();
  for (const role of ROLES) {
    for (const pop of POPS) {
      assert.equal(
        tactUnitCost(role, pop),
        backend.unitPrice[role][String(pop)],
        `tactUnitCost(${role}, ${pop}) 应等于后端 unit_price（前端 ${tactUnitCost(role, pop)} vs 后端 ${backend.unitPrice[role][String(pop)]}）`,
      );
    }
  }
});

test("diff FE-3: tactCoreCapacity 与后端 domain.economy.core_resource_capacity 同输入输出", () => {
  const backend = backendLive();
  for (const pop of CAP_POPS) {
    assert.equal(
      tactCoreCapacity(pop),
      backend.coreCapacity[String(pop)],
      `tactCoreCapacity(${pop}) 应等于后端 core_resource_capacity（前端 ${tactCoreCapacity(pop)} vs 后端 ${backend.coreCapacity[String(pop)]}）`,
    );
  }
});

test("diff FE-1: maxUnitHp 与后端 planning.plan_validator.UNIT_MAX_HP 同输入输出", () => {
  const backend = backendLive();
  for (const [type, hp] of Object.entries(backend.unitMaxHp)) {
    assert.equal(maxUnitHp(type), hp, `maxUnitHp(${type}) 应等于后端 UNIT_MAX_HP（前端 ${maxUnitHp(type)} vs 后端 ${hp}）`);
  }
});

test("anchor FE-2/FE-3/FE-1: 前端规则函数锚定后端实测值（2026-08-12）", () => {
  for (const role of ROLES) {
    for (const pop of POPS) {
      assert.equal(tactUnitCost(role, pop), BACKEND_ANCHOR.unitPrice[role][pop]);
    }
    assert.equal(maxUnitHp(role), BACKEND_ANCHOR.unitMaxHp[role]);
  }
  for (const pop of CAP_POPS) assert.equal(tactCoreCapacity(pop), BACKEND_ANCHOR.coreCapacity[pop]);
  // 前端 clamp 行为：负人口按 0 处理 → 下限 10（后端拒绝负人口，属文档化差异）
  assert.equal(tactCoreCapacity(-5), 10);
  // 未知类型回退：maxUnitHp 非 VANGUARD 一律 2（与后端 UNIT_MAX_HP 无该键的 fail-closed 语义对齐）
  assert.equal(maxUnitHp("CORE"), 2);
});
