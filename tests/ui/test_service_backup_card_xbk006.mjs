// 商用驗收 DATA-ODOO-005-2／X-BK-006-1：服務頁 Overview 的唯讀備份卡。
// 以前三個分頁完全沒有備份時間或狀態；備份失敗時客戶看到的服務永遠正常。
// 純邏輯測試（載入手法同 test_service_presentation_workload_1647.mjs）：把 OverviewTab 的
// getter 本體從原始碼切出來，`_t` 換成恆等函式執行；模板只做靜態檢查（卡片只看
// backup.supported、文案不寫在多行文字節點）。
//
// 跑法：node --test tests/ui/*.mjs（CI：.gitea/workflows/paas-ui-tests.yml）
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

function getterBody(componentSource, name) {
  const head = `get ${name}() {`;
  const start = componentSource.indexOf(head);
  assert.notEqual(start, -1, `找不到 getter ${name}`);
  let depth = 1;
  let index = start + head.length;
  while (index < componentSource.length) {
    const two = componentSource.slice(index, index + 2);
    const char = componentSource[index];
    if (two === "//") {
      index = componentSource.indexOf("\n", index);
    } else if (two === "/*") {
      index = componentSource.indexOf("*/", index) + 2;
    } else if (char === '"' || char === "'" || char === "`") {
      index += 1;
      while (componentSource[index] !== char) {
        index += componentSource[index] === "\\" ? 2 : 1;
      }
      index += 1;
    } else {
      if (char === "{") depth += 1;
      if (char === "}") depth -= 1;
      if (depth === 0) {
        return componentSource.slice(start + head.length, index);
      }
      index += 1;
    }
  }
  throw new Error(`getter ${name} 的大括號沒有配對`);
}

const base = new URL("../../src/static/src/paas/pages/service/tabs/", import.meta.url);
const js = await readFile(new URL("OverviewTab.js", base), "utf8");
const xml = await readFile(new URL("OverviewTab.xml", base), "utf8");

const identity = (message) => message;
const getter = (name) =>
  new Function("_t", `return function () {${getterBody(js, name)}};`)(identity);

/** 模擬元件實例：只放 getter 讀得到的東西。 */
function card(backup) {
  const self = { props: { service: { backup } }, formatSyncedAt: (v) => `fmt(${v})` };
  for (const name of [
    "backup", "backupStatusLabel", "backupBadgeClass", "backupReasonText", "backupHint",
    "backupLastSuccessText",
  ]) {
    Object.defineProperty(self, name, { get: getter(name) });
  }
  return self;
}

test("payload 沒有 backup（舊後端）時視為不支援，卡片不畫", () => {
  assert.equal(card(undefined).backup.supported, false);
  assert.match(xml, /<WoowCard t-if="backup\.supported"/);
});

test("失敗：紅色徽章＋依原因代碼的說明，並告訴租戶服務仍在運作", () => {
  const c = card({ supported: true, health: "failed", failure_reason: "no_space",
    last_success_at: "2026-10-15T18:08:03+00:00", last_failure_at: "2026-10-16T18:09:40+00:00" });
  assert.equal(c.backupStatusLabel, "Failed");
  assert.equal(c.backupBadgeClass, "o_woow_badge_red");
  assert.equal(c.backupReasonText, "The backup storage is full.");
  assert.match(c.backupHint, /keeps running/);
  assert.equal(c.backupLastSuccessText, "fmt(2026-10-15T18:08:03+00:00)");
});

test("未知原因代碼退回通用說明（不顯示叢集原文）", () => {
  assert.equal(card({ supported: true, health: "failed", failure_reason: "weird_code" }).backupReasonText,
    "The backup could not be completed.");
});

test("逾期、正常、未檢查、停權各自的徽章", () => {
  assert.deepEqual(
    ["stale", "ok", "unknown"].map((health) => {
      const c = card({ supported: true, health });
      return [c.backupStatusLabel, c.backupBadgeClass];
    }),
    [["Overdue", "o_woow_badge_amber"], ["Healthy", "o_woow_badge_green"],
      ["Not checked yet", "o_woow_badge_gray"]],
  );
  const paused = card({ supported: true, health: "failed", paused: true });
  assert.equal(paused.backupStatusLabel, "Paused");
  assert.equal(paused.backupBadgeClass, "o_woow_badge_gray");
  assert.match(paused.backupHint, /suspended/);
  assert.equal(card({ supported: true, health: "unknown" }).backupLastSuccessText, "None yet");
});

test("備份卡文案全部走 JS 的 _t（模板裡沒有多行英文文字節點）", () => {
  const block = xml.slice(xml.indexOf('<WoowCard t-if="backup.supported"'),
    xml.indexOf("</WoowCard>", xml.indexOf('<WoowCard t-if="backup.supported"')));
  assert.ok(block.length > 0);
  const textNodes = block.replace(/<[^>]+>/g, "\n").split("\n").map((s) => s.trim()).filter(Boolean);
  assert.deepEqual(textNodes, []);
});
