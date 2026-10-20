import assert from "node:assert/strict";
import test from "node:test";
import { readPaas, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

const calls = [];
globalThis.__notificationRpc = async (...args) => {
  calls.push(args);
  return { success: true, data: { state: "suspended" } };
};
const serviceSource = stubImports(await readPaas("services/llm_service.js"), {
  "@odoo/owl": "const reactive = value => value;",
  "./rpc": "const jsonRpc = (...args) => globalThis.__notificationRpc(...args);",
  "./llm_errors": "const LLM_KEY_CREATE_INTERRUPTED='interrupted', LLM_NETWORK_ERROR='network', LLM_SESSION_EXPIRED='expired';",
});
const { llmService } = await import(toDataUrl(serviceSource));
const noticeToken = "n1.123.1799999999." + "A".repeat(43);

globalThis.__pageHooks = { mounted: [], unmount: [], patched: [], update: [] };
globalThis.__pageApi = llmService;
globalThis.location = { hash: "#/llm/overview", href: "https://example.test/woow#/llm/overview" };
globalThis.window = { addEventListener() {}, removeEventListener() {} };
globalThis.history = { state: { keep: true }, entries: [], replaceState(state, _title, url) {
  this.entries.push(url); this.state = state;
  const hashAt = url.indexOf("#");
  if (hashAt >= 0) { globalThis.location.href = url; globalThis.location.hash = url.slice(hashAt); }
} };
const pageSource = stubImports(await readPaas("pages/llm/LlmPage.js"), {
  "@odoo/owl": "class Component {} const onMounted=fn=>globalThis.__pageHooks.mounted.push(fn); const onPatched=fn=>globalThis.__pageHooks.patched.push(fn); const onWillUnmount=fn=>globalThis.__pageHooks.unmount.push(fn); const onWillUpdateProps=fn=>globalThis.__pageHooks.update.push(fn); const useRef=()=>({el:null}); const useState=x=>x;",
  "@web/core/l10n/translation": "const _t=s=>s;",
  "../../components/icon/WoowIcon": "const WoowIcon={};",
  "../../core/router": "const router={navigate:()=>{}};",
  "../../core/llm_routes": "const LLM_TABS=['overview','keys','usage','models','credit','docs']; const isLlmTabLive=()=>true; const llmTabPath=x=>x; const normalizeLlmTab=x=>x||'overview';",
  "./tabs/LlmCreditTab": "class LlmCreditTab {}",
  "./tabs/LlmModelsTab": "class LlmModelsTab {}",
  "./tabs/LlmOverviewTab": "class LlmOverviewTab {}",
  "./tabs/LlmKeysTab": "class LlmKeysTab {}",
  "./tabs/LlmDocsTab": "class LlmDocsTab {}",
  "../../services/llm_service": "const llmService=globalThis.__pageApi;",
  "./tabs/llm_docs_helpers": "const createLlmDocsKeyHandoff=()=>({clear(){},put(){},peekKey(){},peekMode(){},peekAllowedModels(){}}); const validatedLlmPublicBaseUrl=()=>'';",
  "./tabs/LlmUsageTab": "class LlmUsageTab {}",
  "../../services/llm_errors": "const llmErrorMessage=result=>result.code;",
});
const { LlmPage } = await import(toDataUrl(pageSource));

test("settings and report APIs send exact payloads and reject invalid values", async () => {
  calls.length = 0;
  assert.deepEqual(await llmService.updateSettings(0), { success: true, data: { state: "suspended" } });
  assert.deepEqual(await llmService.updateSettings(100000), { success: true, data: { state: "suspended" } });
  for (const invalid of [-1, 1.5, "50", 100001, NaN]) {
    assert.deepEqual(await llmService.updateSettings(invalid), { success: false, code: "llm_settings_invalid" });
  }
  assert.deepEqual(await llmService.reportNotMe(noticeToken), { success: true, data: { state: "suspended" } });
  for (const invalid of ["", "bad%token", "bad/token", "x".repeat(513), null]) {
    assert.deepEqual(await llmService.reportNotMe(invalid), { success: false, code: "llm_not_me_invalid" });
  }
  assert.deepEqual(calls, [
    ["/api/llm/account/settings", { low_balance_threshold: 0 }],
    ["/api/llm/account/settings", { low_balance_threshold: 100000 }],
    ["/api/llm/account/report_not_me", { token: noticeToken }],
  ]);
  assert.doesNotMatch(JSON.stringify(calls), /x{20}/);
  globalThis.__notificationRpc = async () => ({
    success: false, code: "llm_not_me_invalid", error: "private server exception with token=secret",
  });
  const rejected = await llmService.reportNotMe(noticeToken);
  assert.deepEqual(rejected, { success: false, code: "llm_not_me_invalid" });
  assert.equal(JSON.stringify(rejected).includes("private server exception"), false);
});

test("not-me query is stripped before confirmation, never auto-submits, and submits once on explicit confirm", async () => {
  calls.length = 0;
  globalThis.location.hash = "#/llm/overview?view=compact&not_me=" + noticeToken;
  globalThis.location.href = "https://example.test/woow" + globalThis.location.hash;
  const page = new LlmPage();
  page.props = { tab: "overview" };
  page.setup();
  globalThis.__pageHooks.mounted.at(-1)();
  assert.equal(globalThis.history.entries.at(-1), "https://example.test/woow#/llm/overview?view=compact");
  assert.equal(page.notMeState.confirmation, true);
  assert.equal(page.notMeToken, noticeToken);
  assert.equal(calls.length, 0, "opening the URL never reports automatically");
  assert.equal(JSON.stringify(page.notMeState).includes(noticeToken), false);

  let resolve;
  globalThis.__notificationRpc = (...args) => {
    calls.push(args);
    if (args[0] === "/api/llm/state") return Promise.resolve({ success: true, data: { account: { state: "suspended" } } });
    return new Promise(r => { resolve = r; });
  };
  const first = page.confirmNotMe();
  await page.confirmNotMe();
  assert.equal(calls.length, 1, "double click is ignored while the first request is pending");
  assert.deepEqual(calls[0], ["/api/llm/account/report_not_me", { token: noticeToken }]);
  resolve({ success: true, data: { state: "suspended" } });
  await first;
  assert.equal(page.notMeToken, null);
  assert.equal(page.notMeState.confirmation, false);
  assert.equal(page.notMeState.done, true);
  assert.equal(calls.filter(([url]) => url === "/api/llm/state").length, 1);
  globalThis.__pageHooks.unmount.at(-1)();
});

test("duplicate, invalid, back-navigation, and leave paths discard the token without request", () => {
  calls.length = 0;
  const page = Object.create(LlmPage.prototype);
  page.notMeToken = null;
  page.notMeSubmitting = false;
  page.notMeState = { confirmation: false, busy: false, error: null, done: false };
  globalThis.location.hash = "#/llm/overview?not_me=first&not_me=second";
  globalThis.location.href = "https://example.test/woow" + globalThis.location.hash;
  page.consumeNotMeQuery();
  assert.equal(page.notMeToken, null);
  assert.equal(globalThis.history.entries.at(-1), "https://example.test/woow#/llm/overview");
  assert.equal(page.notMeState.error, "llm_not_me_invalid");
  page.dismissNotMe();

  globalThis.location.hash = "#/llm/overview?not_me=bad%25token";
  globalThis.location.href = "https://example.test/woow" + globalThis.location.hash;
  page.consumeNotMeQuery();
  assert.equal(page.notMeToken, null);
  assert.equal(page.notMeState.error, "llm_not_me_invalid");
  page.notMeToken = noticeToken;
  page.notMeState.confirmation = true;
  page.consumeNotMeQuery(); // URL has no token after replaceState; consumed closure remains until leaving.
  assert.equal(page.notMeToken, noticeToken);
  globalThis.location.hash = "#/llm/keys";
  page.consumeNotMeQuery();
  assert.equal(page.notMeToken, null);
  assert.equal(page.notMeState.confirmation, false);
  assert.equal(calls.length, 0);
});

test("late completion after leaving cannot clear or refresh a newer confirmation", async () => {
  calls.length = 0;
  const page = Object.create(LlmPage.prototype);
  page.notMeToken = noticeToken;
  page.notMeSubmitting = false;
  page.notMeGeneration = 1;
  page.notMeState = { confirmation: true, busy: false, error: null, done: false };
  let resolveReport;
  const oldReport = llmService.reportNotMe;
  const oldFetch = llmService.fetchState;
  let refreshes = 0;
  llmService.reportNotMe = async () => new Promise(resolve => { resolveReport = resolve; });
  llmService.fetchState = async () => { refreshes++; return { success: true }; };
  try {
    const pending = page.confirmNotMe();
    page.consumeNotMeQuery(); // route is currently keys, so this clears the old request's generation
    page.notMeToken = noticeToken;
    page.notMeState.confirmation = true;
    globalThis.location.hash = "#/llm/keys";
    resolveReport({ success: true, data: { state: "suspended" } });
    await pending;
    assert.equal(page.notMeToken, noticeToken);
    assert.equal(page.notMeState.confirmation, true);
    assert.equal(page.notMeState.done, false);
    assert.equal(refreshes, 0);
  } finally {
    llmService.reportNotMe = oldReport;
    llmService.fetchState = oldFetch;
  }
});

test("credit threshold rejects blank, fractional, negative and oversized input; saves whole points", async () => {
  const submitted = [];
  globalThis.__thresholdApi = {
    overview: { account: { state: "active", low_balance_threshold: 25 } },
    fetchCreditOrders: async () => ({ success: true, data: { items: [], total: 0 } }),
    fetchState: async () => ({ success: true, data: { account: { state: "active", low_balance_threshold: 25 } } }),
    updateSettings: async value => { submitted.push(value); return { success: true, data: { low_balance_threshold: value } }; },
  };
  const source = stubImports(await readPaas("pages/llm/tabs/LlmCreditTab.js"), {
    "@odoo/owl": "class Component {} const onMounted=()=>{}; const onWillUnmount=()=>{}; const useState=x=>x;",
    "@web/core/l10n/translation": "const _t=s=>s;",
    "../../../services/billing_service": "const billingService={fetchWallet:async()=>{}};",
    "../../../services/llm_service": "const llmService=globalThis.__thresholdApi;",
    "../../../utils/llm_format": "const formatAiQuota=()=>'', formatTaipeiDateTime=()=>'';",
    "../../../services/llm_errors": "const llmErrorMessage=r=>r.code;",
    "../../../components/llm/LlmConvertModal": "class LlmConvertModal {} const creditOrderText=()=>'', CREDIT_PENDING_STATES=[], creditPollDelay=()=>1;",
  });
  const { LlmCreditTab } = await import(toDataUrl(source));
  const tab = new LlmCreditTab(); tab.setup();
  assert.equal(tab.thresholdVisible, true);
  assert.equal(tab.thresholdInputError, null, "initial render must not show an invalid-input error");
  assert.equal(tab.thresholdInputErrorText, "");
  tab.state.thresholdError = "llm_unavailable";
  assert.equal(tab.thresholdSaveErrorText, "llm_unavailable");
  for (const invalid of ["", "1.5", "-1", "100001", "1e2"]) {
    tab.setThreshold(invalid);
    assert.equal(tab.thresholdInputError, "llm_settings_invalid");
    assert.equal(tab.thresholdInputErrorText, "llm_settings_invalid");
    await tab.saveThreshold();
  }
  assert.deepEqual(submitted, []);
  tab.setThreshold("0"); await tab.saveThreshold();
  assert.deepEqual(submitted, [0]);
  assert.equal(tab.state.thresholdNotice, "Low balance threshold saved.");
  globalThis.__thresholdApi.overview.account.state = "suspended";
  assert.equal(tab.thresholdVisible, true);
  globalThis.__thresholdApi.overview.account.state = "provisioning";
  assert.equal(tab.thresholdVisible, false);
});

test("notification dialog and threshold form have scoped mobile-safe styling", async () => {
  const pageStyles = await readPaas("pages/llm/LlmPage.scss");
  const creditStyles = await readPaas("pages/llm/tabs/LlmCreditTab.scss");
  assert.match(pageStyles, /\.o_woow_llm_not_me\s*\{/);
  assert.match(pageStyles, /@media\s*\(max-width:\s*\$woow-bp-sm\)/);
  assert.match(pageStyles, /\.o_woow_modal_footer button\s*\{\s*width:\s*100%/);
  assert.match(creditStyles, /&__threshold\s*\{/);
  assert.match(creditStyles, /width:\s*100%;\s*min-width:\s*0/);
  assert.match(creditStyles, /&__threshold button\s*\{\s*width:\s*100%/);
});

