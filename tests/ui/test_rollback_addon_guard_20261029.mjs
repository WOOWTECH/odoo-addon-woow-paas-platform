// #2069：回復對話框的「模組預檢」閘門。
//
// 回復到 addons.selected 少了某模組的 revision，而該模組在租戶 DB 是 installed ⇒ 程式碼被拿掉、
// DB 仍說已安裝（Odoo 載不進來，或換 image 時 upgrade-gate exit 4 卡 Init:Error）。後端
// `_rollback_service` 會擋；這裡驗前端：選好版本就呼叫 check-rollback-addons，依 code 顯示面板、
// 決定確認鈕能不能按，且 confirmRollback() 自己再驗一次閘門。
//
// 載入真實的 cloud_service.js 與 RollbackModal.js（stub 掉 owl／rpc／_t），不起瀏覽器；
// 模板無法在 node 渲染，只做靜態對照（實際畫面仍要在沙盒用瀏覽器驗）。
import assert from "node:assert/strict";
import test from "node:test";
import { loadPo, readPaas, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

const po = await loadPo();

globalThis.__gcLang = "zh_TW";
globalThis.__gcT = (message, ...args) => {
  let text = message;
  if (globalThis.__gcLang === "zh_TW") {
    const entry = po.get(message);
    if (entry && entry.msgstr && entry.comments.includes("odoo-javascript")) {
      text = entry.msgstr;
    }
  }
  let i = 0;
  return text.replace(/%s/g, () => String(args[i++]));
};
const translate = "const _t = (message, ...args) => globalThis.__gcT(message, ...args);";

// ---- 真實 cloud_service.js；jsonRpc 換成可記錄、可指定回應的 stub ----
globalThis.__gcRpcCalls = [];
globalThis.__gcRpc = async () => ({});
const serviceSource = stubImports(await readPaas("services/cloud_service.js"), {
  "@odoo/owl": "const reactive = (value) => value;",
  "@web/core/l10n/translation": translate,
  "./rpc":
    "const jsonRpc = (...args) => { globalThis.__gcRpcCalls.push(args); return globalThis.__gcRpc(...args); };",
});
const { cloudService, ROLLBACK_REQUEST_FAILED } = await import(toDataUrl(serviceSource));
globalThis.__gcCloud = cloudService;
globalThis.__gcRequestFailed = ROLLBACK_REQUEST_FAILED;

globalThis.__gcEffects = [];
globalThis.__gcDestroyHooks = [];
globalThis.__gcPanelEl = null;

// ---- 真實 RollbackModal.js；cloudService 指向上面那份（可被個別測試覆蓋） ----
const modalSource = stubImports(await readPaas("components/modal/RollbackModal.js"), {
  "@odoo/owl":
    "class Component { constructor(props) { this.props = props; } } " +
    "const useState = (value) => value; const onMounted = () => {}; " +
    // 排程／捲動用的 hook：useEffect 只登記（測試直接呼叫 _revealPanel），useRef 回傳可指定的 el。
    "const useRef = () => ({ get el() { return globalThis.__gcPanelEl || null; } }); " +
    "const useEffect = (effect, deps) => { globalThis.__gcEffects.push({ effect, deps }); }; " +
    "const onWillDestroy = (fn) => { globalThis.__gcDestroyHooks.push(fn); };",
  "@web/core/l10n/translation": translate,
  "../button/WoowButton": "const WoowButton = {};",
  "../icon/WoowIcon": "const WoowIcon = {};",
  "../../services/cloud_service":
    "const cloudService = globalThis.__gcCloudProxy; const ROLLBACK_REQUEST_FAILED = globalThis.__gcRequestFailed;",
  "../../utils/datetime_helpers": "const formatUserDateTime = (value) => value;",
});
// 代理：測試可暫時覆蓋個別方法，其餘轉給真實 cloudService。
globalThis.__gcOverrides = {};
globalThis.__gcCloudProxy = new Proxy(
  {},
  {
    get: (_t, name) => globalThis.__gcOverrides[name] || globalThis.__gcCloud[name],
  }
);
const {
  RollbackModal,
  classifyRollbackPrecheck,
  ROLLBACK_REFUSAL_CODES,
  ROLLBACK_REMOVES_INSTALLED_ADDONS,
  ROLLBACK_ADDON_CHECK_FAILED,
  ROLLBACK_ADDON_UNVERIFIED,
} = await import(toDataUrl(modalSource));

const REVISIONS = [
  { revision: 1, chart: "x-0.1.0", status: "superseded", is_current: false },
  { revision: 2, chart: "x-0.1.0", status: "superseded", is_current: false },
  { revision: 3, chart: "x-0.1.0", status: "deployed", is_current: true },
];

/** 預檢與回復的 RPC 路由：按 URL 分流，記錄每次呼叫。 */
function route({ precheck, rollback }) {
  globalThis.__gcRpcCalls = [];
  globalThis.__gcRpc = async (url, params) => {
    if (url.endsWith("/check-rollback-addons")) {
      return typeof precheck === "function" ? precheck(params) : precheck;
    }
    if (url.endsWith("/rollback")) {
      return typeof rollback === "function" ? rollback(params) : rollback || { success: true, data: {} };
    }
    throw new Error(`unexpected url ${url}`);
  };
}
const calls = (suffix) => globalThis.__gcRpcCalls.filter(([url]) => url.endsWith(suffix));

async function openModal(extra = {}) {
  const done = { rolledBack: 0 };
  const modal = new RollbackModal({
    service: { id: 7, workspace_id: 3, helm_revision: 3, ...extra },
    onClose() {},
    onRollback() { done.rolledBack += 1; },
  });
  modal.setup();
  globalThis.__gcOverrides.fetchRevisions = async () => ({ success: true, data: REVISIONS });
  await modal.loadRevisions();
  delete globalThis.__gcOverrides.fetchRevisions;
  modal.done = done;
  return modal;
}

/** 選版本並等預檢結束。 */
async function pick(modal, revision) {
  modal.selectRevision(REVISIONS.find((r) => r.revision === revision));
  // selectRevision 內部的 startPrecheck 是 fire-and-forget；讓 microtask 排空。
  await settle();
}
const settle = async () => {
  for (let i = 0; i < 10; i++) await Promise.resolve();
};

const safe = (extra = {}) => ({
  success: true, code: "safe", applicable: true, removed: ["x_mod"],
  modules: { x_mod: { state: "uninstalled", dependents: [], addon_id: 12 } }, blocking: {}, ...extra,
});
const blocked = (extra = {}) => ({
  success: true, code: "blocked", applicable: true, removed: ["x_mod"],
  modules: { x_mod: { state: "installed", dependents: ["y_mod", "z_mod"], addon_id: 12 } },
  blocking: { x_mod: "installed" }, ...extra,
});
const noRemoval = (extra = {}) => ({
  success: true, code: "no_removal", applicable: true, removed: [], modules: {}, blocking: {}, ...extra,
});
const unverified = (extra = {}) => ({
  success: false, code: "unverified", applicable: true, removed: ["x_mod"],
  error: "service is not running", modules: {}, blocking: {}, ...extra,
});
const checkFailed = (extra = {}) => ({
  success: false, code: "check_failed", applicable: true, removed: ["x_mod"],
  error: "operator unreachable", modules: {}, blocking: {}, ...extra,
});

// =============================================================================
// cloud_service
// =============================================================================

test("checkRollbackAddons posts the revision to check-rollback-addons with the long timeout", async () => {
  route({ precheck: blocked() });
  const result = await cloudService.checkRollbackAddons(7, 2);
  assert.deepEqual(result, blocked());
  assert.equal(globalThis.__gcRpcCalls.length, 1);
  const [url, params, timeout] = globalThis.__gcRpcCalls[0];
  assert.equal(url, "/api/cloud-services/services/7/check-rollback-addons");
  assert.deepEqual(params, { revision: 2 });
  assert.equal(timeout, 180000, "與 checkAddonRemoval 同樣的長 timeout（預檢要進租戶 Odoo 讀模組狀態）");
});

test("checkRollbackAddons maps a thrown RPC error to success:false with the message", async () => {
  route({ precheck: () => { throw new Error("Network error"); } });
  assert.deepEqual(await cloudService.checkRollbackAddons(7, 2), {
    success: false,
    error: "Network error",
  });
});

test("rollbackService sends only the revision unless the acknowledgement is explicitly true", async () => {
  route({ rollback: { success: true, data: {} } });
  await cloudService.rollbackService(3, 7, 2);
  await cloudService.rollbackService(3, 7, 2, {});
  await cloudService.rollbackService(3, 7, 2, { acknowledgeUnverifiedAddons: false });
  await cloudService.rollbackService(3, 7, 2, { acknowledgeUnverifiedAddons: "yes" });
  const sent = calls("/rollback");
  assert.equal(sent.length, 4);
  for (const [url, params] of sent) {
    assert.equal(url, "/api/workspaces/3/services/7/rollback");
    assert.deepEqual(params, { revision: 2 }, "沒明確 true 時連 key 都不能出現");
  }
});

test("rollbackService passes the long 180 s timeout to jsonRpc (default 30 s is shorter than the backend budget)", async () => {
  route({ rollback: { success: true, data: {} } });
  await cloudService.rollbackService(3, 7, 2);
  await cloudService.rollbackService(3, 7, 2, { acknowledgeUnverifiedAddons: true });
  const sent = calls("/rollback");
  assert.equal(sent.length, 2);
  for (const [, , timeout] of sent) {
    assert.equal(timeout, 180000);
  }
});

test("the 180 s timeout comment no longer claims the request takes at most ~150 s", async () => {
  const source = await readPaas("services/cloud_service.js");
  const at = source.indexOf("180000\n            );");
  const comment = source.slice(source.lastIndexOf("// #2069：後端回復前", at), at);
  assert.match(comment, /可以超過 180 秒/);
  assert.match(comment, /結果未知/);
  assert.doesNotMatch(comment, /最長約 150 秒/);
});

test("rollbackService forwards acknowledge_unverified_addons: true", async () => {
  route({ rollback: { success: true, data: {} } });
  await cloudService.rollbackService(3, 7, 2, { acknowledgeUnverifiedAddons: true });
  const [[url, params]] = calls("/rollback");
  assert.equal(url, "/api/workspaces/3/services/7/rollback");
  assert.deepEqual(params, { revision: 2, acknowledge_unverified_addons: true });
});

test("rollbackService still returns the refusal error", async () => {
  route({
    rollback: { success: false, error: "refused", data: { code: "rollback_removes_installed_addons" } },
  });
  assert.deepEqual(await cloudService.rollbackService(3, 7, 2), {
    success: false,
    error: "refused",
    code: "rollback_removes_installed_addons",
  });
});

test("rollbackService returns the refusal code from data.code, for every code of the contract", async () => {
  for (const code of [
    "rollback_removes_installed_addons",
    "rollback_addon_check_failed",
    "rollback_addon_unverified",
  ]) {
    route({ rollback: { success: false, error: `no: ${code}`, data: { code } } });
    const result = await cloudService.rollbackService(3, 7, 2);
    assert.equal(result.success, false);
    assert.equal(result.error, `no: ${code}`);
    assert.equal(result.code, code);
  }
});

test("rollbackService has no code when the failure carries none, and success/exception shapes are unchanged", async () => {
  for (const rollback of [
    { success: false, error: "plain" },
    { success: false, error: "plain", data: {} },
    { success: false, error: "plain", data: null },
    { success: false, error: "plain", data: { code: 42 } },
    { success: false, error: "plain", data: { code: "" } },
  ]) {
    route({ rollback });
    const result = await cloudService.rollbackService(3, 7, 2);
    assert.equal(result.code, undefined, JSON.stringify(rollback));
    assert.deepEqual({ success: result.success, error: result.error }, { success: false, error: "plain" });
  }
  route({ rollback: { success: true, data: { id: 7 } } });
  assert.deepEqual(await cloudService.rollbackService(3, 7, 2), { success: true, data: { id: 7 } });
  route({ rollback: () => { throw new Error("Network error"); } });
  assert.deepEqual(await cloudService.rollbackService(3, 7, 2), {
    success: false,
    error: "Network error",
    code: ROLLBACK_REQUEST_FAILED,
  });
});

test("rollbackService returns request_failed only when the RPC itself threw (outcome unknown)", async () => {
  assert.equal(ROLLBACK_REQUEST_FAILED, "request_failed");
  // RPC 丟例外：逾時、網路中斷、伺服器例外＝不知道後端有沒有開始回復
  for (const error of [
    Object.assign(new Error("Request timed out. Please try again."), { timedOut: true }),
    new Error("Network error. Please check your connection and try again."),
    Object.assign(new Error("Server error. Please try again later."), { httpStatus: 502 }),
  ]) {
    route({ rollback: () => { throw error; } });
    const result = await cloudService.rollbackService(3, 7, 2);
    assert.deepEqual(result, { success: false, error: error.message, code: "request_failed" });
  }
  // 後端有回答的 success:false 一律不帶這個 code（含連不上 operator 的那一句）
  for (const rollback of [
    { success: false, error: "Unable to connect to deployment service" },
    { success: false, error: "Service not found" },
    { success: false, error: "no", data: { code: "rollback_addon_unverified" } },
  ]) {
    route({ rollback });
    const result = await cloudService.rollbackService(3, 7, 2);
    assert.notEqual(result.code, "request_failed", rollback.error);
    assert.equal(result.success, false);
  }
  // 成功不受影響
  route({ rollback: { success: true, data: { id: 7 } } });
  assert.deepEqual(await cloudService.rollbackService(3, 7, 2), { success: true, data: { id: 7 } });
});

// =============================================================================
// 契約的每個 code：顯示什麼、確認鈕能不能按
// =============================================================================

test("selecting a revision starts a precheck: button disabled and 'Checking modules...' shown while loading", async () => {
  let release;
  route({ precheck: () => new Promise((resolve) => { release = () => resolve(noRemoval()); }) });
  const modal = await openModal();
  assert.equal(modal.precheckPanel, null, "還沒選版本：沒有面板");
  modal.selectRevision(REVISIONS[0]);
  await settle();
  assert.deepEqual(calls("/check-rollback-addons").map(([, p]) => p), [{ revision: 1 }]);
  assert.equal(modal.state.precheck.status, "loading");
  assert.equal(modal.canRollback, false);
  assert.equal(modal.precheckPanel.kind, "loading");
  assert.equal(modal.precheckPanel.title, "正在檢查模組...");
  // 預檢中即使硬按（含已在確認步驟）也不會送出
  await modal.onRollback();
  assert.equal(modal.state.confirming, false);
  release();
  await settle();
  assert.equal(modal.canRollback, true);
});

test("no_removal: nothing extra is shown and the button is enabled (today's behaviour)", async () => {
  route({ precheck: noRemoval() });
  const modal = await openModal();
  await pick(modal, 1);
  assert.equal(modal.precheckGate.kind, "none");
  assert.equal(modal.precheckPanel, null);
  assert.equal(modal.canRollback, true);
});

test("no_removal with applicable false (not a live woow-odoo tenant) behaves the same", async () => {
  route({ precheck: noRemoval({ applicable: false }) });
  const modal = await openModal();
  await pick(modal, 1);
  assert.equal(modal.precheckPanel, null);
  assert.equal(modal.canRollback, true);
});

test("safe: neutral note lists the removed module names, button enabled", async () => {
  route({ precheck: safe({ removed: ["a_mod", "b_mod"] }) });
  const modal = await openModal();
  await pick(modal, 1);
  const panel = modal.precheckPanel;
  assert.equal(panel.kind, "safe");
  assert.equal(panel.role, "status");
  assert.deepEqual(panel.items.map((i) => i.name), ["a_mod", "b_mod"]);
  assert.equal(panel.title, "回復後，以下模組將不再提供：");
  assert.equal(panel.hint, "這些模組沒有安裝在 Odoo，不影響你的資料。");
  assert.equal(modal.canRollback, true);
});

test("safe without a removed list falls back to the assessed module names", async () => {
  route({ precheck: safe({ removed: undefined }) });
  const modal = await openModal();
  await pick(modal, 1);
  assert.deepEqual(modal.precheckPanel.items.map((i) => i.name), ["x_mod"]);
});

test("blocked: error panel with module, state and dependents; no way to proceed", async () => {
  route({ precheck: blocked() });
  const modal = await openModal();
  await pick(modal, 1);
  const panel = modal.precheckPanel;
  assert.equal(panel.kind, "blocked");
  assert.equal(panel.role, "alert");
  assert.equal(panel.title, "無法回復：仍有模組處於安裝狀態");
  assert.equal(panel.intro, "這次回復會移除仍安裝在 Odoo 中的模組：");
  assert.deepEqual(panel.items, [
    { name: "x_mod", state: "installed", detail: "（被以下模組依賴：y_mod, z_mod）" },
  ]);
  assert.match(panel.hint, /先到 Odoo 將它們解除安裝/);
  assert.equal(modal.canRollback, false);
  assert.equal(modal.precheckAllows, false);
  assert.equal(panel.ackLabel, undefined, "blocked 沒有「我了解」");
  assert.equal(panel.retry, undefined);
  // 勾選旗標殘留也不能放行 blocked
  modal.state.acknowledged = true;
  assert.equal(modal.canRollback, false);
  await modal.onRollback();
  assert.equal(modal.state.confirming, false);
});

test("blocked lists only the blocking modules and omits the dependents note when there are none", async () => {
  route({
    precheck: blocked({
      removed: ["a_mod", "b_mod"],
      modules: {
        a_mod: { state: "uninstalled", dependents: [], addon_id: 1 },
        b_mod: { state: "to upgrade", dependents: [], addon_id: 2 },
      },
      blocking: { b_mod: "to upgrade" },
    }),
  });
  const modal = await openModal();
  await pick(modal, 1);
  assert.deepEqual(modal.precheckPanel.items, [{ name: "b_mod", state: "to upgrade", detail: "" }]);
  assert.equal(modal.canRollback, false);
});

test("blocked panel in English source text", async () => {
  globalThis.__gcLang = "en";
  try {
    route({ precheck: blocked() });
    const modal = await openModal();
    await pick(modal, 1);
    assert.equal(modal.precheckPanel.items[0].detail, "(required by: y_mod, z_mod)");
    assert.match(modal.precheckPanel.hint, /Uninstall them in Odoo first \(Apps → Uninstall\)/);
  } finally {
    globalThis.__gcLang = "zh_TW";
  }
});

test("unverified: warning panel, checkbox gate, button only after ticking", async () => {
  route({ precheck: unverified() });
  const modal = await openModal();
  await pick(modal, 1);
  const panel = modal.precheckPanel;
  assert.equal(panel.kind, "unverified");
  assert.equal(panel.role, "alert");
  assert.deepEqual(panel.items.map((i) => i.name), ["x_mod"]);
  assert.equal(panel.ackLabel, "我了解");
  assert.match(panel.intro, /服務目前不在執行中/);
  assert.match(panel.hint, /資料不會被刪除/);
  assert.equal(modal.state.acknowledged, false);
  assert.equal(modal.canRollback, false);
  modal.state.acknowledged = true;
  assert.equal(modal.canRollback, true);
  modal.state.acknowledged = false;
  assert.equal(modal.canRollback, false);
});

test("unverified without removed names still shows the panel (no list)", async () => {
  route({ precheck: unverified({ removed: undefined }) });
  const modal = await openModal();
  await pick(modal, 1);
  assert.equal(modal.precheckPanel.kind, "unverified");
  assert.deepEqual(modal.precheckPanel.items, []);
  assert.equal(modal.canRollback, false);
});

test("check_failed: error panel with the backend reason, Retry check, button disabled, no override", async () => {
  route({ precheck: checkFailed() });
  const modal = await openModal();
  await pick(modal, 1);
  const panel = modal.precheckPanel;
  assert.equal(panel.kind, "failed");
  assert.equal(panel.role, "alert");
  assert.equal(panel.intro, "operator unreachable");
  assert.equal(panel.retry, true);
  assert.equal(panel.ackLabel, undefined, "check_failed 沒有任何放行方式");
  assert.equal(modal.canRollback, false);
  modal.state.acknowledged = true;
  assert.equal(modal.canRollback, false);
});

test("check_failed without a reason uses the generic fallback text", async () => {
  route({ precheck: checkFailed({ error: "" }) });
  const modal = await openModal();
  await pick(modal, 1);
  assert.equal(modal.precheckPanel.intro, "模組檢查失敗，請再試一次。");
  assert.equal(modal.canRollback, false);
});

test("success:false without a code is treated as a failed check (fail-closed)", async () => {
  route({ precheck: { success: false, error: "boom" } });
  const modal = await openModal();
  await pick(modal, 1);
  assert.equal(modal.precheckPanel.kind, "failed");
  assert.equal(modal.precheckPanel.intro, "boom");
  assert.equal(modal.canRollback, false);
});

test("RPC exception: failed panel with the error text, button disabled", async () => {
  route({ precheck: () => { throw new Error("Request timed out"); } });
  const modal = await openModal();
  await pick(modal, 1);
  assert.equal(modal.precheckPanel.kind, "failed");
  assert.equal(modal.precheckPanel.intro, "Request timed out");
  assert.equal(modal.canRollback, false);
});

test("a precheck that rejects past the service wrapper is also fail-closed", async () => {
  route({ precheck: noRemoval() });
  globalThis.__gcOverrides.checkRollbackAddons = async () => { throw new Error("kaput"); };
  try {
    const modal = await openModal();
    await pick(modal, 1);
    assert.equal(modal.precheckPanel.kind, "failed");
    assert.equal(modal.precheckPanel.intro, "kaput");
    assert.equal(modal.canRollback, false);
  } finally {
    delete globalThis.__gcOverrides.checkRollbackAddons;
  }
});

test("unreadable or contradictory responses never open the gate", () => {
  for (const bad of [
    null, undefined, "ok", 42, {}, { success: true },
    { success: true, code: "something_new", applicable: true },
  ]) {
    assert.equal(classifyRollbackPrecheck(bad).kind, "failed", JSON.stringify(bad));
  }
  // 成功卻帶 blocking、或 code=blocked 但 blocking 空：一律當 blocked
  assert.equal(
    classifyRollbackPrecheck(noRemoval({ blocking: { x_mod: "installed" } })).kind, "blocked");
  assert.equal(
    classifyRollbackPrecheck(safe({ blocking: { x_mod: "installed" } })).kind, "blocked");
  const emptyBlocked = classifyRollbackPrecheck(blocked({ blocking: {} }));
  assert.equal(emptyBlocked.kind, "blocked");
  assert.deepEqual(emptyBlocked.names, ["x_mod"]);
  // success:false 的 unverified 以外的 code 都是 failed
  assert.equal(classifyRollbackPrecheck({ success: false, code: "safe" }).kind, "failed");
  assert.equal(classifyRollbackPrecheck({ success: false, code: "blocked" }).kind, "failed");
});

// =============================================================================
// 時序：過期回應、重試
// =============================================================================

test("a response that arrives after the selection changed is ignored", async () => {
  const pending = {};
  route({
    precheck: ({ revision }) => new Promise((resolve) => { pending[revision] = resolve; }),
  });
  const modal = await openModal();
  modal.selectRevision(REVISIONS[0]); // rev 1：在飛
  modal.selectRevision(REVISIONS[1]); // rev 2：排隊（同時只有一個請求在飛）
  await settle();
  assert.equal(modal.state.selectedRevision, 2);
  assert.deepEqual(calls("/check-rollback-addons").map(([, p]) => p.revision), [1]);
  // rev 1（blocked）先回：選的已是 rev 2，不能顯示，也不能放行或擋住 rev 2
  pending[1](blocked());
  await settle();
  assert.equal(modal.state.precheck.revision, 2);
  assert.equal(modal.precheckGate.kind, "loading", "rev 2 還沒結果：fail-closed");
  assert.equal(modal.canRollback, false);
  // rev 1 的結果一退場就送 rev 2
  assert.deepEqual(calls("/check-rollback-addons").map(([, p]) => p.revision), [1, 2]);
  pending[2](noRemoval());
  await settle();
  assert.equal(modal.precheckGate.kind, "none");
  assert.equal(modal.canRollback, true);
});

test("an earlier response for the previously selected revision cannot unlock the new selection", async () => {
  const pending = {};
  route({
    precheck: ({ revision }) => new Promise((resolve) => { pending[revision] = resolve; }),
  });
  const modal = await openModal();
  modal.selectRevision(REVISIONS[0]);
  modal.selectRevision(REVISIONS[1]);
  await settle();
  pending[1](noRemoval()); // rev 1 回來得早：安全，但現在選的是 rev 2
  await settle();
  assert.equal(modal.precheckGate.kind, "loading", "rev 2 還沒結果：仍視為預檢中");
  assert.equal(modal.canRollback, false);
  pending[2](blocked());
  await settle();
  assert.equal(modal.precheckGate.kind, "blocked");
  assert.equal(modal.canRollback, false);
});

test("switching to another revision and back reuses the settled result (no second request)", async () => {
  route({ precheck: ({ revision }) => (revision === 1 ? blocked() : noRemoval()) });
  const modal = await openModal();
  await pick(modal, 1);
  assert.equal(modal.canRollback, false);
  await pick(modal, 2);
  assert.equal(modal.canRollback, true);
  await pick(modal, 1);
  assert.equal(modal.canRollback, false, "快取的 blocked 照樣擋");
  assert.equal(modal.precheckGate.kind, "blocked");
  assert.equal(modal.state.precheck.status, "ready");
  assert.deepEqual(calls("/check-rollback-addons").map(([, p]) => p.revision), [1, 2]);
});

test("re-clicking the already selected revision does not restart the precheck", async () => {
  route({ precheck: noRemoval() });
  const modal = await openModal();
  await pick(modal, 1);
  await pick(modal, 1);
  assert.equal(calls("/check-rollback-addons").length, 1);
});

test("Retry check re-runs the precheck for the selected revision and can recover", async () => {
  let attempt = 0;
  route({ precheck: () => (++attempt === 1 ? checkFailed() : safe()) });
  const modal = await openModal();
  await pick(modal, 1);
  assert.equal(modal.precheckPanel.kind, "failed");
  assert.equal(modal.canRollback, false);
  const retrying = modal.retryPrecheck();
  assert.equal(modal.precheckPanel.kind, "loading");
  assert.equal(modal.canRollback, false);
  await retrying;
  assert.equal(modal.precheckPanel.kind, "safe");
  assert.equal(modal.canRollback, true);
  assert.deepEqual(calls("/check-rollback-addons").map(([, p]) => p.revision), [1, 1]);
});

test("a late answer from before Retry cannot overwrite the retry's answer", async () => {
  const pending = [];
  route({ precheck: () => new Promise((resolve) => pending.push(resolve)) });
  const modal = await openModal();
  modal.selectRevision(REVISIONS[0]);
  await settle();
  modal.retryPrecheck(); // 第一個請求還在飛：重試排隊，不會同時送第二個
  await settle();
  assert.equal(pending.length, 1);
  pending[0](noRemoval()); // 重試之前的舊回應：不能放行（重試的結果還沒回來）
  await settle();
  assert.equal(modal.precheckGate.kind, "loading");
  assert.equal(modal.canRollback, false);
  assert.equal(pending.length, 2, "舊回應一退場就送重試");
  pending[1](blocked()); // 重試的回應
  await settle();
  assert.equal(modal.precheckGate.kind, "blocked");
  assert.equal(modal.canRollback, false);
});

test("Retry check does nothing without a selection", () => {
  route({ precheck: noRemoval() });
  const modal = new RollbackModal({ service: { id: 7, workspace_id: 3 }, onClose() {}, onRollback() {} });
  modal.setup();
  assert.equal(modal.retryPrecheck(), undefined);
  assert.equal(calls("/check-rollback-addons").length, 0);
});

// =============================================================================
// unverified 的勾選流程與旗標
// =============================================================================

async function runRollbackThrough(modal) {
  await modal.onRollback(); // 第一下：進確認步驟
  assert.equal(modal.state.confirming, true);
  await modal.onRollback(); // 第二下：真的送出
}

test("unverified: the rollback request carries acknowledge_unverified_addons only after ticking", async () => {
  route({ precheck: unverified(), rollback: { success: true, data: {} } });
  const modal = await openModal();
  await pick(modal, 1);
  await modal.onRollback();
  assert.equal(modal.state.confirming, false, "沒勾選：連確認步驟都進不去");
  modal.state.acknowledged = true;
  await runRollbackThrough(modal);
  const [[, params]] = calls("/rollback");
  assert.deepEqual(params, { revision: 1, acknowledge_unverified_addons: true });
  assert.equal(modal.done.rolledBack, 1);
});

test("the flag is never sent for no_removal / safe, even with a stale tick", async () => {
  for (const precheck of [noRemoval(), safe(), noRemoval({ applicable: false })]) {
    route({ precheck, rollback: { success: true, data: {} } });
    const modal = await openModal();
    await pick(modal, 1);
    modal.state.acknowledged = true; // 殘留的勾選不能外溢
    await runRollbackThrough(modal);
    const [[, params]] = calls("/rollback");
    assert.deepEqual(params, { revision: 1 }, precheck.code);
  }
});

test("changing the selection clears the acknowledgement", async () => {
  route({ precheck: ({ revision }) => (revision === 1 ? unverified() : unverified({ removed: ["other"] })) });
  const modal = await openModal();
  await pick(modal, 1);
  modal.state.acknowledged = true;
  assert.equal(modal.canRollback, true);
  await pick(modal, 2);
  assert.equal(modal.state.acknowledged, false);
  assert.equal(modal.canRollback, false);
  await pick(modal, 1);
  assert.equal(modal.state.acknowledged, false, "切回來也要重新勾選");
});

test("Retry check clears the acknowledgement as well", async () => {
  route({ precheck: unverified() });
  const modal = await openModal();
  await pick(modal, 1);
  modal.state.acknowledged = true;
  await modal.retryPrecheck();
  assert.equal(modal.state.acknowledged, false);
  assert.equal(modal.canRollback, false);
});

test("the backend refusal after confirming is shown in the error banner and the modal stays open", async () => {
  route({
    precheck: noRemoval(),
    rollback: { success: false, error: "refused", data: { code: "rollback_removes_installed_addons" } },
  });
  const modal = await openModal();
  await pick(modal, 1);
  await runRollbackThrough(modal);
  assert.equal(modal.state.error, "refused");
  assert.equal(modal.state.rolling, false);
  assert.equal(modal.done.rolledBack, 0);
});

// =============================================================================
// confirmRollback() 自己驗閘門
// =============================================================================

test("confirmRollback refuses when the gate is closed (not only the disabled attribute)", async () => {
  const cases = {
    blocked: blocked(),
    check_failed: checkFailed(),
    unverified_unticked: unverified(),
    no_code: { success: false, error: "x" },
  };
  for (const [name, precheck] of Object.entries(cases)) {
    route({ precheck, rollback: { success: true, data: {} } });
    const modal = await openModal();
    await pick(modal, 1);
    modal.state.confirming = true; // 假設畫面已在確認步驟
    await modal.confirmRollback();
    assert.equal(calls("/rollback").length, 0, name);
    assert.equal(modal.state.rolling, false, name);
    assert.equal(modal.done.rolledBack, 0, name);
  }
});

test("confirmRollback refuses while the precheck is still running or never ran", async () => {
  route({ precheck: () => new Promise(() => {}), rollback: { success: true, data: {} } });
  const modal = await openModal();
  modal.state.selectedRevision = 1; // 沒經過 selectRevision：沒有預檢結果
  modal.state.confirming = true;
  await modal.confirmRollback();
  modal.selectRevision(REVISIONS[1]); // 預檢進行中
  modal.state.confirming = true;
  await modal.confirmRollback();
  assert.equal(calls("/rollback").length, 0);
});

test("confirmRollback sends when the gate is open", async () => {
  route({ precheck: safe(), rollback: { success: true, data: {} } });
  const modal = await openModal();
  await pick(modal, 2);
  modal.state.confirming = true;
  await modal.confirmRollback();
  assert.deepEqual(calls("/rollback").map(([, p]) => p), [{ revision: 2 }]);
  assert.equal(modal.done.rolledBack, 1);
});

// =============================================================================
// 既有行為不變
// =============================================================================

test("the current revision and chart-floor revisions cannot be selected and start no precheck", async () => {
  route({ precheck: noRemoval() });
  const modal = await openModal();
  modal.state.revisions = [
    ...REVISIONS.slice(0, 2),
    { ...REVISIONS[2] },
    { revision: 0, chart: "old", status: "superseded", is_current: false, rollback_allowed: false },
  ];
  modal.selectRevision(modal.state.revisions[2]); // 目前
  modal.selectRevision(modal.state.revisions[3]); // chart 下限擋住
  await settle();
  assert.equal(modal.state.selectedRevision, null);
  assert.equal(calls("/check-rollback-addons").length, 0);
  assert.equal(modal.canRollback, false);
  assert.equal(modal.precheckPanel, null);
});

test("selection is ignored while rolling (no precheck either)", async () => {
  route({ precheck: noRemoval() });
  const modal = await openModal();
  modal.state.rolling = true;
  modal.selectRevision(REVISIONS[0]);
  await settle();
  assert.equal(modal.state.selectedRevision, null);
  assert.equal(calls("/check-rollback-addons").length, 0);
});

test("the panel is shared by both steps: confirming does not change it", async () => {
  route({ precheck: unverified() });
  const modal = await openModal();
  await pick(modal, 1);
  modal.state.acknowledged = true;
  const before = JSON.stringify(modal.precheckPanel);
  await modal.onRollback();
  assert.equal(modal.state.confirming, true);
  assert.equal(JSON.stringify(modal.precheckPanel), before);
  assert.equal(modal.canRollback, true);
  // 回上一步再取消勾選：閘門重新關上
  modal.onBack();
  modal.state.acknowledged = false;
  assert.equal(modal.canRollback, false);
});

test("no panel while the revision list itself is loading or empty", async () => {
  const modal = new RollbackModal({ service: { id: 7, workspace_id: 3 }, onClose() {}, onRollback() {} });
  modal.setup();
  modal.state.selectedRevision = 1;
  assert.equal(modal.precheckPanel, null, "載入中");
  modal.state.loading = false;
  assert.equal(modal.precheckPanel, null, "沒有 revision");
});

// =============================================================================
// 模板（靜態對照；node 無法渲染 OWL 模板）
// =============================================================================

const xml = await readPaas("components/modal/RollbackModal.xml");
const modalJs = await readPaas("components/modal/RollbackModal.js");

test("template: one precheck block outside the step branches, alert role bound from the model", () => {
  const blocks = xml.match(/t-if="precheckPanel"/g) || [];
  assert.equal(blocks.length, 1);
  const at = xml.indexOf('t-if="precheckPanel"');
  assert.ok(at > xml.indexOf("<!-- Revisions List -->"), "放在步驟分支之後 ⇒ 兩個步驟都會顯示");
  assert.ok(at < xml.indexOf('class="o_woow_modal_footer"'), "仍在 modal body 內");
  assert.match(xml, /t-att-role="precheckPanel\.role"/);
  assert.match(modalJs, /role: "alert"/);
});

test("template: the acknowledgement checkbox is inside a label, shows state.acknowledged and reports through onAcknowledge with its token", () => {
  assert.match(
    xml,
    /<label t-if="precheckPanel\.kind === 'unverified'"[^>]*>\s*<input\s[^>]*type="checkbox"[^>]*t-att-checked="state\.acknowledged"[^>]*t-att-data-token="state\.precheck\.seq"[^>]*t-on-change="onAcknowledge"[^>]*\/>\s*<span t-esc="precheckPanel\.ackLabel"\/>\s*<\/label>/
  );
  assert.doesNotMatch(xml, /t-model="state\.acknowledged"/, "t-model 會讓舊面板的勾選直接寫進 state");
});

test("template: Retry check button only for failed checks and wired to retryPrecheck", () => {
  assert.match(xml, /t-if="precheckPanel\.retry"[\s\S]*?onClick\.bind="retryPrecheck"[\s\S]*?Retry check/);
});

test("template: confirm button stays bound to canRollback", () => {
  assert.match(xml, /onClick\.bind="onRollback"\s+ariaDescribedby="confirmDescribedBy"\s+disabled="!canRollback"/);
});

test("every member the new template block uses exists on the component", () => {
  const proto = Object.getOwnPropertyNames(RollbackModal.prototype);
  for (const member of ["precheckPanel", "retryPrecheck", "canRollback", "onRollback"]) {
    assert.ok(proto.includes(member), member);
  }
  const panelKeys = new Set();
  for (const kind of [safe(), blocked(), unverified(), checkFailed(), { success: false }]) {
    const modal = new RollbackModal({ service: { id: 7, workspace_id: 3 }, onClose() {}, onRollback() {} });
    modal.setup();
    modal.state.loading = false;
    modal.state.revisions = REVISIONS;
    modal.state.selectedRevision = 1;
    modal.state.precheck = { revision: 1, status: "ready", result: kind };
    Object.keys(modal.precheckPanel).forEach((k) => panelKeys.add(k));
  }
  for (const key of ["kind", "role", "icon", "title", "items", "intro", "hint", "ackLabel", "retry"]) {
    assert.ok(panelKeys.has(key), `precheckPanel.${key} 要有至少一種面板會提供`);
  }
});

test("no hard-coded CJK in the template or in string literals of the component", () => {
  const xmlNoComments = xml.replace(/<!--[\s\S]*?-->/g, "");
  assert.doesNotMatch(xmlNoComments, /[㐀-鿿]/);
  const jsLiterals = modalJs.split("\n").filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line));
  for (const line of jsLiterals) {
    const code = line.replace(/\/\/.*$/, "");
    assert.doesNotMatch(code, /[㐀-鿿]/, line);
  }
});

// =============================================================================
// footer 常駐的原因行＋面板捲進可視範圍
// =============================================================================

test("the footer reason line mirrors the gate: loading / blocked / unverified-unticked / failed", async () => {
  let release;
  route({ precheck: () => new Promise((resolve) => { release = resolve; }) });
  const modal = await openModal();
  assert.equal(modal.precheckReason, "", "沒選版本：沒有原因行");
  modal.selectRevision(REVISIONS[0]);
  await settle();
  assert.equal(modal.precheckReason, "正在檢查模組...");
  release(blocked());
  await settle();
  assert.equal(modal.precheckReason, "無法回復：仍有模組處於安裝狀態");
  assert.equal(modal.precheckReason, modal.precheckPanel.title, "與面板同一份模型");
});

test("unverified shows the tick sentence until ticked, then nothing", async () => {
  route({ precheck: unverified() });
  const modal = await openModal();
  await pick(modal, 1);
  assert.equal(modal.precheckReason, "請勾選「我了解」才能繼續。");
  modal.state.acknowledged = true;
  assert.equal(modal.canRollback, true);
  assert.equal(modal.precheckReason, "");
  modal.state.acknowledged = false;
  assert.equal(modal.precheckReason, "請勾選「我了解」才能繼續。");
});

test("failed check shows the failed title; no_removal / safe show nothing", async () => {
  route({ precheck: checkFailed() });
  let modal = await openModal();
  await pick(modal, 1);
  assert.equal(modal.precheckReason, "無法檢查模組");
  for (const precheck of [noRemoval(), safe()]) {
    route({ precheck });
    modal = await openModal();
    await pick(modal, 1);
    assert.equal(modal.canRollback, true);
    assert.equal(modal.precheckReason, "", precheck.code);
  }
});

test("the reason line is empty whenever the button is enabled, including while rolling", async () => {
  route({ precheck: safe() });
  const modal = await openModal();
  await pick(modal, 1);
  modal.state.rolling = true;
  assert.equal(modal.canRollback, false, "回復中按鈕 disabled，但原因不是預檢");
  assert.equal(modal.precheckReason, "");
});

test("the English source text of the tick sentence", async () => {
  globalThis.__gcLang = "en";
  try {
    route({ precheck: unverified() });
    const modal = await openModal();
    await pick(modal, 1);
    assert.equal(modal.precheckReason, 'Tick "I understand" to continue.');
  } finally {
    globalThis.__gcLang = "zh_TW";
  }
});

test("template puts the reason line in the footer (outside the scrolling body), before the buttons", () => {
  const footerAt = xml.indexOf('class="o_woow_modal_footer"');
  const reasonAt = xml.indexOf('t-if="precheckReason"');
  assert.ok(reasonAt > footerAt, "原因行在 footer 內，不在會捲動的 body");
  assert.ok(reasonAt < xml.indexOf("Cancel"), "在按鈕之前（窄畫面換到按鈕上一行）");
  assert.equal((xml.match(/t-if="precheckReason"/g) || []).length, 1);
  assert.match(xml, /o_woow_rollback_reason_' \+ precheckPanel\.kind/);
  // 面板仍在清單下方（body 的最後一個區塊）
  assert.ok(xml.indexOf('t-if="precheckPanel"') > xml.indexOf("<!-- Revisions List -->"));
});

test("panel kind changes scroll it into view (block: nearest); effect depends on the kind", async () => {
  route({ precheck: blocked() });
  const modal = await openModal();
  const { effect, deps } = globalThis.__gcEffects.at(-1);
  assert.deepEqual(deps(), [null], "沒有面板");
  const scrolled = [];
  globalThis.__gcPanelEl = { scrollIntoView: (arg) => scrolled.push(arg) };
  try {
    effect();
    assert.deepEqual(scrolled, [{ block: "nearest" }]);
    modal.selectRevision(REVISIONS[0]);
    assert.deepEqual(deps(), ["loading"]);
    await settle();
    assert.deepEqual(deps(), ["blocked"]);
  } finally {
    globalThis.__gcPanelEl = null;
  }
});

test("scrolling is best effort: no element, no scrollIntoView (jsdom) or a throwing one is harmless", async () => {
  route({ precheck: noRemoval() });
  const modal = await openModal();
  globalThis.__gcPanelEl = null;
  modal._revealPanel();
  globalThis.__gcPanelEl = {};
  modal._revealPanel();
  globalThis.__gcPanelEl = { scrollIntoView() { throw new Error("nope"); } };
  try {
    modal._revealPanel();
  } finally {
    globalThis.__gcPanelEl = null;
  }
});

// =============================================================================
// 後端在動作當下拒絕 ⇒ 回選版本步驟、清勾選、強制重新預檢
// =============================================================================

test("the refusal codes are exported and match the backend contract", () => {
  assert.equal(ROLLBACK_REMOVES_INSTALLED_ADDONS, "rollback_removes_installed_addons");
  assert.equal(ROLLBACK_ADDON_CHECK_FAILED, "rollback_addon_check_failed");
  assert.equal(ROLLBACK_ADDON_UNVERIFIED, "rollback_addon_unverified");
  assert.deepEqual(
    [...ROLLBACK_REFUSAL_CODES].sort(),
    [ROLLBACK_ADDON_CHECK_FAILED, ROLLBACK_ADDON_UNVERIFIED, ROLLBACK_REMOVES_INSTALLED_ADDONS].sort()
  );
});

test("a precheck-staleness refusal returns to the selection step, clears ack and rolling, shows the banner and re-prechecks (cache bypassed)", async () => {
  for (const code of ROLLBACK_REFUSAL_CODES) {
    let checks = 0;
    route({
      precheck: () => (++checks === 1 ? safe() : unverified()),
      rollback: { success: false, error: `refused ${code}`, data: { code } },
    });
    const modal = await openModal();
    await pick(modal, 1);
    assert.equal(modal.precheckGate.kind, "safe");
    await runRollbackThrough(modal);
    // 回選版本步驟、錯誤橫幅、勾選與 rolling 清掉
    assert.equal(modal.state.confirming, false, code);
    assert.equal(modal.state.acknowledged, false, code);
    assert.equal(modal.state.rolling, false, code);
    assert.equal(modal.state.error, `refused ${code}`, code);
    // 強制重新預檢：即使 rev 1 已在 cache（safe），仍再送一次
    await settle();
    assert.deepEqual(calls("/check-rollback-addons").map(([, p]) => p.revision), [1, 1], code);
    assert.equal(modal.state.precheck.status, "ready", code);
    // 新結果取代過期的綠色面板：現在要勾「我了解」才能繼續
    assert.equal(modal.precheckGate.kind, "unverified", code);
    assert.equal(modal.canRollback, false, code);
    assert.equal(modal.state.error, `refused ${code}`, "預檢重跑後橫幅仍在");
  }
});

test("a forced re-precheck that fails does not leave the old cached verdict behind", async () => {
  let checks = 0;
  route({
    precheck: () => (++checks === 1 ? safe() : checks === 2 ? checkFailed() : unverified()),
    rollback: { success: false, error: "refused", data: { code: ROLLBACK_REMOVES_INSTALLED_ADDONS } },
  });
  const modal = await openModal();
  await pick(modal, 1);
  await runRollbackThrough(modal);
  await settle();
  assert.equal(modal.precheckGate.kind, "failed", "強制重檢失敗：fail-closed");
  await pick(modal, 2);
  await pick(modal, 1);
  assert.notEqual(modal.precheckGate.kind, "safe", "不能回到快取裡過期的 safe");
  assert.equal(modal.precheckGate.kind, "unverified");
  assert.deepEqual(calls("/check-rollback-addons").map(([, p]) => p.revision), [1, 1, 2, 1]);
});

test("while the forced re-precheck runs the stale verdict is gone and the button stays closed", async () => {
  let checks = 0;
  let release;
  route({
    precheck: () => (++checks === 1 ? safe() : new Promise((resolve) => { release = resolve; })),
    rollback: { success: false, error: "refused", data: { code: ROLLBACK_REMOVES_INSTALLED_ADDONS } },
  });
  const modal = await openModal();
  await pick(modal, 1);
  await runRollbackThrough(modal);
  await settle();
  assert.equal(modal.precheckGate.kind, "loading");
  assert.equal(modal.canRollback, false);
  assert.equal(modal.precheckPanel.kind, "loading");
  release(blocked());
  await settle();
  assert.equal(modal.precheckGate.kind, "blocked");
  assert.equal(modal.canRollback, false);
});

test("a refusal without a staleness code (or an unknown code) keeps today's behaviour", async () => {
  for (const rollback of [
    { success: false, error: "boom" },
    { success: false, error: "boom", data: { code: "something_else" } },
  ]) {
    route({ precheck: safe(), rollback });
    const modal = await openModal();
    await pick(modal, 1);
    await runRollbackThrough(modal);
    await settle();
    assert.equal(modal.state.error, "boom");
    assert.equal(modal.state.confirming, true, "留在確認步驟");
    assert.equal(modal.state.rolling, false);
    assert.equal(calls("/check-rollback-addons").length, 1, "不重新預檢");
    assert.equal(modal.precheckGate.kind, "safe");
  }
});

test("after a refusal the unverified acknowledge box is offered even for the same revision", async () => {
  // 預檢當時服務在跑（safe）、按下確認時服務已停 ⇒ 後端 rollback_addon_unverified
  let checks = 0;
  route({
    precheck: () => (++checks === 1 ? safe() : unverified()),
    rollback: (params) =>
      params.acknowledge_unverified_addons
        ? { success: true, data: {} }
        : { success: false, error: "not running", data: { code: ROLLBACK_ADDON_UNVERIFIED } },
  });
  const modal = await openModal();
  await pick(modal, 1);
  await runRollbackThrough(modal);
  await settle();
  assert.equal(modal.precheckPanel.kind, "unverified");
  assert.equal(modal.precheckPanel.ackLabel, "我了解");
  modal.state.acknowledged = true;
  await runRollbackThrough(modal);
  assert.deepEqual(calls("/rollback").map(([, p]) => p), [
    { revision: 1 },
    { revision: 1, acknowledge_unverified_addons: true },
  ]);
  assert.equal(modal.done.rolledBack, 1);
});

// =============================================================================
// 面板節點隨預檢替換；勾選框 aria-describedby
// =============================================================================

test("the panel container is keyed by its kind (no revision/sequence: OWL throws when a node holding child components is swapped within one frame)", () => {
  assert.match(xml, /<div\s+t-if="precheckPanel"\s+t-key="precheckPanel\.kind"/);
  assert.match(xml, /t-ref="precheckPanel"/);
});

test("checkbox is described by unique per-instance ids of the panel title and hint", async () => {
  route({ precheck: unverified() });
  const a = await openModal();
  const b = await openModal();
  assert.notEqual(a.precheckTitleId, b.precheckTitleId);
  assert.notEqual(a.precheckHintId, b.precheckHintId);
  assert.notEqual(a.precheckTitleId, a.precheckHintId);
  assert.match(a.precheckTitleId, /^o_woow_rollback_precheck_title_\d+$/);
  assert.match(a.precheckHintId, /^o_woow_rollback_precheck_hint_\d+$/);
  assert.match(xml, /<strong t-att-id="precheckTitleId"/);
  assert.match(xml, /<p t-if="precheckPanel\.hint" t-att-id="precheckHintId"/);
  assert.match(
    xml,
    /<input[^>]*type="checkbox"[^>]*t-attf-aria-describedby="\{\{ precheckTitleId \}\} \{\{ precheckHintId \}\}"/
  );
  // 可見標籤仍是短的「我了解」
  await pick(a, 1);
  assert.equal(a.precheckPanel.ackLabel, "我了解");
});

// =============================================================================
// 契約之外的矛盾組合一律 failed
// =============================================================================

test("only no_removal is none; applicable:false is acceptable only with no_removal and no removed names", () => {
  const generic = (r) => {
    const c = classifyRollbackPrecheck(r);
    assert.equal(c.kind, "failed", JSON.stringify(r));
    assert.equal(c.result.error, "", "通用文字（不顯示後端的矛盾內容）");
  };
  // safe 卻 applicable:false（過去被當 none 放行）
  generic({ success: true, code: "safe", applicable: false, removed: ["a"] });
  generic({ success: true, code: "safe", applicable: false });
  // no_removal 卻有 removed
  generic({ success: true, code: "no_removal", applicable: true, removed: ["a"] });
  generic({ success: true, code: "no_removal", applicable: false, removed: ["a"] });
  // removed 不是清單
  generic({ success: true, code: "no_removal", removed: "a" });
  generic({ success: true, code: "safe", removed: "a" });
  // 沒有 code
  generic({ success: true, applicable: false });
  generic({ success: true, applicable: true });
  // 合法的 none
  for (const ok of [
    { success: true, code: "no_removal", applicable: true, removed: [] },
    { success: true, code: "no_removal", applicable: false, removed: [] },
    { success: true, code: "no_removal", applicable: false },
    { success: true, code: "no_removal" },
  ]) {
    assert.equal(classifyRollbackPrecheck(ok).kind, "none", JSON.stringify(ok));
  }
  // 合法的 safe
  assert.equal(classifyRollbackPrecheck(safe()).kind, "safe");
  assert.equal(classifyRollbackPrecheck(safe({ applicable: undefined })).kind, "safe");
});

test("a contradictory success never unlocks the button", async () => {
  for (const precheck of [
    { success: true, code: "safe", applicable: false, removed: ["a"] },
    { success: true, code: "no_removal", removed: ["a"] },
  ]) {
    route({ precheck });
    const modal = await openModal();
    await pick(modal, 1);
    assert.equal(modal.precheckGate.kind, "failed");
    assert.equal(modal.canRollback, false);
    assert.equal(modal.precheckPanel.intro, "模組檢查失敗，請再試一次。");
    assert.equal(modal.precheckReason, "無法檢查模組");
  }
});

// =============================================================================
// 同時最多一個預檢請求、只記最新想要的、結果按版本快取
// =============================================================================

function trackedRoute() {
  const state = { active: 0, max: 0, pending: [], order: [] };
  route({
    precheck: ({ revision }) => {
      state.active += 1;
      state.max = Math.max(state.max, state.active);
      state.order.push(revision);
      return new Promise((resolve) => {
        state.pending.push((value) => { state.active -= 1; resolve(value); });
      });
    },
  });
  return state;
}

test("clicking A, B, C quickly sends only A and C, never two at once", async () => {
  const t = trackedRoute();
  const modal = await openModal();
  modal.state.revisions = [...REVISIONS, { revision: 4, chart: "x-0.1.0", status: "superseded", is_current: false }];
  for (const revision of [1, 2, 4]) {
    modal.selectRevision(modal.state.revisions.find((r) => r.revision === revision));
  }
  await settle();
  assert.deepEqual(t.order, [1], "B 只排隊、被 C 取代，從未送出");
  assert.equal(modal.precheckGate.kind, "loading");
  assert.equal(modal.canRollback, false);
  t.pending[0](noRemoval()); // A 回來：不顯示（已選 C）、不放行
  await settle();
  assert.deepEqual(t.order, [1, 4]);
  assert.equal(modal.precheckGate.kind, "loading");
  assert.equal(modal.canRollback, false);
  t.pending[1](safe());
  await settle();
  assert.equal(modal.precheckGate.kind, "safe");
  assert.equal(modal.state.precheck.revision, 4);
  assert.equal(t.max, 1, "任何時刻最多一個在飛");
  assert.equal(calls("/check-rollback-addons").length, 2);
});

test("A, B, A while A is in flight needs no second request", async () => {
  const t = trackedRoute();
  const modal = await openModal();
  modal.selectRevision(REVISIONS[0]);
  modal.selectRevision(REVISIONS[1]);
  modal.selectRevision(REVISIONS[0]);
  await settle();
  t.pending[0](blocked());
  await settle();
  assert.deepEqual(t.order, [1]);
  assert.equal(modal.precheckGate.kind, "blocked");
  assert.equal(modal.state.precheck.revision, 1);
});

test("a settled result is cached per revision for the modal's lifetime", async () => {
  route({ precheck: ({ revision }) => (revision === 1 ? blocked() : unverified()) });
  const modal = await openModal();
  await pick(modal, 1);
  await pick(modal, 2);
  modal.state.acknowledged = true;
  await pick(modal, 1);
  await pick(modal, 2);
  assert.deepEqual(calls("/check-rollback-addons").map(([, p]) => p.revision), [1, 2]);
  assert.equal(modal.precheckGate.kind, "unverified");
  assert.equal(modal.state.acknowledged, false, "快取命中也要重新勾選");
  assert.equal(modal.canRollback, false);
});

test("selecting a cached revision while another request is in flight drops the queued one", async () => {
  const t = trackedRoute();
  const modal = await openModal();
  modal.selectRevision(REVISIONS[0]);
  await settle();
  t.pending[0](noRemoval());
  await settle(); // rev 1 已快取
  modal.selectRevision(REVISIONS[1]); // 在飛
  await settle();
  modal.selectRevision(REVISIONS[0]); // 命中快取，立刻顯示；原本想要的 rev 2 取消
  assert.equal(modal.precheckGate.kind, "none");
  assert.equal(modal.canRollback, true);
  t.pending[1](blocked()); // rev 2 回來：只進快取，不顯示、不影響 rev 1
  await settle();
  assert.deepEqual(t.order, [1, 2]);
  assert.equal(modal.state.precheck.revision, 1);
  assert.equal(modal.canRollback, true);
});

test("Retry check bypasses the cache; a failed check is not cached", async () => {
  let n = 0;
  route({ precheck: () => (++n === 1 ? checkFailed() : n === 2 ? safe() : blocked()) });
  const modal = await openModal();
  await pick(modal, 1); // failed（不進快取）
  await pick(modal, 2);
  await pick(modal, 1); // 重選：失敗沒被快取，所以重新檢查
  assert.deepEqual(calls("/check-rollback-addons").map(([, p]) => p.revision), [1, 2, 1]);
  assert.equal(modal.precheckGate.kind, "blocked");
  // 已快取的 blocked：Retry 忽略快取再送一次
  await modal.retryPrecheck();
  assert.equal(calls("/check-rollback-addons").length, 4);
});

test("the gate stays closed for a revision that has no settled result", async () => {
  const t = trackedRoute();
  const modal = await openModal();
  modal.selectRevision(REVISIONS[0]);
  await settle();
  t.pending[0](noRemoval());
  await settle();
  assert.equal(modal.canRollback, true);
  modal.selectRevision(REVISIONS[1]); // 沒結果
  assert.equal(modal.canRollback, false);
  await modal.confirmRollback();
  assert.equal(calls("/rollback").length, 0);
});

test("a destroyed modal sends no queued request", async () => {
  const t = trackedRoute();
  const modal = await openModal();
  const destroy = globalThis.__gcDestroyHooks.at(-1);
  modal.selectRevision(REVISIONS[0]);
  modal.selectRevision(REVISIONS[1]);
  await settle();
  destroy();
  t.pending[0](noRemoval());
  await settle();
  assert.deepEqual(t.order, [1], "排隊中的 rev 2 不再送出");
});

test("the awaited startPrecheck resolves once its turn is settled, also when superseded", async () => {
  const t = trackedRoute();
  const modal = await openModal();
  let aDone = false;
  let cDone = false;
  modal.startPrecheck(1).then(() => { aDone = true; });
  modal.state.selectedRevision = 2;
  modal.startPrecheck(2).then(() => { cDone = true; });
  await settle();
  assert.deepEqual([aDone, cDone], [false, false]);
  t.pending[0](noRemoval());
  await settle();
  assert.deepEqual([aDone, cDone], [true, false]);
  t.pending[1](noRemoval());
  await settle();
  assert.deepEqual([aDone, cDone], [true, true]);
});

// =============================================================================
// SCSS 沒有死規則、沒有散落的色碼；產品用語
// =============================================================================

const scss = await readPaas("components/modal/RollbackModal.scss");

const HINT_MSGID =
  "If they are installed, Odoo will stop loading them after the rollback, or the service may fail to start. Rolling back to a revision that includes them recovers the service; no data is deleted.";
const REQUEST_FAILED_MSGID =
  "The request did not finish, so the rollback may already have started. Check the service status before trying again.";

test("the precheck styles carry no dead rules and no scattered colour literals", () => {
  const block = scss.slice(scss.indexOf("// #2069"));
  // 色碼只出現在 $woow-rollback-* 的宣告行
  for (const line of block.split("\n").filter((l) => !l.trim().startsWith("//"))) {
    if (/#[0-9a-fA-F]{3,8}\b|rgba\(\s*\d/.test(line)) {
      assert.match(line, /^\$woow-rollback-[a-z-]+:/, `規則裡不要直接寫色碼：${line}`);
    }
  }
  // unverified 面板沒有狀態標籤（items 只有 name）：不該有它的樣式
  const unverified = block.slice(block.indexOf("&_unverified {"));
  assert.doesNotMatch(unverified.slice(0, unverified.indexOf("\n    }\n") + 7), /precheck_state/);
  // 每個 `&_xxx` 都有對應的模板 class（kind 類別由 precheckPanel.kind 組出來）
  const kinds = ["loading", "safe", "blocked", "unverified", "failed"];
  for (const [, name] of block.matchAll(/^\s*&_([a-z]+)\b/gm)) {
    assert.ok(
      xml.includes(`o_woow_rollback_precheck_${name}`) || kinds.includes(name),
      `SCSS 的 &_${name} 沒有任何元素使用`
    );
  }
  // 狀態標籤沿用共用的紅色 badge，而不是自己再畫一份紅
  assert.match(xml, /class="o_woow_badge o_woow_badge_red o_woow_rollback_precheck_state"/);
});

test("product vocabulary — no 恢復 and no 'roll forward' in the rollback strings; old msgid is gone", () => {
  assert.doesNotMatch(modalJs, /roll forward/i);
  for (const oldMsgid of [
    "If they are installed, they will fail to load after the rollback. Roll forward again to recover; no data is deleted.",
    "If they are installed, they will fail to load after the rollback. Choose a revision that includes them and roll back again to recover; no data is deleted.",
  ]) {
    assert.equal(po.has(oldMsgid), false, "舊 msgid 不留孤兒");
  }
  for (const [msgid, entry] of po) {
    if (entry.references.some((r) => /components\/modal\/RollbackModal\.(js|xml):0$/.test(r))) {
      assert.doesNotMatch(entry.msgstr, /恢復/, msgid);
      assert.doesNotMatch(msgid, /roll forward/i, msgid);
    }
  }
  const hint = po.get(HINT_MSGID);
  assert.ok(hint);
  assert.match(hint.msgstr, /回復/);
  assert.doesNotMatch(hint.msgstr, /恢復|roll forward/i);
});

// =============================================================================
// 勾選綁定在「顯示中的那一份預檢」：舊面板的勾選、快取命中、重試都不能讓新結果一出現就已勾選
// =============================================================================

const tickEvent = (token, checked = true) => ({ target: { dataset: { token: String(token) }, checked } });

test("onAcknowledge accepts a tick only for the currently displayed result (token = precheck.seq)", async () => {
  route({ precheck: unverified() });
  const modal = await openModal();
  await pick(modal, 1);
  const seq = modal.state.precheck.seq;
  modal.onAcknowledge(tickEvent(seq - 1));
  assert.equal(modal.state.acknowledged, false, "舊面板的序號不算數");
  modal.onAcknowledge(tickEvent("not a number"));
  assert.equal(modal.state.acknowledged, false);
  modal.onAcknowledge(tickEvent(seq));
  assert.equal(modal.state.acknowledged, true);
  assert.equal(modal.canRollback, true);
  modal.onAcknowledge(tickEvent(seq, false));
  assert.equal(modal.state.acknowledged, false, "取消勾選");
});

test("a tick given to the old panel in the same frame never acknowledges the next revision's result", async () => {
  const pending = [];
  route({ precheck: ({ revision }) => (revision === 1 ? unverified() : new Promise((resolve) => pending.push(resolve))) });
  const modal = await openModal();
  await pick(modal, 1);
  const oldToken = modal.state.precheck.seq;      // 舊面板上勾選框帶的序號
  modal.selectRevision(REVISIONS[1]);             // 還沒重繪：舊面板的勾選框仍在 DOM
  modal.onAcknowledge(tickEvent(oldToken));       // 使用者在同一個畫格勾了它
  assert.equal(modal.state.acknowledged, false);
  await settle();
  pending[0](unverified({ removed: ["other_mod"] }));
  await settle();
  assert.equal(modal.precheckGate.kind, "unverified");
  assert.equal(modal.state.acknowledged, false, "新結果出現時沒有勾選");
  assert.equal(modal.canRollback, false);
  assert.equal(modal.sendsAcknowledgement, false);
  await modal.onRollback();
  assert.equal(modal.state.confirming, false);
  assert.equal(calls("/rollback").length, 0);
});

test("every path that shows a result starts unacknowledged: settle, cache hit, queued cache hit, forced re-check", async () => {
  route({ precheck: unverified() });
  const modal = await openModal();
  await pick(modal, 1);
  modal.onAcknowledge(tickEvent(modal.state.precheck.seq));
  assert.equal(modal.state.acknowledged, true);
  const seqBefore = modal.state.precheck.seq;
  await pick(modal, 2);                           // 新請求落定
  assert.equal(modal.state.acknowledged, false);
  modal.onAcknowledge(tickEvent(modal.state.precheck.seq));
  await pick(modal, 1);                           // 快取命中
  assert.equal(modal.state.acknowledged, false);
  assert.ok(modal.state.precheck.seq > seqBefore, "每次換掉顯示中的預檢都有新序號");
  modal.onAcknowledge(tickEvent(modal.state.precheck.seq));
  await modal.retryPrecheck();                    // 強制重新預檢
  assert.equal(modal.state.acknowledged, false);
});

test("the checkbox is keyed by the precheck sequence so its DOM is rebuilt whenever the displayed check changes", async () => {
  assert.match(xml, /<label t-if="precheckPanel\.kind === 'unverified'" t-key="state\.precheck\.seq"/);
  route({ precheck: unverified() });
  const modal = await openModal();
  await pick(modal, 1);
  const k1 = modal.state.precheck.seq;
  await pick(modal, 2);
  const k2 = modal.state.precheck.seq;
  await pick(modal, 1);
  const k3 = modal.state.precheck.seq;
  assert.equal(new Set([k1, k2, k3]).size, 3, "切回快取的同一版本也是新的一次顯示");
  modal.state.rolling = true; // 與預檢無關的狀態變化不換序號
  assert.equal(modal.state.precheck.seq, k3);
});

// =============================================================================
// 世代：重試或被拒絕後，舊回應不得進快取、不得顯示
// =============================================================================

test("two Retry clicks in one frame: the older answer is dropped, the newest is shown and cached", async () => {
  const pending = [];
  let n = 0;
  route({ precheck: () => (++n === 1 ? checkFailed() : new Promise((resolve) => pending.push(resolve))) });
  const modal = await openModal();
  await pick(modal, 1);
  assert.equal(modal.precheckGate.kind, "failed");
  modal.retryPrecheck();
  modal.retryPrecheck();                          // 同一個畫格的第二下
  await settle();
  assert.equal(pending.length, 1, "第一個重試在飛，第二個排隊");
  pending[0](noRemoval());                        // 已過期的答案
  await settle();
  assert.equal(modal.precheckGate.kind, "loading", "過期答案不顯示");
  assert.equal(modal.canRollback, false);
  assert.equal(modal._pre.cache.has(1), false, "過期答案不進快取");
  assert.equal(pending.length, 2, "排隊的第二個重試接著送出");
  pending[1](blocked());
  await settle();
  assert.equal(modal.precheckGate.kind, "blocked");
  assert.equal(modal.canRollback, false);
  assert.equal(modal._pre.cache.get(1).code, "blocked");
  assert.deepEqual(calls("/check-rollback-addons").map(([, p]) => p.revision), [1, 1, 1]);
});

test("switching away and back while the newest re-check is in flight does not resurrect the stale answer", async () => {
  const pending = [];
  let n = 0;
  route({ precheck: () => (++n === 1 ? checkFailed() : new Promise((resolve) => pending.push(resolve))) });
  const modal = await openModal();
  await pick(modal, 1);
  modal.retryPrecheck();
  modal.retryPrecheck();
  await settle();
  pending[0](noRemoval());                        // 過期
  await settle();
  modal.selectRevision(REVISIONS[1]);
  modal.selectRevision(REVISIONS[0]);             // 沒有快取可用：等最新的答案
  assert.equal(modal.precheckGate.kind, "loading");
  assert.equal(modal.canRollback, false);
  pending[1](blocked());
  await settle();
  assert.equal(modal.precheckGate.kind, "blocked");
  assert.equal(modal.canRollback, false);
  assert.equal(calls("/check-rollback-addons").length, 3, "沒有多送請求");
});

test("a forced start bumps the revision's generation; a late older answer is not cached or shown", async () => {
  const pending = [];
  route({ precheck: () => new Promise((resolve) => pending.push(resolve)) });
  const modal = await openModal();
  modal.selectRevision(REVISIONS[0]);
  await settle();
  modal.startPrecheck(1, { force: true });        // 第一個請求還在飛
  modal.startPrecheck(1, { force: true });
  await settle();
  pending[0](safe());
  await settle();
  assert.equal(modal._pre.cache.has(1), false);
  assert.equal(modal.precheckGate.kind, "loading");
  pending[1](blocked());
  await settle();
  assert.equal(modal.precheckGate.kind, "blocked");
  assert.equal(modal._pre.cache.get(1).code, "blocked");
});

test("the Retry button is disabled while the selected revision is being checked", async () => {
  assert.match(xml, /onClick\.bind="retryPrecheck" disabled="state\.rolling or precheckLoading"/);
  let release;
  route({ precheck: () => new Promise((resolve) => { release = resolve; }) });
  const modal = await openModal();
  assert.equal(modal.precheckLoading, false, "沒選版本");
  modal.selectRevision(REVISIONS[0]);
  assert.equal(modal.precheckLoading, true);
  release(noRemoval());
  await settle();
  assert.equal(modal.precheckLoading, false);
});

// =============================================================================
// 回復請求進行中對話框被卸載
// =============================================================================

test("unmounted while rolling: a refusal sends no new precheck and writes no state", async () => {
  let release;
  route({
    precheck: safe(),
    rollback: () => new Promise((resolve) => { release = resolve; }),
  });
  const modal = await openModal();
  const destroy = globalThis.__gcDestroyHooks.at(-1);
  await pick(modal, 1);
  await modal.onRollback();
  const rolling = modal.onRollback();
  await settle();
  assert.equal(modal.state.rolling, true);
  destroy();
  const frozen = JSON.stringify(modal.state);
  release({ success: false, error: "refused", code: ROLLBACK_REMOVES_INSTALLED_ADDONS });
  await rolling;
  await settle();
  assert.equal(calls("/check-rollback-addons").length, 1, "卸載後不再送預檢（後端每次預檢要 kubectl exec）");
  assert.equal(JSON.stringify(modal.state), frozen, "卸載後不寫 state");
});

test("unmounted while rolling: an unknown outcome writes nothing either; a success still tells the parent", async () => {
  for (const [rollback, expectedRolled] of [
    [{ success: false, error: "Request timed out", code: ROLLBACK_REQUEST_FAILED }, 0],
    [{ success: false, error: "plain" }, 0],
    [{ success: true, data: {} }, 1],
  ]) {
    let release;
    route({ precheck: safe(), rollback: () => new Promise((resolve) => { release = resolve; }) });
    const modal = await openModal();
    const destroy = globalThis.__gcDestroyHooks.at(-1);
    await pick(modal, 1);
    await modal.onRollback();
    const rolling = modal.onRollback();
    await settle();
    destroy();
    const frozen = JSON.stringify(modal.state);
    release(rollback);
    await rolling;
    await settle();
    assert.equal(JSON.stringify(modal.state), frozen, JSON.stringify(rollback));
    assert.equal(calls("/check-rollback-addons").length, 1);
    assert.equal(modal.done.rolledBack, expectedRolled, JSON.stringify(rollback));
  }
});

test("a precheck answer that arrives after the modal was unmounted writes nothing and starts no queued request", async () => {
  const pending = [];
  route({ precheck: () => new Promise((resolve) => pending.push(resolve)) });
  const modal = await openModal();
  const destroy = globalThis.__gcDestroyHooks.at(-1);
  modal.selectRevision(REVISIONS[0]);   // 在飛
  modal.selectRevision(REVISIONS[1]);   // 排隊
  await settle();
  destroy();
  const frozen = JSON.stringify(modal.state);
  pending[0](safe());
  await settle();
  assert.equal(JSON.stringify(modal.state), frozen, "卸載後在飛的預檢回來也不寫 state");
  assert.equal(pending.length, 1, "排隊中的不再送出");
  // 同上，但在飛的就是選取中的版本（沒有排隊）
  const modal2 = await openModal();
  const destroy2 = globalThis.__gcDestroyHooks.at(-1);
  modal2.selectRevision(REVISIONS[0]);
  await settle();
  destroy2();
  const frozen2 = JSON.stringify(modal2.state);
  pending[1](safe());
  await settle();
  assert.equal(JSON.stringify(modal2.state), frozen2);
  assert.equal(modal2._pre.cache.size, 0);
});

test("the pump itself refuses to send once the modal is unmounted, even when a job is still queued", async () => {
  route({ precheck: safe() });
  const modal = await openModal();
  const destroy = globalThis.__gcDestroyHooks.at(-1);
  destroy();
  let released = 0;
  modal._pre.wanted = { revision: 1, gen: 0, waiters: [() => { released += 1; }] };
  await modal._pumpPrecheck();
  assert.equal(calls("/check-rollback-addons").length, 0);
  assert.equal(modal._pre.wanted, null);
  assert.equal(released, 1, "等這一筆的人照樣被放行");
});

test("startPrecheck on an unmounted modal sends nothing and writes nothing", async () => {
  route({ precheck: safe() });
  const modal = await openModal();
  const destroy = globalThis.__gcDestroyHooks.at(-1);
  await pick(modal, 1);
  destroy();
  const frozen = JSON.stringify(modal.state);
  const before = calls("/check-rollback-addons").length;
  await modal.startPrecheck(1, { force: true });
  await modal.startPrecheck(2);
  assert.equal(calls("/check-rollback-addons").length, before);
  assert.equal(JSON.stringify(modal.state), frozen);
});

// =============================================================================
// 請求沒跑完（結果未知）：回選版本步驟、專用橫幅、強制重新預檢
// =============================================================================

const UNKNOWN_BANNER_ZH = "請求沒有完成，因此回復可能已經開始。請先確認服務狀態，再重新嘗試。";

test("request_failed: back to the selection step, own banner (not the raw error), forced precheck, tick asked again", async () => {
  let checks = 0;
  route({
    precheck: () => (++checks === 1 ? safe() : unverified()),
    rollback: () => { throw new Error("Request timed out. Please try again."); },
  });
  const modal = await openModal();
  await pick(modal, 1);
  await runRollbackThrough(modal);
  await settle();
  assert.equal(modal.state.error, UNKNOWN_BANNER_ZH);
  assert.equal(modal.state.rolling, false);
  assert.equal(modal.state.confirming, false, "回選版本步驟");
  assert.equal(modal.state.acknowledged, false);
  assert.deepEqual(calls("/check-rollback-addons").map(([, p]) => p.revision), [1, 1], "忽略快取、對同一版本重新預檢");
  assert.equal(modal.precheckGate.kind, "unverified", "回復後服務通常不在執行中：要重新勾選");
  assert.equal(modal.canRollback, false);
  assert.equal(modal.done.rolledBack, 0);
});

test("request_failed from the service result is handled the same way, in English too", async () => {
  globalThis.__gcLang = "en";
  try {
    route({ precheck: safe(), rollback: () => { throw new Error("socket hang up"); } });
    const modal = await openModal();
    await pick(modal, 1);
    await runRollbackThrough(modal);
    await settle();
    assert.equal(modal.state.error, REQUEST_FAILED_MSGID);
    assert.equal(modal.state.confirming, false);
    assert.equal(calls("/check-rollback-addons").length, 2);
  } finally {
    globalThis.__gcLang = "zh_TW";
  }
});

test("a rejecting cloudService.rollbackService is also treated as an unknown outcome (rolling does not stay true)", async () => {
  route({ precheck: safe() });
  globalThis.__gcOverrides.rollbackService = async () => { throw new Error("kaput"); };
  try {
    const modal = await openModal();
    await pick(modal, 1);
    await runRollbackThrough(modal);
    await settle();
    assert.equal(modal.state.rolling, false);
    assert.equal(modal.state.error, UNKNOWN_BANNER_ZH);
    assert.equal(modal.state.confirming, false);
    assert.equal(calls("/check-rollback-addons").length, 2);
  } finally {
    delete globalThis.__gcOverrides.rollbackService;
  }
});

test("a backend 'Unable to connect to deployment service' answer keeps today's behaviour (no unknown-outcome handling)", async () => {
  route({ precheck: safe(), rollback: { success: false, error: "Unable to connect to deployment service" } });
  const modal = await openModal();
  await pick(modal, 1);
  await runRollbackThrough(modal);
  await settle();
  assert.equal(modal.state.error, "Unable to connect to deployment service");
  assert.equal(modal.state.confirming, true);
  assert.equal(calls("/check-rollback-addons").length, 1);
});

test("the failure banner does not follow the user to another revision; re-selecting the same one keeps it", async () => {
  route({
    precheck: safe(),
    rollback: { success: false, error: "refused for r1", data: { code: ROLLBACK_REMOVES_INSTALLED_ADDONS } },
  });
  const modal = await openModal();
  await pick(modal, 1);
  await runRollbackThrough(modal);
  await settle();
  assert.equal(modal.state.error, "refused for r1");
  await pick(modal, 1);
  assert.equal(modal.state.error, "refused for r1", "同一個版本：橫幅還在");
  await pick(modal, 2);
  assert.equal(modal.state.error, null, "換版本：橫幅清掉");
});

// =============================================================================
// 無障礙：常駐原因行有 id、確認鈕指向它；文字色對比
// =============================================================================

test("the reason line has a per-instance id and the confirm button points at it only while it is shown", async () => {
  route({ precheck: unverified() });
  const a = await openModal();
  const b = await openModal();
  assert.match(a.precheckReasonId, /^o_woow_rollback_reason_\d+$/);
  assert.notEqual(a.precheckReasonId, b.precheckReasonId);
  assert.equal(a.confirmDescribedBy, undefined, "沒有原因行：不帶屬性");
  await pick(a, 1);
  assert.equal(a.confirmDescribedBy, a.precheckReasonId);
  a.onAcknowledge(tickEvent(a.state.precheck.seq));
  assert.equal(a.confirmDescribedBy, undefined, "按鈕已放行、原因行消失");
  assert.match(xml, /<p\s+t-if="precheckReason"\s+t-att-id="precheckReasonId"/);
  assert.match(xml, /ariaDescribedby="confirmDescribedBy"/);
});

test("WoowButton passes aria-describedby through to the <button>", async () => {
  const buttonXml = await readPaas("components/button/WoowButton.xml");
  const buttonJs = await readPaas("components/button/WoowButton.js");
  assert.match(buttonXml, /t-att-aria-describedby="props\.ariaDescribedby"/);
  assert.match(buttonJs, /ariaDescribedby: \{ type: String, optional: true \}/);
});

function contrast(fg, bg) {
  const lum = ([r, g, b]) => {
    const f = (c) => ((c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const [hi, lo] = [lum(fg), lum(bg)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const over = (fg, alpha, bg) => fg.map((c, i) => Math.round(c * alpha + bg[i] * (1 - alpha)));

test("text colours of the precheck titles and the reason line reach WCAG AA (4.5:1) on white and on their tint", () => {
  const variable = (name) => new RegExp(`\\$${name}:\\s*(#[0-9a-fA-F]{6})`).exec(scss)[1];
  const white = [255, 255, 255];
  const dangerText = hex(variable("woow-rollback-danger-text"));
  const warningText = hex(variable("woow-rollback-warning"));
  const dangerTint = over(hex(variable("woow-rollback-danger")), 0.1, white);
  const warningTint = over([245, 158, 11], 0.12, white);
  for (const [name, fg, bg] of [
    ["danger text on white footer", dangerText, white],
    ["danger text on the blocked/failed tint", dangerText, dangerTint],
    ["warning text on white footer", warningText, white],
    ["warning text on the unverified tint", warningText, warningTint],
  ]) {
    assert.ok(contrast(fg, bg) >= 4.5, `${name}: ${contrast(fg, bg).toFixed(2)}:1`);
  }
  // 淺紅 #ef4444 只能當背景色調，不能直接當文字色
  const block = scss.slice(scss.indexOf("// #2069"));
  assert.doesNotMatch(block, /color:\s*\$woow-rollback-danger;/);
});

// =============================================================================
// i18n
// =============================================================================

test("every new msgid is translated for the front end (odoo-javascript, non-empty, right reference)", () => {
  const expected = [
    ["Checking modules...", "RollbackModal.js"],
    ["These modules will no longer be offered after this rollback:", "RollbackModal.js"],
    ["They are not installed in Odoo, so your data is not affected.", "RollbackModal.js"],
    ["Rollback blocked: modules are still installed", "RollbackModal.js"],
    ["This rollback would remove modules that are still installed in Odoo:", "RollbackModal.js"],
    ["Uninstall them in Odoo first (Apps → Uninstall), or choose a revision that includes them.", "RollbackModal.js"],
    ["(required by: %s)", "RollbackModal.js"],
    ["Installed modules cannot be checked", "RollbackModal.js"],
    ["The service is not running, so the platform cannot check whether the modules removed by this rollback are installed.", "RollbackModal.js"],
    [HINT_MSGID, "RollbackModal.js"],
    [REQUEST_FAILED_MSGID, "RollbackModal.js"],
    ["I understand", "RollbackModal.js"],
    ['Tick "I understand" to continue.', "RollbackModal.js"],
    ["Could not check the modules", "RollbackModal.js"],
    ["The module check failed. Please try again.", "RollbackModal.js"],
    ["Retry check", "RollbackModal.xml"],
  ];
  for (const [msgid, file] of expected) {
    const entry = po.get(msgid);
    assert.ok(entry, `缺 msgid：${msgid}`);
    assert.ok(entry.msgstr, `未翻譯：${msgid}`);
    assert.ok(entry.comments.includes("odoo-javascript"), `缺 #. odoo-javascript：${msgid}`);
    assert.ok(
      entry.references.some((r) => r.endsWith(`components/modal/${file}:0`)),
      `參考行不對：${msgid}`
    );
    assert.match(entry.msgstr, /[㐀-鿿]/, msgid);
  }
});

test("unverified hint states both risks (Odoo stops loading them, the service may fail to start), the way back and that no data is deleted", async () => {
  route({ precheck: unverified() });
  const modal = await openModal();
  await pick(modal, 1);
  const zh = modal.precheckPanel.hint;
  assert.match(zh, /Odoo 就不會再載入它們/);
  assert.match(zh, /服務可能無法啟動/);
  assert.match(zh, /改回復到包含它們的版本/);
  assert.match(zh, /資料不會被刪除/);
  assert.doesNotMatch(zh, /恢復|前滾/);
  globalThis.__gcLang = "en";
  try {
    const en = (await openModal()).precheckPanel;
    assert.equal(en, null, "沒選版本沒有面板");
    const second = await openModal();
    await pick(second, 1);
    assert.equal(second.precheckPanel.hint, HINT_MSGID);
  } finally {
    globalThis.__gcLang = "zh_TW";
  }
  assert.match(HINT_MSGID, /Odoo will stop loading them after the rollback, or the service may fail to start/);
  assert.match(HINT_MSGID, /Rolling back to a revision that includes them recovers the service; no data is deleted\./);
});

test("zh_TW wording: 不在執行中 (not 沒有運行), 不會再載入 (not 會不再載入), 回復 (not 恢復) in every new string", () => {
  const intro = po.get(
    "The service is not running, so the platform cannot check whether the modules removed by this rollback are installed."
  );
  assert.match(intro.msgstr, /服務目前不在執行中/);
  assert.doesNotMatch(intro.msgstr, /沒有運行/);
  for (const msgid of [HINT_MSGID, REQUEST_FAILED_MSGID]) {
    const entry = po.get(msgid);
    assert.doesNotMatch(entry.msgstr, /恢復|會不再載入|前滾/, msgid);
    assert.match(entry.msgstr, /回復/, msgid);
  }
});
