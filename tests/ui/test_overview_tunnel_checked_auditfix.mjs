// 商用驗收 HS-013-2：服務頁「對外位址」那一格要講出資料多新（最後檢查時間），沒有值時不畫；
// EMQX-020-3／HS-013-3：範本的兩則提示各自只在有位址／有原因時出現。
// 純邏輯＋模板字串測試——載入手法同 test_service_presentation_workload_1647.mjs（元件依賴
// @odoo/owl，不能在 node 裡 import，所以把 getter 本體切出來、餵替身執行）。
//
// 跑法：node --test tests/ui/*.mjs
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (relativePath) =>
  readFile(new URL(`../../src/static/src/paas/${relativePath}`, import.meta.url), "utf8");

const overviewJs = await read("pages/service/tabs/OverviewTab.js");
const overviewXml = await read("pages/service/tabs/OverviewTab.xml");

function getterBody(source, name) {
  const head = `get ${name}() {`;
  const start = source.indexOf(head);
  assert.notEqual(start, -1, `找不到 getter ${name}`);
  let depth = 1;
  let index = start + head.length;
  while (index < source.length) {
    const char = source[index];
    if (char === '"' || char === "'" || char === "`") {
      index += 1;
      while (source[index] !== char) index += source[index] === "\\" ? 2 : 1;
    } else if (char === "{") {
      depth += 1;
    } else if (char === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start + head.length, index);
    }
    index += 1;
  }
  throw new Error(`getter ${name} 的大括號沒有配對`);
}

const checkedTextOf = (service) =>
  new Function(
    "_t",
    "formatUserDateTime",
    `return function () {${getterBody(overviewJs, "publicTunnelCheckedText")}};`,
  )(
    (message, ...args) => message.replace("%s", args[0]),
    (value) => `fmt(${value})`,
  ).call({ props: { service } });

test("最後檢查時間：有值時顯示、沒值時不畫", () => {
  assert.equal(
    checkedTextOf({ public_tunnel_checked_at: "2026-10-17T07:00:00+00:00" }),
    "Last checked: fmt(2026-10-17T07:00:00+00:00)",
  );
  assert.equal(checkedTextOf({ public_tunnel_checked_at: null }), "");
  assert.equal(checkedTextOf({}), "");
});

test("模板：最後檢查與兩則提示都在那一格、各自只在對的狀態出現", () => {
  const start = overviewXml.indexOf('t-if="props.service.has_public_tunnel_address"');
  assert.notEqual(start, -1);
  const row = overviewXml.slice(start, overviewXml.indexOf("<!-- Custom Domain -->", start));
  assert.match(row, /t-if="publicTunnelCheckedText" t-esc="publicTunnelCheckedText"/);
  assert.match(
    row,
    /t-if="props\.service\.public_tunnel_url and props\.service\.public_tunnel_address_note" t-esc="props\.service\.public_tunnel_address_note"/,
  );
  assert.match(
    row,
    /t-if="!props\.service\.public_tunnel_url and props\.service\.public_tunnel_error and props\.service\.public_tunnel_error_note" t-esc="props\.service\.public_tunnel_error_note"/,
  );
});
