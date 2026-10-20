// INFRA-TS-004-1：spec `ignored_when` 的前端判準（utils/ignored_when.js）。
// 與後端 CloudAppTemplate._ignored_when_violation 同一個宣告；這裡驗純函式行為。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const helperUrl = new URL("../../src/static/src/paas/utils/ignored_when.js", import.meta.url);
const source = await readFile(helperUrl, "utf8");
const testableSource = source.replace(
  'import { _t } from "@web/core/l10n/translation";',
  "const _t = (message, vars) => message.replace(/%\\((\\w+)\\)s/g, (_m, k) => vars[k]);",
);
assert.notEqual(testableSource, source, "translation import must be stubbed");
const helper = await import(
  `data:text/javascript;base64,${Buffer.from(testableSource).toString("base64")}`,
);

// 與 seed（cloud_app_templates.xml cloud_app_tailscale）同形。
const CONTROL_PLANE = {
  key: "tailscale.controlPlane",
  label: "控制平面",
  type: "select",
  default: "official",
  options: [
    { value: "official", label: "官方 Tailscale" },
    { value: "workspace", label: "同工作區 Headscale" },
    { value: "custom", label: "自訂網址" },
  ],
};
const TAGS = {
  key: "tailscale.tags",
  label: "標籤 tags",
  type: "text",
  ignored_when: { key: "tailscale.controlPlane", values: ["workspace"] },
};
const SPECS = { required: [], optional: [CONTROL_PLANE, TAGS] };

test("條件成立時回說明（用條件欄位與選項的 label）", () => {
  assert.equal(
    helper.ignoredWhenMessage(TAGS, SPECS, { "tailscale.controlPlane": "workspace" }),
    'Not applied when "控制平面" is "同工作區 Headscale"; leave this field empty.',
  );
  // 巢狀 values（服務 API 回來的 helm_values 形態）。
  assert.ok(helper.ignoredWhenMessage(TAGS, SPECS, { tailscale: { controlPlane: " workspace " } }));
});

test("條件不成立、或用預設值（official）時沒有說明", () => {
  for (const values of [
    {},
    { "tailscale.controlPlane": "official" },
    { "tailscale.controlPlane": "custom" },
    { tailscale: { controlPlane: "official" } },
  ]) {
    assert.equal(helper.ignoredWhenMessage(TAGS, SPECS, values), null, JSON.stringify(values));
  }
  assert.equal(helper.ignoredWhenMessage(CONTROL_PLANE, SPECS, {}), null);
});

test("宣告寫錯一律當作沒有（與後端一樣 fail-open）", () => {
  const values = { "tailscale.controlPlane": "workspace" };
  for (const rule of [
    "tailscale.controlPlane=workspace",
    { key: "missing", values: ["workspace"] },
    { key: "tailscale.controlPlane", values: "workspace" },
    { key: "tailscale.controlPlane", values: [] },
    { key: "tailscale.tags", values: ["x"] },
    ["tailscale.controlPlane"],
  ]) {
    const spec = { ...TAGS, ignored_when: rule };
    assert.equal(helper.ignoredWhenMessage(spec, SPECS, values), null, JSON.stringify(rule));
  }
});

test("檢視模式：有值且條件成立才標「未套用」", () => {
  assert.equal(
    helper.ignoredWhenViewNote(TAGS, SPECS, {
      tailscale: { controlPlane: "workspace", tags: "tag:qa" },
    }),
    'Not applied: "控制平面" is "同工作區 Headscale".',
  );
  assert.equal(
    helper.ignoredWhenViewNote(TAGS, SPECS, { tailscale: { controlPlane: "workspace", tags: "  " } }),
    null,
  );
  assert.equal(
    helper.ignoredWhenViewNote(TAGS, SPECS, { tailscale: { controlPlane: "official", tags: "tag:qa" } }),
    null,
  );
});

test("空值判準與後端 _blank 一致", () => {
  for (const blank of [null, undefined, false, "", "   ", []]) {
    assert.equal(helper.isBlankSpecValue(blank), true, JSON.stringify(blank));
  }
  for (const filled of ["tag:qa", 0, true, ["a"]]) {
    assert.equal(helper.isBlankSpecValue(filled), false, JSON.stringify(filled));
  }
});

test("lookupSpecValue：扁平 → 巢狀 → spec 預設", () => {
  assert.equal(helper.lookupSpecValue({ "tailscale.controlPlane": "custom" }, CONTROL_PLANE), "custom");
  assert.equal(helper.lookupSpecValue({ tailscale: { controlPlane: "workspace" } }, CONTROL_PLANE), "workspace");
  assert.equal(helper.lookupSpecValue({}, CONTROL_PLANE), "official");
  assert.equal(helper.lookupSpecValue({}, TAGS), "");
});
