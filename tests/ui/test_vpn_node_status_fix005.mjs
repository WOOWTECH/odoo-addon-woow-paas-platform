// INFRA-FIX-005：VPN 節點（charts/tailscale）「VPN 節點狀態」卡的呈現邏輯。
// INFRA-TS-007 三項缺口的前端這一半：(1) serve 略過的原因看得到，(2) 節點需要登入時有正確的登入說明
// （叢集內部的登入網址不給，改說用 pre-auth key），(3) 在線狀態：NeedsLogin 時 Connection Info 不再寫 ONLINE。
// 載入手法同 test_service_presentation_workload_1647.mjs（把 `_t` 換成恆等函式後以 data: URL import；
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

const vpn = await load(noTranslate(await read("utils/vpn_node_status.js")));
const overviewSource = await read("pages/service/tabs/OverviewTab.js");
const overviewXml = await read("pages/service/tabs/OverviewTab.xml");
const pollerSource = await read("services/vpn_node_status.js");

function getterBody(componentSource, name) {
  const head = `get ${name}() {`;
  const start = componentSource.indexOf(head);
  assert.notEqual(start, -1, `找不到 getter ${name}`);
  let depth = 1;
  let index = start + head.length;
  while (index < componentSource.length) {
    const two = componentSource.slice(index, index + 2);
    const char = componentSource[index];
    if (two === "//") index = componentSource.indexOf("\n", index);
    else if (two === "/*") index = componentSource.indexOf("*/", index) + 2;
    else if (char === '"' || char === "'" || char === "`") {
      index += 1;
      while (componentSource[index] !== char) index += componentSource[index] === "\\" ? 2 : 1;
      index += 1;
    } else {
      if (char === "{") depth += 1;
      if (char === "}") depth -= 1;
      if (depth === 0) return componentSource.slice(start + head.length, index);
      index += 1;
    }
  }
  throw new Error(`getter ${name} 的大括號沒有配對`);
}

const report = (over = {}) => ({
  state: "online",
  probe: "ok",
  control_plane: "workspace",
  tailnet_ips: ["100.64.0.3"],
  dns_name: null,
  login: { kind: "none", url: null, register_key: null },
  serve: { wanted_known: true, forwards: [], skipped: [], ignored_all: null, extra_config: false },
  ...over,
});

/** OverviewTab 的 connectionStatus getter，接上真的 helper。 */
const connectionStatusOf = (service, nodeReport) =>
  new Function(
    "hasConfirmedWorkloadFault",
    "hasNodeProblem",
    `return function () {${getterBody(overviewSource, "connectionStatus")}};`,
  )(() => false, vpn.hasNodeProblem).call({ props: { service }, vpn: { state: { report: nodeReport } } });

test("(3) pod Ready 但節點需要登入／等待核可／離線／停止：Connection Info 不是 ONLINE", () => {
  for (const state of ["needs_login", "needs_approval", "offline", "stopped"]) {
    assert.equal(connectionStatusOf({ state: "running" }, report({ state })), "offline", state);
  }
});

test("(3) 節點在線、啟動中、讀不到，或還沒有報告：維持原本的 online（讀不到不能把健康服務打成離線）", () => {
  for (const nodeReport of [report(), report({ state: "starting" }), report({ state: "unknown", probe: "no_pod" }), null]) {
    assert.equal(connectionStatusOf({ state: "running" }, nodeReport), "online");
  }
});

test("(3) 升級中與其他 state 不受節點狀態影響", () => {
  assert.equal(connectionStatusOf({ state: "upgrading" }, report({ state: "needs_login" })), "updating");
  assert.equal(connectionStatusOf({ state: "error" }, report()), "offline");
});

test("狀態標籤與色調：每個 state 都有標籤，有問題的是 bad，在線是 ok", () => {
  const states = ["online", "offline", "needs_login", "needs_approval", "starting", "stopped", "unknown"];
  const labels = new Set(states.map(vpn.nodeStateLabel));
  assert.equal(labels.size, states.length, "七個狀態標籤互不相同");
  assert.equal(vpn.nodeStateTone("online"), "ok");
  for (const state of ["needs_login", "needs_approval", "offline"]) assert.equal(vpn.nodeStateTone(state), "bad");
  assert.equal(vpn.nodeStateTone("whatever"), "muted");
  assert.equal(vpn.nodeStateLabel("whatever"), vpn.nodeStateLabel("unknown"));
});

test("(3) needs_login 的說明明講『服務在跑但節點已不在線』", () => {
  assert.match(vpn.nodeStateHint(report({ state: "needs_login" })), /service is running, but this node is no longer logged in/);
  assert.equal(vpn.nodeStateHint(report()), "");
  assert.match(vpn.nodeStateHint(report({ state: "unknown", probe: "no_pod" })), /re-checked automatically/);
  assert.equal(vpn.nodeStateHint(null), "");
});

test("(2) 工作區 Headscale：說用 pre-auth key，不給（也無從給）叢集內部登入網址", () => {
  const lines = vpn.loginGuidance(
    report({ state: "needs_login", login: { kind: "cluster_internal", url: null, register_key: "authreq-abc123" } }),
    true,
  );
  assert.match(lines[0].text, /pre-auth key in Headplane of this workspace's Headscale/);
  assert.ok(lines.some((l) => /internal address that a browser cannot open/.test(l.text)));
  const code = lines.find((l) => l.kind === "code");
  assert.equal(code.code, "authreq-abc123");
  assert.equal(lines.filter((l) => l.kind === "link").length, 0);
  assert.doesNotMatch(JSON.stringify(lines), /cluster\.local/);
});

test("(2) 官方 Tailscale：給外部登入連結；自訂控制伺服器：說在自己的伺服器建 key", () => {
  const official = vpn.loginGuidance(
    report({
      state: "needs_login",
      control_plane: "official",
      login: { kind: "external_url", url: "https://login.tailscale.com/a/abc123", register_key: null },
    }),
    true,
  );
  assert.match(official[0].text, /Tailscale admin console/);
  assert.deepEqual(official.find((l) => l.kind === "link").url, "https://login.tailscale.com/a/abc123");
  const custom = vpn.loginGuidance(report({ state: "needs_login", control_plane: "custom" }), true);
  assert.match(custom[0].text, /on your control server/);
  assert.equal(custom.filter((l) => l.kind === "link" || l.kind === "code").length, 0);
});

test("(2) 非 owner／admin 只被請去找管理者，不會看到任何登入入口", () => {
  const lines = vpn.loginGuidance(
    report({
      state: "needs_login",
      login: { kind: "external_url", url: "https://login.tailscale.com/a/abc123", register_key: "authreq-x" },
    }),
    false,
  );
  assert.equal(lines.length, 1);
  assert.match(lines[0].text, /Ask a workspace owner or admin/);
});

test("(2) 沒有需要登入就沒有登入說明", () => {
  for (const state of ["online", "starting", "needs_approval", "offline", "stopped", "unknown"]) {
    assert.deepEqual(vpn.loginGuidance(report({ state }), true), [], state);
  }
  assert.deepEqual(vpn.loginGuidance(null, true), []);
});

test("(1) 轉發清單：生效、尚未生效、Service 不存在，以及被略過的項目都附原因", () => {
  const rows = vpn.forwardRows(
    report({
      serve: {
        wanted_known: true,
        forwards: [
          { port: 9004, service: "svc-0b4a8def-headscale", target_port: 8080, status: "applied" },
          { port: 9101, service: "no-such-svc", target_port: 8080, status: "service_missing" },
          { port: 9105, service: "other", target_port: 80, status: "not_applied" },
        ],
        skipped: [
          { entry: "9102=svc-x.other-ns.svc.cluster.local:80", reason: "other_namespace" },
          { entry: "9103=10.0.0.5:80", reason: "other_namespace" },
          { entry: "70000=a:1", reason: "port_range" },
        ],
        ignored_all: null,
        extra_config: false,
      },
    }),
  );
  assert.deepEqual(
    rows.map((r) => [r.label, r.tone, r.skipped]),
    [
      ["9004 → svc-0b4a8def-headscale:8080", "ok", false],
      ["9101 → no-such-svc:8080", "bad", false],
      ["9105 → other:80", "warn", false],
      ["9102=svc-x.other-ns.svc.cluster.local:80", "bad", true],
      ["9103=10.0.0.5:80", "bad", true],
      ["70000=a:1", "bad", true],
    ],
  );
  assert.match(rows[1].status, /Service not found in this workspace/);
  assert.match(rows[3].status, /Only Services of this workspace are allowed/);
  assert.match(rows[5].status, /between 1 and 65535/);
  assert.equal(new Set(rows.map((r) => r.key)).size, rows.length, "key 不重複（t-key）");
});

test("(1) 每個略過代碼與轉發狀態都有各自的文案；不認得的退回通用文案", () => {
  const reasons = ["bad_format", "port_range", "other_namespace", "not_service_name", "duplicate_port", "too_many", "namespace_unknown"];
  assert.equal(new Set(reasons.map(vpn.skipReasonLabel)).size, reasons.length);
  assert.equal(vpn.skipReasonLabel("made_up"), vpn.skipReasonLabel("other"));
  const statuses = ["applied", "not_applied", "service_missing", "mismatch", "unverified"];
  assert.equal(new Set(statuses.map(vpn.forwardStatusLabel)).size, statuses.length);
  assert.equal(vpn.forwardStatusTone("applied"), "ok");
  assert.equal(vpn.forwardStatusTone("service_missing"), "bad");
});

test("沒有報告或沒有轉發時不拋錯", () => {
  assert.deepEqual(vpn.forwardRows(null), []);
  assert.deepEqual(vpn.forwardRows({}), []);
  assert.deepEqual(vpn.forwardRows(report()), []);
});

test("元件接線：卡片 gate 讀後端旗標、輪詢器只在有能力且 running／upgrading 時打請求", () => {
  assert.match(overviewXml, /t-if="props\.service\.has_vpn_node_status and \(vpnReport or vpn\.state\.loading\)"/);
  assert.match(overviewSource, /has_vpn_node_status === true/);
  assert.match(overviewSource, /\["running", "upgrading"\]\.includes\(this\.props\.service\.state\)/);
  assert.match(pollerSource, /if \(disposed \|\| !getEnabled\(\)\) return \{ skipped: true \}/);
  assert.match(pollerSource, /\/node-status/);
  // 與 useServiceResources 同款：可見才輪詢、不重疊（createResourcePoller）。
  assert.match(pollerSource, /createResourcePoller/);
  assert.doesNotMatch(overviewSource + overviewXml + pollerSource, /['"]tailscale['"]/, "前端不得用 slug 判能力");
});

test("元件的 vpnReport 在沒有能力時一律 null（不會因為殘留結果畫卡）", () => {
  const body = getterBody(overviewSource, "vpnReport");
  const vpnReport = (service, current) =>
    new Function(`return function () {${body}};`)().call({ props: { service }, vpn: { state: { report: current } } });
  assert.equal(vpnReport({ has_vpn_node_status: false }, report()), null);
  assert.equal(vpnReport({}, report()), null);
  assert.deepEqual(vpnReport({ has_vpn_node_status: true }, report()), report());
});
