// #1159 Q30 × #477：AddonsTab 在 init 額度出口 error 下的能力矩陣（只驗現行行為，不改 UI）。
//
// 額度出口 error 可以改 addon（新增會送出更新）；任何 removal 只在 running 才送預檢與更新，
// 其他狀態本地擋下且零 HTTP；helm 失敗造成的 error 整個 tab 不可編輯。
// affordance（非 running 時限制取消勾選）屬 #1306，不在本檔。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const toDataUrl = (source) =>
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;

const owlStub = 'class Component {} const useState = (value) => value;';
const translateStub =
  'const _t = (message, ...args) => args.reduce((text, arg) => text.replace("%s", arg), message);';

async function loadSource(relativePath) {
  return readFile(new URL(relativePath, import.meta.url), "utf8");
}

const dialogSource = (await loadSource(
  "../../src/static/src/paas/components/addon-removal-dialog/AddonRemovalDialog.js",
))
  .replace('import { Component } from "@odoo/owl";', "class Component {}")
  .replace('import { WoowButton } from "../button/WoowButton";', "const WoowButton = {};")
  .replace('import { WoowIcon } from "../icon/WoowIcon";', "const WoowIcon = {};");
const dialogUrl = toDataUrl(dialogSource);

const tabSource = (await loadSource("../../src/static/src/paas/pages/service/tabs/AddonsTab.js"))
  .replace('import { Component, useState } from "@odoo/owl";', owlStub)
  .replace('import { _t } from "@web/core/l10n/translation";', translateStub)
  .replace(/import \{ (WoowCard|WoowIcon|WoowButton|OdooAddonSelector) \} from "[^"]+";/g,
    "const $1 = {};")
  .replace('"../../../components/addon-removal-dialog/AddonRemovalDialog"', `"${dialogUrl}"`)
  .replace('import { cloudService } from "../../../services/cloud_service";',
    "const cloudService = globalThis.__addonsTabCloudService;");
assert.doesNotMatch(tabSource, /from "\.\.?\//, "所有相對 import 都必須被替換成 stub");
// module 載入時就綁定 cloudService，先放一個可變的 stub，各 test 再換上自己的實作。
globalThis.__addonsTabCloudService = {};
const { AddonsTab } = await import(toDataUrl(tabSource));

function makeTab(service) {
  const calls = { check: [], update: [] };
  Object.assign(globalThis.__addonsTabCloudService, {
    async checkAddonRemoval(serviceId, removed) {
      calls.check.push({ serviceId, removed });
      return { success: true, modules: { sale: { state: "uninstalled", dependents: [] } } };
    },
    async updateServiceAddons(workspaceId, serviceId, selectedIds) {
      calls.update.push({ workspaceId, serviceId, selectedIds });
      return { success: true, data: {} };
    },
  });
  const tab = Object.create(AddonsTab.prototype);
  tab.props = { service, workspaceId: 7 };
  tab.state = {
    editing: false, saving: false, checking: false, error: null,
    selectedIds: [], showDialog: false, dialogModules: {},
  };
  return { tab, calls };
}

const addonA = { id: 11, name: "Sales" };
const quotaError = {
  id: 3, state: "error", init_exit: "failed", init_reconfigure_allowed: true,
  odoo_addons: [addonA],
};

test("額度出口 error：可編輯，只新增 addon 會送出更新、不走 removal 預檢", async () => {
  const { tab, calls } = makeTab(quotaError);
  assert.equal(tab.canEdit, true);
  tab.startEditing();
  tab.onSelectionChange([11, 12]);
  await tab.saveAddons();
  assert.deepEqual(calls.check, []);
  assert.deepEqual(calls.update, [{ workspaceId: 7, serviceId: 3, selectedIds: [11, 12] }]);
  assert.equal(tab.state.error, null);
});

test("額度出口 error：取消勾選（removal）本地擋下，零預檢、零更新 HTTP", async () => {
  const { tab, calls } = makeTab(quotaError);
  tab.startEditing();
  tab.onSelectionChange([]);
  await tab.saveAddons();
  assert.deepEqual(calls.check, []);
  assert.deepEqual(calls.update, []);
  assert.match(tab.state.error, /Cannot remove addons while the service is error/);
  assert.equal(tab.state.editing, true, "保留編輯狀態讓使用者改回");
});

test("helm 失敗的 error（非額度出口）：整個 tab 不可編輯", () => {
  const { tab } = makeTab({ ...quotaError, init_exit: false, init_reconfigure_allowed: false });
  assert.equal(tab.canEdit, false);
});

test("running：removal 先走預檢，安全模組才送更新", async () => {
  const { tab, calls } = makeTab({ ...quotaError, state: "running", init_exit: false,
    init_reconfigure_allowed: false });
  assert.equal(tab.canEdit, true);
  tab.startEditing();
  tab.onSelectionChange([]);
  await tab.saveAddons();
  assert.deepEqual(calls.check, [{ serviceId: 3, removed: [11] }]);
  assert.deepEqual(calls.update, [{ workspaceId: 7, serviceId: 3, selectedIds: [] }]);
});
