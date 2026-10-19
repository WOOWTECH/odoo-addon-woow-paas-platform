// Real OWL component tests with fixture data; no service or cluster requests.
// OWL_BROWSER_PATH=/path/to/owl.js READING_TABS_CSS=/path/to/compiled.css
// PLAYWRIGHT_MODULE=/path/to/playwright node tests/frontend/service-reading-tabs.browser.mjs
import assert from "node:assert/strict";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { join } from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = new URL("../../", import.meta.url);
const read = (path) => readFile(new URL(path, root), "utf8");
const asset = (path) => read(`src/static/src/paas/${path}`);
const artifactDir = process.env.READING_TABS_ARTIFACTS || "/tmp/paas-service-reading-tabs-ui";
await mkdir(artifactDir, { recursive: true });
const paths = {
  information: "pages/service/tabs/InformationTab.js", logs: "pages/service/tabs/LogsTab.js",
  helper: "pages/service/tabs/service_logs.js", markdown: "services/markdown_parser.js",
  icon: "components/icon/WoowIcon.js", iconXml: "components/icon/WoowIcon.xml",
  informationXml: "pages/service/tabs/InformationTab.xml", logsXml: "pages/service/tabs/LogsTab.xml",
};
const sources = Object.fromEntries(await Promise.all(Object.entries(paths).map(async ([key, path]) => [key, await asset(path)])));
const guideXml = await read("src/data/cloud_app_documentation.xml");
const openDesignGuide = guideXml.match(/<record id="cloud_app_opendesign"[\s\S]*?<field name="documentation"><!\[CDATA\[([\s\S]*?)\]\]><\/field>/)?.[1];
assert.ok(openDesignGuide, "the seeded Open Design guide must be present");
const messages = {};
let key = "", value = "", field = null;
const flush = () => { if (key && value) messages[key] = value; key = value = ""; field = null; };
for (const line of (await read("src/i18n/zh_TW.po")).split("\n")) {
  if (line.startsWith("msgid ")) { flush(); key = JSON.parse(line.slice(6)); field = "key"; }
  else if (line.startsWith("msgstr ")) { value = JSON.parse(line.slice(7)); field = "value"; }
  else if (line.startsWith('"')) { if (field === "key") key += JSON.parse(line); else if (field === "value") value += JSON.parse(line); }
}
flush();
const browser = await chromium.launch({ headless: process.env.HEADLESS !== "false",
  ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 }, locale: "zh-TW" });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.setContent('<html lang="zh-Hant"><head><meta charset="utf-8"></head><body><main id="preview"></main></body></html>');
  await page.addScriptTag({ path: process.env.OWL_BROWSER_PATH });
  await page.addScriptTag({ path: new URL("src/static/src/paas/lib/marked.min.js", root).pathname });
  await page.addScriptTag({ path: new URL("src/static/src/paas/lib/purify.min.js", root).pathname });
  await page.addStyleTag({ path: process.env.READING_TABS_CSS });
  await page.addStyleTag({ content: `
    *{box-sizing:border-box}body{margin:0;background:#f6f7fb;color:#1a1c21;font:15px system-ui,sans-serif}
    main{max-width:1100px;padding:24px;margin:auto}.o_woow_card{background:white;border:1px solid #e6e8ee;border-radius:18px}
    .o_woow_btn{display:inline-flex;align-items:center;gap:6px;padding:8px 12px;border:1px solid #dce1eb;border-radius:8px;color:#5f698c;background:white;cursor:pointer}
    .o_woow_btn:disabled{opacity:.5}.o_woow_hint{font-size:13px;color:#5f698c}.material-symbols-outlined{font-size:14px!important}
    .preview-tabs{display:flex;gap:8px;margin:16px 0}.preview-tabs button{flex:1;padding:12px;border:0;background:#edf0ff;border-radius:8px}
    @media(max-width:640px){main{padding:16px}}
  ` });
  await page.evaluate(({ sources, messages, openDesignGuide }) => {
    const { Component, useState, useRef, onMounted, onPatched, onWillDestroy, mount, markup } = owl;
    const _t = (message, ...args) => { let i = 0; return (messages[message] || message).replace(/%s/g, () => args[i++]); };
    const clean = (text) => text.replace(/^import .*;\s*$/gm, "").replace(/^export /gm, "");
    const helpers = new Function(`${clean(sources.helper)};return {createLogPoller,filterLogLines,downloadLogSnapshot,logDownloadFilename};`)();
    const parseMarkdown = new Function("markup", `${clean(sources.markdown)};return parseMarkdown;`)(markup);
    const WoowIcon = new Function("Component", `${clean(sources.icon)};return WoowIcon;`)(Component);
    window.logCalls = 0;
    window.lastLogOptions = null;
    window.logFixture = {
      available: true, reason: "", pod: "open-design-main-1234567890-abcdef", container: "app",
      sources: [{ pod: "open-design-main-1234567890-abcdef", containers: ["app", "sidecar"] }, { pod: "open-design-main-replacement-1234567890-abcdef", containers: ["app"] }],
      text: Array.from({ length: 100 }, (_, i) => `${new Date(1792314000000 + i * 1000).toISOString()} INFO request ${i}`).join("\n") + '\nERROR <img src=x onerror=alert(1)>\nINFO ' + 'long-line-'.repeat(40),
      truncated: true,
    };
    const cloudService = {
      async fetchLogs(workspaceId, serviceId, options) {
        window.logCalls++;
        window.lastLogOptions = options;
        return { success: true, data: { ...window.logFixture, pod: options.pod || window.logFixture.pod, container: options.container || "app" } };
      },
      async fetchDocumentation() {
        return { success: true, data: { title: "Open Design", documentation_url: "https://example.com/docs",
          tutorial_url: "https://opendesign-guide.woowtech.io/tutorial.html", deployed_chart_version: "0.5.0",
          current_template_chart_version: "0.5.6",
          content: openDesignGuide + '\n\n<script>window.xss = true</script><img src=x onerror="window.xss = true">\n\n[Unsafe](javascript:alert(1))',
        } };
      },
    };
    const LogsTab = new Function("Component", "useState", "useRef", "onMounted", "onPatched", "onWillDestroy", "_t", "cloudService", "WoowIcon", "formatUserTime",
      "createLogPoller", "filterLogLines", "downloadLogSnapshot", "logDownloadFilename", `${clean(sources.logs)};return LogsTab;`)(
      Component, useState, useRef, onMounted, onPatched, onWillDestroy, _t, cloudService, WoowIcon,
      (date) => date.toISOString(), helpers.createLogPoller, helpers.filterLogLines, helpers.downloadLogSnapshot, helpers.logDownloadFilename,
    );
    const InformationTab = new Function("Component", "useState", "onMounted", "onWillDestroy", "_t", "cloudService", "parseMarkdown", "WoowIcon",
      `${clean(sources.information)};return InformationTab;`)(Component, useState, onMounted, onWillDestroy, _t, cloudService, parseMarkdown, WoowIcon);
    class Preview extends Component {
      static template = "ReadingPreview";
      static components = { LogsTab, InformationTab };
      setup() { this.state = useState({ tab: "information", manage: true }); window.readingPreview = this.state; }
      get service() { return { id: 42, name: "OpenDesign CLI 冷安裝測試 20261012" }; }
    }
    const previewXml = `<t t-name="ReadingPreview"><h1>OpenDesign CLI 冷安裝測試 20261012</h1>
      <nav class="preview-tabs"><button>總覽</button><button>配置</button><button t-on-click="() => {this.state.tab = 'information';}">資訊</button><button t-on-click="() => {this.state.tab = 'logs';}">日誌</button></nav>
      <InformationTab t-if="state.tab === 'information'" service="service" workspaceId="9"/>
      <LogsTab t-if="state.tab === 'logs'" t-key="state.manage" service="service" workspaceId="9" canManage="state.manage"/>
    </t>`;
    const unwrap = (xml) => xml.replace(/<\?xml[^>]*\?>|<\/?templates[^>]*>/g, "");
    return mount(Preview, document.getElementById("preview"), {
      templates: `<templates>${unwrap(sources.iconXml)}${unwrap(sources.informationXml)}${unwrap(sources.logsXml)}${previewXml}</templates>`,
      translateFn: _t, dev: true,
    });
  }, { sources, messages, openDesignGuide });
  await page.waitForSelector(".o_woow_service_information__body");
  assert.match(await page.locator(".o_woow_service_information").innerText(), /相關文件/);
  assert.match(await page.locator(".o_woow_service_information__body").innerText(), /跟著做：完成第一份設計/);
  assert.equal(await page.locator('.o_woow_service_information__body a[href="https://github.com/WOOWTECH/Woow_opendesign_tutorial_site"]').count(), 1);
  const tutorialLink = page.getByRole("link", { name: /WOOWTECH 教學網站/ });
  assert.equal(await tutorialLink.getAttribute("href"), "https://opendesign-guide.woowtech.io/tutorial.html");
  assert.equal(await tutorialLink.getAttribute("target"), "_blank");
  assert.match(await tutorialLink.getAttribute("rel"), /noopener noreferrer/);
  assert.match(await page.locator(".o_woow_service_information__version_note").innerText(), /0\.5\.0.*0\.5\.6/s);
  assert.equal(await page.locator(".o_woow_service_information script, .o_woow_service_information img, .o_woow_service_information a[href^='javascript:']").count(), 0);
  assert.equal(await page.evaluate(() => window.xss), undefined);
  await page.screenshot({ path: join(artifactDir, "information-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: join(artifactDir, "information-mobile.png"), fullPage: true });

  await page.getByRole("button", { name: "日誌", exact: true }).click();
  await page.waitForSelector(".o_woow_service_logs__text");
  assert.match(await page.locator(".o_woow_service_logs").innerText(), /每 5 秒自動更新/);
  assert.equal(await page.locator(".o_woow_service_logs__text img").count(), 0);
  const viewport = page.locator(".o_woow_service_logs__viewport");
  assert.equal(await viewport.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop < 32), true);
  await viewport.evaluate((el) => { el.scrollTop = 0; el.dispatchEvent(new Event("scroll")); });
  await page.getByRole("button", { name: messages.Refresh, exact: true }).click();
  await page.waitForTimeout(100);
  assert.equal(await viewport.evaluate((el) => el.scrollTop), 0);
  await page.getByRole("button", { name: "捲動至最新日誌" }).click();
  assert.equal(await viewport.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop < 32), true);
  await page.getByRole("button", { name: "暫停", exact: true }).click();
  const callsWhenPaused = await page.evaluate(() => window.logCalls);
  await page.waitForTimeout(5100);
  assert.equal(await page.evaluate(() => window.logCalls), callsWhenPaused);
  await page.getByRole("searchbox", { name: "搜尋日誌" }).fill("error");
  await page.waitForFunction(() => document.querySelector(".o_woow_service_logs__text")?.textContent === "ERROR <img src=x onerror=alert(1)>");
  assert.equal(await page.locator(".o_woow_service_logs__text").innerText(), "ERROR <img src=x onerror=alert(1)>");
  const [download] = await Promise.all([
    page.waitForEvent("download"), page.getByRole("button", { name: "下載已載入的日誌" }).click(),
  ]);
  assert.match(await readFile(await download.path(), "utf8"), /INFO request 0/);
  await page.getByRole("searchbox", { name: "搜尋日誌" }).fill("no matching output");
  await page.waitForSelector(".o_woow_service_logs__empty");
  assert.match(await viewport.innerText(), /沒有符合搜尋條件/);
  await page.getByRole("searchbox", { name: "搜尋日誌" }).fill("");
  await page.waitForSelector(".o_woow_service_logs__text");
  await page.getByRole("button", { name: "自動換行", exact: true }).click();
  await page.waitForFunction(() => !document.querySelector(".o_woow_service_logs__text").classList.contains("o_woow_service_logs__text--wrap"));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.equal(await viewport.evaluate((el) => el.scrollWidth > el.clientWidth), true);
  await page.getByRole("button", { name: "自動換行", exact: true }).click();
  await page.screenshot({ path: join(artifactDir, "logs-mobile.png"), fullPage: true });
  await page.setViewportSize({ width: 1280, height: 1000 });
  await page.screenshot({ path: join(artifactDir, "logs-desktop.png"), fullPage: true });

  await page.getByRole("combobox", { name: "執行個體", exact: true }).selectOption("open-design-main-replacement-1234567890-abcdef");
  await page.waitForFunction(() => window.lastLogOptions.pod === "open-design-main-replacement-1234567890-abcdef");
  await page.getByRole("checkbox", { name: "上次執行的容器" }).check();
  await page.waitForFunction(() => window.lastLogOptions.previous === true);
  await page.evaluate(() => { window.readingPreview.manage = false; });
  await page.waitForSelector(".o_woow_service_logs__permission");
  const callsWithoutPermission = await page.evaluate(() => window.logCalls);
  await page.waitForTimeout(5100);
  assert.equal(await page.evaluate(() => window.logCalls), callsWithoutPermission);
  assert.match(await page.locator(".o_woow_service_logs__permission").innerText(), /只有工作區擁有者與管理員/);
  assert.deepEqual(errors, [], "real OWL rendering must not produce browser errors");
  console.log(`Reading tab OWL browser assertions passed; screenshots: ${artifactDir}`);
} finally {
  await browser.close();
}
