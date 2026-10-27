// 2026-10-27：服務頁「MCP 尚未設定連線」提示的呈現邏輯與輪詢 hook。
// 新開的 Nextcloud MCP 在管理頁存好連線之前一律回 503，平台卻顯示 Running；服務頁要在
// not_configured／not_running 時說清楚「去管理頁哪裡設定」，running／unknown 什麼都不顯示，
// 也不能把服務狀態本身改成錯誤。
// 載入手法同 test_vpn_node_status_fix005.mjs（把 `_t` 換成恆等函式後以 data: URL import）；
// hook 與元件的依賴（owl、jsonRpc、poller）以 new Function 注入假的，直接驅動 load／onData／onError。
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

const ADMIN = "https://paas-cs-5a105e8b-230ef033.woowtech.io";

test("not_configured：明講要去管理頁 → Connection 填好並儲存，MCP 才會開始提供工具", () => {
  const notice = utils.mcpSetupNotice("not_configured");
  assert.equal(notice.state, "not_configured");
  assert.equal(notice.tone, "warning", "黃底軟性提示，不是紅色故障");
  assert.match(notice.text, /admin page → Connection/);
  assert.match(notice.text, /fill it in and save/);
  assert.match(notice.text, /starts providing tools only after that/);
});

test("not_running：叫租戶去管理頁看 Logs", () => {
  const notice = utils.mcpSetupNotice("not_running");
  assert.equal(notice.tone, "warning");
  assert.match(notice.text, /child process is not running/);
  assert.match(notice.text, /Logs on the admin page/);
});

test("running／unknown／其他值都不顯示（讀不到、舊映像不能當成錯誤）", () => {
  for (const state of ["running", "unknown", undefined, null, "", "RUNNING", "error", 1, {}]) {
    assert.equal(utils.mcpSetupNotice(state), null, String(state));
  }
});

test("端點回的值收斂成四個；其他一律 unknown", () => {
  assert.deepEqual(utils.MCP_SETUP_STATES, ["running", "not_configured", "not_running", "unknown"]);
  for (const state of utils.MCP_SETUP_STATES) assert.equal(utils.normalizeMcpSetupState(state), state);
  for (const junk of [undefined, null, "", "Not_Configured", "ok", 0, ["not_configured"]]) {
    assert.equal(utils.normalizeMcpSetupState(junk), "unknown");
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

/** 以假的依賴載入 useMcpSetupStatus；回傳 hook 結果、poller 設定與 rpc 呼叫紀錄。 */
function mountHook({ enabled = true, rpcResult = { success: true, data: { state: "not_configured", admin_url: ADMIN } } } = {}) {
  const calls = [];
  const lifecycle = { mounted: [], unmounted: [] };
  let pollerConfig = null;
  const poller = { started: 0, stopped: 0, start() { this.started += 1; }, stop() { this.stopped += 1; }, refresh() {} };
  const useMcpSetupStatus = new Function(
    "useState", "onMounted", "onWillUnmount", "jsonRpc", "createResourcePoller",
    "normalizeMcpSetupState", "safeAdminUrl",
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
    utils.normalizeMcpSetupState,
    utils.safeAdminUrl,
  );
  const state = { enabled };
  const hook = useMcpSetupStatus(() => 7, () => 42, () => state.enabled);
  return { hook, calls, lifecycle, poller, config: pollerConfig, state };
}

test("hook：只在 enabled 時問 /mcp-status（不帶任何參數），回應收斂後放進 state", async () => {
  const { hook, calls, config, lifecycle, poller } = mountHook();
  assert.equal(hook.state.status, null);
  lifecycle.mounted.forEach((fn) => fn());
  assert.equal(poller.started, 1);
  const data = await config.load();
  assert.deepEqual(calls, [["/api/workspaces/7/services/42/mcp-status", {}, 15000]]);
  config.onData(data);
  assert.equal(hook.state.status, "not_configured");
  assert.equal(hook.state.adminUrl, ADMIN);
  assert.equal(hook.state.loading, false);
  lifecycle.unmounted.forEach((fn) => fn());
  assert.equal(poller.stopped, 1);
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
  assert.equal(hook.state.status, null);
  assert.equal(hook.state.adminUrl, "");
});

test("hook：後端回奇怪的值當 unknown；壞網址不進 state", async () => {
  const { hook, config } = mountHook({ rpcResult: { success: true, data: { state: "boom", admin_url: "javascript:alert(1)" } } });
  config.onData(await config.load());
  assert.equal(hook.state.status, "unknown");
  assert.equal(hook.state.adminUrl, "");
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

test("元件：getEnabled 只看 payload 的 has_mcp_setup_status 與 running；提示文案來自 helper", () => {
  let enabledGetter = null;
  const Notice = new Function(
    "Component", "WoowIcon", "useMcpSetupStatus", "adminPageLabel", "mcpSetupNotice",
    `${stripModule(componentSource)}; return McpSetupStatusNotice;`,
  )(
    class {},
    class {},
    (_ws, _svc, getEnabled) => {
      enabledGetter = getEnabled;
      return { state: { status: "not_configured", adminUrl: ADMIN } };
    },
    utils.adminPageLabel,
    utils.mcpSetupNotice,
  );
  const component = new Notice();
  component.props = { workspaceId: 7, service: { id: 42, state: "running", has_mcp_setup_status: true } };
  component.setup();
  assert.equal(enabledGetter(), true);
  for (const service of [
    { id: 42, state: "deploying", has_mcp_setup_status: true },
    { id: 42, state: "running", has_mcp_setup_status: false },
    { id: 42, state: "running" },
    { id: 42, state: "running", has_mcp_setup_status: "true" },
  ]) {
    component.props = { workspaceId: 7, service };
    assert.equal(enabledGetter(), false, JSON.stringify(service));
  }
  assert.equal(component.notice.state, "not_configured");
  assert.equal(component.adminUrl, ADMIN);
  assert.equal(component.adminPageLabel, "Admin page:");
});

test("模板：只有 notice 有值才畫；連結開新分頁且 noopener；服務頁以 hasMcpSetupStatus 掛載", () => {
  assert.match(componentXml, /<div t-if="notice"/);
  assert.match(componentXml, /role="status"/);
  assert.match(componentXml, /t-att-href="adminUrl" target="_blank" rel="noopener noreferrer"/);
  assert.doesNotMatch(componentXml, /error_banner/, "不是紅色故障 banner");
  assert.match(pageXml, /<McpSetupStatusNotice\s+t-if="hasMcpSetupStatus"/);
  assert.match(pageSource, /get hasMcpSetupStatus\(\) \{\s+return this\.service\?\.has_mcp_setup_status === true;/);
  assert.doesNotMatch(pageXml + pageSource, /['"]mcp-nextcloud['"]/, "前端不用 slug 判斷能力");
});
