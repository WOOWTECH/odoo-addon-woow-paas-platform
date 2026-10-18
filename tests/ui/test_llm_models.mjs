import assert from "node:assert/strict";
import test from "node:test";
import { readPaas, stubImports, toDataUrl, loadPo, translator, translateFromGlobal } from "./lib/llm_test_support.mjs";

globalThis.__llmT = translator(await loadPo(), "zh_TW");
globalThis.__catalogue = { models: null, fetchModels: async () => ({ success: true }) };
globalThis.__copy = async () => true;
const source = stubImports(await readPaas("pages/llm/tabs/LlmModelsTab.js"), {
  "@odoo/owl": "class Component {} const onMounted = () => {}; const onWillUnmount = () => {}; const useState = x => x;",
  "@web/core/l10n/translation": translateFromGlobal,
  "../../../services/llm_service": "const llmService = new Proxy({}, {get: (_, k) => globalThis.__catalogue[k]});",
  "../../../utils/clipboard": "const copyText = (...args) => globalThis.__copy(...args);",
  "../../../utils/llm_format": "const formatTaipeiTime = () => '12:00';",
});
const { LlmModelsTab, filterModels, representativeModels, modelRow, priceText } = await import(toDataUrl(source));
const item = (model, vendor, group = "default", tags = ["text"]) => ({ model, vendor, group, tags, pricing_mode: "ratio", price: { input: "0.001", output: "0.004", cache_read: "0.0001", cache_write: "0.00125" } });
const items = [item("vendor/a", "A"), item("vendor/b", "B", "default", ["vision"]), item("vendor/c", "C"), item("vendor/d", "C"), item("qwen", "Self", "free")];

test("search is case-insensitive and combines with capabilities", () => {
  assert.deepEqual(filterModels(items, " VENDOR/B ", "vision").map(x => x.model), ["vendor/b"]);
  assert.deepEqual(filterModels(items, "B", "text"), []);
  assert.deepEqual(filterModels(items, "self").map(x => x.model), ["qwen"]);
});

test("preview uses four live rows with three paid providers and a free model", () => {
  assert.deepEqual(representativeModels(items).map(x => x.model), ["vendor/a", "vendor/b", "vendor/c", "qwen"]);
  assert.deepEqual(representativeModels([]), []);
});

test("rates preserve tiny decimals; fixed and tiered never look like token prices", () => {
  assert.equal(priceText("0.000001"), "0.000001");
  assert.equal(priceText(null), "—");
  assert.equal(priceText("NaN"), "—");
  const fixed = modelRow({ ...items[0], pricing_mode: "fixed", price: { per_call: "0.4" } });
  assert.equal(fixed.note, "每次 0.4 點");
  assert.equal(fixed.input, "—");
  assert.equal(modelRow({ ...items[0], pricing_mode: "tiered", price: null }).note, "依用量計價");
});

test("cache-write footnote limits the 1-hour premium to reported Claude usage", async () => {
  const tab = new LlmModelsTab(); tab.setup();
  assert.equal(tab.cacheNote, "「—」代表未提供獨立價格。Claude 用量若由上游回報 1 小時快取寫入 tokens，該寫入費率為畫面所列快取寫入價格的 1.6 倍。");
  const template = await readPaas("pages/llm/tabs/LlmModelsTab.xml");
  assert.match(template, /class="o_woow_llm_models__cache_note" t-esc="cacheNote"/);
});

test("maintenance never renders stale rows; unavailable and search-empty differ", () => {
  const tab = new LlmModelsTab(); tab.setup();
  globalThis.__catalogue.models = { state: "ok", items };
  assert.equal(tab.unavailable, false);
  tab.state.query = "does-not-exist";
  assert.deepEqual(tab.rows, []);
  assert.equal(tab.unavailable, false);
  globalThis.__catalogue.models = { state: "maintenance", items };
  assert.equal(tab.unavailable, true);
  assert.deepEqual(tab.items, []);
});

test("model copy announces success/failure in Traditional Chinese", async () => {
  const tab = new LlmModelsTab(); tab.setup();
  const copied = [];
  globalThis.__copy = async (text) => { copied.push(text); return true; };
  await tab.copyModel("vendor/a");
  assert.deepEqual(copied, ["vendor/a"]);
  assert.equal(tab.state.announcement, "已複製模型 ID：vendor/a");
  globalThis.__copy = async () => false;
  await tab.copyModel("vendor/a");
  assert.match(tab.state.announcement, /複製失敗/);
});

test("models service discards old tariffs after any transport or maintenance failure", async () => {
  globalThis.__rpc = async () => ({ success: true, data: { state: "ok", items } });
  const serviceSource = stubImports(await readPaas("services/llm_service.js"), {
    "@odoo/owl": "const reactive = x => x;",
    "./rpc": "const jsonRpc = (...args) => globalThis.__rpc(...args);",
    "./llm_errors": "const LLM_KEY_CREATE_INTERRUPTED='interrupted', LLM_NETWORK_ERROR='network', LLM_SESSION_EXPIRED='expired';",
  });
  const { llmService } = await import(toDataUrl(serviceSource));
  await llmService.fetchModels();
  assert.equal(llmService.models.items.length, 5);
  globalThis.__rpc = async () => { throw new Error("private failure"); };
  await llmService.fetchModels();
  assert.deepEqual(llmService.models, { state: "maintenance", items: [] });
  let resolveOld;
  globalThis.__rpc = () => new Promise(resolve => { resolveOld = resolve; });
  const older = llmService.fetchModels();
  globalThis.__rpc = async () => ({ success: true, data: { state: "maintenance", items: [] } });
  await llmService.fetchModels();
  resolveOld({ success: true, data: { state: "ok", items } });
  await older;
  assert.deepEqual(llmService.models, { state: "maintenance", items: [] }, "old requests cannot restore obsolete tariffs");
});
