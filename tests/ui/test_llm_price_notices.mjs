import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { readPaas, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

const serviceSource = stubImports(await readPaas("services/llm_service.js"), {
  "@odoo/owl": "const reactive = x => x;",
  "./rpc": "const jsonRpc = (...args) => globalThis.__noticeRpc(...args);",
  "./llm_errors": "const LLM_KEY_CREATE_INTERRUPTED='interrupted', LLM_NETWORK_ERROR='network', LLM_SESSION_EXPIRED='expired';",
});
const { llmService, priceNoticeDtos } = await import(toDataUrl(serviceSource));
const notice = () => ({
  schema: 1, id: "pc_" + "a".repeat(32), title: '<img src=x onerror="alert(1)">',
  body_text: '<script>alert(1)</script>\n<a href="javascript:alert(1)">link</a>\n' + "x".repeat(300),
  not_before: 1800000000, timezone: "Asia/Taipei", unit: "points_per_million_tokens_or_per_call",
  changes: [{ model: "model/a", mode: "fixed", before: { per_call: "1" }, after: { per_call: "2" } }],
});
const hooks = { mounted: [], unmount: [], update: [] };
globalThis.__noticeHooks = hooks;
globalThis.__noticeService = llmService;
const replacements = {
  "@odoo/owl": "class Component {} const onMounted=f=>globalThis.__noticeHooks.mounted.push(f); const onWillUnmount=f=>globalThis.__noticeHooks.unmount.push(f); const onWillUpdateProps=f=>globalThis.__noticeHooks.update.push(f); const onPatched=()=>{}; const useRef=()=>({el:null}); const useState=x=>x;",
  "@web/core/l10n/translation": "const _t=x=>x;",
  "../../components/icon/WoowIcon": "const WoowIcon={};",
  "../../core/router": "const router={navigate(){}};",
  "../../core/llm_routes": "const LLM_TABS=['overview','keys','usage','models','credit','docs']; const isLlmTabLive=()=>true; const llmTabPath=x=>'llm/'+x; const normalizeLlmTab=x=>x||'overview';",
  "../../services/llm_service": "const llmService=globalThis.__noticeService;",
  "../../services/llm_errors": "const llmErrorMessage=x=>x.code;",
  "./tabs/llm_docs_helpers": "const createLlmDocsKeyHandoff=()=>({clear(){}}); const validatedLlmPublicBaseUrl=()=>'';",
};
for (const name of ["Credit", "Models", "Overview", "Keys", "Docs", "Usage"]) {
  replacements[`./tabs/Llm${name}Tab`] = `class Llm${name}Tab {}`;
}
const originalPage = await readPaas("pages/llm/LlmPage.js");
const pageSource = stubImports(originalPage, replacements);
const { LlmPage } = await import(toDataUrl(pageSource));
const listeners = new Map();
globalThis.window = {
  addEventListener: (name, fn) => listeners.set(name, fn),
  removeEventListener: (name, fn) => { if (listeners.get(name) === fn) listeners.delete(name); },
};
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const tick = () => new Promise(r => setImmediate(r));
function page(tab = "overview") {
  const instance = new LlmPage(); instance.props = { tab }; instance.setup();
  hooks.mounted.at(-1)(); return instance;
}

test("service uses exact empty route contract and drops all private metadata at every level", async () => {
  const dto = notice(); dto.recipients = ["private"]; dto.approval_ref = "private";
  dto.changes[0].recipient = "private"; dto.changes[0].before.secret = "private";
  globalThis.__noticeRpc = async (url, params) => {
    assert.equal(url, "/api/llm/price-notices"); assert.deepEqual(params, {}); return { notices: [dto] };
  };
  const result = await llmService.fetchPriceNotices();
  assert.deepEqual(Object.keys(result.data.notices[0]).sort(), ["body_text", "changes", "id", "not_before", "title"]);
  assert.doesNotMatch(JSON.stringify(result), /private|approval|recipients|timezone|secret/);
  globalThis.__noticeRpc = async () => ({ notices: [] });
  assert.deepEqual(await llmService.fetchPriceNotices(), { success: true, data: { notices: [] } });
  globalThis.__noticeRpc = async () => { throw Error("private failure"); };
  assert.deepEqual(await llmService.fetchPriceNotices(), { success: false, code: "network" });
});

test("malformed and oversized DTOs fail closed; valid canonical schema and version survive", () => {
  for (const patch of [{ title: {} }, { body_text: "x".repeat(65537) }, { not_before: "1800000000" },
    { changes: [] }, { changes: [{ model: "a", mode: "fixed", before: { per_call: "NaN" } }] }]) {
    assert.deepEqual(priceNoticeDtos([{ ...notice(), ...patch }]), []);
  }
  assert.deepEqual(priceNoticeDtos(Array.from({ length: 11 }, notice)), []);
  assert.deepEqual(priceNoticeDtos({ notices: [notice()] }), []);
  assert.equal(priceNoticeDtos([notice(), notice()]).length, 1);
  assert.equal(priceNoticeDtos([{ ...notice(), version: "v1" }])[0].version, "v1");
});

test("direct entry to every tab fetches the shared banner; focus clears immediately and withdrawal wins over old responses", async () => {
  for (const tab of ["overview", "keys", "usage", "models", "credit", "docs"]) {
    globalThis.__noticeRpc = async () => ({ notices: [notice()] });
    const instance = page(tab); await tick();
    assert.equal(instance.priceNotices.items.length, 1, tab);
    const older = deferred(); globalThis.__noticeRpc = () => older.promise;
    const oldFetch = listeners.get("focus")();
    assert.deepEqual(instance.priceNotices.items, []);
    globalThis.__noticeRpc = async () => ({ notices: [] });
    await listeners.get("focus")();
    older.resolve({ notices: [notice()] }); await oldFetch;
    assert.deepEqual(instance.priceNotices.items, [], tab);
    hooks.unmount.at(-1)();
    assert.equal(listeners.has("focus"), false);
  }
});

test("tab update, failure, unmount and page reentry cannot restore withdrawn notices", async () => {
  globalThis.__noticeRpc = async () => ({ notices: [notice()] });
  const first = page(); await tick();
  const pending = deferred(); globalThis.__noticeRpc = () => pending.promise;
  hooks.update.at(-1)({ tab: "docs" });
  assert.deepEqual(first.priceNotices.items, []);
  hooks.unmount.at(-1)();
  globalThis.__noticeRpc = async () => { throw Error("unavailable"); };
  const second = page("docs"); await tick();
  pending.resolve({ notices: [notice()] }); await tick();
  assert.deepEqual(first.priceNotices.items, []);
  assert.deepEqual(second.priceNotices.items, []);
  hooks.unmount.at(-1)();
});

// Optional local runtime gate: no downloads, servers, Odoo or network requests.
// LLM_NOTICE_OWL points to owl.js; LLM_NOTICE_PLAYWRIGHT points to installed Playwright.
test("real OWL renders hostile DTOs as text above all tabs and removes empty banners", {
  skip: !process.env.LLM_NOTICE_OWL || !process.env.LLM_NOTICE_PLAYWRIGHT,
}, async () => {
  const require = createRequire(import.meta.url);
  const { chromium } = require(process.env.LLM_NOTICE_PLAYWRIGHT);
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  try {
    const view = await browser.newPage();
    const errors = []; view.on("pageerror", e => errors.push(e.message));
    await view.route("**/*", route => route.abort());
    await view.setContent('<main id="app"></main>');
    await view.addScriptTag({ content: await readFile(process.env.LLM_NOTICE_OWL, "utf8") });
    const browserReplacements = { ...replacements,
      "@odoo/owl": "const {Component,onMounted,onPatched,onWillUnmount,onWillUpdateProps,useRef,useState}=owl;",
      "../../components/icon/WoowIcon": `class WoowIcon extends owl.Component { static template=owl.xml\`<span/>\`; static props=['name','size']; }`,
      "./tabs/llm_docs_helpers": "const createLlmDocsKeyHandoff=()=>({clear(){},peekKey(){},peekMode(){},peekAllowedModels(){}}); const validatedLlmPublicBaseUrl=()=>'';",
    };
    for (const name of ["Credit", "Models", "Overview", "Keys", "Docs", "Usage"]) {
      browserReplacements[`./tabs/Llm${name}Tab`] = `class Llm${name}Tab extends owl.Component { static template=owl.xml\`<div data-tab="${name}"/>\`; static props=['*']; }`;
    }
    const script = stubImports(originalPage, browserReplacements).replace("export class LlmPage", "class LlmPage");
    await view.evaluate(({ script, template, dto }) => {
      window.__noticeService = { fetchPriceNotices: async () => ({ success: true, data: { notices: [dto] } }) };
      window.__noticeTemplate = template;
      // The source is local code; untrusted announcement fields only enter reactive state.
      window.__noticePageClass = new Function(script + "; return LlmPage;")();
    }, { script, template: await readPaas("pages/llm/LlmPage.xml"), dto: priceNoticeDtos([notice()])[0] });
    for (const tab of ["overview", "keys", "usage", "models", "credit", "docs"]) {
      await view.evaluate(async tab => {
        window.__app = new owl.App(window.__noticePageClass, { templates: window.__noticeTemplate, props: { tab } });
        window.__instance = await window.__app.mount(document.querySelector("#app"));
      }, tab);
      const banner = view.locator(".o_woow_llm_price_notice");
      await banner.waitFor();
      assert.equal(await banner.locator("h2").textContent(), notice().title);
      assert.equal(await banner.locator("p").textContent(), notice().body_text);
      assert.equal(await banner.locator("script,img,p a").count(), 0);
      assert.equal(await banner.locator("a").getAttribute("href"), "#/llm/models");
      assert.equal(await banner.evaluate(el => !!(el.compareDocumentPosition(document.querySelector("nav")) & Node.DOCUMENT_POSITION_FOLLOWING)), true);
      await view.evaluate(async () => {
        window.__noticeService.fetchPriceNotices = async () => ({ success: true, data: { notices: [] } });
        await window.__instance.refreshPriceNotices();
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      });
      assert.equal(await banner.count(), 0, "withdrawal removes the entire banner surface");
      await view.evaluate(() => window.__app.destroy());
      await view.evaluate(dto => {
        window.__noticeService.fetchPriceNotices = async () => ({ success: true, data: { notices: [dto] } });
      }, priceNoticeDtos([notice()])[0]);
    }
    for (const fail of [false, true]) {
      await view.evaluate(async fail => {
        window.__noticeService.fetchPriceNotices = async () => {
          if (fail) throw Error("unavailable");
          return { success: true, data: { notices: [] } };
        };
        window.__app = new owl.App(window.__noticePageClass, { templates: window.__noticeTemplate, props: { tab: "docs" } });
        await window.__app.mount(document.querySelector("#app"));
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      }, fail);
      assert.equal(await view.locator(".o_woow_llm_price_notice").count(), 0);
      assert.equal(await view.locator('[data-tab="Docs"]').count(), 1);
      await view.evaluate(() => window.__app.destroy());
    }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
