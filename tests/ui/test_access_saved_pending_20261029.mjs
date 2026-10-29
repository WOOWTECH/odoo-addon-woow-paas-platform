// Access 變更「已儲存、但還在套用」(`saved: true`、`success: false`) 在前端的處理。
//
// 2026-10-28 正式站 REV-01 實測：保持期內刪 policy，後端回 `success:false` ＋ 說明，但 policy 已經刪掉。
// 舊的前端把它當成寫入失敗——畫面還留著已刪的 policy，使用者被叫去「重試」。這支只驗：
//   * 信封 helper 與三個 service 在 saved 時保留 `saved`／`data`／`error`；
//   * AddPolicyModal 在 saved 時關閉並把說明交給父層；
//   * 三個 surface 的 handler 在 saved 時重新載入，並把說明顯示出來（原始碼接線）。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const toDataUrl = (source) =>
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const loadSource = (relativePath) => readFile(new URL(relativePath, import.meta.url), "utf8");

const helperSource = await loadSource("../../src/static/src/paas/services/access_mutation.js");
const helperUrl = toDataUrl(helperSource);
const { accessMutation } = await import(helperUrl);

const SAVED_PENDING = {
  success: false,
  saved: true,
  error: "Access settings were saved. Existing sessions are being revoked.",
  access_configuration_ready: false,
  data: { id: 7 },
};

test("信封：成功只帶 data；saved 但未完成保留 saved／data／error；一般失敗 saved 為 false", () => {
  assert.deepEqual(accessMutation({ success: true, data: { id: 1 }, saved: true }), {
    success: true,
    data: { id: 1 },
  });
  assert.deepEqual(accessMutation(SAVED_PENDING), {
    success: false,
    error: SAVED_PENDING.error,
    saved: true,
    data: { id: 7 },
  });
  const failed = accessMutation({ success: false, error: "Policy not found" });
  assert.equal(failed.saved, false);
  assert.equal(failed.error, "Policy not found");
  // 只認明確的 true：字串或 1 不算已儲存
  assert.equal(accessMutation({ success: false, saved: "yes", error: "x" }).saved, false);
});

async function loadService(file, name, rpc) {
  const source = (await loadSource(`../../src/static/src/paas/services/${file}`))
    .replace('import { _t } from "@web/core/l10n/translation";', "const _t = (m) => m;")
    .replace('import { accessMutation } from "./access_mutation";',
      `import { accessMutation } from "${helperUrl}";`)
    .replace('import { jsonRpc } from "./rpc";', "const jsonRpc = (...a) => globalThis.__rpc(...a);");
  assert.doesNotMatch(source, /from "\.\.?\//, "所有相對 import 都必須被替換");
  globalThis.__rpc = rpc;
  return (await import(toDataUrl(source)))[name];
}

const MUTATIONS = {
  "security_access.js": ["securityAccessService", [
    ["updateRouteAccess", [1, 2, 3, { is_protected: true }]],
    ["createPolicy", [1, 2, 3, {}]],
    ["updatePolicy", [1, 2, 3, {}]],
    ["deletePolicy", [1, 2, 3]],
    ["reorderPolicies", [1, 2, [3, 4]]],
  ]],
  "smart_home_access.js": ["smartHomeAccessService", [
    ["updateRouteAccess", [1, 2, 3, { is_protected: true }]],
    ["createPolicy", [1, 2, 3, {}]],
    ["updatePolicy", [1, 2, 3, {}]],
    ["deletePolicy", [1, 2, 3]],
    ["reorderPolicies", [1, 2, [3, 4]]],
  ]],
  "cloud_service_access.js": ["cloudServiceAccessService", [
    ["updateConfig", [1, 2, { is_protected: true }]],
    ["createPolicy", [1, 2, {}]],
    ["updatePolicy", [1, 2, 3, {}]],
    ["deletePolicy", [1, 2, 3]],
    ["reorderPolicies", [1, 2, [3, 4]]],
  ]],
};

for (const [file, [name, methods]] of Object.entries(MUTATIONS)) {
  test(`${file}：寫入類方法在 saved 時保留 saved／data／error`, async () => {
    const service = await loadService(file, name, async () => SAVED_PENDING);
    for (const [method, args] of methods) {
      const res = await service[method](...args);
      assert.deepEqual(res, {
        success: false,
        error: SAVED_PENDING.error,
        saved: true,
        data: { id: 7 },
      }, `${name}.${method}`);
    }
  });

  test(`${file}：寫入類方法的一般失敗與成功維持原樣`, async () => {
    const service = await loadService(file, name, null);
    for (const [method, args] of methods) {
      globalThis.__rpc = async () => ({ success: false, error: "nope" });
      assert.deepEqual(await service[method](...args),
        { success: false, error: "nope", saved: false, data: undefined }, `${name}.${method}`);
      globalThis.__rpc = async () => ({ success: true, data: [1] });
      assert.deepEqual(await service[method](...args), { success: true, data: [1] }, `${name}.${method}`);
    }
  });
}

test("AddPolicyModal：saved 時關閉並把說明交給父層；成功不帶說明；真正的失敗留在 modal", async () => {
  const source = (await loadSource("../../src/static/src/paas/components/modal/AddPolicyModal.js"))
    .replace('import { Component, useState } from "@odoo/owl";',
      "class Component {} const useState = (v) => v;")
    .replace('import { _t } from "@web/core/l10n/translation";', "const _t = (m) => m;")
    .replace('import { WoowButton } from "../button/WoowButton";', "const WoowButton = {};")
    .replace('import { WoowIcon } from "../icon/WoowIcon";', "const WoowIcon = {};");
  assert.doesNotMatch(source, /from "\.\.?\//, "所有相對 import 都必須被替換");
  const { AddPolicyModal } = await import(toDataUrl(source));

  const run = async (response) => {
    const modal = Object.create(AddPolicyModal.prototype);
    const saved = [];
    modal.props = { policy: null, savePolicy: async () => response, onSaved: (...a) => saved.push(a), onClose() {} };
    modal.state = {
      name: "p", decision: "allow", saving: false, error: null,
      include: [{ rule_type: "email_list", rule_value: "a@example.com" }], require: [], exclude: [],
    };
    await modal.confirm();
    return { saved, error: modal.state.error };
  };

  assert.deepEqual(await run({ success: true, data: {} }), { saved: [[null]], error: null });
  assert.deepEqual(await run(accessMutation(SAVED_PENDING)), { saved: [[SAVED_PENDING.error]], error: null });
  assert.deepEqual(await run({ success: false, error: "Name taken" }), { saved: [], error: "Name taken" });
});

test("三個 surface：刪 policy／改設定在 saved 時重新載入，並顯示說明", async () => {
  const page = (relativePath) => loadSource(`../../src/static/src/paas/pages/${relativePath}`);
  const method = (source, signature) => {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, signature);
    const next = source.indexOf("\n  async ", start + signature.length);
    return source.slice(start, next === -1 ? undefined : next);
  };
  const sa = await page("security-access/SecurityAccessDetailPage.js");
  const sh = await page("smart-home/tabs/SmartHomeAccessTab.js");
  const cs = await page("service/tabs/AccessTab.js");
  for (const [label, source, signature, reload, errorField] of [
    ["SA 刪 policy", sa, "async deletePolicy(", "loadDetail", "actionError"],
    ["SA 改設定", sa, "async saveRouteAccess(", "loadDetail", "actionError"],
    ["SH 刪 policy", sh, "async deletePolicy(", "reloadPolicies", "error"],
    ["SH 改設定", sh, "async saveRouteAccess(", "_replaceRoute", "error"],
    ["CS 刪 policy", cs, "async deletePolicy(", "reloadPolicies", "error"],
    ["CS 改設定", cs, "async saveConfig(", "EMPTY_ACCESS", "error"],
  ]) {
    const body = method(source, signature);
    assert.match(body, /if \(res\.success \|\| res\.saved\) \{/, `${label}：saved 也要刷新畫面`);
    assert.match(body, new RegExp(reload), `${label}：刷新的呼叫還在`);
    assert.match(body, new RegExp(`if \\(!res\\.success\\) \\{\\s*this\\.state\\.${errorField} = res\\.error;`),
      `${label}：說明要顯示出來`);
  }
  for (const [label, source, errorField] of [["SA", sa, "actionError"], ["SH", sh, "error"], ["CS", cs, "error"]]) {
    assert.match(source, new RegExp(`onPolicySaved\\s*(=\\s*async\\s*)?\\(notice\\)[\\s\\S]*?if \\(notice\\) \\{\\s*this\\.state\\.${errorField} = notice;`),
      `${label}：modal 交來的說明要顯示出來`);
  }
});
