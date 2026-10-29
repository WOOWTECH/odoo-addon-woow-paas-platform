// INFRA-FIX-018-1／AI-HER-003-1：回復對話框寫明「只保留最近 N 個版本」。
//
// operator 的 helm 只保留每個 release 最近 N 筆 revision（HELM_HISTORY_MAX，含更新失敗的那幾筆），
// 更舊的已被清掉、無法回復。商用驗收時對話框沒有任何提示，Hermes 247 升級前的版本「消失」了
// 也看不出原因。N 由 revisions API 的 history_max 帶來（前端不寫死）；舊後端沒有這個欄位時
// 不顯示。用 stub 載入真實的 cloud_service.js 與 RollbackModal.js，不起瀏覽器；
// 實際畫面仍要在沙盒用瀏覽器驗。
import assert from "node:assert/strict";
import test from "node:test";
import { loadPo, readPaas, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

const po = await loadPo();

// Odoo 前端 `_t` 的替身：只用 odoo-javascript 條目；位置參數 `%s` 依序代換。
globalThis.__rbLang = "zh_TW";
globalThis.__rbT = (message, ...args) => {
  let text = message;
  if (globalThis.__rbLang === "zh_TW") {
    const entry = po.get(message);
    if (entry && entry.msgstr && entry.comments.includes("odoo-javascript")) {
      text = entry.msgstr;
    }
  }
  let i = 0;
  return text.replace(/%s/g, () => String(args[i++]));
};
const translate = "const _t = (message, ...args) => globalThis.__rbT(message, ...args);";

// ---- cloud_service.fetchRevisions：history_max 往前端傳 ----
globalThis.__rbRpc = async () => ({});
const serviceSource = stubImports(await readPaas("services/cloud_service.js"), {
  "@odoo/owl": "const reactive = (value) => value;",
  "@web/core/l10n/translation": translate,
  "./rpc": "const jsonRpc = (...args) => globalThis.__rbRpc(...args);",
});
const { cloudService } = await import(toDataUrl(serviceSource));

// ---- RollbackModal ----
globalThis.__rbFetch = async () => ({ success: true, data: [] });
const modalSource = stubImports(await readPaas("components/modal/RollbackModal.js"), {
  "@odoo/owl":
    "class Component { constructor(props) { this.props = props; } } " +
    "const useState = (value) => value; const onMounted = () => {}; " +
    "const useRef = () => ({ el: null }); const useEffect = () => {}; const onWillDestroy = () => {};",
  "@web/core/l10n/translation": translate,
  "../button/WoowButton": "const WoowButton = {};",
  "../icon/WoowIcon": "const WoowIcon = {};",
  "../../services/cloud_service":
    "const ROLLBACK_REQUEST_FAILED = 'request_failed'; " +
    "const cloudService = { fetchRevisions: (...args) => globalThis.__rbFetch(...args), " +
    "checkRollbackAddons: async () => ({ success: true, code: 'no_removal' }) };",
  "../../utils/datetime_helpers": "const formatUserDateTime = (value) => value;",
});
const { RollbackModal } = await import(toDataUrl(modalSource));

async function openModal(fetchResult) {
  globalThis.__rbFetch = async () => fetchResult;
  const modal = new RollbackModal({
    service: { id: 7, workspace_id: 3, helm_revision: 2 },
    onClose() {},
    onRollback() {},
  });
  modal.setup();
  await modal.loadRevisions();
  return modal;
}

const REVISIONS = [
  { revision: 1, chart: "x-0.1.0", status: "superseded" },
  { revision: 2, chart: "x-0.1.0", status: "deployed" },
];

test("fetchRevisions passes the operator's history_max through", async () => {
  globalThis.__rbRpc = async () => ({ success: true, data: REVISIONS, history_max: 20 });
  const result = await cloudService.fetchRevisions(3, 7);
  assert.equal(result.success, true);
  assert.deepEqual(result.data, REVISIONS);
  assert.equal(result.historyMax, 20);
});

test("fetchRevisions from an older backend reports historyMax null", async () => {
  globalThis.__rbRpc = async () => ({ success: true, data: REVISIONS });
  const result = await cloudService.fetchRevisions(3, 7);
  assert.equal(result.historyMax, null);
});

test("rollback dialog states how many revisions are kept (zh_TW)", async () => {
  globalThis.__rbLang = "zh_TW";
  const modal = await openModal({ success: true, data: REVISIONS, historyMax: 20 });
  assert.equal(modal.historyHint, "只保留最近 20 個版本（含更新失敗的版本），更早的版本已無法回復。");
});

test("rollback dialog hint follows the reported value (en source)", async () => {
  globalThis.__rbLang = "en";
  const modal = await openModal({ success: true, data: REVISIONS, historyMax: 35 });
  assert.match(modal.historyHint, /^Only the most recent 35 revisions are kept/);
  globalThis.__rbLang = "zh_TW";
});

test("no hint when the backend does not report a limit", async () => {
  for (const historyMax of [null, undefined, 0, "20"]) {
    const modal = await openModal({ success: true, data: REVISIONS, historyMax });
    assert.equal(modal.historyHint, "", `historyMax=${String(historyMax)}`);
  }
});

test("template renders the hint above the revision list", async () => {
  const xml = await readPaas("components/modal/RollbackModal.xml");
  assert.match(xml, /<div t-if="historyHint"[^>]*o_woow_rollback_history_hint[^>]*>[\s\S]*?<span t-esc="historyHint"\/>/);
});
