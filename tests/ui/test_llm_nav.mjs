// LLM-01c（DESIGN §6.1）：「AI 模型」分頁的導覽——路由表、子頁白名單、gate、側欄／底欄、
// Header 的「返回」、template 名稱不撞名。
//
// 載入真實檔案、把 owl／_t／session／service 的 import 換成 stub（同 test_service_presentation_1159
// 的手法）；不起 Odoo、不起瀏覽器。畫面與 400px 截圖仍要在沙盒用瀏覽器驗收（票 AC7、AC8）。
import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";
import { PAAS, readPaas as read, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

const translateStub = "const _t = (message, values) => values ? message.replace(/%\\((\\w+)\\)s/g, (_, k) => String(values[k])) : message;";

// ----------------------------------------------------------------------------- 載入

const routesSource = await read("core/llm_routes.js");
const routesUrl = toDataUrl(routesSource);
const llmRoutes = await import(routesUrl);

const navSectionsUrl = toDataUrl(await read("core/nav_sections.js"));
const navSections = await import(navSectionsUrl);

globalThis.__llmNav = { llm: true, projects: true };
const flagStubs = "const showLlm = () => globalThis.__llmNav.llm; const showProjects = () => globalThis.__llmNav.projects;";

globalThis.window = { location: { hash: "" }, addEventListener() {} };
const routerSource = stubImports(await read("core/router.js"), {
  "@odoo/owl": "const reactive = (value) => value;",
  "@web/core/l10n/translation": translateStub,
  "./platform_config": flagStubs,
  "./llm_routes": `const { buildLlmRoutes, llmGateRedirect } = await import("${routesUrl}");`,
  "./invitation_context": "const captureInvitationFragment = () => {}; const clearInvitationToken = () => {}; const getInvitationRevision = () => 0;",
});
const { router } = await import(toDataUrl(routerSource));

function go(hash, { llm = true, projects = true } = {}) {
  globalThis.__llmNav.llm = llm;
  globalThis.__llmNav.projects = projects;
  window.location.hash = hash;
  router.parseRoute();
  return { current: router.current, params: { ...router.params }, hash: window.location.hash };
}

const platformSource = stubImports(await read("core/platform_config.js"), {
  // session 在模組載入時就綁定：用 Proxy 讓各 test 換值。
  "@web/session": "const session = new Proxy({}, { get: (_, key) => (globalThis.__llmSession || {})[key] });",
  "./llm_routes": `const { llmVisibleFromSession } = await import("${routesUrl}");`,
});
const platformConfig = await import(toDataUrl(platformSource));

const sidebarSource = stubImports(await read("layout/sidebar/Sidebar.js"), {
  "@odoo/owl": "class Component {} const useState = (value) => value; const onMounted = () => {};",
  "@web/core/l10n/translation": translateStub,
  "../../services/profile_service": "const profileService = {};",
  "../../core/platform_config": "const helpUrl = () => ''; " + flagStubs,
  "../../core/nav_sections": `const { sidebarSection } = await import("${navSectionsUrl}");`,
});
const { Sidebar } = await import(toDataUrl(sidebarSource));

const bottomNavSource = stubImports(await read("layout/bottom_nav/BottomNav.js"), {
  "@odoo/owl": "class Component {}",
  "@web/core/l10n/translation": translateStub,
  "../../core/platform_config": flagStubs,
  "../../core/nav_sections": `const { bottomNavSection } = await import("${navSectionsUrl}");`,
});
const { BottomNav } = await import(toDataUrl(bottomNavSource));

const headerSource = stubImports(await read("layout/header/Header.js"), {
  "@odoo/owl": "class Component {} const useState = (value) => value; const onMounted = () => {};",
  "@web/core/l10n/translation": translateStub,
  "../../services/billing_service": "const billingService = {};",
  "../../services/profile_service": "const profileService = {};",
  "../../services/breadcrumb_service": "const breadcrumbService = {};",
  "../../utils/billing_helpers": "const formatPoints = String;",
  "../../services/notification_service": "const notificationService = {};",
  "../../components/notification-center/NotificationPanel": "const NotificationPanel = {};",
  "../../components/account-menu/AccountMenu": "const AccountMenu = {};",
});
const { Header } = await import(toDataUrl(headerSource));

function header(current) {
  const instance = Object.create(Header.prototype);
  instance.props = { router: { current, routes: router.routes } };
  instance.breadcrumbService = { trailFor: () => null };
  return instance;
}

// ----------------------------------------------------------------------------- llm_routes（純模組）

test("llm_routes.js 是純模組：不 import owl、session、_t", () => {
  assert.doesNotMatch(routesSource, /^\s*import\s/m);
});

test("子頁白名單與 pattern：六個子頁、順序固定，白名單外一律不吃", () => {
  assert.deepEqual([...llmRoutes.LLM_TABS], ["overview", "keys", "usage", "models", "credit", "docs"]);
  for (const tab of llmRoutes.LLM_TABS) {
    assert.match(`llm/${tab}`, llmRoutes.LLM_TAB_PATTERN);
  }
  for (const path of ["llm", "llm/", "llm/bogus", "llm/overview/x", "xllm/overview", "llm/Overview"]) {
    assert.doesNotMatch(path, llmRoutes.LLM_TAB_PATTERN, path);
  }
  assert.deepEqual([...llmRoutes.LLM_LIVE_TABS], ["overview"]);
  assert.equal(llmRoutes.isLlmTabLive("overview"), true);
  assert.equal(llmRoutes.isLlmTabLive("keys"), false);
});

test("兩條 route 共用 routeName llm：pattern 在前、exact 在後，名稱相同", () => {
  const [withTab, exact] = llmRoutes.buildLlmRoutes("AI Models");
  assert.equal(withTab.routeName, "llm");
  assert.equal(exact.routeName, "llm");
  assert.ok(withTab.pattern instanceof RegExp);
  assert.equal(exact.pattern, undefined);
  assert.equal(exact.path, "llm");
  assert.equal(withTab.name, "AI Models");
  assert.equal(exact.name, "AI Models");
});

test("normalizeLlmTab／llmTabPath：未知或缺少子頁退回總覽", () => {
  assert.equal(llmRoutes.normalizeLlmTab(undefined), "overview");
  assert.equal(llmRoutes.normalizeLlmTab("bogus"), "overview");
  assert.equal(llmRoutes.normalizeLlmTab("usage"), "usage");
  assert.equal(llmRoutes.llmTabPath("keys"), "llm/keys");
  assert.equal(llmRoutes.llmTabPath("nope"), "llm/overview");
});

test("開關 fail closed：只有布林 true 才顯示", () => {
  assert.equal(llmRoutes.llmVisibleFromSession({ woow_show_llm: true }), true);
  for (const value of [false, "true", 1, null, undefined]) {
    assert.equal(llmRoutes.llmVisibleFromSession({ woow_show_llm: value }), false, String(value));
  }
  assert.equal(llmRoutes.llmVisibleFromSession({}), false);
  assert.equal(llmRoutes.llmVisibleFromSession(undefined), false);
  assert.equal(llmRoutes.llmGateRedirect("llm", false), "dashboard");
  assert.equal(llmRoutes.llmGateRedirect("llm", true), null);
  assert.equal(llmRoutes.llmGateRedirect("billing", false), null);
});

test("platform_config.showLlm 讀 session.woow_show_llm", () => {
  globalThis.__llmSession = { woow_show_llm: true };
  assert.equal(platformConfig.showLlm(), true);
  globalThis.__llmSession = { woow_show_llm: false };
  assert.equal(platformConfig.showLlm(), false);
  globalThis.__llmSession = {};
  assert.equal(platformConfig.showLlm(), false);
});

// ----------------------------------------------------------------------------- router.js

test("router：#/llm/<子頁> 走 pattern、params.tab 是子頁、不留 params.id", () => {
  for (const tab of llmRoutes.LLM_TABS) {
    const result = go(`#/llm/${tab}`);
    assert.equal(result.current, "llm", tab);
    assert.equal(result.params.tab, tab);
    assert.equal("id" in result.params, false);
  }
});

test("router：#/llm 走 exact、沒有 params.tab（頁面退回總覽）；白名單外是 not-found", () => {
  const exact = go("#/llm");
  assert.equal(exact.current, "llm");
  assert.equal(exact.params.tab, undefined);
  assert.equal(go("#/llm/bogus").current, "not-found");
});

test("router：重新整理（重跑 parseRoute）停在同一個子頁", () => {
  go("#/llm/usage");
  router.parseRoute();
  assert.equal(router.current, "llm");
  assert.equal(router.params.tab, "usage");
});

test("router gate：開關關時 pattern 與 exact 兩條路徑都導回總覽並改 hash", () => {
  for (const hash of ["#/llm", "#/llm/overview", "#/llm/keys"]) {
    const result = go(hash, { llm: false });
    assert.equal(result.current, "dashboard", hash);
    assert.deepEqual(result.params, {});
    assert.equal(result.hash, "#/dashboard");
  }
  // 陽性對照：gate 不影響其他路由，專案 gate 照舊。
  assert.equal(go("#/billing", { llm: false }).current, "billing");
  assert.equal(go("#/projects", { llm: true, projects: false }).current, "dashboard");
  assert.equal(go("#/projects", { llm: false, projects: true }).current, "ai-projects");
});

test("router：llm 兩條 route 都在路由表、pattern 在 exact 之前，麵包屑名稱是 AI Models", () => {
  const indexes = router.routes
    .map((route, index) => (route.routeName === "llm" ? index : -1))
    .filter((index) => index >= 0);
  assert.equal(indexes.length, 2);
  assert.ok(router.routes[indexes[0]].pattern && !router.routes[indexes[1]].pattern);
  assert.equal(router.routes.find((route) => route.routeName === "llm").name, "AI Models");
});

// ----------------------------------------------------------------------------- 側欄／底欄／Header

test("nav_sections：llm 在側欄與底欄都是自己的分區", () => {
  assert.equal(navSections.sidebarSection("llm"), "llm");
  assert.equal(navSections.bottomNavSection("llm"), "llm");
});

test("側欄：AI 模型排在工作區、專案之後、財務分區之前；跟著開關顯示，選中時會亮", () => {
  const sidebar = new Sidebar();
  sidebar.profile = { partner: null };
  const keys = sidebar.navItems.map((item) => item.path || `[${item.section}]`);
  assert.deepEqual(keys.slice(0, 5), ["dashboard", "workspaces", "projects", "llm", "[Finance]"]);
  const item = sidebar.navItems.find((entry) => entry.path === "llm");
  assert.equal(item.name, "AI Models");
  globalThis.__llmNav.llm = true;
  assert.equal(sidebar.showItem(item), true);
  globalThis.__llmNav.llm = false;
  assert.equal(sidebar.showItem(item), false);
  // 開關只管 AI 模型那一項。
  assert.equal(sidebar.showItem(sidebar.navItems.find((entry) => entry.path === "billing")), true);
  sidebar.props = { router: { current: "llm" } };
  assert.equal(sidebar.isActive(item), true);
  assert.equal(sidebar.isActive(sidebar.navItems.find((entry) => entry.path === "dashboard")), false);
});

test("底欄：保留「專案」；show_projects 開時 6 格、關時 5 格；AI 模型跟著開關", () => {
  const nav = new BottomNav();
  const visible = (llm, projects) => {
    globalThis.__llmNav.llm = llm;
    globalThis.__llmNav.projects = projects;
    return nav.navItems.filter((item) => nav.showItem(item)).map((item) => item.path);
  };
  assert.deepEqual(visible(true, true), ["dashboard", "workspaces", "projects", "llm", "billing", "settings"]);
  assert.deepEqual(visible(true, false), ["dashboard", "workspaces", "llm", "billing", "settings"]);
  assert.deepEqual(visible(false, true), ["dashboard", "workspaces", "projects", "billing", "settings"]);
  nav.props = { router: { current: "llm" } };
  assert.equal(nav.isActive(nav.navItems.find((item) => item.path === "llm")), true);
});

test("Header：AI 模型（含子頁）是頂層，手機版沒有「返回」；麵包屑顯示 AI Models", () => {
  const llmHeader = header("llm");
  assert.equal(llmHeader.parentCrumb, null);
  assert.equal(llmHeader.breadcrumbItems.at(-1).label, "AI Models");
  // 陽性對照：真正的子頁有返回目的地。
  assert.deepEqual(header("billing-topup").parentCrumb, { label: "Billing", path: "billing" });
});

// ----------------------------------------------------------------------------- template

async function* walk(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const url = new URL(entry.name + (entry.isDirectory() ? "/" : ""), dir);
    if (entry.isDirectory()) yield* walk(url);
    else yield url;
  }
}

test("OWL template 名稱全域唯一；AI 模型的 template 一律 woow_paas_platform.Llm 前綴", async () => {
  const seen = new Map();
  const llmNames = [];
  for await (const url of walk(new URL("../", PAAS))) {
    if (!url.pathname.endsWith(".xml")) continue;
    const source = await readFile(url, "utf8");
    for (const match of source.matchAll(/t-name="([^"]+)"/g)) {
      const name = match[1];
      assert.ok(!seen.has(name), `template 撞名：${name}（${seen.get(name)} 與 ${url.pathname}）`);
      seen.set(name, url.pathname);
      if (/\/(pages|components)\/llm\//.test(url.pathname)) llmNames.push(name);
    }
  }
  assert.ok(seen.has("woow_paas_platform.OverviewTab"), "服務詳情頁的 OverviewTab 必須還在（陽性對照）");
  assert.deepEqual(llmNames.sort(), [
    "woow_paas_platform.LlmKeyRevealModal",
    "woow_paas_platform.LlmOverviewTab",
    "woow_paas_platform.LlmPage",
  ]);
});

test("Root 掛載 LlmPage，子頁由 router.params.tab 帶入", async () => {
  const rootXml = await read("root.xml");
  assert.match(rootXml, /router\.current === 'llm'">\s*(<!--[\s\S]*?-->\s*)?<LlmPage tab="llmTab"\/>/);
  const rootJs = await read("root.js");
  assert.match(rootJs, /get llmTab\(\) \{\s*return normalizeLlmTab\(this\.router\.params\.tab\);/);
});
