// Odoo 模組分頁的文案必須與平台語意一致：勾選只讓模組「可用」（複製進 addons_path），
// 不會自動安裝（chart values〈addons〉：勾選不自動 -i、移除不代為 uninstall）。
// 之前編輯模式寫「選擇要安裝…」，客戶以為勾選＝裝好，實際要自己去 Odoo 應用程式安裝。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const toDataUrl = (source) =>
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const loadSource = (relativePath) => readFile(new URL(relativePath, import.meta.url), "utf8");

const owlStub = "class Component {} const useState = (value) => value;";
const translateStub =
  'const _t = (message, ...args) => args.reduce((text, arg) => text.replace("%s", arg), message);';

const dialogSource = (await loadSource(
  "../../src/static/src/paas/components/addon-removal-dialog/AddonRemovalDialog.js",
))
  .replace('import { Component } from "@odoo/owl";', "class Component {}")
  .replace('import { WoowButton } from "../button/WoowButton";', "const WoowButton = {};")
  .replace('import { WoowIcon } from "../icon/WoowIcon";', "const WoowIcon = {};");

const tabSource = (await loadSource("../../src/static/src/paas/pages/service/tabs/AddonsTab.js"))
  .replace('import { Component, useState } from "@odoo/owl";', owlStub)
  .replace('import { _t } from "@web/core/l10n/translation";', translateStub)
  .replace(/import \{ (WoowCard|WoowIcon|WoowButton|OdooAddonSelector) \} from "[^"]+";/g,
    "const $1 = {};")
  .replace('"../../../components/addon-removal-dialog/AddonRemovalDialog"',
    `"${toDataUrl(dialogSource)}"`)
  .replace('import { cloudService } from "../../../services/cloud_service";',
    "const cloudService = {};");
assert.doesNotMatch(tabSource, /from "\.\.?\//, "所有相對 import 都必須被替換成 stub");
const { AddonsTab } = await import(toDataUrl(tabSource));

const template = await loadSource("../../src/static/src/paas/pages/service/tabs/AddonsTab.xml");

function makeTab(service) {
  const tab = Object.create(AddonsTab.prototype);
  tab.props = { service, workspaceId: 7, canManage: true };
  return tab;
}

test("編輯模式的提示不再說「安裝」：勾選只是讓模組可用，安裝在 Odoo 應用程式", () => {
  assert.doesNotMatch(template, /Select the addons to install on this Odoo instance/);
  assert.match(template, /Selecting an addon does not install it/);
  assert.match(template, /install it from Odoo Apps/);
});

test("檢視模式提示下一步（到 Odoo 應用程式安裝），並提供直達按鈕", () => {
  assert.match(template, /Selected addons are available in Odoo Apps\. Install them there before using them\./);
  assert.match(template, /t-if="odooAppsUrl"[\s\S]*?onClick\.bind="openOdooApps"/);
});

test("openOdooApps 開新分頁到該服務的 Odoo 應用程式；沒有子網域就什麼都不做", () => {
  const opened = [];
  const previous = globalThis.window;
  globalThis.window = { open: (...args) => opened.push(args) };
  try {
    makeTab({ id: 3, subdomain: "acme-odoo" }).openOdooApps();
    assert.deepEqual(opened, [["https://acme-odoo.woowtech.io/odoo/apps/modules", "_blank"]]);
    makeTab({ id: 4, subdomain: "" }).openOdooApps();
    assert.equal(opened.length, 1, "沒有子網域時不開任何分頁");
  } finally {
    globalThis.window = previous;
  }
});
