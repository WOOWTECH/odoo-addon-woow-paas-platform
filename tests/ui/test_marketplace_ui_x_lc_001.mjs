// X-LC-001-3／-4／-5：應用市集前端（價格斷行、麵包屑工作區名稱、只列有範本的分類頁籤）。
// 後端搜尋（% _ 萬用字元、標籤）在 Odoo 測試 src/tests/test_marketplace_search_x_lc_001.py。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { readPaas, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

const reactiveStub = "const reactive = (value) => value;";
const translateStub = "const _t = (message) => message;";

const { CategoryFilter } = await import(toDataUrl(stubImports(
  await readPaas("components/marketplace/CategoryFilter.js"), {
    "@odoo/owl": "class Component {}",
    "@web/core/l10n/translation": translateStub,
  })));

const { breadcrumbService } = await import(toDataUrl(stubImports(
  await readPaas("services/breadcrumb_service.js"), { "@odoo/owl": reactiveStub })));

globalThis.__wsRpc = async () => ({ success: false });
const { workspaceService } = await import(toDataUrl(stubImports(
  await readPaas("services/workspace_service.js"), {
    "@odoo/owl": reactiveStub,
    "@web/core/l10n/translation": translateStub,
    "./rpc": "const jsonRpc = (...args) => globalThis.__wsRpc(...args);",
  })));

function filter(props) {
  const f = new CategoryFilter();
  f.props = props;
  return f.categories.map((c) => c.key);
}

test("X-LC-001-5：分類頁籤只列有公開範本的分類（＋全部），沒有範本的資料庫／數據分析不出現", () => {
  assert.deepEqual(
    filter({ selectedCategory: "all", availableCategories: ["mcp", "ai_llm", "web"] }),
    ["all", "ai_llm", "web", "mcp"], "依原本的頁籤順序");
  assert.deepEqual(
    filter({ selectedCategory: "database", availableCategories: ["ai_llm"] }),
    ["all", "ai_llm", "database"], "目前選中的分類保留，選取狀態不會指向看不到的頁籤");
  assert.equal(filter({ selectedCategory: "all" }).length, 9, "舊後端沒給 categories：全部照舊");
});

test("X-LC-001-4：工作區名稱晚到時只換掉同一 route、同一工作區那一段", () => {
  const trail = [
    { label: "Workspaces", path: "workspaces" },
    { label: "Workspace", path: "workspace/50" },
    { label: "App Marketplace", path: null },
  ];
  breadcrumbService.set("marketplace", trail);
  assert.equal(breadcrumbService.relabelWorkspace("marketplace", 50, "Commercial QA B"), true);
  assert.equal(breadcrumbService.trail[1].label, "Commercial QA B");
  assert.equal(trail[1].label, "Workspace", "不可就地改原陣列");

  // 使用者已切到別頁（別頁 publish 了自己的 trail）：晚到的結果不得蓋掉。
  breadcrumbService.set("configure", [{ label: "Workspace", path: "workspace/50" }]);
  assert.equal(breadcrumbService.relabelWorkspace("marketplace", 50, "X"), false);
  assert.equal(breadcrumbService.trail[0].label, "Workspace");
  // 同一 route 但已是別的工作區：不改。
  assert.equal(breadcrumbService.relabelWorkspace("configure", 51, "Other"), false);
  assert.equal(breadcrumbService.relabelWorkspace("configure", 50, null), false);
});

test("X-LC-001-4：resolveWorkspaceName 先用快取、缺時讀一次並記住、失敗回 null", async () => {
  const calls = [];
  globalThis.__wsRpc = async (url, params) => {
    calls.push([url, params]);
    return url === "/api/workspaces/50" ? { success: true, data: { id: 50, name: "QA B" } } : { success: false };
  };
  workspaceService.workspaces = [{ id: 7, name: "Listed" }];
  assert.equal(await workspaceService.resolveWorkspaceName(7), "Listed");
  assert.equal(calls.length, 0, "清單快取有就不打 API");
  assert.equal(await workspaceService.resolveWorkspaceName(50), "QA B");
  assert.deepEqual(calls[0], ["/api/workspaces/50", { action: "get" }]);
  assert.equal(await workspaceService.resolveWorkspaceName(50), "QA B");
  assert.equal(calls.length, 1, "讀過一次就記住");
  assert.equal(workspaceService.cachedWorkspaceName(50), "QA B");
  assert.equal(await workspaceService.resolveWorkspaceName(99), null);
  globalThis.__wsRpc = async () => { throw new Error("network"); };
  assert.equal(await workspaceService.resolveWorkspaceName(98), null);
  assert.deepEqual(workspaceService.operationLoading, {}, "不影響其他頁的載入狀態");
});

// 設定頁（configure）的工作區名稱由 PR #1763 處理（它改成讀 /api/workspaces/<id> 的回覆並據以
// 判斷角色），本 PR 不碰那一頁，避免與 #1763 衝突。
test("X-LC-001-4：市集、服務詳情都會補讀工作區名稱", async () => {
  for (const [rel, route] of [
    ["pages/marketplace/AppMarketplacePage.js", "marketplace"],
    ["pages/service/ServiceDetailPage.js", "service-detail"],
  ]) {
    const src = await readPaas(rel);
    assert.match(src, /workspaceService\.resolveWorkspaceName\(wsId\)/, rel);
    assert.match(src, new RegExp(`relabelWorkspace\\("${route}", wsId, name\\)`), rel);
    assert.doesNotMatch(src, /workspaceService\.workspaces\.find/, `${rel} 改走 cachedWorkspaceName`);
  }
});

test("X-LC-001-3：卡片價格不換行、不被標籤擠壓", async () => {
  const scss = await readFile(
    new URL("../../src/static/src/paas/styles/pages/40_marketplace.scss", import.meta.url), "utf8");
  const block = (selector) => {
    const m = scss.match(new RegExp(`\\n${selector.replace(/\./g, "\\.")} \\{([^}]*)\\}`));
    assert.ok(m, `找不到 ${selector}`);
    return m[1];
  };
  const price = block(".o_woow_app_card_price");
  assert.match(price, /white-space:\s*nowrap/);
  assert.match(price, /flex-shrink:\s*0/);
  assert.match(block(".o_woow_app_card_tags"), /min-width:\s*0/);
});
