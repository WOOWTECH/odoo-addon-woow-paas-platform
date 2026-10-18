// X-LC-002-3、X-LC-006-3～7：開通表單錯誤定位、複製回饋、390px 徽章、觸控目標、發票麵包屑。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { readPaas, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

const formFeedback = await import(toDataUrl(stubImports(await readPaas("utils/form_feedback.js"), {
  "@odoo/owl": "const useEffect = () => {}; const useRef = () => ({ el: null });",
})));

// ---- 最小的假 DOM：只實作 revealFirstError 用到的介面 -------------------------------
class FakeEl {
  constructor(classes, { tag = "div", children = [] } = {}) {
    this.classes = new Set(classes);
    this.tag = tag;
    this.children = children;
    this.parent = null;
    this.attrs = {};
    this.log = [];
    for (const c of children) c.parent = this;
  }
  matches(selector) {
    return selector.split(",").map((s) => s.trim()).some((s) => {
      if (s.startsWith(".")) return this.classes.has(s.slice(1));
      return s.split(":")[0] === this.tag && !this.attrs.disabled;
    });
  }
  *walk() {
    for (const c of this.children) {
      yield c;
      yield* c.walk();
    }
  }
  querySelector(selector) {
    for (const el of this.walk()) if (el.matches(selector)) return el;
    return null;
  }
  closest(selector) {
    for (let el = this; el; el = el.parent) if (el.matches(selector)) return el;
    return null;
  }
  hasAttribute(name) { return name in this.attrs; }
  setAttribute(name, value) { this.attrs[name] = value; }
  scrollIntoView(opts) { this.log.push(["scroll", opts]); }
  focus(opts) { this.log.push(["focus", opts]); }
}

test("X-LC-006-3：前端驗證失敗 → 捲到第一個錯誤欄位並把焦點放進輸入框", () => {
  const nameInput = new FakeEl([], { tag: "input" });
  const nameGroup = new FakeEl(["o_woow_form_group", "o_woow_has_error"], {
    children: [nameInput, new FakeEl(["o_woow_field_error"], { tag: "p" })],
  });
  const emailInput = new FakeEl([], { tag: "input" });
  const emailGroup = new FakeEl(["o_woow_form_group", "o_woow_has_error"], { children: [emailInput] });
  const root = new FakeEl(["o_woow_configure_page"], {
    children: [new FakeEl(["o_woow_section"], { children: [nameGroup, emailGroup] })],
  });
  const focused = formFeedback.revealFirstError(root, formFeedback.FIELD_ERROR_SELECTOR);
  assert.equal(focused, nameInput, "第一個錯誤（服務名稱）");
  assert.deepEqual(nameGroup.log, [["scroll", { block: "center", behavior: "smooth" }]]);
  assert.deepEqual(nameInput.log, [["focus", { preventScroll: true }]]);
  assert.deepEqual(emailInput.log, []);
});

test("X-LC-002-3：後端拒絕 → 捲到頁尾錯誤橫幅並聚焦（沒有輸入框就讓橫幅本身可聚焦）", () => {
  const banner = new FakeEl(["o_woow_error_message", "o_woow_launch_error"]);
  const plansError = new FakeEl(["o_woow_error_message"]);
  const root = new FakeEl([], { children: [plansError, banner] });
  const focused = formFeedback.revealFirstError(root, formFeedback.LAUNCH_ERROR_SELECTOR);
  assert.equal(focused, banner, "只找後端錯誤橫幅，不會跑去方案錯誤");
  assert.equal(banner.attrs.tabindex, "-1");
  assert.deepEqual(banner.log.map((x) => x[0]), ["scroll", "focus"]);
  assert.equal(formFeedback.revealFirstError(new FakeEl([]), formFeedback.LAUNCH_ERROR_SELECTOR), null);
  assert.equal(formFeedback.revealFirstError(null, formFeedback.LAUNCH_ERROR_SELECTOR), null);
});

test("X-LC-002-3／X-LC-006-3：開通表單接上 useFormErrorReveal（只在送出失敗時觸發）", async () => {
  const js = await readPaas("pages/configure/AppConfigurationPage.js");
  const xml = await readPaas("pages/configure/AppConfigurationPage.xml");
  assert.match(js, /useFormErrorReveal\(\s*"configureRoot",\s*\(\) => this\.state\.errorRevealToken,\s*\(\) => this\.state\.launchError,?\s*\)/);
  assert.match(js, /errorRevealToken: 0/);
  const validate = js.slice(js.indexOf("validateForm() {"), js.indexOf("onPlanSelect("));
  assert.match(validate, /if \(!valid\) \{\s*this\.state\.errorRevealToken\+\+;/, "驗證失敗才遞增");
  assert.doesNotMatch(js.slice(js.indexOf("clearError"), js.indexOf("validateForm() {")), /errorRevealToken/,
    "邊打字邊清錯誤時不觸發捲動");
  assert.match(xml, /<div class="o_woow_configure_page" t-ref="configureRoot">/);
  assert.match(xml, /class="o_woow_error_message o_woow_launch_error" role="alert" tabindex="-1"/);
});

test("X-LC-006-4：每顆複製鈕旁都有看得到、讀屏會播報的回饋（成功／失敗）", async () => {
  const xml = await readPaas("pages/service/tabs/OverviewTab.xml");
  const keys = [...xml.matchAll(/class="o_woow_copy_btn" t-on-click="\(\) => this\.copyToClipboard\([^,]+, '(\w+)'\)"/g)]
    .map((m) => m[1]);
  assert.deepEqual(keys, ["url", "tunnel", "domain"]);
  for (const key of keys) {
    const re = new RegExp(
      `<span class="o_woow_copy_feedback" role="status" aria-live="polite">` +
      `<t t-if="copyState\\.copied === '${key}'">Copied\\.</t>` +
      `<t t-elif="copyState\\.failed === '${key}'">Copy failed\\. Please select the text and copy it manually\\.</t></span>`);
    assert.match(xml, re, key);
  }
  const po = await readFile(new URL("../../src/i18n/zh_TW.po", import.meta.url), "utf8");
  assert.match(po, /#\. odoo-javascript\n(?:#:.*\n)+msgid "Copied\."\nmsgstr "已複製"/, "沿用既有譯文");
});

test("X-LC-006-5：工作區卡片徽章不縮、不換行（名稱才縮）", async () => {
  const scss = await readFile(new URL("../../src/static/src/paas/styles/pages/20_workspace.scss", import.meta.url), "utf8");
  const header = scss.slice(scss.indexOf(".o_woow_workspace_item_header {"), scss.indexOf(".o_woow_workspace_item_desc"));
  assert.match(header, /\.o_woow_badge \{\s*flex-shrink: 0;\s*white-space: nowrap;/);
  assert.match(header, /min-width: 0;/);
});

test("X-LC-006-7：手機版連結、麵包屑、複製鈕至少 44px 高，並排控制項有間距", async () => {
  const scss = await readFile(
    new URL("../../src/static/src/paas/styles/components/_touch_controls.scss", import.meta.url), "utf8");
  const block = scss.slice(scss.indexOf("@media (max-width: 768px)"));
  for (const sel of [".o_woow_breadcrumb_link", ".o_woow_billing_view_history_link",
    ".o_woow_billing_view_all_link", ".o_woow_billing_support_item a", ".o_woow_info_value a"]) {
    assert.ok(block.includes(sel), sel);
  }
  assert.match(block, /min-height: 44px;/);
  assert.match(block, /\.o_woow_copy_btn \{[^}]*min-width: 44px;[^}]*min-height: 44px;/);
  assert.match(block, /\.o_woow_billing_sub_header_actions \{[^}]*gap: 0\.75rem;/);
  assert.match(block, /^\s*\.o_woow_content \{/m, "多一層祖先，蓋過之後載入的各頁 scss");
});

test("X-LC-006-6：發票詳情麵包屑不再寫死英文 'Detail'", async () => {
  const xml = await readPaas("pages/billing/InvoiceDetailPage.xml");
  const js = await readPaas("pages/billing/InvoiceDetailPage.js");
  assert.doesNotMatch(xml, /'Detail'/);
  assert.match(xml, /t-esc="breadcrumbLabel"/);
  assert.match(js, /get breadcrumbLabel\(\) \{[\s\S]*?_t\("Invoice Detail"\)/);
  const po = await readFile(new URL("../../src/i18n/zh_TW.po", import.meta.url), "utf8");
  assert.match(po, /#\. odoo-javascript\n(?:#:.*\n)+msgid "Invoice Detail"\nmsgstr "發票詳情"/);
});
