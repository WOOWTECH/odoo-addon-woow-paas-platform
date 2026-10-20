// LLM-07 paid key management: import real source with the repository's import-stub convention.
import assert from "node:assert/strict";
import test from "node:test";
import { readPaas, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

const owlStub = "class Component {} const onMounted = (fn) => { globalThis.__keyHooks.mounted.push(fn); }; const onWillUnmount = (fn) => { globalThis.__keyHooks.unmount.push(fn); }; const useState = (value) => value;";
globalThis.__keyHooks = { mounted: [], unmount: [] };
globalThis.__keyRefs = {};

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
  "@odoo/owl": "class Component {} const onMounted = () => {}; const onWillUnmount = () => {}; const useRef = (name) => globalThis.__keyRefs[name] || (globalThis.__keyRefs[name] = { el: null }); const useState = (value) => value;",
  "@web/core/l10n/translation": "const _t = (message, values) => values ? message.replace(/%\\((\\w+)\\)s/g, (_, k) => String(values[k])) : message;",
  "../../../components/icon/WoowIcon": "const WoowIcon = {};",
  "../../../components/button/WoowButton": "const WoowButton = {};",
  "../../../services/llm_service": `const llmService = (await import("${serviceUrl}")).llmService;`,
  "../../../utils/llm_format": "const formatTaipeiDateTime = (value) => value || '—'; const isExpiringSoon = () => false;",
  "../../../services/llm_errors": `const llmErrorMessage = (result) => result.code === "llm_tos_outdated" ? "Terms of Service updated. Please accept them again." : result.code;`,
  "../../../components/llm/LlmKeyFormModal": "const LlmKeyFormModal = {};",
  "../../../components/llm/LlmKeyRevealModal": "const LlmKeyRevealModal = {};",
  "../../../components/llm/LlmKeyActionConfirmModal": "const LlmKeyActionConfirmModal = {};",
  "../../../core/router": "const router = { navigate: (...args) => { globalThis.__keyNavigation = args; } };",
});
const keyTabUrl = toDataUrl(keyTabSource);
const { LlmKeysTab, parseExtendId, applyExtendQueryOnce, extendQueryFromHash, stripExtendFromHash, paidKeyModels, keyRow, keyActionEligibility } = await import(keyTabUrl);
const formSource = stubImports(await readPaas("components/llm/LlmKeyFormModal.js"), {
  "@odoo/owl": "class Component {} const useRef = () => ({ el: null }); const useState = (value) => value; const onMounted = () => {}; const onWillUnmount = () => {};",
  "@web/core/l10n/translation": "const _t = (message, values) => values ? message.replace(/%\\((\\w+)\\)s/g, (_, k) => String(values[k])) : message;",
  "../icon/WoowIcon": "const WoowIcon = {};",
  "../button/WoowButton": "const WoowButton = {};",
  "../../services/llm_service": `const llmService = (await import("${serviceUrl}")).llmService;`,
  "../../services/llm_errors": "const llmErrorMessage = (result) => result.code;",
  "../../utils/modal_focus": "const inertOutside = () => () => {}; const trapTab = () => {};",
});
const { LlmKeyFormModal, validatePaidDraft, normalizePaidDraft, paidKeyEditDiff } = await import(toDataUrl(formSource));

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
  tab.service = {
    keys: llmService.keys, overviewLoaded: true, overviewError: null,
    overview: { account: { tos: { blocking: false } } },
  };
  tab.keysHeadingRef = { el: { focus() {} } };
  tab.state = {
    loading: false, overviewReady: true, showForm: true, showReveal: false, pendingDraft: null,
    extendConfirmation: null, notice: null, redirectToOverviewAfterReveal: false,
  };
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
  tab.keysHeadingRef = { el: null };
  tab.state = { createError: null };
  assert.ok(!tab.errorText.includes(raw));
  tab.setCreateError({ success: false, code: "llm_network_error", error: raw });
  assert.ok(!tab.state.createError.includes(raw));
});

test("failed paid reveal keeps mapped recovery feedback after close until another attempt", async () => {
  const raw = "socket exploded secret-value";
  const tab = Object.create(LlmKeysTab.prototype);
  tab.service = { keys: [], keysError: null };
  tab.state = { loading: false, showReveal: true, pendingDraft: { name: "Ops" }, createError: null };
  tab.keysHeadingRef = { el: { calls: 0, focus() { this.calls++; } } };
  llmService.createPaidKey = async () => ({ success: false, code: "llm_network_error", error: raw });
  llmService.fetchKeys = () => Promise.resolve({ success: true });

  await tab.createPendingPaidKey("request-paid-id");
  const mappedError = tab.state.createError;
  assert.ok(mappedError);
  assert.ok(!mappedError.includes(raw));
  tab.onRevealClosed();
  assert.equal(tab.state.createError, mappedError);
  assert.equal(tab.state.showReveal, false);
  assert.equal(tab.state.pendingDraft, null);
  assert.equal(tab.keysHeadingRef.el.calls, 1);

  tab.state.pendingDraft = { name: "Retry" };
  llmService.createPaidKey = async () => ({ success: true, data: { key: "sk-hidden" } });
  await tab.createPendingPaidKey("next-request");
  assert.equal(tab.state.createError, null);
  tab.state.createError = mappedError;
  tab.state.pendingDraft = { name: "Retry" };
  llmService.createPaidKey = async () => { throw new Error(raw); };
  const rejected = await tab.createPendingPaidKey("rejected-request");
  assert.equal(rejected.success, false);
  assert.ok(tab.state.createError);
  assert.ok(!tab.state.createError.includes(raw));
  tab.onRevealClosed();
  assert.ok(tab.state.createError);
  assert.equal(tab.keysHeadingRef.el.calls, 2);
});

test("keys route goes live only with LlmPage registration and rendered component", async () => {
  assert.ok(routes.LLM_LIVE_TABS.includes("keys"));
  assert.equal(routes.isLlmTabLive("keys"), true);
  const page = stubImports(await readPaas("pages/llm/LlmPage.js"), {
    "@odoo/owl": "class Component {} const onMounted = () => {}; const onPatched = () => {}; const onWillUnmount = () => {}; const onWillUpdateProps = () => {}; const useRef = () => ({ el: null }); const useState = (value) => value;",
    "@web/core/l10n/translation": "const _t = (message) => message;",
    "../../components/icon/WoowIcon": "const WoowIcon = {};",
    "../../core/router": "const router = { navigate() {} };",
    "../../core/llm_routes": `const { LLM_TABS, isLlmTabLive, llmTabPath, normalizeLlmTab } = await import("${toDataUrl(routesSource)}");`,
    "./tabs/LlmCreditTab": "const LlmCreditTab = {};",
    "./tabs/LlmModelsTab": `const LlmModelsTab = (await import("${modelsUrl}")).LlmModelsTab;`,
    "./tabs/LlmOverviewTab": "const LlmOverviewTab = {};",
    "./tabs/LlmKeysTab": `const LlmKeysTab = (await import("${keyTabUrl}")).LlmKeysTab;`,
    "./tabs/LlmDocsTab": "const LlmDocsTab = {};",
    "../../services/llm_service": "const llmService = {};",
    "../../services/llm_errors": "const llmErrorMessage = result => result.code;",
    "./tabs/llm_docs_helpers": "const createLlmDocsKeyHandoff = () => ({ put() {}, peekKey() { return ''; }, peekMode() { return 'paid'; }, clear() {} }); const validatedLlmPublicBaseUrl = () => null;",
    "./tabs/LlmUsageTab": "const LlmUsageTab = {};",
  });
  assert.match(page, /LlmKeysTab/);
  assert.match(page, /LlmUsageTab/);
  const xml = await readPaas("pages/llm/LlmPage.xml");
  assert.match(xml, /<LlmKeysTab t-elif="currentTab === 'keys'" onOpenDocs\.bind="openDocsWithKey"\/>/);
  assert.match(xml, /<LlmUsageTab t-elif="currentTab === 'usage'"\/>/);
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
  tab.service = { keys: [{ id: 12, kind: "paid", status: "active", expires_at: "2027-01-01" }] };
  tab.extendConsumed = new Set();
  tab.state = { confirmation: null, notice: null, actionError: null };
  globalThis.location = { hash: "#/llm/keys?extend=12&view=compact", href: "https://example.test/woow#/llm/keys?extend=12&view=compact" };
  globalThis.history = { state: { keep: true }, replaceState(_state, _title, value) { this.url = value; } };
  tab.consumeExtendQuery();
  assert.equal(tab.state.confirmation.item.id, 12);
  assert.equal(tab.state.confirmation.action, "extend");
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

test("action eligibility excludes unsafe states; terms block only re-enable", () => {
  const paid = { kind: "paid", status: "active", expires_at: "2027-01-01" };
  assert.deepEqual(keyActionEligibility(paid), { edit: true, extend: true, disable: true, enable: false, delete: true });
  const noActions = { edit: false, extend: false, disable: false, enable: false, delete: false };
  for (const status of ["unknown", "creating", "unrecognized_remote_state"]) {
    assert.deepEqual(keyActionEligibility({ ...paid, status }), noActions, status);
  }
  assert.deepEqual(keyActionEligibility({ ...paid, status: "failed_reveal" }), {
    edit: false, extend: false, disable: false, enable: false, delete: true,
  });
  for (const status of ["expired", "exhausted", "free_capped", "suspended", "overdraft"]) {
    assert.equal(keyActionEligibility({ ...paid, status }).enable, false, status);
    assert.equal(keyActionEligibility({ ...paid, status }).extend, true, status);
  }
  assert.equal(keyActionEligibility({ ...paid, expires_at: null }).extend, false);
  assert.equal(keyActionEligibility({ ...paid, kind: "free_trial" }).edit, false);
  assert.equal(keyActionEligibility({ ...paid, status: "disabled" }, true).enable, false);
  assert.equal(keyActionEligibility({ ...paid, status: "disabled" }, false).enable, true);
  for (const status of ["free_capped", "suspended", "overdraft"]) {
    assert.notEqual(keyRow({ id: 2, status }).statusLabel, status);
  }
});

test("creating and unrecognized statuses cannot open or dispatch key actions", async () => {
  const calls = [];
  const originals = {
    updateKey: llmService.updateKey, extendKey: llmService.extendKey,
    setKeyStatus: llmService.setKeyStatus, deleteKey: llmService.deleteKey,
  };
  try {
    llmService.updateKey = async (...args) => { calls.push(["updateKey", ...args]); return { success: true }; };
    llmService.extendKey = async (...args) => { calls.push(["extendKey", ...args]); return { success: true }; };
    llmService.setKeyStatus = async (...args) => { calls.push(["setKeyStatus", ...args]); return { success: true }; };
    llmService.deleteKey = async (...args) => { calls.push(["deleteKey", ...args]); return { success: true }; };
    const item = { id: 41, kind: "paid", status: "creating", display_name: "Pending", expires_at: "2027-01-01" };
    const tab = Object.create(LlmKeysTab.prototype);
    tab.service = { keys: [item], overviewLoaded: true, overviewError: null, overview: { account: { tos: { blocking: false } } } };
    tab.state = { busyKey: null, showForm: false, editKey: null, confirmation: null, actionError: null, notice: null };
    tab.edit(item);
    tab.openConfirmation(item, "extend");
    await tab.saveEdit(item, { name: "Changed" });
    tab.state.confirmation = { item, action: "delete" };
    await tab.confirmAction("");
    assert.equal(tab.state.showForm, false);
    assert.equal(tab.state.confirmation.action, "delete");
    assert.deepEqual(calls, []);
  } finally {
    Object.assign(llmService, originals);
  }
});

test("malformed confirmation action makes no state change or service call", async () => {
  const item = { id: 42, kind: "paid", status: "active", display_name: "Key", expires_at: "2027-01-01" };
  const tab = Object.create(LlmKeysTab.prototype);
  tab.service = { keys: [item], overviewLoaded: true, overviewError: null, overview: { account: { tos: { blocking: false } } } };
  tab.state = { busyKey: null, confirmation: { item, action: "constructor" }, actionError: null, notice: null };
  const before = { ...tab.state, confirmation: { ...tab.state.confirmation } };
  const originals = {
    extendKey: llmService.extendKey, setKeyStatus: llmService.setKeyStatus, deleteKey: llmService.deleteKey,
  };
  const calls = [];
  try {
    llmService.extendKey = async (...args) => { calls.push(["extendKey", ...args]); return { success: true }; };
    llmService.setKeyStatus = async (...args) => { calls.push(["setKeyStatus", ...args]); return { success: true }; };
    llmService.deleteKey = async (...args) => { calls.push(["deleteKey", ...args]); return { success: true }; };
    await tab.confirmAction("");
    assert.deepEqual(tab.state, before);
    assert.deepEqual(calls, []);
  } finally {
    Object.assign(llmService, originals);
  }
});

test("desktop and mobile each render the same real semantic actions; no fake disabled action or extension wording", async () => {
  const xml = await readPaas("pages/llm/tabs/LlmKeysTab.xml");
  const desktop = xml.slice(xml.indexOf("o_woow_llm_keys__desktop"), xml.indexOf("o_woow_llm_keys__mobile"));
  const mobile = xml.slice(xml.indexOf("o_woow_llm_keys__mobile"), xml.indexOf("<LlmKeyFormModal"));
  const tabJs = await readPaas("pages/llm/tabs/LlmKeysTab.js");
  for (const action of ["edit", "extend", "disable", "enable", "delete"]) {
    assert.match(desktop, new RegExp(`row\\.${action}`));
    assert.match(mobile, new RegExp(`row\\.${action}`));
  }
  assert.doesNotMatch(xml, /disabled="true">Actions|confirmation preview|next implementation step/i);
  assert.equal((xml.match(/this\.service\.keys\.find\(\(key\) => key\.id === row\.id\)/g) || []).length, 10,
    "both responsive action groups pass the original DTO to handlers");
  assert.doesNotMatch(tabJs, /\/api\/llm\/keys\/(?:update|extend|set_status|delete)/);
  assert.match(await readPaas("components/llm/LlmKeyActionConfirmModal.xml"), /role="dialog"/);
});

test("a globally busy key disables every action control and handler", async () => {
  const calls = [];
  const originals = {
    updateKey: llmService.updateKey, extendKey: llmService.extendKey,
    setKeyStatus: llmService.setKeyStatus, deleteKey: llmService.deleteKey,
  };
  try {
    for (const method of ["updateKey", "extendKey", "setKeyStatus", "deleteKey"]) {
      llmService[method] = async (...args) => { calls.push([method, ...args]); return { success: true }; };
    }
    const item = { id: 51, kind: "paid", status: "active", display_name: "Key", expires_at: "2027-01-01" };
    const tab = Object.create(LlmKeysTab.prototype);
    tab.service = { keys: [item], overviewLoaded: true, overviewError: null, overview: { account: { tos: { blocking: false } } } };
    tab.state = { busyKey: 99, showForm: false, editKey: null, confirmation: null, actionError: null, notice: null };
    tab.edit(item);
    tab.openConfirmation(item, "extend");
    await tab.saveEdit(item, { name: "Changed" });
    await tab.enable({ ...item, status: "disabled" });
    tab.state.confirmation = { item, action: "delete" };
    await tab.confirmAction("");
    assert.equal(tab.state.showForm, false);
    assert.equal(tab.state.confirmation.action, "delete");
    assert.deepEqual(calls, []);
  } finally {
    Object.assign(llmService, originals);
  }
  const xml = await readPaas("pages/llm/tabs/LlmKeysTab.xml");
  for (const label of ["Edit", "Extend", "Disable", "Delete"]) {
    assert.equal((xml.match(new RegExp(`<button[^\\n]*t-att-disabled="state\\.busyKey !== null"[^\\n]*>${label}</button>`, "g")) || []).length, 2, label);
  }
  assert.equal((xml.match(/t-att-disabled="!row\.enable \|\| state\.busyKey !== null"/g) || []).length, 2);
  assert.match(xml, /busy="state\.busyKey !== null"/);
});

test("tab dispatches only the service action methods; blocked enable performs no call", async () => {
  const calls = [];
  const original = {
    updateKey: llmService.updateKey, extendKey: llmService.extendKey,
    setKeyStatus: llmService.setKeyStatus, deleteKey: llmService.deleteKey,
    fetchKeys: llmService.fetchKeys,
  };
  llmService.updateKey = async (...args) => { calls.push(["updateKey", ...args]); return { success: true }; };
  llmService.extendKey = async (...args) => { calls.push(["extendKey", ...args]); return { success: true }; };
  llmService.setKeyStatus = async (...args) => { calls.push(["setKeyStatus", ...args]); return { success: true }; };
  llmService.deleteKey = async (...args) => { calls.push(["deleteKey", ...args]); return { success: true }; };
  llmService.fetchKeys = async () => ({ success: true });
  const tab = Object.create(LlmKeysTab.prototype);
  const item = { id: 9, kind: "paid", status: "active", display_name: "Exact Name", expires_at: "2027-01-01" };
  tab.service = { keys: [item], overviewLoaded: true, overviewError: null, overview: { account: { tos: { blocking: true, needs_reaccept: false } } } };
  tab.state = { busyKey: null, confirmation: { item, action: "extend" }, actionError: null, notice: null };
  assert.equal(tab.tosBlocking, true);
  await tab.saveEdit(item, { name: "New" });
  await tab.confirmAction("");
  tab.state.confirmation = { item, action: "disable" };
  await tab.confirmAction("");
  tab.state.confirmation = { item, action: "delete" };
  await tab.confirmAction("Exact Name");
  tab.state.confirmation = null;
  await tab.enable({ ...item, status: "disabled" });
  assert.deepEqual(calls, [
    ["updateKey", 9, { name: "New" }], ["extendKey", 9],
    ["setKeyStatus", 9, false], ["deleteKey", 9, "Exact Name"],
  ]);
  Object.assign(llmService, original);
});

test("direct keys refresh loads state and keys; re-enable stays blocked until valid ToS state arrives", async () => {
  const originals = {
    fetchKeys: llmService.fetchKeys, fetchState: llmService.fetchState, setKeyStatus: llmService.setKeyStatus,
    keys: llmService.keys, overview: llmService.overview, overviewError: llmService.overviewError, overviewLoaded: llmService.overviewLoaded,
  };
  try {
    const item = { id: 61, kind: "paid", status: "disabled", display_name: "Key" };
    let keyFetches = 0;
    let stateFetches = 0;
    const statusCalls = [];
    llmService.keys = [item];
    llmService.overview = null;
    llmService.overviewError = null;
    llmService.overviewLoaded = false;
    llmService.fetchKeys = async () => { keyFetches++; return { success: true, data: { items: [item] } }; };
    llmService.fetchState = async () => {
      stateFetches++;
      llmService.overviewLoaded = true;
      llmService.overviewError = null;
      llmService.overview = { account: { tos: { blocking: false, needs_reaccept: true } } };
      return { success: true };
    };
    llmService.setKeyStatus = async (...args) => { statusCalls.push(args); return { success: true }; };
    const tab = Object.create(LlmKeysTab.prototype);
    tab.state = { loading: false, busyKey: null, actionError: null, notice: null };
    tab.destroyed = false;
    tab.service = llmService;
    tab.consumeExtendQuery = () => {};
    assert.equal(tab.tosBlocking, true);
    await tab.enable(item);
    assert.deepEqual(statusCalls, []);
    await tab.refresh();
    assert.equal(keyFetches, 1);
    assert.equal(stateFetches, 1);
    assert.equal(tab.tosBlocking, false, "explicit false overrides needs_reaccept after state loads");
    await tab.enable(item);
    assert.deepEqual(statusCalls, [[61, true]]);
  } finally {
    Object.assign(llmService, originals);
  }
});

test("keys refresh fails closed against stale terms and exposes an Overview route", async () => {
  const originals = {
    fetchKeys: llmService.fetchKeys, fetchState: llmService.fetchState, setKeyStatus: llmService.setKeyStatus,
    keys: llmService.keys, overview: llmService.overview, overviewError: llmService.overviewError,
    overviewLoaded: llmService.overviewLoaded,
  };
  try {
    const item = { id: 63, kind: "paid", status: "disabled", display_name: "Key" };
    const stateRefresh = deferred();
    const statusCalls = [];
    llmService.keys = [item];
    llmService.overviewLoaded = true;
    llmService.overviewError = null;
    llmService.overview = { account: { tos: { blocking: false } } };
    llmService.fetchKeys = async () => ({ success: true });
    llmService.fetchState = () => stateRefresh.promise;
    llmService.setKeyStatus = async (...args) => { statusCalls.push(args); return { success: true }; };
    const tab = Object.create(LlmKeysTab.prototype);
    tab.service = llmService;
    tab.state = {
      loading: false, overviewReady: true, busyKey: null, showForm: false, showReveal: false,
      pendingDraft: null, createError: null, actionError: null, redirectToOverviewAfterReveal: false,
    };
    tab.destroyed = false;
    tab.consumeExtendQuery = () => {};
    delete globalThis.__keyNavigation;

    const refreshing = tab.refresh();
    assert.equal(tab.tosBlocking, true, "a stale blocking:false value is not trusted during this refresh");
    tab.openCreate();
    assert.equal(tab.state.showForm, false);
    assert.match(tab.state.createError, /Terms of Service/);
    tab.state.showForm = true;
    tab.onDraft({ name: "Key", limitPoints: null, expires: "90d", models: ["paid-a"] });
    assert.equal(tab.state.showForm, false);
    assert.equal(tab.state.showReveal, false);
    assert.equal(tab.state.pendingDraft, null);
    await tab.enable(item);
    assert.deepEqual(statusCalls, []);

    stateRefresh.resolve({ success: true });
    await refreshing;
    assert.equal(tab.tosBlocking, false);
    tab.openCreate();
    assert.equal(tab.state.showForm, true);
    tab.goToOverview();
    assert.deepEqual(globalThis.__keyNavigation, ["llm"]);
  } finally {
    Object.assign(llmService, originals);
    delete globalThis.__keyNavigation;
  }
  const xml = await readPaas("pages/llm/tabs/LlmKeysTab.xml");
  assert.match(xml, /disabled="tosBlocking \|\| state\.busyKey !== null"/);
  assert.match(xml, /t-if="tosBlocking"/);
  assert.match(xml, /onClick\.bind="goToOverview"/);
  assert.match(xml, /t-att-aria-describedby="tosBlocking/);
});

test("enable fails closed when overview state is unknown, incomplete, or failed", async () => {
  const original = llmService.setKeyStatus;
  const calls = [];
  try {
    llmService.setKeyStatus = async (...args) => { calls.push(args); return { success: true }; };
    const item = { id: 62, kind: "paid", status: "disabled", display_name: "Key" };
    const tab = Object.create(LlmKeysTab.prototype);
    tab.state = { busyKey: null, actionError: null, notice: null };
    for (const service of [
      { overviewLoaded: true, overviewError: null, overview: { account: { tos: { blocking: false } } } },
      { overviewLoaded: false, overviewError: null, overview: { account: { tos: { blocking: false } } } },
      { overviewLoaded: true, overviewError: null, overview: null },
      { overviewLoaded: true, overviewError: null, overview: { account: {} } },
      { overviewLoaded: true, overviewError: "llm_network_error", overview: { account: { tos: { blocking: false } } } },
    ]) {
      tab.service = { keys: [item], ...service };
      assert.equal(tab.tosBlocking, true);
      await tab.enable(item);
    }
    assert.deepEqual(calls, []);
  } finally {
    llmService.setKeyStatus = original;
  }
});

test("ToS blocking takes precedence and outdated enable refreshes state then routes to llm", async () => {
  const originals = { setKeyStatus: llmService.setKeyStatus, fetchState: llmService.fetchState };
  let stateRefreshes = 0;
  llmService.setKeyStatus = async () => ({ success: false, code: "llm_tos_outdated" });
  llmService.fetchState = async () => { stateRefreshes++; return { success: true }; };
  const tab = Object.create(LlmKeysTab.prototype);
  const item = { id: 31, kind: "paid", status: "disabled", display_name: "Key" };
  tab.service = { keys: [item], overviewLoaded: true, overviewError: null, overview: { account: { tos: { blocking: false, needs_reaccept: true } } } };
  tab.state = { overviewReady: true, busyKey: null, actionError: null, notice: null };
  assert.equal(tab.tosBlocking, false, "explicit blocking false takes precedence over legacy fallback");
  await tab.enable(item);
  assert.deepEqual(globalThis.__keyNavigation, ["llm"]);
  assert.equal(stateRefreshes, 1);
  assert.match(tab.state.actionError, /Terms of Service/);
  assert.equal(tab.state.notice, tab.state.actionError);
  llmService.setKeyStatus = originals.setKeyStatus;
  llmService.fetchState = originals.fetchState;
});

test("stale paid-key creation refreshes terms and routes only after the reveal closes", async () => {
  const originals = {
    createPaidKey: llmService.createPaidKey, fetchKeys: llmService.fetchKeys, fetchState: llmService.fetchState,
  };
  try {
    const stateRefresh = deferred();
    let stateFetches = 0;
    let keyFetches = 0;
    llmService.createPaidKey = async () => ({
      success: false, code: "llm_tos_outdated", error: "raw upstream terms message",
    });
    llmService.fetchState = () => { stateFetches++; return stateRefresh.promise; };
    llmService.fetchKeys = async () => { keyFetches++; return { success: true }; };
    const tab = Object.create(LlmKeysTab.prototype);
    tab.service = { overviewLoaded: true, overviewError: null, overview: { account: { tos: { blocking: false } } }, keys: [] };
    tab.state = {
      overviewReady: true, showReveal: true, pendingDraft: { name: "Key" }, createError: null,
      redirectToOverviewAfterReveal: false,
    };
    tab.keysHeadingRef = { el: { focus() { assert.fail("terms redirect should not focus the departing keys heading"); } } };
    tab.destroyed = false;
    delete globalThis.__keyNavigation;

    const pending = tab.createPendingPaidKey("request-id");
    for (let attempt = 0; attempt < 3 && !stateFetches; attempt++) await Promise.resolve();
    assert.equal(stateFetches, 1);
    assert.equal(tab.state.overviewReady, false);
    assert.equal(globalThis.__keyNavigation, undefined);
    stateRefresh.resolve({ success: true });
    const result = await pending;
    assert.equal(result.code, "llm_tos_outdated");
    assert.equal(tab.state.showReveal, true);
    assert.equal(tab.state.redirectToOverviewAfterReveal, true);
    assert.equal(globalThis.__keyNavigation, undefined, "the reveal owns the router leave guard until it closes");
    assert.ok(!JSON.stringify(tab.state).includes("raw upstream terms message"));

    tab.onRevealClosed();
    assert.equal(tab.state.showReveal, false);
    assert.equal(tab.state.pendingDraft, null);
    assert.deepEqual(globalThis.__keyNavigation, ["llm"]);
    assert.equal(keyFetches, 0, "a failed creation does not add a redundant key-list refresh before routing");
  } finally {
    Object.assign(llmService, originals);
    delete globalThis.__keyNavigation;
  }
});

test("delete confirmation uses exact paid name and sends the empty free-trial confirm name", async () => {
  const calls = [];
  const original = llmService.deleteKey;
  llmService.deleteKey = async (...args) => { calls.push(args); return { success: true }; };
  const tab = Object.create(LlmKeysTab.prototype);
  tab.state = { busyKey: null, actionError: null, notice: null };
  tab.service = { keys: [] };
  const paid = { id: 21, kind: "paid", status: "active", display_name: " Name " };
  tab.state.confirmation = { item: paid, action: "delete" };
  await tab.confirmAction("Name");
  assert.deepEqual(calls, []);
  tab.state.confirmation = { item: paid, action: "delete" };
  await tab.confirmAction(" Name ");
  tab.state.confirmation = { item: { id: 22, kind: "free_trial", status: "failed_reveal", display_name: "Free" }, action: "delete" };
  await tab.confirmAction("ignored");
  assert.deepEqual(calls, [[21, " Name "], [22, ""]]);
  llmService.deleteKey = original;
});

test("edit diff omits untouched expiry, preserves null semantics, and no-op submits nothing", async () => {
  const item = { display_name: "Key", limit_points: 100, models: ["paid-a"], used_points: 30, expires_at: "2027-01-01" };
  const draft = { name: "Key", limitPoints: "100", expires: "keep", models: ["paid-a"] };
  assert.deepEqual(paidKeyEditDiff(item, draft, ["paid-a", "paid-b"]), {});
  const changed = paidKeyEditDiff(item, { ...draft, name: "Renamed", limitPoints: "", models: ["paid-a", "paid-b"], expires: "never" }, ["paid-a", "paid-b"]);
  assert.deepEqual(changed, { name: "Renamed", limitPoints: null, expires: "never", models: null });
  assert.equal(Object.hasOwn(paidKeyEditDiff(item, { ...draft, expires: "keep" }, ["paid-a"]), "expires"), false);
  let submitted = 0;
  const form = Object.create(LlmKeyFormModal.prototype);
  form.props = { keyItem: item, onClose: () => { submitted++; } };
  form.state = { ...draft, confirmingClose: false };
  form.confirmClose = () => { submitted++; };
  llmService.models = { items: [{ model: "paid-a", group: "default" }, { model: "paid-b", group: "default" }] };
  form.submitDraft();
  assert.equal(submitted, 1);
});

test("form save lifecycle blocks duplicate submit and ignores unmounted update continuation", async () => {
  const originalModels = llmService.models;
  try {
    llmService.models = { items: [{ model: "paid-a", group: "default" }] };
    const item = { display_name: "Key", limit_points: 100, models: ["paid-a"], used_points: 0 };
    let closed = 0;
    const duplicate = Object.create(LlmKeyFormModal.prototype);
    duplicate.props = { keyItem: item, onClose: () => closed++, onUpdate: async () => assert.fail("duplicate update") };
    duplicate.state = { name: "Key", limitPoints: "100", expires: "keep", models: ["paid-a"], confirmingClose: false, error: null, saving: true };
    duplicate.confirmClose = () => { closed++; };
    duplicate.submitDraft();
    assert.equal(closed, 0, "a second save cannot convert an in-flight edit into a no-op close");

    const update = deferred();
    const unmounted = Object.create(LlmKeyFormModal.prototype);
    unmounted.props = { keyItem: item, onUpdate: () => update.promise };
    unmounted.state = { saving: false, error: null };
    unmounted.destroyed = false;
    const pending = unmounted.submitEdit({ name: "Changed" });
    assert.equal(unmounted.state.saving, true);
    unmounted.destroyed = true;
    update.resolve({ success: false, code: "llm_network_error", error: "raw transport text" });
    assert.equal((await pending).success, false);
    assert.equal(unmounted.state.saving, true);
    assert.equal(unmounted.state.error, null);

    const mounted = Object.create(LlmKeyFormModal.prototype);
    mounted.props = { keyItem: item, onUpdate: async () => ({ success: false, code: "llm_network_error", error: "raw transport text" }) };
    mounted.state = { saving: false, error: null };
    mounted.destroyed = false;
    await mounted.submitEdit({ name: "Changed" });
    assert.equal(mounted.state.saving, false);
    assert.equal(mounted.state.error, "llm_network_error");
  } finally {
    llmService.models = originalModels;
  }
  assert.match(await readPaas("components/llm/LlmKeyFormModal.xml"), /type="checkbox"[^>]*t-att-disabled="state\.saving"/);
});

test("destroyed tab action continuations do not mutate state or route", async () => {
  const originals = {
    updateKey: llmService.updateKey, setKeyStatus: llmService.setKeyStatus,
    deleteKey: llmService.deleteKey, fetchState: llmService.fetchState,
  };
  const item = { id: 71, kind: "paid", status: "active", display_name: "Key", expires_at: "2027-01-01" };
  const service = { keys: [item], overviewLoaded: true, overviewError: null, overview: { account: { tos: { blocking: false } } } };
  delete globalThis.__keyNavigation;
  try {
    const update = deferred();
    llmService.updateKey = () => update.promise;
    const editTab = Object.create(LlmKeysTab.prototype);
    editTab.service = service;
    editTab.destroyed = false;
    editTab.state = { busyKey: null, showForm: true, editKey: item, confirmation: null, actionError: null, notice: null };
    const editPending = editTab.saveEdit(item, { name: "Changed" });
    assert.equal(editTab.state.busyKey, item.id);
    const editStart = { ...editTab.state };
    editTab.destroyed = true;
    update.resolve({ success: true });
    assert.equal((await editPending).success, true);
    assert.deepEqual(editTab.state, editStart);

    const status = deferred();
    llmService.setKeyStatus = () => status.promise;
    const enableTab = Object.create(LlmKeysTab.prototype);
    enableTab.service = service;
    enableTab.destroyed = false;
    enableTab.state = { overviewReady: true, busyKey: null, actionError: null, notice: null };
    const enablePending = enableTab.enable({ ...item, status: "disabled" });
    assert.equal(enableTab.state.busyKey, item.id);
    const enableStart = { ...enableTab.state };
    enableTab.destroyed = true;
    status.resolve({ success: true });
    assert.equal((await enablePending).success, true);
    assert.deepEqual(enableTab.state, enableStart);

    const deletion = deferred();
    llmService.deleteKey = () => deletion.promise;
    const deleteTab = Object.create(LlmKeysTab.prototype);
    deleteTab.service = service;
    deleteTab.destroyed = false;
    deleteTab.state = { busyKey: null, confirmation: { item, action: "delete" }, actionError: null, notice: null };
    const deletePending = deleteTab.confirmAction("Key");
    assert.equal(deleteTab.state.busyKey, item.id);
    const deleteStart = { ...deleteTab.state };
    deleteTab.destroyed = true;
    deletion.resolve({ success: true });
    assert.equal((await deletePending).success, true);
    assert.deepEqual(deleteTab.state, deleteStart);

    const refreshed = deferred();
    let stateFetches = 0;
    llmService.setKeyStatus = async () => ({ success: false, code: "llm_tos_outdated" });
    llmService.fetchState = () => { stateFetches++; return refreshed.promise; };
    const routeTab = Object.create(LlmKeysTab.prototype);
    routeTab.service = service;
    routeTab.destroyed = false;
    routeTab.state = { overviewReady: true, busyKey: null, actionError: null, notice: null };
    const routePending = routeTab.enable({ ...item, status: "disabled" });
    for (let attempt = 0; attempt < 3 && !stateFetches; attempt++) await Promise.resolve();
    assert.equal(stateFetches, 1);
    assert.equal(routeTab.state.busyKey, item.id);
    const routeStart = { ...routeTab.state };
    routeTab.destroyed = true;
    refreshed.resolve({ success: true });
    assert.equal((await routePending).code, "llm_tos_outdated");
    assert.deepEqual(routeTab.state, routeStart);
    assert.equal(globalThis.__keyNavigation, undefined);
  } finally {
    Object.assign(llmService, originals);
    delete globalThis.__keyNavigation;
  }
});

test("focus-managed confirmation modal blocks close while busy and falls back to the keys heading", async () => {
  const modalSource = stubImports(await readPaas("components/llm/LlmKeyActionConfirmModal.js"), {
    "@odoo/owl": "class Component {} const useRef = () => ({ el: null }); const useState = (value) => value; const onMounted = () => {}; const onWillUnmount = () => {};",
    "@web/core/l10n/translation": "const _t = (message) => message;",
    "../button/WoowButton": "const WoowButton = {};",
    "../../utils/modal_focus": "const inertOutside = () => () => {}; const trapTab = () => {};",
  });
  const { LlmKeyActionConfirmModal, restoreModalFocus } = await import(toDataUrl(modalSource));
  let closed = 0;
  const modal = Object.create(LlmKeyActionConfirmModal.prototype);
  modal.props = { action: "delete", item: { kind: "paid", display_name: " Name " }, busy: true, onClose: () => closed++, onConfirm: () => assert.fail("busy confirm") };
  modal.state = { confirmName: " Name " };
  modal.requestClose();
  modal.confirm();
  assert.equal(closed, 0);
  modal.props.busy = false;
  assert.equal(modal.nameMatches, true);
  modal.state.confirmName = "Name";
  assert.equal(modal.nameMatches, false);
  assert.match(await readPaas("components/llm/LlmKeyActionConfirmModal.xml"), /aria-modal="true" aria-labelledby=.*aria-describedby=/);

  let triggerFocuses = 0;
  let headingFocuses = 0;
  const tab = Object.create(LlmKeysTab.prototype);
  tab.keysHeadingRef = { el: { isConnected: true, focus() { headingFocuses++; } } };
  assert.equal(restoreModalFocus({ isConnected: true, focus() { triggerFocuses++; } }, () => tab.focusKeysHeading()), true);
  assert.equal(triggerFocuses, 1);
  assert.equal(headingFocuses, 0, "a connected trigger keeps normal focus return");
  assert.equal(restoreModalFocus({ isConnected: false, focus() { triggerFocuses++; } }, () => tab.focusKeysHeading()), false);
  assert.equal(triggerFocuses, 1, "a removed delete trigger is never focused");
  assert.equal(headingFocuses, 1, "the stable keys heading receives fallback focus");
  const keysXml = await readPaas("pages/llm/tabs/LlmKeysTab.xml");
  assert.match(keysXml, /onReturnFocus\.bind="focusKeysHeading"/);
  assert.match(await readPaas("components/llm/LlmKeyActionConfirmModal.js"), /restoreModalFocus\(this\.returnFocus, this\.props\.onReturnFocus\)/);
});
