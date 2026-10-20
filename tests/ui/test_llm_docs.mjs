// LLM-15: docs rendering contract, safe examples, and one-time key lifecycle.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { readPaas, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

const DEMO_KEY = "one-time-demo-key";
const [modal, modalXml, docs, docsXml, scss, page, pageXml, routes, helpers, po] = await Promise.all([
  readPaas("components/llm/LlmKeyRevealModal.js"),
  readPaas("components/llm/LlmKeyRevealModal.xml"),
  readPaas("pages/llm/tabs/LlmDocsTab.js"),
  readPaas("pages/llm/tabs/LlmDocsTab.xml"),
  readPaas("pages/llm/tabs/LlmDocsTab.scss"),
  readPaas("pages/llm/LlmPage.js"),
  readPaas("pages/llm/LlmPage.xml"),
  readPaas("core/llm_routes.js"),
  readPaas("pages/llm/tabs/llm_docs_helpers.js"),
  readFile(new URL("../../src/i18n/zh_TW.po", import.meta.url), "utf8"),
]);
const helperModule = await import("data:text/javascript;base64," + Buffer.from(helpers).toString("base64"));
const { createLlmDocsKeyHandoff, validatedLlmPublicBaseUrl, publishedPaidModel, shellQuote } = helperModule;

function stripComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/<!-- [\s\S]*? -->/g, "");
}

function noPersistentSecret(name, source) {
  assert.doesNotMatch(source, /\b(?:localStorage|sessionStorage|indexedDB|cookieStore|document\.cookie)\b/i, name + " must not persist a key");
  assert.doesNotMatch(source, /\b(?:router\.navigate|location\.(?:href|hash|search|assign|replace))\b[^;\n]*\b(?:state\.key|initialKey)\b/, name + " must not put a key in navigation");
}

test("reveal modal replaces its own sentinel, closes, then hands off before replacing the hash", () => {
  const source = stripComments(modal);
  assert.match(source, /onOpenDocs\s*:\s*\{\s*type\s*:\s*Function\s*,\s*optional\s*:\s*true/);
  assert.match(source, /this\.state\.phase !== "shown" \|\| typeof this\.state\.key !== "string"/);
  assert.match(source, /const \{ woowLlmReveal, \.\.\.otherState \} = currentState;[\s\S]*window\.history\.replaceState\(otherState, "", window\.location\.href\)/);
  assert.match(source, /onOpenDocs\(key, kind, models\);[\s\S]*if \(ownSentinel\)[\s\S]*window\.location\.replace\("#\/llm\/docs"\)[\s\S]*else[\s\S]*router\.navigate\("llm\/docs"\)/);
  assert.match(source, /close\s*\(\s*\)\s*\{[\s\S]{0,500}forgetKey\(\)/);
  assert.match(modalXml, /t-on-click="openDocs"/);
});

test("page connects paid and free reveals and consumes the key in the docs component", () => {
  const source = stripComments(page);
  assert.match(pageXml, /<LlmOverviewTab\b[^>]*onOpenDocs\.bind="openDocsWithKey"/);
  assert.match(pageXml, /<LlmKeysTab\b[^>]*onOpenDocs\.bind="openDocsWithKey"/);
  assert.match(pageXml, /<LlmDocsTab\b[^>]*initialKey="docsKeyHandoff\.peekKey\(\)"[^>]*initialMode="docsKeyHandoff\.peekMode\(\)"[^>]*initialAllowedModels="docsKeyHandoff\.peekAllowedModels\(\)"[^>]*onConsumeKey\.bind="consumeDocsKey"/);
  assert.match(source, /openDocsWithKey\(key, kind, allowedModels\)[\s\S]{0,180}docsKeyHandoff\.put\(key,[\s\S]{0,120}allowedModels/);
  assert.match(source, /onWillUnmount\(\(\)\s*=>\s*this\.docsKeyHandoff\.clear\(\)\)/);
  assert.match(source, /setTab\(tab\)\s*\{[\s\S]{0,180}docsKeyHandoff\.clear\(\)/);
  assert.match(source, /onWillUpdateProps\(\(nextProps\)[\s\S]{0,150}normalizeLlmTab\(nextProps\.tab\) !== "docs"[\s\S]{0,100}docsKeyHandoff\.clear\(\)/);
  assert.match(routes, /LLM_LIVE_TABS\s*=\s*Object\.freeze\(\[[^\]]*"docs"/);

  const handoff = createLlmDocsKeyHandoff();
  handoff.put(DEMO_KEY, "free");
  assert.equal(handoff.peekKey(), DEMO_KEY);
  assert.equal(handoff.peekMode(), "free");
  assert.deepEqual(handoff.consume(), { key: DEMO_KEY, mode: "free", allowedModels: null });
  assert.equal(handoff.peekKey(), "");
  assert.equal(handoff.peekMode(), "paid");
  handoff.put(DEMO_KEY, "paid");
  handoff.put(DEMO_KEY, "paid", ["allowed-model"]);
  assert.deepEqual(handoff.peekAllowedModels(), ["allowed-model"]);
  handoff.clear(); // route leave or page unmount before the Docs component consumes it
  assert.equal(handoff.peekKey(), "");
  assert.equal(handoff.peekAllowedModels(), null);
});

test("Docs clears its local copy on unmount and never stores it in llmService", () => {
  assert.match(docs, /useState\(\{[\s\S]{0,100}key:\s*this\.props\.initialKey\s*\|\|\s*""/);
  assert.match(docs, /if\s*\(this\.props\.initialKey\)\s*this\.props\.onConsumeKey\?\.\(\)/);
  assert.match(docs, /onWillUnmount\(\(\)\s*=>\s*\{[\s\S]{0,160}this\.state\.key\s*=\s*""/);
  assert.doesNotMatch(docs, /llmService\.\w+\s*=\s*this\.state\.key/);
  for (const [name, source] of [["modal", modal], ["Docs JS", docs], ["Docs XML", docsXml], ["page", page], ["page XML", pageXml]]) {
    noPersistentSecret(name, source);
  }
});

test("reveal-to-Docs integration closes the guard, consumes locally, filters models, and clears on mode switch", async () => {
  const modalModule = await import(toDataUrl(stubImports(modal, {
    "@odoo/owl": "class Component {} const onMounted = () => {}; const onPatched = () => {}; const onWillUnmount = () => {}; const useRef = () => ({ el: null }); const useState = (value) => value;",
    "@web/core/l10n/translation": "const _t = (message) => message;",
    "../button/WoowButton": "const WoowButton = {};",
    "../icon/WoowIcon": "const WoowIcon = {};",
    "../../core/router": "const router = { navigate: (route) => globalThis.__fallbackRouteCalls.push(route), setLeaveGuard: () => () => {} };",
    "../../utils/clipboard": "const copyText = async () => true;",
    "../../utils/modal_focus": "const inertOutside = () => () => {}; const trapTab = () => {};",
    "../../services/llm_service": "const newClientRequestId = () => 'request-id';",
    "../../services/llm_errors": "const LLM_KEY_CREATE_INTERRUPTED = 'interrupted'; const llmErrorMessage = (result) => result.code;",
  })));
  const handoff = createLlmDocsKeyHandoff();
  const events = [];
  const originalDocument = globalThis.document;
  const originalWindow = globalThis.window;
  globalThis.document = { removeEventListener() {} };
  globalThis.window = {
    history: {
      state: { appState: "preserved", woowLlmReveal: "request-id" },
      replaceState(state, _title, href) {
        events.push("sentinel-cleanup");
        assert.equal(href, "https://example.test/woow#/llm/keys");
        this.state = state;
      },
      back() { events.push("history-back"); },
    },
    location: {
      href: "https://example.test/woow#/llm/keys",
      replace(hash) { events.push("navigate:" + hash); },
    },
    removeEventListener() {},
  };
  const reveal = Object.create(modalModule.LlmKeyRevealModal.prototype);
  reveal.props = {
    kind: "paid",
    models: ["allowed-paid"],
    onOpenDocs: (key, kind, models) => {
      events.push("handoff");
      handoff.put(key, kind === "free_trial" ? "free" : "paid", models);
    },
    onClose: () => events.push("close"),
  };
  reveal.state = { key: DEMO_KEY, phase: "shown", models: null, modelAllowlist: [] };
  reveal.closed = false;
  reveal.destroyed = false;
  reveal.clientRequestId = "request-id";
  reveal.sentinelPushed = true;
  reveal.removeLeaveGuard = () => events.push("guard-removed");
  reveal.openDocs();
  assert.deepEqual(events, ["sentinel-cleanup", "guard-removed", "close", "handoff", "navigate:#/llm/docs"]);
  assert.deepEqual(globalThis.window.history.state, { appState: "preserved" });
  assert.ok(!events.includes("history-back"));
  assert.equal(reveal.state.key, null);
  assert.equal(handoff.peekKey(), DEMO_KEY);

  const fallbackEvents = [];
  globalThis.__fallbackRouteCalls = [];
  globalThis.window.history.state = { appState: "preserved" };
  globalThis.window.history.replaceState = () => fallbackEvents.push("sentinel-cleanup");
  globalThis.window.history.back = () => fallbackEvents.push("history-back");
  const fallback = Object.create(modalModule.LlmKeyRevealModal.prototype);
  fallback.props = { kind: "paid", onOpenDocs: () => fallbackEvents.push("handoff"), onClose: () => fallbackEvents.push("close") };
  fallback.state = { key: DEMO_KEY, phase: "shown", models: null, modelAllowlist: [] };
  fallback.closed = false;
  fallback.destroyed = false;
  fallback.clientRequestId = "request-id";
  fallback.sentinelPushed = true;
  fallback.openDocs();
  assert.deepEqual(fallbackEvents, ["close", "handoff"]);
  assert.deepEqual(globalThis.__fallbackRouteCalls, ["llm/docs"]);
  assert.deepEqual(events.filter((event) => event.startsWith("navigate:")), ["navigate:#/llm/docs"]);
  globalThis.document = originalDocument;
  globalThis.window = originalWindow;

  globalThis.__docsUnmount = [];
  const docsModule = await import(toDataUrl(stubImports(docs, {
    "@odoo/owl": "class Component {} const onMounted = (fn) => { globalThis.__docsMounted = fn; }; const onWillUnmount = (fn) => globalThis.__docsUnmount.push(fn); const useState = (value) => value;",
    "@web/core/l10n/translation": "const _t = (message, values) => values ? message.replace(/%\\((\\w+)\\)s/g, (_, key) => String(values[key])) : message;",
    "../../../components/button/WoowButton": "const WoowButton = {};",
    "../../../components/card/WoowCard": "const WoowCard = {};",
    "../../../components/icon/WoowIcon": "const WoowIcon = {};",
    "../../../utils/clipboard": "const copyText = async () => true;",
    "../../../services/llm_service": "const llmService = { overviewLoaded: true, modelsLoaded: true, models: { state: 'ok', items: [{ model: 'first-paid', group: 'default' }, { model: 'allowed-paid', group: 'default' }] }, fetchState() {}, fetchModels() {} };",
    "./llm_docs_helpers": `const { publishedPaidModel, shellQuote } = await import("${toDataUrl(helpers)}");`,
  })));
  const guide = Object.create(docsModule.LlmDocsTab.prototype);
  guide.props = {
    initialKey: handoff.peekKey(), initialMode: handoff.peekMode(),
    initialAllowedModels: handoff.peekAllowedModels(), baseUrl: "https://newapi.woowtech.io/v1",
    onConsumeKey: () => handoff.clear(),
  };
  guide.setup();
  assert.equal(guide.state.key, DEMO_KEY);
  assert.equal(guide.selectedModel, "allowed-paid");
  assert.equal(handoff.peekKey(), "");
  guide.selectMode("free");
  assert.equal(guide.state.key, "");
  assert.equal(guide.model, "qwen3-4b-cluster1");
  assert.equal(guide.apiKey, "YOUR_API_KEY");
  globalThis.__docsUnmount.forEach((cleanup) => cleanup());
  assert.equal(guide.state.key, "");
});

test("Base URL comes only from the configured public endpoint and paid IDs come from the catalog", () => {
  assert.equal(validatedLlmPublicBaseUrl({ public_base_url: "https://newapi.woowtech.io/v1" }), "https://newapi.woowtech.io/v1");
  assert.equal(validatedLlmPublicBaseUrl({ public_base_url: "https://newapi-stg.woowtech.io/v1" }), "https://newapi-stg.woowtech.io/v1");
  for (const public_base_url of [
    "http://newapi.woowtech.io/v1", "https://evil.example/v1", "https://newapi.woowtech.io/v1/",
    "https://newapi.woowtech.io/v1?x=1", "https://user@newapi.woowtech.io/v1", "https://newapi.woowtech.io:444/v1",
  ]) assert.equal(validatedLlmPublicBaseUrl({ public_base_url }), null);
  assert.equal(validatedLlmPublicBaseUrl({ public_base_url: "https://newapi.woowtech.io/v1" }), validatedLlmPublicBaseUrl({ public_base_url: "https://newapi.woowtech.io/v1" }));
  assert.equal(publishedPaidModel({ state: "ok", items: [{ model: "published-model", group: "default" }] }), "published-model");
  assert.equal(publishedPaidModel({ state: "ok", items: [
    { model: "first-paid", group: "default" }, { model: "allowed-paid", group: "default" },
  ] }, ["allowed-paid"]), "allowed-paid");
  assert.equal(publishedPaidModel({ state: "ok", items: [{ model: "not-allowed", group: "default" }] }, ["allowed-paid"]), null);
  assert.equal(publishedPaidModel({ state: "ok", items: [{ model: "free-only", group: "free" }] }), null);
  assert.equal(publishedPaidModel({ state: "maintenance", items: [{ model: "stale-model", group: "default" }] }), null);
  assert.match(page, /validatedLlmPublicBaseUrl\(this\.llm\.overview\)/);
  assert.match(docs, /publishedPaidModel\(this\.llm\.models, this\.props\.initialAllowedModels\)/);
  assert.match(docs, /YOUR_PUBLISHED_PAID_MODEL/);
  assert.doesNotMatch(docs, /gpt-4o-mini/);
});

test("paid and free curl, Python, and Node examples use escaped values and the correct models", () => {
  const quote = shellQuote("{\"content\":\"it's\"}");
  assert.equal(quote, "'{\"content\":\"it'\\''s\"}'");
  assert.match(docs, /-d \$\{shellQuote\(body\)\}/);
  assert.match(docs, /JSON\.stringify\(key\)/);
  assert.match(docs, /JSON\.stringify\(base\)/);
  assert.match(docs, /JSON\.stringify\(model\)/);
  assert.match(docs, /this\.state\.mode === "free" \? "qwen3-4b-cluster1"/);
  assert.match(docs, /get selectedModel\(\) \{ return publishedPaidModel\(this\.llm\.models, this\.props\.initialAllowedModels\); \}/);
  assert.match(docs, /this\.selectedModel \|\| "YOUR_PUBLISHED_PAID_MODEL"/);
  assert.match(docs, /python: `from openai import OpenAI/);
  assert.match(docs, /node: `import OpenAI from "openai"/);
  assert.match(docs, /tools\(\)[\s\S]{0,400}id: "curl"[\s\S]{0,220}id: "python"[\s\S]{0,220}id: "node"[\s\S]{0,260}id: "n8n"[\s\S]{0,220}id: "webui"[\s\S]{0,240}id: "opencode"/);
  assert.match(docsXml, /state\.mode === 'paid'/);
  assert.match(docsXml, /state\.mode === 'free'/);
});

test("guide includes approved limits, FAQ, billing note, AGPL source, and mobile overflow", () => {
  for (const phrase of [
    "Paid keys allow up to 120 successful requests per minute",
    "Free trial keys allow up to 10 requests per minute",
    "The 120 successful-request ceiling is shared across paid and free traffic.",
    "Free requests use a token bucket of 10. One token is refilled every six seconds, and failed requests count.",
    "The approximate daily free allowance is 200 successful requests.",
    "Key creation and credit conversion have a 10-per-hour limit after the request reaches new-api",
    "All key-creation and conversion attempts also have a separate 30-per-hour guard",
    "Converted AI credit is non-refundable, including when closing the account.",
    "Cloudflare timed out while waiting for the upstream request",
  ]) assert.ok(docs.includes(phrase), "missing guide fact: " + phrase);
  for (const status of ["401", "403", "429", "503", "524"]) assert.match(docsXml, new RegExp(`<h3>${status}<\\/h3>`));
  for (const phrase of ["無效的令牌", "Invalid token", "Accept-Language is en", "insufficient AI credit", "lacks permission for the requested model"]) assert.ok(docs.includes(phrase));
  assert.match(docsXml, /https:\/\/github\.com\/QuantumNous\/new-api\/tree\/v1\.0\.0-rc\.44/);
  assert.match(docs, /AGPL-3\.0/);
  assert.doesNotMatch(docsXml, /Claude\s+Code/i);
  assert.match(scss, /overflow-x:\s*auto/);
  assert.match(scss, /@media\s*\(max-width:\s*400px\)/);
  assert.match(docsXml, /class="o_woow_llm_docs__tabs"/);
});

test("API guide PO entries are marked for Odoo JavaScript translation", () => {
  const guidePo = po.slice(po.indexOf("# LLM-15 使用說明頁"), po.indexOf("# 🔴 跨模組覆蓋"));
  assert.ok(guidePo.length > 0);
  assert.equal((guidePo.match(/#\. odoo-javascript\n(?:#:.*\n)*msgid /g) || []).length, (guidePo.match(/^msgid /gm) || []).length);
  assert.match(guidePo, /msgid "View usage guide"\nmsgstr "查看使用說明"/);
});

 test("contract test uses only a harmless demo credential", async () => {
  const source = await readFile(new URL(import.meta.url), "utf8");
  assert.ok(source.includes(DEMO_KEY));
  assert.doesNotMatch(source, /sk-[A-Za-z0-9_-]{20,}/);
});
