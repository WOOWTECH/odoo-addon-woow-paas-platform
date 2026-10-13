// Real Owl template rendering with mock telemetry; no production API calls.
// OWL_BROWSER_PATH=/path/to/owl.js RESOURCE_CSS_PATH=/path/to/compiled.css
// PLAYWRIGHT_MODULE=/path/to/playwright node tests/frontend/service-resources.browser.mjs
import assert from "node:assert/strict";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { join } from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = new URL("../../", import.meta.url);
const read = (path) => readFile(new URL(path, root), "utf8");
const artifactDir = process.env.RESOURCE_UI_ARTIFACTS || "/tmp/service-resources-ui";
await mkdir(artifactDir, { recursive: true });
const [helper, component, template, po] = await Promise.all([
    read("src/static/src/paas/utils/resource_observations.js"),
    read("src/static/src/paas/components/resource-usage/ResourceUsage.js"),
    read("src/static/src/paas/components/resource-usage/ResourceUsage.xml"),
    read("src/i18n/zh_TW.po"),
]);
const messages = {};
let key = "", value = "", field = null;
const flush = () => { if (key && value) messages[key] = value; key = value = ""; field = null; };
for (const line of po.split("\n")) {
    if (line.startsWith("msgid ")) { flush(); key = JSON.parse(line.slice(6)); field = "key"; }
    else if (line.startsWith("msgstr ")) { value = JSON.parse(line.slice(7)); field = "value"; }
    else if (line.startsWith('"')) { if (field === "key") key += JSON.parse(line); else if (field === "value") value += JSON.parse(line); }
}
flush();
const browser = await chromium.launch({ headless: true,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) });
try {
    const page = await browser.newPage({ viewport: { width: 1360, height: 1000 }, deviceScaleFactor: 1, locale: "zh-TW" });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.setContent('<html lang="zh-Hant"><head><meta charset="utf-8"></head><body><main id="preview"></main></body></html>');
    await page.addScriptTag({ path: process.env.OWL_BROWSER_PATH });
    await page.addStyleTag({ path: process.env.RESOURCE_CSS_PATH });
    await page.addStyleTag({ content: `
        body{margin:0;background:#f5f6f8;color:#1a1c21;font:14px system-ui,"Noto Sans CJK TC",sans-serif}
        main{max-width:1200px;margin:auto;padding:32px}.preview-eyebrow{color:#5f698c;font-size:13px;margin-bottom:8px}
        h1{font-size:26px;margin:0 0 6px}h2{font-size:19px;margin:28px 0 12px}.preview-subtitle{color:#5f698c;margin:0 0 22px}
        .preview-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.preview-card{background:#fff;padding:20px;border:1px solid #e9edf5;border-radius:16px}
        .preview-card h3{margin:0}.preview-type{color:#5f698c;font-size:12px;margin-top:5px}.preview-badge{float:right;color:#26724a;background:#eaf5ef;padding:4px 9px;border-radius:20px;font-size:11px}
        @media(max-width:700px){main{padding:16px}.preview-grid{grid-template-columns:1fr}}
    ` });
    await page.evaluate(({ helper, component, template, messages }) => {
        const { Component, useState, mount } = owl;
        const _t = (message, ...args) => { let index = 0; return (messages[message] || message).replace(/%s/g, () => args[index++]); };
        const formatUserDateTime = (value) => new Intl.DateTimeFormat("zh-TW", {
            timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
        }).format(new Date(value));
        const clean = (text) => text.replace(/^import .*;\s*$/gm, "").replace(/^export /gm, "");
        const { resourceRows } = new Function("_t", `${clean(helper)};return {resourceRows};`)(_t);
        const ResourceUsage = new Function("Component", "_t", "resourceRows", "formatUserDateTime", `${clean(component)};return ResourceUsage;`)(Component, _t, resourceRows, formatUserDateTime);
        const now = Date.parse("2026-10-13T02:00:00Z");
        const sampled_at = "2026-10-13T01:59:50Z";
        const metric = (usage, limit, extra = {}) => ({ usage, limit, request: limit, limit_status: "bounded", status: "ok", sampled_at, coverage: { observed: 2, expected: 2 }, ...extra });
        const fixture = (cpu, ram, storage, extra = {}) => ({ status: "ok", workload_type: "container",
            cpu: metric(cpu, 32.2), memory: metric(ram * 1024 ** 3, 64.125 * 1024 ** 3),
            storage: { usage: storage * 1024 ** 3, capacity: 252 * 1024 ** 3, requested: 256 * 1024 ** 3, status: "ok", sampled_at, coverage: { observed: 1, expected: 1 } }, ...extra });
        class Preview extends Component {
            static components = { ResourceUsage };
            static template = "ResourceUsagePreview";
            setup() {
                this.state = useState({ now, detail: fixture(3.42, 18.6, 106), cards: [
                    { id: "pi1", name: "pi1", type: "AI 工作環境", observation: fixture(3.42, 18.6, 106) },
                    { id: "db", name: "客戶資料庫", type: "PostgreSQL", observation: fixture(0, 2.4, 37, { cpu: metric(0, 4), memory: metric(2.4 * 1024 ** 3, 8 * 1024 ** 3), storage: { usage: 37 * 1024 ** 3, capacity: 50 * 1024 ** 3, status: "ok", sampled_at } }) },
                    { id: "partial", name: "自動化工作流", type: "n8n", observation: fixture(0.42, 1.4, 0, { status: "partial", cpu: metric(0.42, 4, { status: "partial", coverage: { observed: 1, expected: 2 } }), storage: { usage: null, capacity: 20 * 1024 ** 3, status: "unavailable" } }) },
                ] });
                window.resourcePreview = this.state;
            }
        }
        const previewTemplate = `<templates><t t-name="ResourceUsagePreview">
            <div class="preview-eyebrow">Woow PaaS ／ 工作區</div>
            <h1>服務資源概況</h1><p class="preview-subtitle">查看實際使用量與可用容量，掌握服務目前的負載。</p>
            <h2>pi1 · 服務詳情</h2><div data-preview="detail"><ResourceUsage observation="state.detail" now="state.now"/></div>
            <h2>工作區服務</h2><div class="preview-grid"><div t-foreach="state.cards" t-as="card" t-key="card.id" class="preview-card" t-att-data-preview="card.id">
                <span class="preview-badge">執行中</span><h3 t-esc="card.name"/><div class="preview-type" t-esc="card.type"/>
                <ResourceUsage observation="card.observation" compact="true" now="state.now"/>
            </div></div>
        </t></templates>`;
        const unwrap = (xml) => xml.replace(/<\?xml[^>]*\?>|<\/?templates[^>]*>/g, "");
        return mount(Preview, document.getElementById("preview"), { templates: `<templates>${unwrap(template)}${unwrap(previewTemplate)}</templates>`, dev: true });
    }, { helper, component, template, messages });
    await page.waitForSelector('[data-preview="detail"] [data-resource="cpu"]');
    const detail = page.locator('[data-preview="detail"]');
    assert.match(await detail.innerText(), /3\.42.*32\.2/s);
    assert.match(await detail.innerText(), /18\.6.*64\.13/s);
    assert.match(await detail.innerText(), /106.*252/s);
    assert.match(await detail.innerText(), /每 30 秒更新/);
    const zero = page.locator('[data-preview="db"] [data-resource="cpu"]');
    assert.equal(await zero.locator("strong").innerText(), "0");
    assert.equal(await zero.locator('[role="progressbar"]').getAttribute("aria-valuenow"), "0");
    const partial = page.locator('[data-preview="partial"] [data-resource="cpu"]');
    assert.equal(await partial.locator('[role="progressbar"]').count(), 0);
    assert.match(await partial.innerText(), /1\/2/);
    const missing = page.locator('[data-preview="partial"] [data-resource="storage"]');
    assert.equal(await missing.locator("strong").innerText(), "—");
    await page.screenshot({ path: join(artifactDir, "service-resources-desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: join(artifactDir, "service-resources-mobile.png"), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, "mobile layout must not overflow horizontally");
    await page.evaluate(() => { window.resourcePreview.now += 300000; });
    await page.waitForFunction(() => document.querySelector('[data-preview="detail"] [data-resource="cpu"]').dataset.status === "stale");
    assert.match(await detail.innerText(), /資料已過期/);
    assert.equal(await detail.locator('[data-resource="cpu"] strong').innerText(), "3.42");
    await page.evaluate(() => { window.resourcePreview.detail = { status: "unsupported", cpu: { status: "unsupported" }, memory: { status: "unsupported" }, storage: { status: "unsupported" } }; });
    await page.waitForFunction(() => document.querySelector('[data-preview="detail"] [data-resource="cpu"]').dataset.status === "unsupported");
    assert.equal(await detail.locator('[role="progressbar"]').count(), 0);
    assert.match(await detail.innerText(), /暫不支援/);
    assert.deepEqual(errors, [], "real Owl template must render without browser errors");
    console.log(`Owl browser assertions passed; screenshots: ${artifactDir}`);
} finally {
    await browser.close();
}
