// LLM-04（DESIGN §6.4）：《AI 模型服務條款》對話框。
//
// 守線：條文一律經 safeHtml()（DOMPurify）才顯示；read 模式不能同意；accept 模式送的是對話框裡顯示的那一版，
// 送出前又改版（llm_tos_outdated）就換成新版、不關閉；條文裡的外部連結一律開新分頁（不讓 /woow 整頁跳走）；
// Esc／背景／右上角都能關（同意送出中除外）；卸載或關閉之後才回來的回應直接丟掉。
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
  "const useRef = (name) => (globalThis.__llmRefs[name] = globalThis.__llmRefs[name] || { el: null });",
  "const onMounted = (fn) => globalThis.__llmHooks.mounted.push(fn);",
  "const onWillUnmount = (fn) => globalThis.__llmHooks.unmount.push(fn);",
].join(" ");

function eventTarget(extra = {}) {
  const listeners = new Map();
  return Object.assign({
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

globalThis.document = eventTarget();
globalThis.window = eventTarget({ opened: [], open(...args) { this.opened.push(args); } });
globalThis.__llmFakeService = {};
globalThis.__llmSanitized = [];
globalThis.__llmSafeHtml = (html) => {
  globalThis.__llmSanitized.push(html);
  return { sanitized: html };
};

const errorsUrl = toDataUrl(stubImports(await readPaas("services/llm_errors.js"), {
  "@web/core/l10n/translation": translateFromGlobal,
}));
const modalFocusUrl = toDataUrl(await readPaas("utils/modal_focus.js"));
const source = stubImports(await readPaas("components/llm/LlmTermsModal.js"), {
  "@odoo/owl": owlStub,
  "@web/core/l10n/translation": translateFromGlobal,
  "../button/WoowButton": "const WoowButton = {};",
  "../icon/WoowIcon": "const WoowIcon = {};",
  "../../services/html_sanitize": "const safeHtml = (...args) => globalThis.__llmSafeHtml(...args);",
  "../../services/llm_service":
    "const llmService = new Proxy({}, { get: (_, k) => globalThis.__llmFakeService[k] });",
  "../../services/llm_errors": `const { llmErrorMessage } = await import("${errorsUrl}");`,
  "../../utils/modal_focus": `const { inertOutside, trapTab } = await import("${modalFocusUrl}");`,
});
const { LlmTermsModal, termsLinkAction } = await import(toDataUrl(source));

const settle = () => new Promise((resolve) => setImmediate(resolve));

const TERMS = Object.freeze({
  version: "2026-10-draft-1",
  title: "AI 模型服務條款（草稿，尚未生效）",
  content_html: "<p><strong>草稿，尚未生效。</strong></p><script>alert(1)</script>",
  is_draft: true,
  state: "published",
});

/** 建一個對話框：`terms` 是 fetchTerms 依序回的結果，`accept` 是 acceptTerms 的實作。 */
function open({ mode = "read", blocking, terms = [{ success: true, data: TERMS }], accept } = {}) {
  globalThis.__llmHooks = { mounted: [], unmount: [] };
  globalThis.__llmRefs = {};
  globalThis.__llmSanitized = [];
  globalThis.document = eventTarget();
  globalThis.window.opened = [];
  const calls = { fetchTerms: 0, accept: [] };
  const queue = [...terms];
  globalThis.__llmFakeService = {
    async fetchTerms() {
      calls.fetchTerms += 1;
      return queue.length > 1 ? queue.shift() : queue[0];
    },
    async acceptTerms(version) {
      calls.accept.push(version);
      return accept ? accept(version) : { success: true, data: { tos: {} } };
    },
  };
  const closes = [];
  const modal = new LlmTermsModal();
  modal.props = { mode, onClose: (arg) => closes.push(arg) };
  if (blocking !== undefined) modal.props.blocking = blocking;
  modal.setup();
  return { modal, closes, calls };
}

async function mount(modal) {
  for (const fn of globalThis.__llmHooks.mounted) await fn();
  await settle();
  return modal;
}

test("termsLinkAction：http(s) 開新分頁、mailto 交給瀏覽器、其他（#、相對路徑、javascript:）擋下", () => {
  assert.equal(termsLinkAction("https://github.com/QuantumNous/new-api"), "new_tab");
  assert.equal(termsLinkAction(" HTTP://example.test/x "), "new_tab");
  assert.equal(termsLinkAction("mailto:support@woowtech.io"), "default");
  for (const href of ["#/llm/keys", "/woow#/billing", "javascript:alert(1)", "", null, undefined, 7]) {
    assert.equal(termsLinkAction(href), "block", String(href));
  }
});

test("打開就讀 published 版：條文經 safeHtml()、標題是資料值、草稿要標示", async () => {
  const { modal, calls } = open();
  assert.equal(modal.state.phase, "loading");
  assert.equal(modal.heading, "《AI 模型服務條款》", "還沒讀到時用固定名稱");
  await mount(modal);
  assert.equal(calls.fetchTerms, 1);
  assert.equal(modal.state.phase, "shown");
  assert.deepEqual(globalThis.__llmSanitized, [TERMS.content_html], "原始 HTML 只交給 safeHtml()");
  assert.deepEqual(modal.state.content, { sanitized: TERMS.content_html });
  assert.equal(modal.heading, TERMS.title);
  assert.equal(modal.versionText, "版本：2026-10-draft-1");
  assert.equal(modal.state.isDraft, true);
  assert.equal(modal.draftText, "條款目前是草稿，尚未生效。");
  assert.equal(document.has("keydown"), true);
});

test("讀不到條款：顯示碼表句子（不轉述後端文字）", async () => {
  const { modal } = open({ terms: [{ success: false, code: "llm_tos_unavailable" }] });
  await mount(modal);
  assert.equal(modal.state.phase, "error");
  assert.equal(modal.state.loadError, "《AI 模型服務條款》暫時無法顯示，請稍後再試");
  const network = open({ terms: [{ success: false, code: "llm_network_error" }] });
  await mount(network.modal);
  assert.equal(network.modal.state.loadError, "AI 模型服務暫時無法連線，請稍後再試");
  const empty = open({ terms: [{ success: true, data: { version: "" } }] });
  await mount(empty.modal);
  assert.equal(empty.modal.state.phase, "error");
});

test("read 模式不能同意；關閉回報讀到的版本", async () => {
  const { modal, closes, calls } = open({ mode: "read" });
  await mount(modal);
  assert.equal(modal.canAccept, false);
  await modal.accept();
  assert.deepEqual(calls.accept, []);
  modal.requestClose();
  assert.deepEqual(closes, [{ accepted: false, version: TERMS.version }]);
  assert.equal(document.has("keydown"), false, "關閉時拆掉 listener");
  modal.requestClose();
  assert.equal(closes.length, 1, "只關一次");
});

test("accept 模式：送顯示中的那一版；成功就關閉並回報 accepted", async () => {
  let resolve;
  const { modal, closes, calls } = open({
    mode: "accept",
    blocking: true,
    accept: () => new Promise((r) => { resolve = r; }),
  });
  await mount(modal);
  assert.equal(modal.canAccept, true);
  assert.equal(modal.acceptHint, "同意後即可再建立金鑰、轉換點數與重新啟用金鑰；不論是否同意，既有金鑰都照常可用。");
  const pending = modal.accept();
  assert.equal(modal.state.accepting, true);
  assert.equal(modal.canAccept, false, "送出中不能再按");
  modal.requestClose();
  document.dispatch("keydown", { key: "Escape" });
  assert.deepEqual(closes, [], "送出中 Esc 與關閉都不動");
  resolve({ success: true, data: { tos: {} } });
  await pending;
  assert.deepEqual(calls.accept, [TERMS.version]);
  assert.deepEqual(closes, [{ accepted: true, version: TERMS.version }]);
});

test("accept 模式的提示跟著 gate：會擋才說「同意後即可再建立金鑰…」，不擋的改版不暗示有東西被擋", async () => {
  const blocking = "同意後即可再建立金鑰、轉換點數與重新啟用金鑰；不論是否同意，既有金鑰都照常可用。";
  const notBlocking = "這次改版不影響目前的操作；既有金鑰都照常可用。";
  for (const [value, hint] of [[true, blocking], [false, notBlocking], [undefined, notBlocking]]) {
    const { modal } = open({ mode: "accept", blocking: value });
    await mount(modal);
    assert.equal(modal.canAccept, true, String(value));
    assert.equal(modal.acceptHint, hint, `blocking=${value}`);
  }
});

test("accept 模式：送出前又改版（llm_tos_outdated）⇒ 顯示新版、不關閉，要再按一次", async () => {
  const v2 = { ...TERMS, version: "2026-11-1", title: "AI 模型服務條款", is_draft: false };
  const { modal, closes, calls } = open({
    mode: "accept",
    terms: [{ success: true, data: TERMS }, { success: true, data: v2 }],
    accept: (version) => (version === v2.version
      ? { success: true, data: { tos: {} } }
      : { success: false, code: "llm_tos_outdated", current_version: v2.version }),
  });
  await mount(modal);
  await modal.accept();
  assert.deepEqual(closes, []);
  assert.equal(calls.fetchTerms, 2);
  assert.equal(modal.state.version, v2.version);
  assert.equal(modal.state.isDraft, false);
  assert.equal(modal.state.actionError, "條款已更新，請重新同意");
  await modal.accept();
  assert.deepEqual(calls.accept, [TERMS.version, v2.version]);
  assert.deepEqual(closes, [{ accepted: true, version: v2.version }]);
});

test("accept 失敗（其他碼）：顯示碼表句子、不關閉", async () => {
  const { modal, closes } = open({
    mode: "accept",
    accept: () => ({ success: false, code: "llm_unavailable", error: "upstream host x" }),
  });
  await mount(modal);
  await modal.accept();
  assert.deepEqual(closes, []);
  assert.equal(modal.state.actionError, "AI 模型服務暫時無法連線，請稍後再試");
  assert.equal(modal.canAccept, true);
});

test("條文裡的連結：外部連結開新分頁（noopener）、# 連結擋下、mailto 照預設、條文外的點擊不管", async () => {
  const { modal } = open();
  await mount(modal);
  const content = { contains: (el) => el.inContent === true };
  globalThis.__llmRefs.content.el = content;
  const click = (href, inContent = true) => {
    const link = { inContent, href: href, getAttribute: () => href };
    const ev = { prevented: false, preventDefault() { this.prevented = true; },
      target: { closest: () => link } };
    modal.onContentClick(ev);
    return ev.prevented;
  };
  assert.equal(click("https://github.com/QuantumNous/new-api/tree/v1.0.0-rc.44"), true);
  assert.deepEqual(window.opened, [["https://github.com/QuantumNous/new-api/tree/v1.0.0-rc.44",
    "_blank", "noopener,noreferrer"]]);
  assert.equal(click("#/llm/keys"), true);
  assert.equal(click("javascript:alert(1)"), true);
  assert.equal(window.opened.length, 1, "只有 http(s) 會開分頁");
  assert.equal(click("mailto:support@woowtech.io"), false);
  assert.equal(click("https://example.test", false), false, "條文外的連結不管");
  const ev = { prevented: false, preventDefault() { this.prevented = true; }, target: { closest: () => null } };
  modal.onContentClick(ev);
  assert.equal(ev.prevented, false);
});

test("背景點擊只在點到背景本身時關閉；卸載後才回來的回應丟掉", async () => {
  const { modal, closes } = open();
  await mount(modal);
  const inner = {};
  modal.onBackdropClick({ target: inner, currentTarget: {} });
  assert.deepEqual(closes, []);
  const backdrop = {};
  modal.onBackdropClick({ target: backdrop, currentTarget: backdrop });
  assert.equal(closes.length, 1);

  let resolve;
  const late = open({ terms: [] });
  globalThis.__llmFakeService.fetchTerms = () => new Promise((r) => { resolve = r; });
  for (const fn of globalThis.__llmHooks.mounted) fn();
  for (const fn of globalThis.__llmHooks.unmount) fn();
  resolve({ success: true, data: TERMS });
  await settle();
  assert.equal(late.modal.state.phase, "loading", "卸載之後不寫 state");
  assert.deepEqual(globalThis.__llmSanitized, []);
});
