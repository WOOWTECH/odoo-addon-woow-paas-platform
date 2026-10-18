// 商用驗收 INFRA-FIX-005 覆核後的補強（疊在 PR #1771 之上）：前端這一半。
// - INFRA-TS-007-1：轉發清單在儲存前就擋下 entrypoint 會略過的項目（與後端／entrypoint 同一套規則）。
// - INFRA-TS-005-5：控制平面與 key 種類錯配、自訂網址留空在表單就提示；服務頁「VPN 節點狀態」卡
//   顯示登入失敗原因（key 過期／已用過／不對／連不到控制伺服器），以及「已登入，設定的 key 不會被使用」。
//
// 跑法：node --test tests/ui/*.mjs
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (relativePath) =>
  readFile(new URL(`../../src/static/src/paas/${relativePath}`, import.meta.url), "utf8");
const load = async (code) => import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
// `_t` 換成會把 %s 代進去的恆等函式（同 Odoo 的 sprintf 行為）。
const noTranslate = (source) =>
  source.replace(
    'import { _t } from "@web/core/l10n/translation";',
    "const _t = (message, ...args) => { let i = 0; return message.replace(/%s/g, () => String(args[i++])); };",
  );

const ts = await load(noTranslate(await read("utils/tailscale_settings.js")));
const vpn = await load(noTranslate(await read("utils/vpn_node_status.js")));
const NS = "paas-ws-5e41efa6";

test("TS-007-1：實測輸入逐條被擋，原因與 entrypoint 一致", () => {
  const code = (raw, ns = NS) => ts.checkServe(raw, ns)?.code ?? null;
  assert.equal(code("9102=svc-x.other-ns.svc.cluster.local:80"), "serve_other_namespace");
  assert.equal(code("9103=10.0.0.5:80"), "serve_other_namespace");
  assert.equal(code("70000=a:1"), "serve_port_range");
  assert.equal(code("9101=no-such-svc"), "serve_bad_format");
  assert.equal(code("9105=localhost:80"), "serve_not_service_name");
  assert.equal(code("9004=a:80,9004=b:80"), "serve_duplicate_port");
  assert.equal(code(Array.from({ length: 33 }, (_, i) => `${1000 + i}=svc-a:80`).join(",")), "serve_too_many");
});

test("TS-007-1：同工作區的各種寫法與空白都放行；namespace 不明時不做同工作區檢查", () => {
  const ok = `9004=svc-0b4a8def-headscale.${NS}.svc.cluster.local:8080, 8069=svc-1a2b3c4d:80, 8070=SVC-X.${NS}.svc:1`;
  assert.equal(ts.checkServe(ok, NS), null);
  assert.equal(ts.checkServe("", NS), null);
  assert.equal(ts.checkServe(undefined, NS), null);
  assert.equal(ts.checkServe(`8070=svc-x.${NS}:80`, null), null, "開通頁還不知道 namespace，交給後端");
  assert.equal(ts.checkServe(`8070=svc-x.${NS}:80`, "")?.code, "serve_other_namespace");
});

test("TS-005-5：控制平面與 key 種類錯配、自訂網址留空", () => {
  const code = (plane, server, key) => ts.checkLogin(plane, server, key)?.code ?? null;
  assert.equal(code("official", "", "hskey-auth-abc"), "auth_key_not_tailscale");
  assert.equal(code("workspace", "", "tskey-auth-k"), "auth_key_is_tailscale");
  assert.equal(code("custom", "https://hs.example.com", "tskey-auth-k"), "auth_key_is_tailscale");
  assert.equal(code("custom", "https://controlplane.tailscale.com", "tskey-auth-k"), null);
  assert.equal(code("custom", "", ""), "login_server_required");
  assert.equal(code("custom", "headscale.example.com", ""), "login_server_invalid");
  assert.equal(code("official", "", "tskey-auth-k\nX"), "auth_key_whitespace");
  assert.equal(code("official", "", ""), null);
  assert.equal(code("workspace", "", "hskey-auth-abc"), null);
});

const SPECS = [
  { key: "tailscale.controlPlane", validate: "tailscale_login" },
  { key: "tailscale.loginServer" },
  { key: "tailscale.authKey", type: "password" },
  { key: "tailscale.serve", validate: "tailscale_serve", label: "轉發清單" },
  { key: "tailscale.hostname" },
];

test("settingsErrors：錯誤掛在對的欄位、key 不回顯；只改別的欄位不重驗已存的組合", () => {
  const errors = ts.settingsErrors(SPECS, {
    "tailscale.controlPlane": "workspace",
    "tailscale.authKey": "tskey-auth-kSECRET",
    "tailscale.serve": "70000=a:1",
  }, { namespace: NS });
  assert.deepEqual(Object.keys(errors).sort(), ["tailscale.authKey", "tailscale.serve"]);
  assert.match(errors["tailscale.serve"], /70000=a:1/);
  assert.doesNotMatch(errors["tailscale.authKey"], /SECRET/);

  const untouched = ts.settingsErrors(SPECS, {
    "tailscale.controlPlane": "custom", "tailscale.loginServer": "", "tailscale.hostname": "office",
  }, { namespace: NS, changed: (key) => key === "tailscale.hostname" });
  assert.deepEqual(untouched, {});

  const server = ts.settingsErrors(SPECS, { "tailscale.controlPlane": "custom", "tailscale.loginServer": "" },
    { namespace: NS });
  assert.deepEqual(Object.keys(server), ["tailscale.loginServer"]);
});

test("settingsErrors：沒有 validate 的範本完全不受影響", () => {
  assert.deepEqual(ts.settingsErrors([{ key: "tailscale.serve" }], { "tailscale.serve": "70000=a:1" }, {}), {});
});

const report = (over = {}) => ({
  state: "needs_login", probe: "ok", control_plane: "workspace", tailnet_ips: [], dns_name: null,
  login: { kind: "none", url: null, register_key: null }, login_error: null, authkey_ignored: false,
  serve: { wanted_known: true, forwards: [], skipped: [], ignored_all: null, extra_config: false },
  ...over,
});

test("TS-005-5：登入失敗原因看得到（取代只在容器日誌）", () => {
  assert.match(vpn.loginErrorHint(report({ login_error: "authkey_expired" })), /expired/i);
  assert.match(vpn.loginErrorHint(report({ login_error: "authkey_used" })), /already used/i);
  assert.match(vpn.loginErrorHint(report({ login_error: "authkey_invalid" })), /rejected/i);
  assert.match(vpn.loginErrorHint(report({ login_error: "control_unreachable" })), /could not reach/i);
  assert.match(vpn.loginErrorHint(report({ login_error: "other" })), /failed/i);
  assert.equal(vpn.loginErrorHint(report()), "");
  assert.equal(vpn.loginErrorHint(report({ state: "online", login_error: "authkey_expired" })), "");
  assert.equal(vpn.loginErrorHint(null), "");
});

test("TS-005-5：已登入的節點不會使用設定裡的 key——要讓人看得到", () => {
  assert.match(vpn.authKeyNotice(report({ state: "online", authkey_ignored: true })), /not used/i);
  assert.equal(vpn.authKeyNotice(report({ state: "online", authkey_ignored: false })), "");
  assert.equal(vpn.authKeyNotice(null), "");
});

test("OverviewTab 把兩段新說明畫在 VPN 節點狀態卡上", async () => {
  const js = await read("pages/service/tabs/OverviewTab.js");
  const xml = await read("pages/service/tabs/OverviewTab.xml");
  assert.match(js, /loginErrorHint\(/);
  assert.match(js, /authKeyNotice\(/);
  assert.match(xml, /vpnLoginError/);
  assert.match(xml, /vpnAuthKeyNotice/);
});

test("兩個表單都跑 spec 驅動的檢查", async () => {
  for (const rel of ["pages/service/tabs/ConfigurationTab.js", "pages/configure/AppConfigurationPage.js"]) {
    assert.match(await read(rel), /settingsErrors\(/, rel);
  }
});
