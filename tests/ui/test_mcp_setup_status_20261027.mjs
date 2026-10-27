// 2026-10-27（#2040）：服務頁「MCP 尚未設定連線」提示的呈現邏輯、輪詢 hook、服務頁與 Overview 的接線。
// 新開的 Nextcloud MCP 在管理頁存好連線之前一律回 503，平台卻顯示 Running；服務頁要在
// not_configured／not_running 時說清楚「去管理頁哪裡設定」，running／unknown 什麼都不顯示，
// 也不能把服務狀態本身改成錯誤。第五輪：
//  - P1：同一頁 Connection Info 不再寫 ONLINE（改「待設定」「MCP 未運作」）；非管理成員另一種說法、
//    不給管理頁連結；not_running 不用「子行程」；開通視窗多一句「先到 Connection 存好連線」。
//  - P2：提示顯示中，視窗重新取得焦點也立刻重問（並排的視窗不會觸發 visibilitychange）。
//  - P3：backend=missing（MCP 在運作、後端連線還沒設定）→ 較輕的提示、Connection Info「後端未設定」。
// 載入手法同 test_vpn_node_status_fix005.mjs（把 `_t` 換成恆等函式後以 data: URL import；
// 元件 getter 的本體從原始碼切出來、餵真的 helper 執行）。
//
// 跑法：node --test tests/ui/*.mjs
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (relativePath) =>
  readFile(new URL(`../../src/static/src/paas/${relativePath}`, import.meta.url), "utf8");
const load = async (code) => import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
const noTranslate = (source) =>
  source.replace('import { _t } from "@web/core/l10n/translation";', "const _t = (message) => message;");
const stripModule = (source) => source.replace(/^import [\s\S]*?;\s*$/gm, "").replace(/^export /gm, "");

const utils = await load(noTranslate(await read("utils/mcp_setup_status.js")));
const hookSource = await read("services/mcp_setup_status.js");
const componentSource = await read("components/mcp-setup-status/McpSetupStatusNotice.js");
const componentXml = await read("components/mcp-setup-status/McpSetupStatusNotice.xml");
const pageXml = await read("pages/service/ServiceDetailPage.xml");
const pageSource = await read("pages/service/ServiceDetailPage.js");
const overviewSource = await read("pages/service/tabs/OverviewTab.js");
const overviewXml = await read("pages/service/tabs/OverviewTab.xml");
const modalSource = await read("components/modal/OneTimeCredentialsModal.js");
const modalXml = await read("components/modal/OneTimeCredentialsModal.xml");
const configureXml = await read("pages/configure/AppConfigurationPage.xml");

const ADMIN = "https://paas-cs-5a105e8b-230ef033.woowtech.io";

/** class member（`get name() {` 或 `name() {`）的本體；略過字串與註解裡的大括號。 */
function memberBody(source, head) {
  const start = source.indexOf(head);
  assert.notEqual(start, -1, `找不到 ${head}`);
  let depth = 1;
  let index = start + head.length;
  while (index < source.length) {
    const two = source.slice(index, index + 2);
    const char = source[index];
    if (two === "//") index = source.indexOf("\n", index);
    else if (two === "/*") index = source.indexOf("*/", index) + 2;
    else if (char === '"' || char === "'" || char === "`") {
      index += 1;
      while (source[index] !== char) index += source[index] === "\\" ? 2 : 1;
      index += 1;
    } else {
      if (char === "{") depth += 1;
      if (char === "}") depth -= 1;
      if (depth === 0) return source.slice(start + head.length, index);
      index += 1;
    }
  }
  throw new Error(`${head} 的大括號沒有配對`);
}
const getter = (source, name, deps = {}) =>
  new Function(...Object.keys(deps), `return function () {${memberBody(source, `get ${name}() {`)}};`)(
    ...Object.values(deps));

// ── 呈現邏輯（utils）────────────────────────────────────────────────────────────

test("情況只有三種：not_configured、not_running、running＋backend=missing；其他一律不提示", () => {
  assert.deepEqual(utils.MCP_SETUP_CONDITIONS, ["not_configured", "not_running", "backend_missing"]);
  for (const backend of ["configured", "missing", "unknown", undefined]) {
    assert.equal(utils.mcpSetupCondition("not_configured", backend), "not_configured");
    assert.equal(utils.mcpSetupCondition("not_running", backend), "not_running");
  }
  assert.equal(utils.mcpSetupCondition("running", "missing"), "backend_missing");
  for (const [state, backend] of [
    ["running", "configured"], ["running", "unknown"], ["running", undefined], ["unknown", "missing"],
    ["unknown", "unknown"], [null, null], ["RUNNING", "missing"], ["running", "Missing"], [{}, []],
  ]) {
    assert.equal(utils.mcpSetupCondition(state, backend), "", `${state}/${backend}`);
  }
});

test("端點回的值收斂成封閉集合；其他一律 unknown", () => {
  assert.deepEqual(utils.MCP_SETUP_STATES, ["running", "not_configured", "not_running", "unknown"]);
  assert.deepEqual(utils.MCP_BACKEND_STATES, ["configured", "missing", "unknown"]);
  for (const state of utils.MCP_SETUP_STATES) assert.equal(utils.normalizeMcpSetupState(state), state);
  for (const backend of utils.MCP_BACKEND_STATES) assert.equal(utils.normalizeMcpBackendState(backend), backend);
  for (const junk of [undefined, null, "", "Not_Configured", "ok", 0, ["not_configured"], "MISSING"]) {
    assert.equal(utils.normalizeMcpSetupState(junk), "unknown");
    assert.equal(utils.normalizeMcpBackendState(junk), "unknown");
  }
});

test("not_configured：管理者被叫去管理頁 → Connection；非管理成員被請去找擁有者或管理員", () => {
  const manager = utils.mcpSetupNotice("not_configured", true);
  assert.deepEqual([manager.condition, manager.tone, manager.icon], ["not_configured", "warning", "settings"]);
  assert.match(manager.text, /admin page → Connection, fill it in and save/);
  assert.match(manager.text, /starts providing tools only after that/);
  for (const canManage of [false, undefined, "true", 1]) {
    const member = utils.mcpSetupNotice("not_configured", canManage);
    assert.equal(member.tone, "warning");
    assert.match(member.text, /ask a workspace owner or admin/, String(canManage));
    assert.doesNotMatch(member.text, /fill it in and save/, "做不到的指示不給非管理者");
  }
});

test("not_running：不用「子行程／child process」；非管理成員另一種說法", () => {
  const manager = utils.mcpSetupNotice("not_running", true);
  assert.equal(manager.tone, "warning");
  assert.match(manager.text, /The MCP server is not running\. Check Logs on the admin page\./);
  const member = utils.mcpSetupNotice("not_running", false);
  assert.match(member.text, /ask a workspace owner or admin to check Logs/i);
  for (const notice of [manager, member]) assert.doesNotMatch(notice.text, /child process|子行程/i);
});

test("backend_missing（P3）：較輕的 info 提示，說工具會失敗、指向 Connection", () => {
  const manager = utils.mcpSetupNotice("backend_missing", true);
  assert.deepEqual([manager.condition, manager.tone, manager.icon], ["backend_missing", "info", "info"]);
  assert.match(manager.text, /backend connection is not set up yet/);
  assert.match(manager.text, /tools that need the backend will not work/);
  assert.match(manager.text, /under Connection on the admin page/);
  const member = utils.mcpSetupNotice("backend_missing", false);
  assert.equal(member.tone, "info");
  assert.match(member.text, /ask a workspace owner or admin/);
});

test("不是三種情況（running、unknown、其他值）都不顯示", () => {
  for (const condition of ["", "running", "unknown", undefined, null, "RUNNING", "error", 1, {}]) {
    assert.equal(utils.mcpSetupNotice(condition, true), null, String(condition));
    assert.equal(utils.mcpSetupNotice(condition, false), null, String(condition));
  }
});

test("Connection Info：三種情況各一個值與文字，其他回空字串（沿用原本的 online／updating／offline）", () => {
  assert.equal(utils.mcpConnectionStatusFor("not_configured"), "needs_setup");
  assert.equal(utils.mcpConnectionStatusFor("not_running"), "mcp_not_running");
  assert.equal(utils.mcpConnectionStatusFor("backend_missing"), "backend_missing");
  for (const other of ["", undefined, null, "running", "unknown", "online"]) {
    assert.equal(utils.mcpConnectionStatusFor(other), "", String(other));
  }
  assert.equal(utils.mcpConnectionStatusLabel("needs_setup"), "NEEDS SETUP");
  assert.equal(utils.mcpConnectionStatusLabel("mcp_not_running"), "MCP NOT RUNNING");
  assert.equal(utils.mcpConnectionStatusLabel("backend_missing"), "BACKEND NOT SET");
  for (const other of ["online", "offline", "updating", "", undefined]) {
    assert.equal(utils.mcpConnectionStatusLabel(other), "", String(other));
  }
});

test("管理頁網址只收後端給的 https://<hostname>，其他不當成連結", () => {
  assert.equal(utils.safeAdminUrl(ADMIN), ADMIN);
  assert.equal(utils.safeAdminUrl(`${ADMIN}/`), `${ADMIN}/`);
  for (const bad of [
    undefined, null, "", 42, "javascript:alert(1)", "http://paas-cs-x.woowtech.io",
    "https://paas-cs-x.woowtech.io:8443", "https://u:p@paas-cs-x.woowtech.io",
    "https://paas-cs-x.woowtech.io/private_abc/mcp", "https://paas-cs-x.woowtech.io/?q=1",
    "https://paas-cs-x.woowtech.io/#x", "https://localhost", "//paas-cs-x.woowtech.io", "data:text/html,x",
  ]) {
    assert.equal(utils.safeAdminUrl(bad), "", String(bad));
  }
});

// ── 輪詢 hook ────────────────────────────────────────────────────────────────────

/** 以假的依賴載入 useMcpSetupStatus；回傳 hook 結果、poller 設定、rpc 呼叫紀錄、假視窗與時鐘。 */
function mountHook({
  enabled = true,
  rpcResult = { success: true, data: { state: "not_configured", backend: "unknown", admin_url: ADMIN } },
} = {}) {
  const calls = [];
  const lifecycle = { mounted: [], unmounted: [] };
  let pollerConfig = null;
  const poller = {
    started: 0, stopped: 0, refreshed: 0,
    start() { this.started += 1; }, stop() { this.stopped += 1; }, refresh() { this.refreshed += 1; },
  };
  const listeners = new Map();
  const windowRef = {
    addEventListener(type, fn) { listeners.set(type, fn); },
    removeEventListener(type, fn) { if (listeners.get(type) === fn) listeners.delete(type); },
  };
  const clock = { now: 100000 };
  const timers = { next: 1, pending: new Map(), cancelled: [] };
  const schedule = (fn, ms) => {
    const id = timers.next++;
    timers.pending.set(id, { fn, ms });
    return id;
  };
  const cancel = (id) => {
    timers.cancelled.push(id);
    timers.pending.delete(id);
  };
  const useMcpSetupStatus = new Function(
    "useState", "onMounted", "onWillUnmount", "jsonRpc", "createResourcePoller",
    "mcpSetupCondition", "normalizeMcpBackendState", "normalizeMcpSetupState", "safeAdminUrl",
    `${stripModule(hookSource)}; return useMcpSetupStatus;`,
  )(
    (value) => value,
    (fn) => lifecycle.mounted.push(fn),
    (fn) => lifecycle.unmounted.push(fn),
    async (...args) => {
      calls.push(args);
      if (rpcResult instanceof Error) throw rpcResult;
      return rpcResult;
    },
    (config) => {
      pollerConfig = config;
      return poller;
    },
    utils.mcpSetupCondition,
    utils.normalizeMcpBackendState,
    utils.normalizeMcpSetupState,
    utils.safeAdminUrl,
  );
  const state = { enabled };
  const hook = useMcpSetupStatus(() => 7, () => 42, () => state.enabled,
    { windowRef, now: () => clock.now, schedule, cancel });
  return { hook, calls, lifecycle, poller, config: pollerConfig, state, listeners, clock, timers };
}

test("hook：只在 enabled 時問 /mcp-status（不帶任何參數），state／backend／網址收斂後放進 state", async () => {
  const { hook, calls, config, lifecycle, poller, listeners } = mountHook({
    rpcResult: { success: true, data: { state: "running", backend: "missing", admin_url: ADMIN } },
  });
  assert.equal(hook.state.status, null);
  assert.equal(hook.state.backend, "unknown");
  lifecycle.mounted.forEach((fn) => fn());
  assert.equal(poller.started, 1);
  assert.ok(listeners.has("focus"), "掛上視窗 focus 監聽");
  const data = await config.load();
  assert.deepEqual(calls, [["/api/workspaces/7/services/42/mcp-status", {}, 15000]]);
  config.onData(data);
  assert.deepEqual([hook.state.status, hook.state.backend, hook.state.adminUrl], ["running", "missing", ADMIN]);
  assert.equal(hook.state.loading, false);
  lifecycle.unmounted.forEach((fn) => fn());
  assert.equal(poller.stopped, 1);
  assert.ok(!listeners.has("focus"), "卸載時拿掉 focus 監聽");
});

test("hook：沒有能力或服務不在 running 時一個請求都不發，並清掉舊結果", async () => {
  const { hook, calls, config, state } = mountHook();
  config.onData(await config.load());
  assert.equal(hook.state.status, "not_configured");
  state.enabled = false;
  const skipped = await config.load();
  assert.deepEqual(skipped, { skipped: true });
  assert.equal(calls.length, 1);
  config.onData(skipped);
  assert.deepEqual([hook.state.status, hook.state.backend, hook.state.adminUrl], [null, "unknown", ""]);
});

test("hook：後端回奇怪的值當 unknown；壞網址不進 state", async () => {
  const { hook, config } = mountHook({
    rpcResult: { success: true, data: { state: "boom", backend: "<b>", admin_url: "javascript:alert(1)" } },
  });
  config.onData(await config.load());
  assert.deepEqual([hook.state.status, hook.state.backend, hook.state.adminUrl], ["unknown", "unknown", ""]);
});

test("hook：請求失敗（success:false、網路錯誤）保留上一筆，不讓提示閃掉或閃出來", async () => {
  const ok = mountHook();
  ok.config.onData(await ok.config.load());
  for (const failure of [{ success: false, error: "Service not found" }, new Error("network")]) {
    const failing = mountHook({ rpcResult: failure });
    await assert.rejects(() => failing.config.load());
  }
  ok.config.onError();
  assert.equal(ok.hook.state.status, "not_configured");
  assert.equal(ok.hook.state.loading, false);
});

test("hook（P2）：提示顯示中，視窗重新取得焦點就立刻重問；剛問過、沒有提示、卸載後都不問", async () => {
  const f = mountHook();
  f.lifecycle.mounted.forEach((fn) => fn());
  const focus = f.listeners.get("focus");
  focus();                                   // 還沒有結果：沒有提示 ⇒ 不問
  assert.equal(f.poller.refreshed, 0);
  f.config.onData(await f.config.load());     // not_configured（load 記下這次請求的時間）
  f.clock.now += 500;
  focus();                                   // 剛問過（visibilitychange 與 focus 常常一起來）
  assert.equal(f.poller.refreshed, 0);
  f.clock.now += 1500;
  focus();
  assert.equal(f.poller.refreshed, 1, "存好連線、切回這個視窗：不必等下一輪 30 秒");
  f.hook.state.status = "running";
  f.hook.state.backend = "configured";
  f.clock.now += 5000;
  focus();
  assert.equal(f.poller.refreshed, 1, "健康時不因為 focus 多打請求");
  f.hook.state.backend = "missing";          // backend_missing 也是要動手的情況
  focus();
  assert.equal(f.poller.refreshed, 2);
  f.lifecycle.unmounted.forEach((fn) => fn());
  focus();
  assert.equal(f.poller.refreshed, 2);
});

test("hook（P2）：提示顯示中每 10 秒再問一次；健康、不問、卸載時就停", async () => {
  const f = mountHook();
  f.lifecycle.mounted.forEach((fn) => fn());
  f.config.onData(await f.config.load());            // not_configured
  assert.equal(f.timers.pending.size, 1);
  const [[firstId, first]] = [...f.timers.pending];
  assert.equal(first.ms, 10000);
  first.fn();                                        // 10 秒到：重問
  f.timers.pending.delete(firstId);
  assert.equal(f.poller.refreshed, 1);
  f.config.onError();                                // 這一問失敗：畫面照舊提示 ⇒ 再排一次
  assert.equal(f.timers.pending.size, 1);
  f.config.onData({ skipped: false, data: { state: "running", backend: "configured", admin_url: ADMIN } });
  assert.equal(f.timers.pending.size, 0, "健康了：回到 30 秒一輪，不再額外排");
  f.config.onData({ skipped: false, data: { state: "running", backend: "missing", admin_url: ADMIN } });
  assert.equal(f.timers.pending.size, 1, "backend_missing 也是要動手的情況");
  f.config.onData({ skipped: true });
  assert.equal(f.timers.pending.size, 0, "不問了（服務離開 running）：取消");
  f.config.onData({ skipped: false, data: { state: "not_running", backend: "unknown", admin_url: ADMIN } });
  assert.equal(f.timers.pending.size, 1);
  f.lifecycle.unmounted.forEach((fn) => fn());
  assert.equal(f.timers.pending.size, 0, "卸載時取消");
  const match = hookSource.match(/export const MCP_ACTIONABLE_REFRESH_MS = (\d+);/);
  assert.ok(match && Number(match[1]) < 30000, "比平常的 30 秒短");
});

test("hook：focus 的間隔常數是給 visibilitychange＋focus 去重用的（短於輪詢、長於一次往返）", () => {
  const match = hookSource.match(/export const MCP_FOCUS_REFRESH_GAP_MS = (\d+);/);
  assert.ok(match);
  const gap = Number(match[1]);
  assert.ok(gap >= 500 && gap <= 5000, String(gap));
});

// ── 元件：純呈現 ─────────────────────────────────────────────────────────────────

function notice(props) {
  const Notice = new Function(
    "Component", "WoowIcon", "adminPageLabel", "mcpSetupNotice",
    `${stripModule(componentSource)}; return McpSetupStatusNotice;`,
  )(class {}, class {}, utils.adminPageLabel, utils.mcpSetupNotice);
  const component = new Notice();
  component.props = props;
  return component;
}

test("元件：管理者看到管理頁連結；非管理成員看到「請擁有者或管理員」、沒有連結", () => {
  const manager = notice({ condition: "not_configured", adminUrl: ADMIN, canManage: true });
  assert.equal(manager.notice.condition, "not_configured");
  assert.match(manager.notice.text, /fill it in and save/);
  assert.equal(manager.adminUrl, ADMIN);
  assert.equal(manager.adminPageLabel, "Admin page:");
  for (const canManage of [false, undefined]) {
    const member = notice({ condition: "not_configured", adminUrl: ADMIN, canManage });
    assert.match(member.notice.text, /ask a workspace owner or admin/);
    assert.equal(member.adminUrl, "", "拿不到管理密碼的人不給連結");
  }
  assert.equal(notice({ condition: "backend_missing", adminUrl: ADMIN, canManage: true }).notice.tone, "info");
  assert.equal(notice({ condition: "", canManage: true }).notice, null);
  assert.doesNotMatch(componentSource, /^import .*services\//m, "輪詢不在元件裡（一頁一個 poller）");
  assert.doesNotMatch(componentSource, /useMcpSetupStatus\(|jsonRpc\(/);
});

test("元件模板：只有 notice 有值才畫；連結只在 adminUrl 有值時、開新分頁且 noopener", () => {
  assert.match(componentXml, /<div t-if="notice"/);
  assert.match(componentXml, /o_woow_mcp_setup_notice--\{\{ notice\.tone \}\}/);
  assert.match(componentXml, /t-att-data-mcp-condition="notice\.condition"/);
  assert.match(componentXml, /role="status"/);
  assert.match(componentXml, /<span t-if="adminUrl" class="o_woow_mcp_setup_notice__link">/);
  assert.match(componentXml, /t-att-href="adminUrl" target="_blank" rel="noopener noreferrer"/);
  assert.doesNotMatch(componentXml, /error_banner/, "不是紅色故障 banner");
});

// ── 服務頁：一頁一個 poller、gate、立刻重問 ─────────────────────────────────────

test("服務頁：gate 只看 payload 的 has_mcp_setup_status 與 running", () => {
  const hasFlag = getter(pageSource, "hasMcpSetupStatus");
  const enabled = getter(pageSource, "mcpSetupEnabled");
  const page = (service) => {
    const self = { service };
    Object.defineProperty(self, "hasMcpSetupStatus", { get: hasFlag });
    return self;
  };
  assert.equal(enabled.call(page({ state: "running", has_mcp_setup_status: true })), true);
  for (const service of [
    { state: "deploying", has_mcp_setup_status: true },
    { state: "upgrading", has_mcp_setup_status: true },
    { state: "running", has_mcp_setup_status: false },
    { state: "running" },
    { state: "running", has_mcp_setup_status: "true" },
    null,
  ]) {
    assert.equal(Boolean(enabled.call(page(service))), false, JSON.stringify(service));
  }
});

test("服務頁：mcpSetupCondition 不問的時候一律空字串（不吃上一筆殘留的結果）", () => {
  const condition = getter(pageSource, "mcpSetupCondition", { mcpSetupCondition: utils.mcpSetupCondition });
  const page = (enabled, status, backend) => ({ mcpSetupEnabled: enabled, mcpSetup: { state: { status, backend } } });
  assert.equal(condition.call(page(true, "not_configured", "unknown")), "not_configured");
  assert.equal(condition.call(page(true, "not_running", "unknown")), "not_running");
  assert.equal(condition.call(page(true, "running", "missing")), "backend_missing");
  assert.equal(condition.call(page(true, "running", "configured")), "");
  assert.equal(condition.call(page(true, "unknown", "missing")), "");
  assert.equal(condition.call(page(false, "not_configured", "unknown")), "", "服務離開 running：舊結果不再顯示");
});

test("服務頁：可問與否一變（剛載入、進出 running）就立刻重問一次，沒變就不重問", () => {
  const sync = new Function(`return function () {${memberBody(pageSource, "_syncMcpSetupPoller() {")}};`)();
  const page = { _mcpSetupEnabled: false, mcpSetupEnabled: false, refreshed: 0 };
  page.mcpSetup = { refresh: () => { page.refreshed += 1; } };
  sync.call(page);
  assert.equal(page.refreshed, 0, "服務還沒載入、仍不可問：不重問");
  page.mcpSetupEnabled = true;
  sync.call(page);
  assert.equal(page.refreshed, 1, "服務載入完成且 running：不等 30 秒");
  sync.call(page);
  assert.equal(page.refreshed, 1, "背景重讀服務、狀態沒變：不多打");
  page.mcpSetupEnabled = false;
  sync.call(page);
  assert.equal(page.refreshed, 2, "離開 running：重問一次讓 hook 清掉舊結果");
  assert.match(memberBody(pageSource, "async loadService({ silent = false } = {}) {"),
    /this\.startPollingIfNeeded\(\);\s+this\._syncMcpSetupPoller\(\);/);
});

test("服務頁：hook 在 setup 裡建一次，頁首提示與 OverviewTab 都讀同一個值", () => {
  assert.match(pageSource, /this\.mcpSetup = useMcpSetupStatus\(\s*\(\) => this\.props\.workspaceId,\s*\(\) => this\.props\.serviceId,\s*\(\) => this\.mcpSetupEnabled,\s*\);/);
  const setupBody = memberBody(pageSource, "setup() {");
  assert.ok(setupBody.indexOf("useMcpSetupStatus(") < setupBody.indexOf("onMounted("),
    "poller 先 start，再載入服務（載入完才觸發 _syncMcpSetupPoller）");
  assert.match(pageXml, /<McpSetupStatusNotice\s+t-if="mcpSetupCondition"\s+condition="mcpSetupCondition"\s+adminUrl="mcpSetup\.state\.adminUrl"\s+canManage="canManageService"\/>/);
  assert.match(pageXml, /<OverviewTab\s+t-if="currentTab === 'overview'"\s+service="service"\s+workspaceId="props\.workspaceId"\s+mcpSetupCondition="mcpSetupCondition"/);
  assert.doesNotMatch(pageXml + pageSource, /['"]mcp-nextcloud['"]/, "前端不用 slug 判斷能力");
});

// ── Overview：Connection Info ───────────────────────────────────────────────────

/** OverviewTab 的 connectionStatus／mcpConnectionLabel，接上真的 helper。 */
function overview(service, { condition = "", workloadFault = false, nodeProblem = false } = {}) {
  const self = {
    props: { service, mcpSetupCondition: condition },
    vpn: { state: { report: nodeProblem ? { state: "needs_login" } : null } },
  };
  Object.defineProperty(self, "mcpSetupConnectionStatus", {
    get: getter(overviewSource, "mcpSetupConnectionStatus", { mcpConnectionStatusFor: utils.mcpConnectionStatusFor }),
  });
  Object.defineProperty(self, "connectionStatus", {
    get: getter(overviewSource, "connectionStatus", {
      hasConfirmedWorkloadFault: () => workloadFault,
      hasNodeProblem: (report) => Boolean(report),
    }),
  });
  Object.defineProperty(self, "mcpConnectionLabel", {
    get: getter(overviewSource, "mcpConnectionLabel", { mcpConnectionStatusLabel: utils.mcpConnectionStatusLabel }),
  });
  return self;
}

test("Connection Info（P1）：提示在說要設定時不再寫 ONLINE，改成同一種說法", () => {
  const running = { state: "running" };
  assert.deepEqual([overview(running, { condition: "not_configured" }).connectionStatus,
    overview(running, { condition: "not_configured" }).mcpConnectionLabel], ["needs_setup", "NEEDS SETUP"]);
  assert.deepEqual([overview(running, { condition: "not_running" }).connectionStatus,
    overview(running, { condition: "not_running" }).mcpConnectionLabel], ["mcp_not_running", "MCP NOT RUNNING"]);
  assert.deepEqual([overview(running, { condition: "backend_missing" }).connectionStatus,
    overview(running, { condition: "backend_missing" }).mcpConnectionLabel], ["backend_missing", "BACKEND NOT SET"]);
});

test("Connection Info：running／unknown（沒有情況）維持 ONLINE；更嚴重的狀況仍是 OFFLINE；其他狀態不受影響", () => {
  for (const condition of ["", undefined]) {
    const view = overview({ state: "running" }, { condition });
    assert.equal(view.connectionStatus, "online");
    assert.equal(view.mcpConnectionLabel, "");
  }
  for (const flags of [{ workloadFault: true }, { nodeProblem: true }]) {
    assert.equal(overview({ state: "running" }, { condition: "not_configured", ...flags }).connectionStatus, "offline");
  }
  assert.equal(overview({ state: "upgrading" }, { condition: "not_configured" }).connectionStatus, "updating");
  assert.equal(overview({ state: "error" }, { condition: "not_running" }).connectionStatus, "offline");
  assert.equal(overview({ state: "deploying", last_upgraded_at: "2026-10-01" }, { condition: "backend_missing" }).connectionStatus,
    "updating");
});

test("Connection Info 模板：新文字排在 OFFLINE 之前、沿用同一格的 class", () => {
  assert.match(overviewXml,
    /<t t-if="connectionStatus === 'online'">ONLINE<\/t>\s*<t t-elif="connectionStatus === 'updating'">UPDATING<\/t>\s*<t t-elif="mcpConnectionLabel" t-esc="mcpConnectionLabel"\/>\s*<t t-else="">OFFLINE<\/t>/);
  assert.match(overviewXml, /t-att-class="'o_woow_info_value o_woow_connection_status ' \+ connectionStatus"/);
  assert.match(overviewSource, /mcpSetupCondition: \{ type: String, optional: true \}/);
});

// ── 開通的一次性憑證視窗（P1 b） ────────────────────────────────────────────────

const modal = await load(
  noTranslate(modalSource)
    .replace('import { Component, useState } from "@odoo/owl";', "const Component = class {}; const useState = (v) => v;")
    .replace('import { WoowButton } from "../button/WoowButton";', "const WoowButton = class {};")
    .replace('import { WoowIcon } from "../icon/WoowIcon";', "const WoowIcon = class {};"),
);

test("開通視窗：只有 has_mcp_setup_status === true 才多一句「先到 Connection 存好連線」", () => {
  const hint = modal.oneTimeMcpSetupHint({ has_mcp_setup_status: true, admin_url: ADMIN });
  assert.match(hint, /Save the connection under Connection on the admin page first/);
  assert.match(hint, /MCP starts providing tools only after that/);
  for (const data of [
    null, undefined, {}, { has_mcp_setup_status: false }, { has_mcp_setup_status: "true" },
    { admin_url: ADMIN, template: { admin_password_carrier: "config_json" } },
  ]) {
    assert.equal(modal.oneTimeMcpSetupHint(data), "", JSON.stringify(data));
  }
});

test("開通視窗模板：setupHint 有值才畫；開通頁傳進去，Reset（服務頁）不傳", () => {
  assert.match(modalSource, /setupHint: \{ type: String, optional: true \}/);
  assert.match(modalXml, /<p t-if="props\.setupHint" class="o_woow_hint o_woow_onetime_setup_hint">\s*<strong t-esc="props\.setupHint"\/>/);
  assert.match(configureXml, /mode="'provision'"\s+setupHint="state\.oneTimeSetupHint"/);
  assert.doesNotMatch(pageXml, /setupHint=/, "Reset 的服務多半早就存好連線，不顯示這句");
});
