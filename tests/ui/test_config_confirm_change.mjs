// service-update-api-validation（INFRA-HS-013-4）：ConfigurationTab 的 spec `confirm_change`。
//
// Headscale 改 ngrok 網域＝VPN 登入網址改變、所有裝置要重新登入，而且平台無法核對網域是否屬於
// 租戶的 ngrok 帳號。帶 `confirm_change` 說明的欄位有改時，「儲存」先顯示說明、按「確認並儲存」
// 才送出；沒改那一格（或只改別格）照舊直接送。Save 按鈕經 onClick.bind 呼叫時第一個參數是
// 事件物件，不能被當成「已確認」。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const toDataUrl = (source) =>
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;

const tabSource = (await readFile(
  new URL("../../src/static/src/paas/pages/service/tabs/ConfigurationTab.js", import.meta.url),
  "utf8",
))
  .replace('import { Component, useState } from "@odoo/owl";',
    "class Component {} const useState = (value) => value;")
  .replace('import { _t } from "@web/core/l10n/translation";', "const _t = (message) => message;")
  .replace(/import \{ (WoowCard|WoowIcon|WoowButton|HelmValueForm) \} from "[^"]+";/g,
    "const $1 = {};")
  .replace('import { cloudService } from "../../../services/cloud_service";',
    "const cloudService = globalThis.__configTabCloudService;")
  // 其他相對 import（例：之後加進來的 utils 驗證函式）一律換成「回空物件」的 stub——本檔只驗
  // confirm_change，不讓別的欄位驗證擋住存檔。
  .replace(/import \{ ([^}]+) \} from "\.\.?\/[^"]+";/g, (_m, names) =>
    names.split(",").map((name) => `const ${name.trim()} = () => ({});`).join(" "));
assert.doesNotMatch(tabSource, /from "\.\.?\//, "所有相對 import 都必須被替換成 stub");
globalThis.__configTabCloudService = {};
const { ConfigurationTab } = await import(toDataUrl(tabSource));

const CONFIRM = "改 ngrok 網域會改變 VPN 登入網址";
const SPECS = {
  required: [],
  optional: [
    { key: "ngrok.enabled", label: "ngrok", type: "boolean", default: false },
    { key: "ngrok.domain", label: "ngrok 網域", type: "text", format: "domain", confirm_change: CONFIRM },
    { key: "headscale.logLevel", label: "Log", type: "select", default: "info", options: ["info", "debug"] },
  ],
};

function makeTab() {
  const calls = [];
  globalThis.__configTabCloudService.updateService = async (...args) => {
    calls.push(args);
    return { success: true, data: {} };
  };
  const tab = new ConfigurationTab();
  tab.props = {
    workspaceId: 52,
    canManage: true,
    service: {
      id: 238,
      state: "running",
      template: { helm_value_specs: SPECS },
      helm_values: { ngrok: { enabled: true, domain: "old.ngrok-free.app" } },
    },
  };
  tab.setup();
  tab.startEditing();
  return { tab, calls };
}

const clickEvent = { type: "click", currentTarget: {} };

test("changing a confirm_change field asks first and sends nothing", async () => {
  const { tab, calls } = makeTab();
  tab.onHelmValueUpdate("ngrok.domain", "new.ngrok-free.app");
  await tab.saveChanges(clickEvent);
  assert.equal(calls.length, 0);
  assert.deepEqual(tab.state.pendingConfirm, [CONFIRM]);
  assert.equal(tab.state.editing, true);
});

test("confirming sends the update once", async () => {
  const { tab, calls } = makeTab();
  tab.onHelmValueUpdate("ngrok.domain", "new.ngrok-free.app");
  await tab.saveChanges(clickEvent);
  await tab.confirmSave();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][2]["ngrok.domain"], "new.ngrok-free.app");
  assert.deepEqual(tab.state.pendingConfirm, []);
});

test("dismissing or editing again drops the pending confirmation", async () => {
  const { tab, calls } = makeTab();
  tab.onHelmValueUpdate("ngrok.domain", "new.ngrok-free.app");
  await tab.saveChanges(clickEvent);
  tab.dismissConfirm();
  assert.deepEqual(tab.state.pendingConfirm, []);
  await tab.saveChanges(clickEvent);
  tab.onHelmValueUpdate("ngrok.domain", "other.ngrok-free.app");
  assert.deepEqual(tab.state.pendingConfirm, []);
  assert.equal(calls.length, 0);
});

test("other fields save directly without confirmation", async () => {
  const { tab, calls } = makeTab();
  tab.onHelmValueUpdate("headscale.logLevel", "debug");
  await tab.saveChanges(clickEvent);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][2]["ngrok.domain"], "old.ngrok-free.app", "未改的網域照常整包送出");
  assert.deepEqual(tab.state.pendingConfirm, []);
});
