// LLM-09: date bounds, chart stacks, asynchronous filter races, safe errors, and narrow layout.
import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { loadPo, readPaas, stubImports, toDataUrl, translateFromGlobal, translator } from "./lib/llm_test_support.mjs";

const po = await loadPo();
globalThis.__llmT = translator(po, "zh_TW");

const helpersUrl = toDataUrl(await readPaas("pages/llm/tabs/llm_usage_helpers.js"));
const helpers = await import(helpersUrl);
const chartSource = stubImports(await readPaas("components/llm/LlmUsageChart.js"), {
  "@odoo/owl": "class Component {}",
  "@web/core/l10n/translation": translateFromGlobal,
});
const { usageChartLayout } = await import(toDataUrl(chartSource));
const formatUrl = toDataUrl(stubImports(await readPaas("utils/llm_format.js"), {
  "@web/core/l10n/translation": translateFromGlobal,
}));
const errorsUrl = toDataUrl(stubImports(await readPaas("services/llm_errors.js"), {
  "@web/core/l10n/translation": translateFromGlobal,
}));

const hooks = { mounted: [], unmount: [] };
globalThis.__llmUsageHooks = hooks;
const owlStub = [
  "class Component {}",
  "const useState = value => value;",
  "const onMounted = fn => globalThis.__llmUsageHooks.mounted.push(fn);",
  "const onWillUnmount = fn => globalThis.__llmUsageHooks.unmount.push(fn);",
].join(" ");
globalThis.__llmUsageService = {};
const tabSource = stubImports(await readPaas("pages/llm/tabs/LlmUsageTab.js"), {
  "@odoo/owl": owlStub,
  "@web/core/l10n/translation": translateFromGlobal,
  "../../../components/llm/LlmUsageChart": "const LlmUsageChart = {};",
  "../../../services/llm_service": "const llmService = globalThis.__llmUsageService;",
  "../../../services/llm_errors": `const { llmErrorMessage } = await import("${errorsUrl}");`,
  "../../../utils/clipboard": "const copyText = async value => globalThis.__llmCopy(value);",
  "../../../utils/llm_format": `const { formatAiPoints, formatCount, formatSpendPoints, formatTaipeiDateTime, formatTokens, quotaToPointsText } = await import("${formatUrl}");`,
  "./llm_usage_helpers": `const { rangeDates, taipeiToday, usageRequestParams, validateUsageDates } = await import("${helpersUrl}");`,
});
const usage = await import(toDataUrl(tabSource));

test("preset date boundaries use Taipei dates and custom ranges obey 31/180-day bounds", () => {
  assert.deepEqual(helpers.rangeDates("7d", "2026-10-20"), { start: "2026-10-14", end: "2026-10-20" });
  assert.deepEqual(helpers.rangeDates("30d", "2026-10-20"), { start: "2026-09-21", end: "2026-10-20" });
  assert.deepEqual(helpers.rangeDates("month", "2026-10-20"), { start: "2026-10-01", end: "2026-10-20" });
  assert.deepEqual(helpers.rangeDates("last_month", "2026-10-20"), { start: "2026-09-01", end: "2026-09-30" });
  assert.equal(helpers.validateUsageDates("2026-09-19", "2026-10-20", "2026-10-20"), "range");
  assert.equal(helpers.validateUsageDates("2026-04-23", "2026-04-23", "2026-10-20"), "history");
  assert.equal(helpers.validateUsageDates("2026-02-30", "2026-03-01", "2026-10-20"), "date");
  assert.equal(helpers.validateUsageDates("2026-10-20", "2026-10-20", "2026-10-20"), null);
});

test("request parameters include selected key/model and log status only for logs; page size stays bounded", () => {
  const filters = { range: "custom", start: "2026-10-01", end: "2026-10-20", keyId: "7", model: "gpt-safe", status: "error" };
  const summary = helpers.usageRequestParams({ ...filters, status: "" }, 2);
  const logs = helpers.usageRequestParams(filters, 2);
  assert.deepEqual(summary, { range: "custom", start: "2026-10-01", end: "2026-10-20", key_id: 7, model: "gpt-safe", page: 2, page_size: 20 });
  assert.equal(logs.status, "error");
  assert.equal(logs.page_size, 20);
});

test("chart stacks series at a shared scale and preserves all dates for horizontal scrolling", () => {
  const dates = Array.from({ length: 31 }, (_, index) => `2026-10-${String(index + 1).padStart(2, "0")}`);
  const chart = usageChartLayout({ dates, metric: "spend", series: [
    { id: "a", label: "Model A", values: dates.map(() => 2) },
    { id: "b", label: "Model B", values: dates.map(() => 3) },
  ] });
  assert.equal(chart.bars.length, 31);
  assert.equal(chart.width, 31 * 42);
  assert.equal(chart.max, 5);
  assert.equal(chart.bars[0].segments.length, 2);
  assert.ok(Math.abs(chart.bars[0].segments.reduce((sum, row) => sum + row.height, 0) - chart.plotHeight) < 0.001);
  assert.equal(usage.usageErrorLabel("upstream_error"), "服務暫時無法使用");
  assert.equal(usage.usageErrorLabel("unrecognized-upstream-text"), "請求失敗");
});

test("latest filter request wins when older summary/chart/log responses arrive afterward", async () => {
  const pending = { summary: [], daily: [], logs: [] };
  const calls = { summary: [], daily: [], logs: [] };
  const defer = (kind, params) => new Promise((resolve) => {
    calls[kind].push(params);
    pending[kind].push(resolve);
  });
  Object.assign(globalThis.__llmUsageService, {
    keys: [], models: { items: [] },
    fetchUsageSummary: params => defer("summary", params),
    fetchUsageDaily: params => defer("daily", params),
    fetchUsageLogs: params => defer("logs", params),
  });
  globalThis.__llmCopy = async () => false;
  globalThis.__llmUsageHooks.unmount = [];
  const tab = new usage.LlmUsageTab();
  tab.props = {};
  tab.setup();
  const today = helpers.taipeiToday();
  tab.state.range = "custom";
  tab.state.start = today;
  tab.state.end = today;
  assert.equal(tab.dateError, "", `date validation for ${today}`);
  const oldRequest = tab.loadAll(1);
  const currentRequest = tab.loadAll(2);
  assert.equal(calls.logs[1].page, 2);
  pending.summary[1]({ success: true, data: { quota: 31250, spend_points: 2, requests: 2, tokens: 200, data_as_of: "2026-10-20T00:00:00Z", complete: true } });
  pending.daily[1]({ success: true, data: { dates: ["2026-10-20"], series: [], metric: "spend", complete: true } });
  pending.logs[1]({ success: true, data: { items: [{ time_tpe: "2026-10-20T08:00:00", status: "error", error_code: "upstream_error", quota: 1, request_id: "r2" }], total: 21 } });
  await currentRequest;
  pending.summary[0]({ success: true, data: { quota: 0, spend_points: 0, requests: 0, tokens: 0, complete: true } });
  pending.daily[0]({ success: true, data: { dates: [], series: [], complete: true } });
  pending.logs[0]({ success: true, data: { items: [{ request_id: "r1" }], total: 1 } });
  await oldRequest;
  assert.equal(tab.state.summary.requests, 2);
  assert.equal(tab.state.items[0].request_id, "r2");
  assert.equal(tab.state.page, 2);
  assert.equal(tab.rows[0].displayStatus, "失敗");
  assert.equal(tab.logLabels.status, "狀態");
  assert.equal(tab.logLabels.time, "時間（台北時間）");
  assert.equal(tab.rows[0].safeError, "服務暫時無法使用");
  assert.equal(tab.rows[0].displaySpend, "<0.0001 點");
  for (const dispose of globalThis.__llmUsageHooks.unmount) dispose();
});

test("400px usage layout keeps a seven-day chart viewport, scrolls wider charts, and collapses logs into rows", async () => {
  const root = new URL("../../", import.meta.url);
  const usageScss = await readFile(new URL("src/static/src/paas/pages/llm/tabs/LlmUsageTab.scss", root), "utf8");
  const chartScss = await readFile(new URL("src/static/src/paas/components/llm/LlmUsageChart.scss", root), "utf8");
  const xml = await readPaas("pages/llm/tabs/LlmUsageTab.xml");
  assert.match(usageScss, /@media \(max-width: 600px\)/);
  assert.match(usageScss, /tbody tr \{[^}]*border/);
  assert.match(chartScss, /max-width: 294px/);
  assert.match(chartScss, /overflow-x: auto/);
  assert.match(xml, /aria-label="Request log pages"/);
  assert.match(xml, /copyRequestId/);
});

test("invalid filters invalidate older requests and prevent usage fill requests", async () => {
  const pending = { summary: [], daily: [], logs: [] };
  let calls = 0;
  const defer = (kind) => new Promise((resolve) => { pending[kind].push(resolve); calls += 1; });
  Object.assign(globalThis.__llmUsageService, {
    keysLoaded: true, modelsLoaded: true,
    fetchUsageSummary: () => defer("summary"), fetchUsageDaily: () => defer("daily"), fetchUsageLogs: () => defer("logs"),
  });
  globalThis.__llmUsageHooks.mounted = [];
  globalThis.__llmUsageHooks.unmount = [];
  const tab = new usage.LlmUsageTab();
  tab.props = {};
  tab.setup();
  tab.state.summary = { requests: 99 };
  tab.state.daily = { dates: ["old"] };
  tab.state.items = [{ request_id: "old" }];
  const validRequest = tab.loadAll(1);
  const beforeInvalid = { summary: tab.summaryEpoch, daily: tab.dailyEpoch, logs: tab.logsEpoch };
  tab.state.range = "custom";
  tab.state.start = "not-a-date";
  tab.state.end = helpers.taipeiToday();
  await tab.loadAll(1);
  assert.ok(tab.summaryEpoch > beforeInvalid.summary);
  assert.ok(tab.dailyEpoch > beforeInvalid.daily);
  assert.ok(tab.logsEpoch > beforeInvalid.logs);
  assert.equal(tab.state.summary, null);
  assert.equal(tab.state.daily, null);
  assert.deepEqual(tab.state.items, []);
  assert.equal(tab.state.loading, false);
  assert.equal(tab.state.logsLoading, false);
  const beforeFill = calls;
  await tab.loadSummaryFill();
  assert.equal(calls, beforeFill, "invalid ranges cannot enqueue an SWR fill request");
  pending.summary[0]({ success: true, data: { requests: 1, complete: true } });
  pending.daily[0]({ success: true, data: { dates: [], complete: true } });
  pending.logs[0]({ success: true, data: { items: [], total: 0 } });
  await validRequest;
  assert.equal(tab.state.error, "llm_invalid_range");
  assert.equal(tab.state.summary, null, "late valid response cannot replace invalid-filter state");
  for (const dispose of globalThis.__llmUsageHooks.unmount) dispose();
});

test("direct Usage mount asynchronously hydrates reactive key/model filters", async () => {
  let resolveKeys;
  let resolveModels;
  Object.assign(globalThis.__llmUsageService, {
    keys: [], models: { items: [] }, keysLoaded: false, modelsLoaded: false,
    fetchKeys: () => new Promise((resolve) => { resolveKeys = resolve; }),
    fetchModels: () => new Promise((resolve) => { resolveModels = resolve; }),
    fetchUsageSummary: async () => ({ success: true, data: { complete: true } }),
    fetchUsageDaily: async () => ({ success: true, data: { complete: true, dates: [], series: [] } }),
    fetchUsageLogs: async () => ({ success: true, data: { items: [], total: 0 } }),
  });
  globalThis.__llmUsageHooks.mounted = [];
  globalThis.__llmUsageHooks.unmount = [];
  const tab = new usage.LlmUsageTab();
  tab.props = {};
  tab.setup();
  assert.deepEqual(tab.keys, []);
  assert.deepEqual(tab.models, []);
  const mounted = globalThis.__llmUsageHooks.mounted[0]();
  await Promise.resolve();
  globalThis.__llmUsageService.keys = [{ id: 4, kind: "free_trial", display_name: "" }];
  globalThis.__llmUsageService.models = { items: [{ model: "model-one" }] };
  globalThis.__llmUsageService.keysLoaded = true;
  globalThis.__llmUsageService.modelsLoaded = true;
  resolveKeys({ success: true });
  resolveModels({ success: true });
  await mounted;
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(tab.keys, [{ id: "4", label: "免費試用" }]);
  assert.deepEqual(tab.models, ["model-one"]);
  for (const dispose of globalThis.__llmUsageHooks.unmount) dispose();
});
