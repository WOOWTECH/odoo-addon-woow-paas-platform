// LLM-01c（DESIGN §6.2、§6.3.1、§6.3.2、§5.4）：總覽的狀態畫面、金鑰清單列、一次性金鑰對話框。
//
// 一次性對話框的守線：明文只在對話框自己的 state；顯示中要勾「我已妥善保存此金鑰」才能〔完成〕；
// 點背景／Esc／返回鍵／換頁先問一次；建立中不能關（關了就再也拿不到明文）；關閉時清掉明文、
// 回呼不帶金鑰；卸載之後才回來的回應直接丟掉。
import assert from "node:assert/strict";
import test, { afterEach } from "node:test";
import {
  loadPo, readPaas, stubImports, toDataUrl, translateFromGlobal, translator,
} from "./lib/llm_test_support.mjs";

const po = await loadPo();
globalThis.__llmT = translator(po, "zh_TW");

const owlStub = [
  "class Component {}",
  "const useState = (value) => value;",
  "const useRef = (name) => (globalThis.__llmRefs[name] = globalThis.__llmRefs[name] || { el: null });",
  "const onMounted = (fn) => globalThis.__llmHooks.mounted.push(fn);",
  "const onPatched = (fn) => globalThis.__llmHooks.patched.push(fn);",
  "const onWillUnmount = (fn) => globalThis.__llmHooks.unmount.push(fn);",
].join(" ");
const freshHooks = () => ({ mounted: [], patched: [], unmount: [] });
globalThis.__llmHooks = freshHooks();
globalThis.__llmRefs = {};

/** 最小的 EventTarget：document 與 window 各一個（Map<type, Set<fn>>）。 */
function eventTarget(extra = {}) {
  const listeners = new Map();
  return Object.assign({
    listeners,
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      if (listeners.has(type)) listeners.get(type).delete(fn);
    },
    dispatch(type, ev = {}) {
      for (const fn of [...(listeners.get(type) || [])]) fn(ev);
    },
    has(type) {
      return listeners.has(type) && listeners.get(type).size > 0;
    },
  }, extra);
}

globalThis.document = eventTarget({ hidden: false });

/** history 的替身：pushState／back 的次數與最上面那筆的 state。 */
function fakeHistory() {
  const stack = [{ state: null }];
  return {
    pushes: 0,
    backs: 0,
    get state() {
      return stack[stack.length - 1].state;
    },
    pushState(state) {
      this.pushes += 1;
      stack.push({ state });
    },
    back() {
      this.backs += 1;
      if (stack.length > 1) stack.pop();
    },
    /** 使用者按返回鍵：彈掉最上面一筆、發 popstate。 */
    userBack() {
      if (stack.length > 1) stack.pop();
      globalThis.window.dispatch("popstate", {});
    },
    /**
     * hash 換頁（連結、location.hash）：瀏覽器推一筆新網址、**先發 popstate** 再發 hashchange；
     * 這裡由 router 的離開守衛擋下並用 replaceState 還原網址（state 沿用新那筆的 null）。
     */
    hashNavigate(hash) {
      const location = globalThis.window.location;
      const original = location.href;
      stack.push({ state: null });
      location.href = original.replace(/#.*$/, hash);
      globalThis.window.dispatch("popstate", {});
      const blocked = globalThis.__llmRouter.guard ? globalThis.__llmRouter.guard() : false;
      if (blocked) {
        location.href = original;
      }
      return blocked;
    },
    depth() {
      return stack.length;
    },
  };
}
globalThis.window = eventTarget({
  location: { href: "https://example.test/woow#/llm/overview", hash: "#/llm/overview", reload() {} },
  history: fakeHistory(),
});

const errorsUrl = toDataUrl(stubImports(await readPaas("services/llm_errors.js"), {
  "@web/core/l10n/translation": translateFromGlobal,
}));
const formatUrl = toDataUrl(stubImports(await readPaas("utils/llm_format.js"), {
  "@web/core/l10n/translation": translateFromGlobal,
}));
const modalFocusUrl = toDataUrl(await readPaas("utils/modal_focus.js"));

// llm_service、clipboard、router 換成可替換的 stub（各 test 自己設行為）。
globalThis.__llmFakeService = {};
globalThis.__llmCopy = async () => true;
globalThis.__llmRouter = {
  guard: null,
  setLeaveGuard(fn) {
    this.guard = fn;
    return () => {
      if (this.guard === fn) this.guard = null;
    };
  },
};
const modalSource = stubImports(await readPaas("components/llm/LlmKeyRevealModal.js"), {
  "@odoo/owl": owlStub,
  "@web/core/l10n/translation": translateFromGlobal,
  "../button/WoowButton": "const WoowButton = {};",
  "../icon/WoowIcon": "const WoowIcon = {};",
  "../../core/router": "const router = globalThis.__llmRouter;",
  "../../utils/clipboard": "const copyText = (...args) => globalThis.__llmCopy(...args);",
  "../../utils/modal_focus": `const { inertOutside, trapTab } = await import("${modalFocusUrl}");`,
  "../../services/llm_service":
    "const llmService = new Proxy({}, { get: (_, k) => globalThis.__llmFakeService[k] });"
    + " const newClientRequestId = () => '0f0e0d0c-0b0a-4908-8706-050403020100';",
  "../../services/llm_errors": `const { LLM_KEY_CREATE_INTERRUPTED, llmErrorMessage } = await import("${errorsUrl}");`,
});
const modalUrl = toDataUrl(modalSource);
const { LlmKeyRevealModal, buildEnvVarsText, FREE_TRIAL_EXAMPLE_MODEL } = await import(modalUrl);

const modelsSource = stubImports(await readPaas("pages/llm/tabs/LlmModelsTab.js"), {
  "@odoo/owl": owlStub,
  "@web/core/l10n/translation": translateFromGlobal,
  "../../../services/llm_service": "const llmService = {};",
  "../../../utils/clipboard": "const copyText = async () => true;",
  "../../../utils/llm_format": `const { formatTaipeiTime } = await import("${formatUrl}");`,
});
const modelsUrl = toDataUrl(modelsSource);
const overviewSource = stubImports(await readPaas("pages/llm/tabs/LlmOverviewTab.js"), {
  "@odoo/owl": owlStub,
  "@web/core/l10n/translation": translateFromGlobal,
  "./LlmModelsTab": `const { representativeModels } = await import("${modelsUrl}");`,
  "../../../components/button/WoowButton": "const WoowButton = {};",
  "../../../components/card/WoowCard": "const WoowCard = {};",
  "../../../components/icon/WoowIcon": "const WoowIcon = {};",
  "../../../components/llm/LlmKeyRevealModal": "const LlmKeyRevealModal = {};",
  "../../../components/llm/LlmTermsModal": "const LlmTermsModal = {};",
  "../../../services/llm_service":
    "const llmService = new Proxy({}, { get: (_, k) => globalThis.__llmFakeService[k] });",
  "../../../services/llm_errors": `const { LLM_SESSION_EXPIRED, llmErrorMessage, llmSuspendReason } = await import("${errorsUrl}");`,
  "../../../utils/clipboard": "const copyText = (...args) => globalThis.__llmCopy(...args);",
  "../../../utils/llm_format": `const { formatAiQuota, formatFreeTrialUsage, formatLatency, formatTaipeiDate, formatTaipeiTime, formatTokens, formatWholePoints, isExpiringSoon, isZeroCreditQuota, overdraftPointsText } = await import("${formatUrl}");`,
});
const overview = await import(toDataUrl(overviewSource));

const KEY = "sk-" + "Zq9Xw8Vu".repeat(6);
const BASE = "https://newapi.woowtech.io/v1";

/** setTimeout 的替身：記下每一次的毫秒數，手動觸發。 */
function withTimers(fn) {
  const original = { set: globalThis.setTimeout, clear: globalThis.clearTimeout };
  const timers = [];
  globalThis.setTimeout = (cb, ms) => {
    timers.push({ cb, ms });
    return timers.length;
  };
  globalThis.clearTimeout = () => {};
  const restore = () => {
    globalThis.setTimeout = original.set;
    globalThis.clearTimeout = original.clear;
  };
  return Promise.resolve(fn(timers)).finally(restore);
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

function openModal({ create, baseUrl } = {}) {
  globalThis.__llmHooks = freshHooks();
  globalThis.__llmRefs = {};
  globalThis.window.history = fakeHistory();
  globalThis.__llmRouter.guard = null;
  const closes = [];
  globalThis.__llmFakeService = { createFreeTrialKey: create };
  const modal = new LlmKeyRevealModal();
  modal.props = { kind: "free_trial", baseUrl, onClose: (...args) => closes.push(args) };
  modal.setup();
  return { modal, closes };
}

async function mount(modal) {
  for (const fn of globalThis.__llmHooks.mounted) await fn();
  // create() 是 onMounted 裡發出的 promise；等它跑完。
  await settle();
  return modal;
}

function unmount() {
  for (const fn of globalThis.__llmHooks.unmount) fn();
}

// 每個 fixture 都走與 OWL 相同的卸載生命週期；非同步 load 尚未回來時也會被 alive 擋下。
afterEach(() => unmount());

// ----------------------------------------------------------------------------- 總覽畫面

test("llmOverviewScreen：依 /api/llm/state 決定畫面（§6.2）", () => {
  const screen = overview.llmOverviewScreen;
  const withState = (state) => ({ loaded: true, overview: { account: state ? { state } : null }, error: null });
  assert.equal(screen({ loaded: false }), "loading");
  assert.equal(screen({ loaded: true, overview: null, error: "llm_disabled" }), "disabled");
  assert.equal(screen({ loaded: true, overview: null, error: "llm_network_error" }), "unavailable");
  assert.equal(screen(withState(null)), "not_activated");
  assert.equal(screen(withState("provisioning")), "provisioning");
  assert.equal(screen(withState("error")), "failed");
  assert.equal(screen(withState("active")), "active");
  assert.equal(screen(withState("suspended")), "suspended");
  assert.equal(screen(withState("closed")), "closed", "已關閉不落進「尚未開通」");
  // 輪詢途中一次失敗：有舊資料就照舊顯示，不把畫面換成「無法連線」。
  assert.equal(screen({ ...withState("provisioning"), error: "llm_network_error" }), "provisioning");
  // 開關在頁面載入後被關掉：即使有舊資料也顯示「尚未開放」。
  assert.equal(screen({ ...withState("active"), error: "llm_disabled" }), "disabled");
  // 登入逾時：即使有舊的「開通中」也換成「請重新整理頁面」（重試沒用、不能一直轉）。
  assert.equal(screen({ ...withState("provisioning"), error: "llm_session_expired" }), "auth");
  assert.equal(screen({ loaded: true, overview: null, error: "llm_auth_required" }), "auth");
});

test("金鑰清單列：只有遮罩；免費試用的名稱、類型、今日用完、建立中斷、即將到期", () => {
  const now = Date.parse("2026-10-17T00:00:00Z");
  const base = {
    id: 3, kind: "free_trial", display_name: "", masked_key: "sk-Zq9X…Vu8w", status: "active",
    disabled_by: null, expires_at: "2027-01-15T00:00:00+00:00",
  };
  const row = overview.llmKeyRow(base, now);
  assert.equal(row.name, "免費試用金鑰");
  assert.equal(row.kind, "免費試用");
  assert.equal(row.masked, "sk-Zq9X…Vu8w");
  assert.equal(row.statusLabel, "啟用中");
  assert.equal(row.expires, "2027-01-15");
  assert.equal(row.expiringSoon, false);
  assert.ok(!JSON.stringify(row).includes(KEY));

  const capped = overview.llmKeyRow({ ...base, status: "disabled", disabled_by: "free_daily_cap" }, now);
  assert.equal(capped.statusLabel, "今日試用已用完");
  assert.equal(capped.statusNote, "台北時間 00:00 自動恢復");

  assert.equal(overview.llmKeyRow({ ...base, status: "failed_reveal", masked_key: null }, now).statusLabel, "建立中斷");
  assert.equal(overview.llmKeyRow({ ...base, status: "failed_reveal", masked_key: null }, now).masked, "—");
  assert.equal(overview.llmKeyRow({ ...base, status: "expired" }, now).statusLabel, "已到期");
  assert.equal(overview.llmKeyRow({ ...base, status: "mystery" }, now).statusLabel, "狀態不明");
  // 原型鏈上的名字不能被當成狀態（一般屬性查找會拿到 Object 的東西）。
  for (const status of ["constructor", "__proto__", "toString", null]) {
    assert.equal(overview.llmKeyRow({ ...base, status }, now).statusLabel, "狀態不明", String(status));
  }
  assert.equal(overview.llmKeyRow({ ...base, expires_at: "2026-10-20T00:00:00Z" }, now).expiringSoon, true);
  assert.equal(overview.llmKeyRow({ ...base, expires_at: null }, now).expires, "永不過期");
  assert.equal(overview.llmKeyRow({ ...base, kind: "paid", display_name: "n8n-正式" }, now).kind, "付費");
});

test("最近呼叫列：台北時間、模型、tokens、耗時", () => {
  const row = overview.llmUsageRow({
    time: "2026-10-17T03:05:00+00:00", model: "qwen3-4b-cluster1", input_tokens: 12,
    output_tokens: 34567, duration_ms: 2000, request_id: "req-1",
  }, 0);
  assert.deepEqual(row, {
    key: "req-1", time: "11:05", model: "qwen3-4b-cluster1", input: "12", output: "3.4 萬", latency: "2 秒",
  });
  assert.equal(overview.llmUsageRow({}, 4).key, "row-4");
});

function makeTab(state, extra = {}) {
  globalThis.__llmHooks = freshHooks();
  globalThis.__llmRefs = {};
  const calls = { activate: [], fetchState: 0, fetchKeys: 0, fetchUsage: 0 };
  const fake = {
    overview: state,
    overviewError: null,
    overviewLoaded: true,
    keys: [],
    keysLoaded: true,
    keysError: null,
    usage: null,
    usageError: null,
    stateResults: [],
    async fetchModels() { return { success: false }; },
    async fetchState() {
      calls.fetchState += 1;
      const next = fake.stateResults.shift();
      if (next && !next.success) {
        fake.overviewError = next.code;
        return next;
      }
      if (next) fake.overview = next.data;
      fake.overviewError = null;
      return { success: true, data: fake.overview };
    },
    async activate(version) {
      calls.activate.push(version);
      return fake.activateResult;
    },
    async fetchKeys() {
      calls.fetchKeys += 1;
    },
    async fetchUsageRecent() {
      calls.fetchUsage += 1;
      return fake.usageResult;
    },
    usageResult: { success: true, data: { data_as_of: "2026-10-17T03:05:00+00:00" } },
    activateResult: { success: true, data: { state: "provisioning" } },
    ...extra,
  };
  globalThis.__llmFakeService = fake;
  const tab = new overview.LlmOverviewTab();
  tab.props = {};
  tab.setup();
  // 不跑 onMounted（不開計時器）；直接測動作。
  return { tab, calls, fake };
}

const ACTIVE = Object.freeze({
  account: {
    state: "active", balance_quota: 0, balance_points: 0, overdraft_points: 0, suspend_reason: null,
    tos: { accepted_version: "2026-10-draft-1", current_version: "2026-10-draft-1", needs_reaccept: false, blocking: false },
  },
  free_trial: { key_id: null, today_count: null, daily_limit: 200 },
  public_base_url: null,
  guard: { tripped: false },
});

test("開通：沒勾條款不送；勾了送 state 給的條款版本；llm_provisioning 也當成開通中", async () => {
  const notActivated = { account: null, tos_current_version: "2026-10-draft-1", tos_current_draft: true, free_trial: {} };
  const { tab, calls, fake } = makeTab(notActivated);
  assert.equal(tab.screen, "not_activated");
  assert.equal(tab.tosIsDraft, true);
  assert.equal(tab.pricingFallbackText, "價格暫時無法顯示，開通後可在「模型與價格」查看。");
  await tab.activate();
  assert.deepEqual(calls.activate, []);
  tab.onTosChange({ target: { checked: true } });
  fake.activateResult = { success: false, code: "llm_provisioning" };
  fake.stateResults = [{ success: true, data: { ...notActivated, account: { state: "provisioning" } } }];
  await withTimers(async (timers) => {
    await tab.activate();
    assert.deepEqual(timers.map((t) => t.ms), [overview.PROVISION_POLL_FAST_MS]);
  });
  assert.deepEqual(calls.activate, ["2026-10-draft-1"]);
  assert.equal(tab.state.actionError, null);
  assert.equal(tab.screen, "provisioning");
  assert.match(tab.provisioningText, /通常 1 分鐘內完成/);
  assert.equal(tab.provisioningProgressLabel, "正在建立 AI 模型帳戶");
});

test("條款（LLM-04）：沒有 published 的條款不能開通；送出的版本過期就取消勾選、重新讀狀態", async () => {
  const none = { account: null, tos_current_version: null, tos_current_draft: false, free_trial: {} };
  const { tab, calls, fake } = makeTab(none);
  assert.equal(tab.tosUnavailable, true);
  assert.equal(tab.tosUnavailableText, "《AI 模型服務條款》暫時無法顯示，請稍後再試");
  tab.onTosChange({ target: { checked: true } });
  assert.equal(tab.canActivate, false);
  await tab.activate();
  assert.deepEqual(calls.activate, []);

  fake.overview = { ...none, tos_current_version: "2026-10-draft-1" };
  assert.equal(tab.tosUnavailable, false);
  assert.equal(tab.tosIsDraft, false);
  for (const code of ["llm_tos_required", "llm_tos_outdated"]) {
    tab.onTosChange({ target: { checked: true } });
    fake.activateResult = { success: false, code, current_version: "2026-10-draft-2" };
    const before = calls.fetchState;
    await tab.activate();
    assert.equal(tab.state.tosAccepted, false, code);
    assert.equal(calls.fetchState, before + 1, code);
  }
  assert.equal(tab.state.actionError, "條款已更新，請重新同意");
});

test("條款對話框：連結開 read、橫幅開 accept；關閉時焦點還給觸發鈕；同意後重新讀狀態", async () => {
  const notActivated = { account: null, tos_current_version: "2026-10-draft-1", free_trial: {} };
  const { tab, calls } = makeTab(notActivated);
  let focused = 0;
  const link = { isConnected: true, focus: () => { focused += 1; } };
  tab.openTerms("read", { currentTarget: link });
  assert.equal(tab.state.showTerms, "read");
  tab.openTerms("accept", { currentTarget: {} });
  assert.equal(tab.state.showTerms, "read", "已經開著就不換");
  tab.onTosChange({ target: { checked: true } });
  tab.onTermsClosed({ accepted: false, version: "2026-10-draft-1" });
  assert.equal(tab.state.showTerms, null);
  assert.equal(focused, 1);
  assert.equal(tab.state.tosAccepted, true, "讀到的就是畫面上的版本：勾選保留");
  assert.equal(calls.fetchState, 0);

  // 對話框讀到的比畫面上的新（頁面打開之後改版）：取消勾選、重新讀狀態。
  tab.openTerms("read", { currentTarget: link });
  tab.onTermsClosed({ accepted: false, version: "2026-10-draft-2" });
  assert.equal(tab.state.tosAccepted, false);
  assert.equal(calls.fetchState, 1);

  // 橫幅的〔查看並同意〕：同意了 ⇒ 重新讀狀態、給讀屏播報。
  const gone = { isConnected: false, focus: () => { throw new Error("不在畫面上的鈕不能 focus"); } };
  tab.openTerms("accept", { currentTarget: gone });
  assert.equal(tab.state.showTerms, "accept");
  tab.onTermsClosed({ accepted: true, version: "2026-10-draft-2" });
  assert.equal(tab.state.showTerms, null);
  assert.equal(calls.fetchState, 2);
  assert.equal(tab.state.announcement, "已同意新版《AI 模型服務條款》。");
});

test("開通失敗的訊息走碼表，不顯示後端文字", async () => {
  const { tab, fake } = makeTab({ account: null, tos_current_version: "2026-10-draft-1", free_trial: {} });
  tab.onTosChange({ target: { checked: true } });
  fake.activateResult = { success: false, code: "llm_unavailable" };
  await tab.activate();
  assert.equal(tab.state.actionError, "AI 模型服務暫時無法連線，請稍後再試");
});

test("開通輪詢：連續失敗時退避（6→12→30 秒）、3 次後提示；成功一次回到 3 秒", () => {
  const delay = overview.provisionPollDelay;
  assert.equal(delay(0, 0), overview.PROVISION_POLL_FAST_MS);
  assert.equal(delay(2 * 60 * 1000 + 1, 0), overview.PROVISION_POLL_SLOW_MS);
  assert.deepEqual([1, 2, 3, 4, 9].map((n) => delay(0, n)), [6000, 12000, 30000, 30000, 30000]);
  assert.equal(overview.PROVISION_FAILURE_HINT_AFTER, 3);
});

test("開通輪詢：失敗累計退避、提示；背景分頁不打、回前景補一次；完成時給讀屏播報", async () => {
  const provisioning = { ...ACTIVE, account: { ...ACTIVE.account, state: "provisioning" } };
  const { tab, calls, fake } = makeTab(provisioning);
  await withTimers(async (timers) => {
    fake.stateResults = [
      { success: false, code: "llm_network_error" },
      { success: false, code: "llm_network_error" },
      { success: false, code: "llm_network_error" },
    ];
    await tab.load();
    await tab.load();
    assert.equal(tab.state.pollFailing, false);
    await tab.load();
    assert.equal(tab.screen, "provisioning", "有舊資料就照舊顯示開通中");
    assert.equal(tab.state.pollFailing, true);
    assert.equal(tab.pollFailingText, "暫時無法取得最新狀態，系統會自動重試。");
    assert.deepEqual(timers.map((t) => t.ms), [6000, 12000, 30000]);

    // 背景分頁：計時器到了也不打，回到前景才補一次。
    const before = calls.fetchState;
    globalThis.document.hidden = true;
    timers.at(-1).cb();
    assert.equal(calls.fetchState, before);
    assert.equal(tab.pollPaused, true);
    globalThis.document.hidden = false;
    fake.stateResults = [{ success: true, data: ACTIVE }];
    tab.onVisibilityChange();
    await settle();
    assert.equal(calls.fetchState, before + 1);
    assert.equal(tab.screen, "active");
    assert.equal(tab.state.pollFailing, false);
    assert.equal(tab.state.announcement, "AI 模型服務已開通。");
  });
});

test("登入逾時：畫面換成「請重新整理頁面」、不再輪詢", async () => {
  const provisioning = { ...ACTIVE, account: { ...ACTIVE.account, state: "provisioning" } };
  const { tab, fake } = makeTab(provisioning);
  await withTimers(async (timers) => {
    fake.stateResults = [{ success: false, code: "llm_session_expired" }];
    await tab.load();
    assert.equal(tab.screen, "auth");
    assert.equal(tab.authText, "登入已逾時，請重新整理頁面並重新登入");
    assert.deepEqual(timers, [], "不排下一次輪詢");
  });
});

test("最近 10 筆：背景分頁不刷；回前景只有滿 30 秒才補；登入逾時就停", async () => {
  const { tab, calls, fake } = makeTab(ACTIVE);
  await withTimers(async (timers) => {
    await tab.refreshUsage();
    assert.equal(calls.fetchUsage, 1);
    assert.deepEqual(timers.map((t) => t.ms), [overview.USAGE_REFRESH_MS]);
    // 計時器還沒到就切回前景：不刷（反覆切分頁不會多打）。
    tab.onVisibilityChange();
    assert.equal(calls.fetchUsage, 1);
    // 計時器在背景到期：先不刷，回前景補一次。
    globalThis.document.hidden = true;
    timers[0].cb();
    assert.equal(calls.fetchUsage, 1);
    assert.equal(tab.usagePaused, true);
    globalThis.document.hidden = false;
    tab.onVisibilityChange();
    await settle();
    assert.equal(calls.fetchUsage, 2);
    // 登入逾時：不再排下一次。
    fake.usageResult = { success: false, code: "llm_session_expired" };
    const scheduled = timers.length;
    timers.at(-1).cb();
    await settle();
    assert.equal(calls.fetchUsage, 3);
    assert.equal(timers.length, scheduled);
  });
});

test("最近 10 筆：還沒有快取時每 3 秒快問、最多 5 次，之後回到 30 秒（SWR，LLM-02）", async () => {
  const delay = overview.usageRefreshDelay;
  const empty = { success: true, data: { items: [], data_as_of: null } };
  assert.equal(delay(empty, 0), overview.USAGE_FIRST_FILL_MS);
  assert.equal(delay(empty, overview.USAGE_FIRST_FILL_POLLS), overview.USAGE_REFRESH_MS);
  assert.equal(delay({ success: true, data: { data_as_of: "2026-10-17T03:05:00+00:00" } }, 0),
    overview.USAGE_REFRESH_MS);
  assert.equal(delay({ success: false, code: "llm_unavailable" }, 0), overview.USAGE_REFRESH_MS);
  const { tab, fake } = makeTab(ACTIVE);
  await withTimers(async (timers) => {
    fake.usageResult = empty;
    for (let i = 0; i < overview.USAGE_FIRST_FILL_POLLS + 3; i += 1) {
      await tab.refreshUsage();
    }
    assert.deepEqual(timers.map((t) => t.ms), [
      ...Array(overview.USAGE_FIRST_FILL_POLLS).fill(overview.USAGE_FIRST_FILL_MS),
      ...Array(3).fill(overview.USAGE_REFRESH_MS),
    ]);
    assert.equal(tab.state.usageWaiting, false, "快問完了就不再顯示載入中");
    fake.usageResult = { success: true, data: { data_as_of: "2026-10-17T03:05:00+00:00" } };
    await tab.refreshUsage();
    assert.equal(tab.usageFirstFillPolls, 0, "拿到資料就歸零");
  });
});

test("已開通：額度用整數 quota 捨去、今日免費試用 X／200、沒有 Base URL 時顯示尚未提供", () => {
  const { tab, fake } = makeTab(ACTIVE);
  assert.equal(tab.screen, "active");
  assert.equal(tab.balanceText, "0.00 點");
  assert.equal(tab.showZeroCreditBanner, true);
  assert.equal(tab.zeroCreditText, "AI 額度為 0。您可以先使用免費試用模型；從點數錢包轉入 AI 額度的功能即將推出。");
  assert.equal(tab.freeTrialUsageText, "—／200 次");
  fake.usage = { today_count: 1, daily_limit: 200, items: [] };
  assert.equal(tab.freeTrialUsageText, "1／200 次");
  assert.equal(tab.baseUrl, null);
  assert.equal(tab.revealBaseUrl, undefined);

  fake.overview = { ...ACTIVE, account: { ...ACTIVE.account, balance_quota: 12 * 15625 + 15624, balance_points: 12 } };
  assert.equal(tab.balanceText, "12.99 點", "不是假的 12.00");
  assert.equal(tab.showZeroCreditBanner, false);
  // 只有整數點數（沒有 quota）：不帶小數。
  fake.overview = { ...ACTIVE, account: { ...ACTIVE.account, balance_quota: undefined, balance_points: 12 } };
  assert.equal(tab.balanceText, "12 點");
});

test("已開通：已有試用金鑰就不能再建；說明給走得通的下一步；過期的建立中不算", () => {
  const { tab, fake } = makeTab(ACTIVE);
  assert.equal(tab.canCreateFreeTrialKey, true);
  tab.openFreeTrialKey();
  assert.equal(tab.state.showReveal, true);
  assert.equal(tab.canCreateFreeTrialKey, false, "對話框開著時不能再開一個");
  tab.state.showReveal = false;

  fake.overview = { ...ACTIVE, free_trial: { ...ACTIVE.free_trial, key_id: 9 } };
  assert.equal(tab.canCreateFreeTrialKey, false);
  assert.equal(tab.freeTrialExistsText, "您已經有一把免費試用金鑰（每個帳號一把）。如果沒有保存到金鑰，請聯絡客服協助重設。");
  fake.keys = [{ id: 9, kind: "free_trial", status: "expired" }];
  assert.equal(tab.freeTrialExistsText, "您的免費試用金鑰已到期。金鑰管理功能上線前，請聯絡客服協助重設。");

  // 建立中超過 5 分鐘（worker 途中被殺）：仍然可以按〔建立〕（後端會先收尾）。
  const old = new Date(Date.now() - overview.STALE_CREATING_MS - 1000).toISOString();
  fake.keys = [{ id: 9, kind: "free_trial", status: "creating", created_at: old }];
  assert.equal(tab.hasFreeTrialKey, false);
  assert.equal(tab.canCreateFreeTrialKey, true);
  fake.keys = [{ id: 9, kind: "free_trial", status: "creating", created_at: new Date().toISOString() }];
  assert.equal(tab.hasFreeTrialKey, true, "剛開始建的那把照常算數");
});

test("清單還沒回來時是載入中，不先閃「還沒有…」", () => {
  const { tab, fake } = makeTab(ACTIVE);
  fake.keysLoaded = false;
  assert.equal(tab.keysLoading, true);
  assert.equal(tab.usageLoading, true);
  assert.equal(tab.listLoadingText, "載入中…");
  fake.keysLoaded = true;
  fake.usage = { today_count: 0, items: [], data_as_of: "2026-10-17T03:05:00+00:00" };
  assert.equal(tab.keysLoading, false);
  assert.equal(tab.usageLoading, false);
  assert.equal(tab.emptyUsageText, "今天還沒有免費試用呼叫紀錄。");
  // 後端還沒有今天的快取（SWR，LLM-02）：快問期間是載入中；快問完了還沒有就明說「還拿不到」。
  fake.usage = { today_count: null, items: [], data_as_of: null };
  tab.state.usageWaiting = true;
  assert.equal(tab.usageLoading, true);
  tab.state.usageWaiting = false;
  assert.equal(tab.usageLoading, false);
  assert.equal(tab.emptyUsageText, "用量資料暫時還拿不到，請稍後再看。");
  fake.usage = null;
  fake.usageError = "llm_unavailable";
  assert.equal(tab.usageLoading, false, "失敗時顯示錯誤，不是一直載入中");
});

test("橫幅：條款已更新、系統維護、已透支（§6.2）；維護中或條款過期不能建金鑰", () => {
  const { tab, fake } = makeTab(ACTIVE);
  assert.equal(tab.needsReaccept, false);
  assert.equal(tab.guardTripped, false);
  assert.equal(tab.overdraftText, null);

  const updated = (tos) => ({ ...ACTIVE, account: { ...ACTIVE.account, tos: { ...ACTIVE.account.tos, ...tos } } });
  fake.overview = updated({ current_version: "2026-11-1", needs_reaccept: true, blocking: true });
  assert.equal(tab.needsReaccept, true);
  assert.equal(tab.tosBlocking, true);
  assert.equal(tab.needsReacceptText, "《AI 模型服務條款》已更新，請重新同意才能建立金鑰、轉換點數或重新啟用金鑰；既有金鑰不受影響。");
  assert.equal(tab.canCreateFreeTrialKey, false);
  // requires_reaccept=false 的改版：只有橫幅，不擋（畫面與 gate 一致，LLM-05 審查）。
  fake.overview = updated({ current_version: "2026-11-1", needs_reaccept: true, blocking: false });
  assert.equal(tab.needsReaccept, true);
  assert.equal(tab.tosBlocking, false);
  assert.equal(tab.needsReacceptText, "《AI 模型服務條款》已更新，請查看新版條款；目前的操作不受影響。");
  assert.equal(tab.canCreateFreeTrialKey, true);
  // 舊後端沒有 blocking：版本不同就當成會擋。
  fake.overview = updated({ needs_reaccept: true, blocking: undefined });
  assert.equal(tab.tosBlocking, true);

  fake.overview = { ...ACTIVE, guard: { tripped: true } };
  assert.equal(tab.guardTripped, true);
  assert.equal(tab.guardText, "系統維護中，暫時無法建立金鑰或轉換點數；既有金鑰不受影響。");
  assert.equal(tab.canCreateFreeTrialKey, false);

  fake.overview = { ...ACTIVE, account: { ...ACTIVE.account, balance_quota: -15626, overdraft_points: 2 } };
  assert.equal(tab.overdraftText, "AI 額度已透支 1.01 點，下次轉換會先扣抵。");
  assert.equal(tab.balanceText, "-1.01 點");
  assert.equal(tab.showZeroCreditBanner, false);
});

test("已暫停：顯示原因（沒有或不認得的原因也不會指向自己）、不能建金鑰", () => {
  const suspended = {
    account: { state: "suspended", balance_quota: 5 * 15625, overdraft_points: 0, suspend_reason: "policy" },
    free_trial: {},
  };
  const { tab, fake } = makeTab(suspended);
  assert.equal(tab.screen, "suspended");
  assert.equal(tab.suspendedText, "AI 模型服務已暫停：違反《AI 模型服務條款》，請聯絡客服。");
  assert.equal(tab.canCreateFreeTrialKey, false);
  for (const reason of [null, "weird", "constructor"]) {
    fake.overview = { ...suspended, account: { ...suspended.account, suspend_reason: reason } };
    assert.equal(tab.suspendedText, "AI 模型服務已暫停，請聯絡客服。", String(reason));
    assert.doesNotMatch(tab.suspendedText, /原因見橫幅/);
  }
});

test("開通失敗：tracer 的 error 是終態，直接說要聯絡客服；按鈕只重新讀狀態", async () => {
  const failed = { ...ACTIVE, account: { ...ACTIVE.account, state: "error" } };
  const { tab, calls } = makeTab(failed);
  assert.equal(tab.screen, "failed");
  assert.equal(tab.failedText, "AI 模型帳戶建立失敗，請聯絡客服");
  await tab.refresh();
  assert.equal(calls.fetchState, 1);
  assert.equal(tab.state.refreshing, false);
});

test("已關閉：不落進「尚未開通」、說明餘額不退與聯絡客服", () => {
  const closed = { ...ACTIVE, account: { ...ACTIVE.account, state: "closed", balance_quota: 3 * 15625 } };
  const { tab, fake } = makeTab(closed);
  assert.equal(tab.screen, "closed");
  assert.equal(tab.closedText, "AI 模型帳戶已關閉。剩餘額度 3.00 點依條款不予退還。如需重新開通，請聯絡客服。");
  fake.overview = { ...closed, account: { ...closed.account, balance_quota: 0 } };
  assert.equal(tab.closedText, "AI 模型帳戶已關閉。如需重新開通，請聯絡客服。");
});

test("對話框關閉：焦點回到「免費試用」標題、重新讀狀態", () => {
  const { tab, calls } = makeTab(ACTIVE);
  let focused = 0;
  globalThis.__llmRefs.freeTrialHeading.el = { focus: () => { focused += 1; } };
  tab.state.showReveal = true;
  tab.onRevealClosed();
  assert.equal(tab.state.showReveal, false);
  assert.equal(focused, 1);
  assert.equal(calls.fetchState, 1);
});

// ----------------------------------------------------------------------------- 一次性金鑰對話框

test("〔複製為環境變數〕：一定要有 Base URL；export 兩行（shell 之後啟動的工具讀得到）", () => {
  assert.equal(buildEnvVarsText(KEY, BASE),
    `export OPENAI_API_KEY=${KEY}\nexport OPENAI_BASE_URL=${BASE}`);
  // 沒有 Base URL：不給（只有金鑰那行的話，工具會連到預設的 api.openai.com、把金鑰送給第三方）。
  assert.equal(buildEnvVarsText(KEY, null), null);
  assert.equal(buildEnvVarsText(KEY, ""), null);
  assert.equal(buildEnvVarsText(null, BASE), null);
  assert.equal(FREE_TRIAL_EXAMPLE_MODEL, "qwen3-4b-cluster1");
});

test("沒有 Base URL：〔複製為環境變數〕不出現、改顯示說明；硬按也不會複製只有金鑰的那行", async () => {
  const copied = [];
  globalThis.__llmCopy = async (text) => {
    copied.push(text);
    return true;
  };
  const { modal } = openModal({ create: async () => ({ success: true, data: { key: KEY, base_url: null } }) });
  await mount(modal);
  assert.equal(modal.state.phase, "shown");
  assert.equal(modal.envVarsAvailable, false);
  assert.equal(modal.envUnavailableText,
    "Base URL 尚未提供，暫時無法產生環境變數。請勿把這把金鑰用在預設連到 OpenAI 的工具。");
  await modal.copy("env");
  assert.deepEqual(copied, []);
  assert.equal(modal.state.copyFailed, true);
  modal.close();
  globalThis.__llmCopy = async () => true;
});

test("對話框文案（§6.3.2）：標題跟著階段走；免費試用的內文給走得通的出路", async () => {
  let release;
  const { modal } = openModal({ create: () => new Promise((resolve) => { release = resolve; }) });
  for (const fn of globalThis.__llmHooks.mounted) fn();
  assert.equal(modal.title, "正在建立免費試用金鑰");
  release({ success: false, code: "llm_free_key_exists" });
  await settle();
  assert.equal(modal.state.phase, "error");
  assert.equal(modal.title, "金鑰沒有建立完成");
  modal.close();

  const shown = openModal({ create: async () => ({ success: true, data: { key: KEY } }) });
  await mount(shown.modal);
  assert.equal(shown.modal.title, "請立即複製您的 API 金鑰");
  assert.equal(shown.modal.intro, "基於安全考量，完整金鑰只會顯示這一次。關閉視窗後無法再查看；如果遺失，請聯絡客服協助重設。");
  assert.deepEqual(shown.modal.copyLabels, {
    key: "複製金鑰", base: "複製 Base URL", model: "複製模型名稱", env: "複製為環境變數",
  });
  shown.modal.close();
});

test("建立中不能關（點背景、Esc、關閉、返回鍵、換頁都沒反應）", async () => {
  let release;
  const { modal, closes } = openModal({
    create: () => new Promise((resolve) => { release = resolve; }),
  });
  for (const fn of globalThis.__llmHooks.mounted) fn();
  const history = globalThis.window.history;
  assert.equal(modal.state.phase, "creating");
  assert.equal(history.pushes, 1, "打開時推一筆哨兵");
  modal.requestClose();
  globalThis.document.dispatch("keydown", { key: "Escape" });
  modal.onBackdropClick({ target: 1, currentTarget: 1 });
  history.userBack();
  assert.equal(history.pushes, 2, "返回鍵彈掉哨兵後補回去");
  assert.equal(globalThis.__llmRouter.guard(), true, "換頁被擋下");
  const unload = { prevented: false, preventDefault() { this.prevented = true; } };
  globalThis.window.dispatch("beforeunload", unload);
  assert.equal(unload.prevented, true, "重新整理／關分頁要提醒");
  assert.deepEqual(closes, []);
  assert.equal(modal.state.confirmClose, false);
  release({ success: false, code: "llm_unavailable" });
  await settle();
  assert.equal(modal.state.phase, "error");
  // 沒建成：不再擋換頁與重新整理。
  assert.equal(globalThis.__llmRouter.guard(), false);
  const after = { prevented: false, preventDefault() { this.prevented = true; } };
  globalThis.window.dispatch("beforeunload", after);
  assert.equal(after.prevented, false);
  modal.close();
});

test("顯示一次：要勾「我已妥善保存此金鑰」才能完成；背景／Esc／返回鍵／換頁先確認；關閉後清掉明文、回呼不帶金鑰", async () => {
  const requests = [];
  const { modal, closes } = openModal({
    baseUrl: "https://newapi-stg.woowtech.io/v1",
    create: async (id) => {
      requests.push(id);
      return { success: true, data: { key: KEY, masked_key: "sk-Zq9X…Vu8w", base_url: null } };
    },
  });
  await mount(modal);
  const history = globalThis.window.history;
  assert.deepEqual(requests, ["0f0e0d0c-0b0a-4908-8706-050403020100"]);
  assert.equal(modal.state.phase, "shown");
  assert.equal(modal.state.key, KEY);
  assert.equal(modal.state.baseUrl, "https://newapi-stg.woowtech.io/v1", "回應沒帶 base_url 時沿用 state 的");
  assert.equal(modal.envVarsAvailable, true);

  // 沒勾就按〔完成〕：不關。
  modal.finish();
  assert.deepEqual(closes, []);

  // Esc／點背景：先跳確認，不直接關。
  globalThis.document.dispatch("keydown", { key: "Escape" });
  assert.equal(modal.state.confirmClose, true);
  modal.cancelClose();
  modal.onBackdropClick({ target: 1, currentTarget: 1 });
  assert.equal(modal.state.confirmClose, true);
  modal.onBackdropClick({ target: 1, currentTarget: 2 }); // 點在對話框裡面：不算
  modal.cancelClose();
  // 返回鍵（手機側滑、Android 返回）：哨兵補回去、跳確認。
  history.userBack();
  assert.equal(modal.state.confirmClose, true);
  assert.equal(history.pushes, 2);
  modal.cancelClose();
  // 側欄等換頁：被擋下、跳確認。
  assert.equal(globalThis.__llmRouter.guard(), true);
  assert.equal(modal.state.confirmClose, true);
  modal.cancelClose();
  assert.deepEqual(closes, []);

  modal.onSavedChange({ target: { checked: true } });
  assert.equal(modal.canFinish, true);
  modal.finish();
  assert.deepEqual(closes, [[{ created: true }]]);
  assert.equal(modal.state.key, null);
  assert.ok(!JSON.stringify(closes).includes(KEY));
  // 關閉時守衛全部拆掉，自己推的哨兵收回（history.back 一次）。
  assert.equal(history.backs, 1);
  assert.equal(globalThis.__llmRouter.guard, null);
  for (const type of ["popstate", "beforeunload"]) {
    assert.equal(globalThis.window.has(type), false, type);
  }
  assert.equal(globalThis.document.has("keydown"), false);
  // 卸載（冪等）：不再多退一次。
  unmount();
  assert.equal(history.backs, 1);
});

test("hash 換頁被擋下：網址還原之後才補哨兵；關閉時收回哨兵落在對話框這一頁（不是剛才想去的那頁）", async () => {
  const { modal, closes } = openModal({ create: async () => ({ success: true, data: { key: KEY } }) });
  await mount(modal);
  const history = globalThis.window.history;
  const href = globalThis.window.location.href;
  assert.equal(history.pushes, 1);
  assert.equal(history.hashNavigate("#/billing"), true, "換頁被擋下");
  assert.equal(history.pushes, 1, "popstate 當下網址還是 #/billing，不能在那時補哨兵");
  assert.equal(modal.state.confirmClose, true);
  await settle();
  assert.equal(history.pushes, 2, "網址還原之後補一筆哨兵");
  modal.close();
  assert.deepEqual(closes, [[{ created: true }]]);
  assert.equal(history.backs, 1, "收回自己的哨兵");
  assert.equal(globalThis.window.location.href, href);
  assert.equal(history.state, null, "落在被還原成對話框這一頁的那筆（state 是 null，不是哨兵）");
});

test("〔仍要關閉〕也會清掉明文；返回鍵彈掉哨兵之後關閉不會再多退一次", async () => {
  const { modal, closes } = openModal({ create: async () => ({ success: true, data: { key: KEY } }) });
  await mount(modal);
  const history = globalThis.window.history;
  modal.requestClose();
  modal.close();
  assert.equal(modal.state.key, null);
  assert.deepEqual(closes, [[{ created: true }]]);
  assert.equal(history.backs, 1);

  const failed = openModal({ create: async () => ({ success: false, code: "llm_unavailable" }) });
  await mount(failed.modal);
  const failedHistory = globalThis.window.history;
  failedHistory.userBack(); // 沒建成時返回鍵＝直接關
  assert.deepEqual(failed.closes, [[{ created: false }]]);
  assert.equal(failedHistory.backs, 0, "哨兵已經被返回鍵彈掉");
});

test("建立中被卸載（例如重新整理途中）：之後才回來的回應直接丟掉，明文不寫進 state", async () => {
  let release;
  const { modal } = openModal({ create: () => new Promise((resolve) => { release = resolve; }) });
  for (const fn of globalThis.__llmHooks.mounted) fn();
  unmount();
  release({ success: true, data: { key: KEY, masked_key: "sk-Zq9X…Vu8w" } });
  await settle();
  assert.equal(modal.state.key, null);
  assert.equal(modal.state.phase, "creating");
  assert.ok(!JSON.stringify(modal.state).includes(KEY));
  assert.equal(globalThis.__llmRouter.guard, null);
});

test("建立後半段丟例外、或 service 自己丟例外：一律進 error（不會卡在沒有關閉鈕的建立中）", async () => {
  const throwing = openModal({
    create: async () => ({
      success: true,
      data: { key: KEY, get masked_key() { throw new Error("boom"); } },
    }),
  });
  await mount(throwing.modal);
  assert.equal(throwing.modal.state.phase, "error");
  assert.equal(throwing.modal.state.key, null);
  assert.match(throwing.modal.state.errorMessage, /金鑰可能已建立但沒有顯示/);
  throwing.modal.close();

  const rejecting = openModal({ create: async () => { throw new Error("unexpected"); } });
  await mount(rejecting.modal);
  assert.equal(rejecting.modal.state.phase, "error");
  assert.match(rejecting.modal.state.errorMessage, /請聯絡客服協助重設/);
  rejecting.modal.close();
});

test("複製：成功顯示「已複製」並給讀屏播報；失敗要有提示；環境變數用 buildEnvVarsText", async () => {
  const copied = [];
  globalThis.__llmCopy = async (text) => {
    copied.push(text);
    return true;
  };
  const { modal } = openModal({
    baseUrl: BASE,
    create: async () => ({ success: true, data: { key: KEY } }),
  });
  await mount(modal);
  await modal.copy("key");
  assert.equal(modal.state.copied, "key");
  assert.equal(modal.copiedAnnouncement, "已複製金鑰。");
  await modal.copy("env");
  assert.equal(modal.state.copied, "env");
  assert.equal(modal.copiedAnnouncement, "已複製環境變數。");
  assert.deepEqual(copied, [KEY, buildEnvVarsText(KEY, BASE)]);
  globalThis.__llmCopy = async () => false;
  await modal.copy("base");
  assert.equal(modal.state.copied, null);
  assert.equal(modal.copiedAnnouncement, "");
  assert.equal(modal.state.copyFailed, true);
  assert.equal(modal.copyFailedText, "複製失敗，請手動選取文字複製。");
  globalThis.__llmCopy = async () => true;
  modal.close();
});

test("沒建成：顯示碼表句子、可直接關閉（created=false）；成功卻沒金鑰當成「可能已建立」", async () => {
  const failed = openModal({ create: async () => ({ success: false, code: "llm_free_key_exists" }) });
  await mount(failed.modal);
  assert.equal(failed.modal.state.phase, "error");
  assert.equal(failed.modal.state.errorMessage, "每個帳號只能有一把免費試用金鑰");
  failed.modal.requestClose();
  assert.deepEqual(failed.closes, [[{ created: false }]]);

  const odd = openModal({ create: async () => ({ success: true, data: { key: null } }) });
  await mount(odd.modal);
  assert.equal(odd.modal.state.phase, "error");
  assert.match(odd.modal.state.errorMessage, /金鑰可能已建立但沒有顯示/);
  // 「可能已建立」要給兩種清單狀態都走得通的下一步。
  assert.match(odd.modal.state.errorMessage, /建立中斷.*重新建立/);
  assert.match(odd.modal.state.errorMessage, /啟用中.*請聯絡客服/);
  odd.modal.close();
});
