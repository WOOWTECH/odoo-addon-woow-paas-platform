// LLM-01c（DESIGN §6.2、§6.3.1、§6.3.2、§5.4）：總覽的狀態畫面、金鑰清單列、一次性金鑰對話框。
//
// 一次性對話框的守線：明文只在對話框自己的 state；顯示中要勾「我已妥善保存此金鑰」才能〔完成〕；
// 點背景／Esc 先問一次；建立中不能關（關了就再也拿不到明文）；關閉時清掉明文、回呼不帶金鑰。
import assert from "node:assert/strict";
import test from "node:test";
import {
  loadPo, readPaas, stubImports, toDataUrl, translateFromGlobal, translator,
} from "./lib/llm_test_support.mjs";

const po = await loadPo();
globalThis.__llmT = translator(po, "zh_TW");

const owlStub = [
  "class Component {}",
  "const useState = (value) => value;",
  "const onMounted = (fn) => globalThis.__llmHooks.mounted.push(fn);",
  "const onWillUnmount = (fn) => globalThis.__llmHooks.unmount.push(fn);",
].join(" ");
globalThis.__llmHooks = { mounted: [], unmount: [] };

const listeners = new Map();
globalThis.document = {
  hidden: false,
  addEventListener: (type, fn) => listeners.set(type, fn),
  removeEventListener: (type, fn) => {
    if (listeners.get(type) === fn) listeners.delete(type);
  },
};

const errorsUrl = toDataUrl(stubImports(await readPaas("services/llm_errors.js"), {
  "@web/core/l10n/translation": translateFromGlobal,
}));
const formatUrl = toDataUrl(stubImports(await readPaas("utils/llm_format.js"), {
  "@web/core/l10n/translation": translateFromGlobal,
}));

// llm_service 與 clipboard 換成可替換的 stub（各 test 自己設行為）。
globalThis.__llmFakeService = {};
globalThis.__llmCopy = async () => true;
const modalSource = stubImports(await readPaas("components/llm/LlmKeyRevealModal.js"), {
  "@odoo/owl": owlStub,
  "@web/core/l10n/translation": translateFromGlobal,
  "../button/WoowButton": "const WoowButton = {};",
  "../icon/WoowIcon": "const WoowIcon = {};",
  "../../utils/clipboard": "const copyText = (...args) => globalThis.__llmCopy(...args);",
  "../../services/llm_service":
    "const llmService = new Proxy({}, { get: (_, k) => globalThis.__llmFakeService[k] });"
    + " const newClientRequestId = () => '0f0e0d0c-0b0a-4908-8706-050403020100';",
  "../../services/llm_errors": `const { llmErrorMessage } = await import("${errorsUrl}");`,
});
const modalUrl = toDataUrl(modalSource);
const { LlmKeyRevealModal, buildEnvVarsText, FREE_TRIAL_EXAMPLE_MODEL } = await import(modalUrl);

const overviewSource = stubImports(await readPaas("pages/llm/tabs/LlmOverviewTab.js"), {
  "@odoo/owl": owlStub,
  "@web/core/l10n/translation": translateFromGlobal,
  "../../../components/button/WoowButton": "const WoowButton = {};",
  "../../../components/card/WoowCard": "const WoowCard = {};",
  "../../../components/icon/WoowIcon": "const WoowIcon = {};",
  "../../../components/llm/LlmKeyRevealModal": "const LlmKeyRevealModal = {};",
  "../../../services/llm_service":
    "const llmService = new Proxy({}, { get: (_, k) => globalThis.__llmFakeService[k] });",
  "../../../services/llm_errors": `const { llmErrorMessage, llmSuspendReason } = await import("${errorsUrl}");`,
  "../../../utils/clipboard": "const copyText = (...args) => globalThis.__llmCopy(...args);",
  "../../../utils/llm_format": `const { formatAiPoints, formatFreeTrialUsage, formatLatency, formatTaipeiDate, formatTaipeiTime, formatTokens, isExpiringSoon } = await import("${formatUrl}");`,
});
const overview = await import(toDataUrl(overviewSource));

const KEY = "sk-" + "Zq9Xw8Vu".repeat(6);

function openModal({ create, baseUrl } = {}) {
  globalThis.__llmHooks = { mounted: [], unmount: [] };
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
  await new Promise((resolve) => setImmediate(resolve));
  return modal;
}

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
  // 輪詢途中一次失敗：有舊資料就照舊顯示，不把畫面換成「無法連線」。
  assert.equal(screen({ ...withState("provisioning"), error: "llm_network_error" }), "provisioning");
  // 開關在頁面載入後被關掉：即使有舊資料也顯示「尚未開放」。
  assert.equal(screen({ ...withState("active"), error: "llm_disabled" }), "disabled");
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

function makeTab(state) {
  globalThis.__llmHooks = { mounted: [], unmount: [] };
  const calls = { activate: [], fetchState: 0 };
  const fake = {
    overview: state,
    overviewError: null,
    overviewLoaded: true,
    keys: [],
    usage: null,
    async fetchState() {
      calls.fetchState += 1;
      return { success: true, data: fake.overview };
    },
    async activate(version) {
      calls.activate.push(version);
      return fake.activateResult;
    },
    async fetchKeys() {},
    async fetchUsageRecent() {},
    activateResult: { success: true, data: { state: "provisioning" } },
  };
  globalThis.__llmFakeService = fake;
  const tab = new overview.LlmOverviewTab();
  tab.props = {};
  tab.setup();
  // 不跑 onMounted（不開計時器）；直接測動作。
  return { tab, calls, fake };
}

test("開通：沒勾條款不送；勾了送 state 給的條款版本；llm_provisioning 也當成開通中", async () => {
  const notActivated = { account: null, tos_current_version: "draft-0", free_trial: {} };
  const { tab, calls, fake } = makeTab(notActivated);
  assert.equal(tab.screen, "not_activated");
  assert.equal(tab.tosIsDraft, true);
  await tab.activate();
  assert.deepEqual(calls.activate, []);
  tab.onTosChange({ target: { checked: true } });
  fake.activateResult = { success: false, code: "llm_provisioning" };
  fake.fetchState = async () => {
    calls.fetchState += 1;
    fake.overview = { ...notActivated, account: { state: "provisioning" } };
    return { success: true, data: fake.overview };
  };
  const originalSetTimeout = globalThis.setTimeout;
  const timers = [];
  globalThis.setTimeout = (fn, ms) => {
    timers.push(ms);
    return 0;
  };
  try {
    await tab.activate();
  } finally {
    globalThis.setTimeout = originalSetTimeout;
  }
  assert.deepEqual(calls.activate, ["draft-0"]);
  assert.equal(tab.state.actionError, null);
  assert.equal(tab.screen, "provisioning");
  assert.deepEqual(timers, [overview.PROVISION_POLL_FAST_MS]);
  assert.match(tab.provisioningText, /通常 1 分鐘內完成/);
});

test("開通失敗的訊息走碼表，不顯示後端文字", async () => {
  const { tab, fake } = makeTab({ account: null, tos_current_version: "draft-0", free_trial: {} });
  tab.onTosChange({ target: { checked: true } });
  fake.activateResult = { success: false, code: "llm_unavailable" };
  await tab.activate();
  assert.equal(tab.state.actionError, "AI 模型服務暫時無法連線，請稍後再試");
});

test("已開通：額度捨去、今日免費試用 X／200、沒有 Base URL 時顯示尚未提供、已有試用金鑰就不能再建", () => {
  const active = {
    account: { state: "active", balance_points: 0, overdraft_points: 0, suspend_reason: null },
    free_trial: { key_id: null, today_count: null, daily_limit: 200 },
    public_base_url: null,
  };
  const { tab, fake } = makeTab(active);
  assert.equal(tab.screen, "active");
  assert.equal(tab.balanceText, "0.00 點");
  assert.equal(tab.showZeroCreditBanner, true);
  assert.equal(tab.freeTrialUsageText, "—／200 次");
  fake.usage = { today_count: 1, daily_limit: 200, items: [] };
  assert.equal(tab.freeTrialUsageText, "1／200 次");
  assert.equal(tab.freeTrialUsageSentence, "今日免費試用 1／200 次");
  assert.equal(tab.baseUrl, null);
  assert.equal(tab.revealBaseUrl, undefined);
  assert.equal(tab.canCreateFreeTrialKey, true);
  tab.openFreeTrialKey();
  assert.equal(tab.state.showReveal, true);
  assert.equal(tab.canCreateFreeTrialKey, false, "對話框開著時不能再開一個");
  fake.overview = { ...active, free_trial: { ...active.free_trial, key_id: 9 } };
  tab.state.showReveal = false;
  assert.equal(tab.canCreateFreeTrialKey, false);
});

test("已暫停：顯示原因、不能建金鑰", () => {
  const { tab } = makeTab({
    account: { state: "suspended", balance_points: 5, overdraft_points: 0, suspend_reason: "policy" },
    free_trial: {},
  });
  assert.equal(tab.screen, "suspended");
  assert.equal(tab.suspendedText, "AI 模型服務已暫停：違反《AI 模型服務條款》，請聯絡客服");
  assert.equal(tab.canCreateFreeTrialKey, false);
});

// ----------------------------------------------------------------------------- 一次性金鑰對話框

test("〔複製為環境變數〕：一行一個；沒有 Base URL 時只有金鑰那行", () => {
  assert.equal(buildEnvVarsText(KEY, "https://newapi.woowtech.io/v1"),
    `OPENAI_API_KEY=${KEY}\nOPENAI_BASE_URL=https://newapi.woowtech.io/v1`);
  assert.equal(buildEnvVarsText(KEY, null), `OPENAI_API_KEY=${KEY}`);
  assert.equal(FREE_TRIAL_EXAMPLE_MODEL, "qwen3-4b-cluster1");
});

test("對話框文案（§6.3.2）", () => {
  const { modal } = openModal({ create: async () => ({ success: false, code: "llm_unavailable" }) });
  assert.equal(modal.title, "請立即複製您的 API 金鑰");
  assert.equal(modal.intro, "基於安全考量，完整金鑰只會顯示這一次。關閉視窗後無法再查看；遺失時請建立新金鑰並停用舊的。");
});

test("建立中不能關（點背景、Esc、關閉都沒反應）", async () => {
  let release;
  const { modal, closes } = openModal({
    create: () => new Promise((resolve) => { release = resolve; }),
  });
  for (const fn of globalThis.__llmHooks.mounted) fn();
  assert.equal(modal.state.phase, "creating");
  modal.requestClose();
  listeners.get("keydown")({ key: "Escape" });
  modal.onBackdropClick({ target: 1, currentTarget: 1 });
  assert.deepEqual(closes, []);
  assert.equal(modal.state.confirmClose, false);
  release({ success: false, code: "llm_unavailable" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(modal.state.phase, "error");
});

test("顯示一次：要勾「我已妥善保存此金鑰」才能完成；背景／Esc 先確認；關閉後清掉明文、回呼不帶金鑰", async () => {
  const requests = [];
  const { modal, closes } = openModal({
    baseUrl: "https://newapi-stg.woowtech.io/v1",
    create: async (id) => {
      requests.push(id);
      return { success: true, data: { key: KEY, masked_key: "sk-Zq9X…Vu8w", base_url: null } };
    },
  });
  await mount(modal);
  assert.deepEqual(requests, ["0f0e0d0c-0b0a-4908-8706-050403020100"]);
  assert.equal(modal.state.phase, "shown");
  assert.equal(modal.state.key, KEY);
  assert.equal(modal.state.baseUrl, "https://newapi-stg.woowtech.io/v1", "回應沒帶 base_url 時沿用 state 的");

  // 沒勾就按〔完成〕：不關。
  modal.finish();
  assert.deepEqual(closes, []);

  // Esc／點背景：先跳確認，不直接關。
  listeners.get("keydown")({ key: "Escape" });
  assert.equal(modal.state.confirmClose, true);
  modal.cancelClose();
  modal.onBackdropClick({ target: 1, currentTarget: 1 });
  assert.equal(modal.state.confirmClose, true);
  modal.onBackdropClick({ target: 1, currentTarget: 2 }); // 點在對話框裡面：不算
  modal.cancelClose();
  assert.deepEqual(closes, []);

  modal.onSavedChange({ target: { checked: true } });
  assert.equal(modal.canFinish, true);
  modal.finish();
  assert.deepEqual(closes, [[{ created: true }]]);
  assert.equal(modal.state.key, null);
  assert.ok(!JSON.stringify(closes).includes(KEY));

  // 卸載時移除 Esc listener。
  for (const fn of globalThis.__llmHooks.unmount) fn();
  assert.equal(listeners.has("keydown"), false);
});

test("〔仍要關閉〕也會清掉明文", async () => {
  const { modal, closes } = openModal({ create: async () => ({ success: true, data: { key: KEY } }) });
  await mount(modal);
  modal.requestClose();
  modal.close();
  assert.equal(modal.state.key, null);
  assert.deepEqual(closes, [[{ created: true }]]);
});

test("複製：成功顯示「已複製」；失敗要有提示；環境變數用 buildEnvVarsText", async () => {
  const copied = [];
  globalThis.__llmCopy = async (text) => {
    copied.push(text);
    return true;
  };
  const { modal } = openModal({
    baseUrl: "https://newapi.woowtech.io/v1",
    create: async () => ({ success: true, data: { key: KEY } }),
  });
  await mount(modal);
  await modal.copy("key");
  assert.equal(modal.state.copied, "key");
  await modal.copy("env");
  assert.equal(modal.state.copied, "env");
  assert.deepEqual(copied, [KEY, buildEnvVarsText(KEY, "https://newapi.woowtech.io/v1")]);
  globalThis.__llmCopy = async () => false;
  await modal.copy("base");
  assert.equal(modal.state.copied, null);
  assert.equal(modal.state.copyFailed, true);
  assert.equal(modal.copyFailedText, "複製失敗，請手動選取文字複製。");
  globalThis.__llmCopy = async () => true;
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
});
