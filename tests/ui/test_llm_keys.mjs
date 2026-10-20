// LLM-07 paid key management: import real source with the repository's import-stub convention.
import assert from "node:assert/strict";
import test from "node:test";
import { readPaas, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

const owlStub = "class Component {} const onMounted = (fn) => { globalThis.__keyHooks.mounted.push(fn); }; const onWillUnmount = (fn) => { globalThis.__keyHooks.unmount.push(fn); }; const useState = (value) => value;";
globalThis.__keyHooks = { mounted: [], unmount: [] };

const routesSource = await readPaas("core/llm_routes.js");
const routes = await import(toDataUrl(routesSource));

globalThis.__llmKeyCalls = [];
const errorsSource = await readPaas("services/llm_errors.js");
const errorsUrl = toDataUrl(stubImports(errorsSource, {
  "@web/core/l10n/translation": "const _t = (message, values) => values ? message.replace(/%\\((\\w+)\\)s/g, (_, k) => String(values[k])) : message;",
}));
const serviceSource = stubImports(await readPaas("services/llm_service.js"), {
  "@odoo/owl": "const reactive = (value) => value;",
  "./rpc": "const jsonRpc = (...args) => { globalThis.__llmKeyCalls.push(args); return Promise.resolve({ success: true, data: { id: 8 } }); };",
  "./llm_errors": `const { LLM_KEY_CREATE_INTERRUPTED, LLM_NETWORK_ERROR, LLM_SESSION_EXPIRED } = await import("${errorsUrl}");`,
});
const serviceUrl = toDataUrl(serviceSource);
const { llmService, KEY_CREATE_TIMEOUT_MS } = await import(serviceUrl);

const revealModalSource = stubImports(await readPaas("components/llm/LlmKeyRevealModal.js"), {
  "@odoo/owl": "class Component {} const useState = (value) => value; const useRef = () => ({ el: null }); const onMounted = () => {}; const onPatched = () => {}; const onWillUnmount = () => {};",
  "@web/core/l10n/translation": "const _t = (message) => message;",
  "../button/WoowButton": "const WoowButton = {};",
  "../icon/WoowIcon": "const WoowIcon = {};",
  "../../core/router": "const router = { setLeaveGuard: () => () => {} };",
  "../../utils/clipboard": "const copyText = async () => true;",
  "../../utils/modal_focus": "const inertOutside = () => () => {}; const trapTab = () => {};",
  "../../services/llm_service": "const newClientRequestId = () => 'request-paid-id';",
  "../../services/llm_errors": "const LLM_KEY_CREATE_INTERRUPTED = 'interrupted'; const llmErrorMessage = (result) => result.code;",
});
const { LlmKeyRevealModal: PaidRevealModal } = await import(toDataUrl(revealModalSource));

const modelsSource = stubImports(await readPaas("pages/llm/tabs/LlmModelsTab.js"), {
  "@odoo/owl": owlStub,
  "@web/core/l10n/translation": "const _t = (message) => message;",
  "../../../services/llm_service": `const llmService = (await import("${serviceUrl}")).llmService; const newClientRequestId = () => 'request-paid-id';`,
  "../../../utils/clipboard": "const copyText = async () => true;",
  "../../../utils/llm_format": "const formatTaipeiTime = (value) => value;",
});
const modelsUrl = toDataUrl(modelsSource);
const keyTabSource = stubImports(await readPaas("pages/llm/tabs/LlmKeysTab.js"), {
  "@odoo/owl": "class Component {} const onMounted = () => {}; const onWillUnmount = () => {}; const useState = (value) => value;",
  "@web/core/l10n/translation": "const _t = (message, values) => values ? message.replace(/%\\((\\w+)\\)s/g, (_, k) => String(values[k])) : message;",
  "../../../components/icon/WoowIcon": "const WoowIcon = {};",
  "../../../components/button/WoowButton": "const WoowButton = {};",
  "../../../services/llm_service": `const llmService = (await import("${serviceUrl}")).llmService;`,
  "../../../utils/llm_format": "const formatTaipeiDateTime = (value) => value || '—'; const isExpiringSoon = () => false;",
  "../../../services/llm_errors": `const llmErrorMessage = (result) => result.code;`,
  "../../../components/llm/LlmKeyFormModal": "const LlmKeyFormModal = {};",
  "../../../components/llm/LlmKeyRevealModal": "const LlmKeyRevealModal = {};",
});
const keyTabUrl = toDataUrl(keyTabSource);
const { LlmKeysTab, parseExtendId, applyExtendQueryOnce, extendQueryFromHash, stripExtendFromHash, paidKeyModels, keyRow } = await import(keyTabUrl);
const formSource = stubImports(await readPaas("components/llm/LlmKeyFormModal.js"), {
  "@odoo/owl": "class Component {} const useRef = () => ({ el: null }); const useState = (value) => value; const onMounted = () => {}; const onWillUnmount = () => {};",
  "@web/core/l10n/translation": "const _t = (message, values) => values ? message.replace(/%\\((\\w+)\\)s/g, (_, k) => String(values[k])) : message;",
  "../icon/WoowIcon": "const WoowIcon = {};",
  "../button/WoowButton": "const WoowButton = {};",
  "../../services/llm_service": `const llmService = (await import("${serviceUrl}")).llmService;`,
  "../../utils/modal_focus": "const inertOutside = () => () => {}; const trapTab = () => {};",
});
const { LlmKeyFormModal, validatePaidDraft, normalizePaidDraft } = await import(toDataUrl(formSource));

test("createPaidKey sends the closed paid payload through sanitized call with create deadline", async () => {
  globalThis.__llmKeyCalls.length = 0;
  const values = { name: "Production", limitPoints: null, expires: "never", models: ["model-a"] };
  await llmService.createPaidKey("request-id", values);
  assert.deepEqual(globalThis.__llmKeyCalls, [["/api/llm/keys/create", {
    client_request_id: "request-id", kind: "paid", name: "Production", limit_points: null,
    expires: "never", models: ["model-a"],
  }, KEY_CREATE_TIMEOUT_MS]]);
  assert.equal(KEY_CREATE_TIMEOUT_MS, 40000);
});

test("paid key handoff keeps only safe draft in parent and never caches plaintext", async () => {
  globalThis.__llmKeyCalls.length = 0;
  const secret = "sk-" + "PaidTest".repeat(5);
  llmService.keys = [];
  llmService.createPaidKey = async (requestId, draft) => {
    assert.equal(requestId, "request-paid-id");
    assert.deepEqual(draft, { name: "Ops", limitPoints: null, expires: "90d", models: ["paid-a"] });
    return { success: true, data: { key: secret, masked_key: "sk-Paid…Test" } };
  };
  const tab = Object.create(LlmKeysTab.prototype);
  tab.service = llmService;
  tab.state = { loading: false, showForm: true, showReveal: false, pendingDraft: null, extendConfirmation: null, notice: null };
  tab.destroyed = false;
  tab.onDraft({ name: "Ops", limitPoints: null, expires: "90d", models: ["paid-a"] });
  assert.equal(tab.state.showForm, false);
  assert.equal(tab.state.showReveal, true);
  assert.deepEqual(tab.state.pendingDraft, { name: "Ops", limitPoints: null, expires: "90d", models: ["paid-a"] });
  assert.ok(!JSON.stringify(tab.state).includes(secret));
  const result = await tab.createPendingPaidKey("request-paid-id");
  assert.equal(result.data.key, secret);
  assert.ok(!Object.hasOwn(llmService.keys, "key"));
  assert.ok(!JSON.stringify(llmService.keys).includes(secret));
  assert.ok(!JSON.stringify(tab.state).includes(secret));
  const reveal = Object.create(PaidRevealModal.prototype);
  reveal.props = { kind: "paid", allowedModels: ["paid-a", "paid-b"], models: ["paid-a", "free-a"] };
  reveal.state = { models: [...reveal.props.models], modelAllowlist: [...reveal.props.allowedModels] };
  assert.deepEqual(reveal.displayedModels, ["paid-a"]);
  reveal.state.models = null;
  assert.equal(reveal.paidModelsText, "All paid models");
  assert.ok(!reveal.paidModelsText.includes("qwen3-4b-cluster1"));
  reveal.state.key = secret;
  reveal.forgetKey();
  assert.equal(reveal.state.key, null);
  assert.equal(reveal.state.modelAllowlist.length, 0);
  tab.onRevealClosed({ created: true });
  assert.equal(tab.state.pendingDraft, null);
  assert.equal(tab.state.showReveal, false);
  assert.ok(!JSON.stringify(tab.state).includes(secret));
});

test("paid draft errors exposed to users are mapped, never raw transport text", () => {
  const raw = "socket exploded secret-value";
  const tab = Object.create(LlmKeysTab.prototype);
  tab.service = { keys: [], keysError: "llm_network_error" };
  tab.state = { createError: null };
  assert.ok(!tab.errorText.includes(raw));
  tab.setCreateError({ success: false, code: "llm_network_error", error: raw });
  assert.ok(!tab.state.createError.includes(raw));
});

test("keys route goes live only with LlmPage registration and rendered component", async () => {
  assert.ok(routes.LLM_LIVE_TABS.includes("keys"));
  assert.equal(routes.isLlmTabLive("keys"), true);
  const page = stubImports(await readPaas("pages/llm/LlmPage.js"), {
    "@odoo/owl": "class Component {} const onMounted = () => {}; const onPatched = () => {}; const useRef = () => ({ el: null });",
    "@web/core/l10n/translation": "const _t = (message) => message;",
    "../../components/icon/WoowIcon": "const WoowIcon = {};",
    "../../core/router": "const router = { navigate() {} };",
    "../../core/llm_routes": `const { LLM_TABS, isLlmTabLive, llmTabPath, normalizeLlmTab } = await import("${toDataUrl(routesSource)}");`,
    "./tabs/LlmCreditTab": "const LlmCreditTab = {};",
    "./tabs/LlmModelsTab": `const LlmModelsTab = (await import("${modelsUrl}")).LlmModelsTab;`,
    "./tabs/LlmOverviewTab": "const LlmOverviewTab = {};",
    "./tabs/LlmKeysTab": `const LlmKeysTab = (await import("${keyTabUrl}")).LlmKeysTab;`,
  });
  assert.match(page, /LlmKeysTab/);
  const xml = await readPaas("pages/llm/LlmPage.xml");
  assert.match(xml, /<LlmKeysTab t-elif="currentTab === 'keys'"\/>/);
});

test("paid model options emit null for all models and filter restricted selections to default group", () => {
  llmService.models = { state: "ok", items: [
    { model: "paid-a", group: "default" }, { model: "free-a", group: "free" },
    { model: "paid-b", group: "default" },
  ] };
  assert.deepEqual(paidKeyModels(llmService.models.items).map((item) => item.model), ["paid-a", "paid-b"]);
  assert.deepEqual(normalizePaidDraft({ name: " key ", limitPoints: "", expires: "90d", models: ["paid-a", "paid-b", "free-a"] }, ["paid-a", "paid-b"]), {
    name: "key", limitPoints: null, expires: "90d", models: null,
  });
  assert.deepEqual(normalizePaidDraft({ name: "x", limitPoints: "", expires: "30d", models: ["paid-a", "free-a"] }, ["paid-a", "paid-b"]), {
    name: "x", limitPoints: null, expires: "30d", models: ["paid-a"],
  });
  assert.equal(normalizePaidDraft({ name: "x", limitPoints: "", expires: "7d", models: null }, ["paid-a"]).expires, "90d");
  assert.equal(JSON.stringify(normalizePaidDraft({ name: "x", limitPoints: "", expires: "90d", models: ["free-a"] }, ["paid-a", "paid-b"]).models).includes("free-a"), false);
});

test("paid reveal models prop accepts null or canonical model ID arrays and drives the reveal summary", () => {
  const prop = PaidRevealModal.props.models;
  assert.deepEqual(prop.type, [Array, { value: null }]);
  assert.equal(prop.optional, true);
  assert.equal(prop.validate(null), true);
  assert.equal(prop.validate(["vendor/model-v1", "qwen3-4b-cluster1"]), true);
  assert.equal(prop.validate([]), true);
  assert.equal(prop.validate(undefined), false);
  assert.equal(prop.validate("vendor/model-v1"), false);
  assert.equal(prop.validate(["invalid model id"]), false);
  assert.equal(prop.validate(["model", null]), false);

  const reveal = Object.create(PaidRevealModal.prototype);
  reveal.props = { kind: "paid", allowedModels: ["vendor/model-v1", "vendor/model-v2"], models: null };
  reveal.state = { models: reveal.props.models, modelAllowlist: [...reveal.props.allowedModels] };
  assert.equal(reveal.paidModelsText, "All paid models");
  reveal.state.models = ["vendor/model-v2"];
  assert.equal(reveal.paidModelsText, "vendor/model-v2");
});

test("limited edit rejects total limits below used and reports remaining after change", () => {
  assert.deepEqual(validatePaidDraft({ name: "Key", limitPoints: "29", expires: "90d", models: ["m"] }, {
    paidModels: ["m"], usedPoints: 30,
  }), { valid: false, error: "below_used", remaining: null });
  assert.deepEqual(validatePaidDraft({ name: "Key", limitPoints: "100", expires: "90d", models: ["m"] }, {
    paidModels: ["m"], usedPoints: 30,
  }), { valid: true, error: null, remaining: 70 });
  assert.deepEqual(validatePaidDraft({ name: "Key", limitPoints: "0", expires: "90d", models: ["m"] }, {
    paidModels: ["m"], usedPoints: 0,
  }), { valid: false, error: "limit_must_be_positive", remaining: null });
  assert.equal(validatePaidDraft({ name: "Key", limitPoints: "", expires: "90d", models: null }, {
    paidModels: ["m"], usedPoints: 0,
  }).valid, true, "blank total limit remains unlimited");
  assert.deepEqual(validatePaidDraft({ name: "Key", limitPoints: "", expires: "90d", models: null }, {
    paidModels: [], usedPoints: 0,
  }), { valid: false, error: "invalid", remaining: null });
});

test("extend query accepts current integer IDs once and ignores malformed or unmatched IDs", () => {
  for (const value of ["", "1.5", "-2", "1x", "NaN", "9007199254740992"]) assert.equal(parseExtendId(value), null);
  assert.equal(parseExtendId("12"), 12);
  assert.deepEqual(applyExtendQueryOnce("?extend=12", [{ id: 12 }, { id: 13 }], new Set()), { id: 12, consumed: true });
  assert.deepEqual(applyExtendQueryOnce("?extend=99", [{ id: 12 }], new Set()), { id: null, consumed: true });
  assert.deepEqual(applyExtendQueryOnce("?extend=12", [{ id: 12 }], new Set(["12"])), { id: null, consumed: true });
  assert.deepEqual(applyExtendQueryOnce("?extend=1%2Fkeys%2Fcreate", [{ id: 1 }], new Set()), { id: null, consumed: true });
  assert.deepEqual(applyExtendQueryOnce("?extend=12&extend=12", [{ id: 12 }], new Set()), { id: null, consumed: true });
});

test("hash extend hint parses and strips only extend while preserving route and unrelated query; no write is called", () => {
  assert.equal(extendQueryFromHash("#/llm/keys?extend=12&view=compact"), "extend=12&view=compact");
  assert.equal(applyExtendQueryOnce(extendQueryFromHash("#/llm/keys?extend=12&view=compact"), [{ id: 12 }], new Set()).id, 12);
  assert.equal(stripExtendFromHash("#/llm/keys?extend=12&view=compact"), "#/llm/keys?view=compact");
  assert.equal(stripExtendFromHash("#/llm/keys?extend=12"), "#/llm/keys");
  for (const hash of ["#/llm/keys?extend=1.5", "#/llm/keys?extend=-1", "#/llm/keys?extend=12&extend=12", "#/llm/keys?extend=99"]) {
    const consumed = new Set();
    const value = extendQueryFromHash(hash);
    assert.equal(applyExtendQueryOnce(value, [{ id: 12 }], consumed).id, null, hash);
  }
  globalThis.__llmKeyCalls.length = 0;
  const tab = Object.create(LlmKeysTab.prototype);
  tab.service = { keys: [{ id: 12 }] };
  tab.extendConsumed = new Set();
  tab.state = { extendConfirmation: null, notice: null };
  globalThis.location = { hash: "#/llm/keys?extend=12&view=compact", href: "https://example.test/woow#/llm/keys?extend=12&view=compact" };
  globalThis.history = { state: { keep: true }, replaceState(_state, _title, value) { this.url = value; } };
  tab.consumeExtendQuery();
  assert.equal(tab.state.extendConfirmation, 12);
  assert.equal(globalThis.history.url, "https://example.test/woow#/llm/keys?view=compact");
  assert.deepEqual(globalThis.__llmKeyCalls, []);
  delete globalThis.location;
  delete globalThis.history;
});

test("limited key rows expose bounded accessible progress; unlimited rows show used without progress", () => {
  const limited = keyRow({ id: 3, kind: "paid", limit_points: 100, used_points: 130, masked_key: "sk-abc…xyz" });
  assert.equal(limited.progressMax, 100);
  assert.equal(limited.progressValue, 100);
  assert.match(limited.progressLabel, /130/);
  assert.equal(keyRow({ id: 4, kind: "paid", limit_points: null, used_points: 30 }).limited, false);
});

test("form draft emits safe metadata only and never starts paid creation", () => {
  globalThis.__llmKeyCalls.length = 0;
  llmService.models = { items: [{ model: "paid-a", group: "default" }, { model: "paid-b", group: "default" }] };
  const form = Object.create(LlmKeyFormModal.prototype);
  form.state = { name: "Ops", limitPoints: "", expires: "90d", models: null };
  assert.deepEqual(form.safeDraft(), { name: "Ops", limitPoints: null, expires: "90d", models: null });
  assert.equal(typeof form.submitDraft, "function");
  assert.equal(globalThis.__llmKeyCalls.length, 0);
});
