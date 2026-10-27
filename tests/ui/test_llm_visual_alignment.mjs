import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../../", import.meta.url);

async function read(relativePath) {
  return readFile(new URL(relativePath, root), "utf8");
}

const stylePaths = [
  "src/static/src/paas/pages/llm/tabs/LlmKeysTab.scss",
  "src/static/src/paas/pages/llm/tabs/LlmUsageTab.scss",
  "src/static/src/paas/pages/llm/tabs/LlmModelsTab.scss",
  "src/static/src/paas/pages/llm/tabs/LlmCreditTab.scss",
  "src/static/src/paas/pages/llm/tabs/LlmDocsTab.scss",
];

test("AI Models shell shares the Billing page width and the platform content gutter", async () => {
  const [page, billing] = await Promise.all([
    read("src/static/src/paas/pages/llm/LlmPage.scss"),
    read("src/static/src/paas/pages/billing/BillingOverviewPage.scss"),
  ]);
  assert.match(billing, /max-width:\s*1200px/);
  assert.match(page, /\.o_woow_llm_page\s*\{[^}]*max-width:\s*1200px/s);
  assert.doesNotMatch(page, /padding:\s*24px 32px/);
  assert.match(page, /font-size:\s*1\.75rem/);
  assert.match(page, /\.o_woow_llm_card[\s\S]*font-variant-numeric:\s*tabular-nums/);
});

test("all live AI Models tabs use the platform surface, radius, color, and focus tokens", async () => {
  for (const path of stylePaths) {
    const scss = await read(path);
    assert.match(scss, /\$woow-text-primary/, path);
    assert.match(scss, /\$woow-card-light/, path);
    assert.match(scss, /\$woow-radius-(?:default|lg)/, path);
    assert.match(scss, /:focus-visible/, path);
    assert.doesNotMatch(scss, /#212121|#646262|#6183fc|#6183FC|#3f63e8|#dce0e8|#edf0f4/, path);
    assert.doesNotMatch(scss, /transition:\s*all\b/, path);
  }
});

test("data-heavy tabs keep readable numbers and model IDs instead of browser defaults", async () => {
  const [usage, models, credit, keys, chart] = await Promise.all([
    read(stylePaths[1]),
    read(stylePaths[2]),
    read(stylePaths[3]),
    read(stylePaths[0]),
    read("src/static/src/paas/components/llm/LlmUsageChart.scss"),
  ]);
  for (const [name, scss] of Object.entries({ usage, models, credit, keys })) {
    assert.match(scss, /font-variant-numeric:\s*tabular-nums/, name);
  }
  assert.match(models, /code\s*\{[^}]*color:\s*inherit/s);
  assert.match(usage, /&__cards strong\s*\{[^}]*font-size:\s*1\.75rem/s);
  assert.match(chart, /grid-template-columns:\s*repeat\(auto-fit,\s*minmax\(11rem,\s*1fr\)\)/);
});

test("Models and Docs keep one page heading and use subordinate tab headings", async () => {
  const [modelsXml, docsXml] = await Promise.all([
    read("src/static/src/paas/pages/llm/tabs/LlmModelsTab.xml"),
    read("src/static/src/paas/pages/llm/tabs/LlmDocsTab.xml"),
  ]);
  assert.match(modelsXml, /<header class="o_woow_llm_models__heading">\s*<h2>Models and prices<\/h2>/);
  assert.doesNotMatch(modelsXml + docsXml, /<main\b|<h1\b/);
});
