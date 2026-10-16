// LLM-01c（DESIGN §6.6〔REV〕42）：「AI 模型」分頁的數字與時間格式。
//
// 規則：餘額／可用額度無條件捨去（不高估）；餘額 2 位、單筆花費 4 位（不足 0.0001 顯示 <0.0001）；
// 合計先加總 quota 再換算；千分位；tokens 達一萬用「萬」；時間一律台北時間；不出現 USD／quota。
// `_t` 用 zh_TW.po 的 odoo-javascript 條目（與 Odoo 前端相同的過濾），所以斷言的是畫面上的中文。
import assert from "node:assert/strict";
import test from "node:test";
import {
  loadPo, readPaas, stubImports, toDataUrl, translateFromGlobal, translator,
} from "./lib/llm_test_support.mjs";

const po = await loadPo();
const zh = translator(po, "zh_TW");
const en = translator(po, "en");
globalThis.__llmT = zh;

const format = await import(toDataUrl(stubImports(await readPaas("utils/llm_format.js"), {
  "@web/core/l10n/translation": translateFromGlobal,
})));

function inLang(lang, fn) {
  globalThis.__llmT = lang === "en" ? en : zh;
  try {
    return fn();
  } finally {
    globalThis.__llmT = zh;
  }
}

test("餘額無條件捨去到 2 位：0.004 點顯示 0.00、不被浮點誤差少算", () => {
  assert.equal(format.floorPointsText(0.004), "0.00");
  assert.equal(format.floorPointsText(0.0099), "0.00");
  assert.equal(format.floorPointsText(0.29), "0.29"); // 0.29 × 100 ＝ 28.999…
  assert.equal(format.floorPointsText(0.299999), "0.29");
  assert.equal(format.floorPointsText(12.345), "12.34");
  assert.equal(format.floorPointsText(0), "0.00");
  assert.equal(format.floorPointsText(1234567.891), "1,234,567.89");
  // 負數（透支）往負無限大捨去：不低估欠款。
  assert.equal(format.floorPointsText(-0.004), "-0.01");
  assert.equal(format.floorPointsText(null), null);
  assert.equal(format.floorPointsText("abc"), null);
});

test("AI 額度帶單位「點」：zh_TW 是「點」、英文是 pts；未知值顯示 —", () => {
  assert.equal(format.formatAiPoints(0), "0.00 點");
  assert.equal(format.formatAiPoints(1234.5678), "1,234.56 點");
  assert.equal(format.formatAiPoints(null), "—");
  assert.equal(inLang("en", () => format.formatAiPoints(0)), "0.00 pts");
});

test("quota → 點：整數運算、捨去；合計先加總 quota 再換算", () => {
  assert.equal(format.QUOTA_PER_POINT, 15625);
  assert.equal(format.quotaToPointsText(15625), "1.00");
  assert.equal(format.quotaToPointsText(15624), "0.99");
  assert.equal(format.quotaToPointsText(156), "0.00");
  assert.equal(format.quotaToPointsText(157), "0.01");
  assert.equal(format.quotaToPointsText(15625 * 1000000), "1,000,000.00");
  assert.equal(format.quotaToPointsText(-1), "-0.01");
  assert.equal(format.quotaToPointsText(1, 4), "0.0000");
  assert.equal(format.quotaToPointsText(2, 4), "0.0001");
  assert.equal(format.quotaToPointsText(1.5), null);
  // 7812＋7813 quota：逐列捨去再加是 0.49＋0.50＝0.99，先加總再換算才是 1.00（統計卡對得上明細）。
  const rows = [7812, 7813];
  assert.deepEqual(rows.map((q) => format.quotaToPointsText(q)), ["0.49", "0.50"]);
  assert.equal(format.quotaToPointsText(rows.reduce((a, b) => a + b, 0)), "1.00");
});

test("單筆花費 4 位小數、捨去；大於 0 但不足 0.0001 顯示 <0.0001", () => {
  assert.equal(format.formatSpendPoints(0.00005), "<0.0001 點");
  assert.equal(format.formatSpendPoints(0.0001), "0.0001 點");
  assert.equal(format.formatSpendPoints(0.123456), "0.1234 點");
  assert.equal(format.formatSpendPoints(0), "0.0000 點");
  assert.equal(format.formatSpendPoints(null), "—");
});

test("次數千分位；今日免費試用「1／200 次」，次數未知顯示 —（不是 0）", () => {
  assert.equal(format.formatCount(1234567), "1,234,567");
  assert.equal(format.formatCount(0), "0");
  assert.equal(format.formatCount(null), "—");
  assert.equal(format.formatFreeTrialUsage(1, 200), "1／200 次");
  assert.equal(format.formatFreeTrialUsage(null, 200), "—／200 次");
  assert.equal(inLang("en", () => format.formatFreeTrialUsage(1, 200)), "1 / 200 calls");
});

test("tokens：未滿一萬千分位；達一萬 zh_TW 用「萬」、英文用 K（捨去到 1 位）", () => {
  assert.equal(format.formatTokens(9999), "9,999");
  assert.equal(format.formatTokens(10000), "1 萬");
  assert.equal(format.formatTokens(12345), "1.2 萬");
  assert.equal(format.formatTokens(19999), "1.9 萬");
  assert.equal(format.formatTokens(123456789), "12,345.6 萬");
  assert.equal(inLang("en", () => format.formatTokens(12345)), "12.3K");
  assert.equal(inLang("en", () => format.formatTokens(10000)), "10K");
  assert.equal(format.formatTokens(-1), "—");
  assert.equal(format.formatTokens(null), "—");
});

test("耗時：毫秒 → 秒（1 位小數、捨去）", () => {
  assert.equal(format.formatLatency(1500), "1.5 秒");
  assert.equal(format.formatLatency(3000), "3 秒");
  assert.equal(format.formatLatency(0), "0 秒");
  assert.equal(format.formatLatency(undefined), "—");
});

test("時間一律台北時間（UTC+8），不看執行環境的時區", () => {
  assert.equal(format.formatTaipeiDateTime("2026-10-16T16:30:00+00:00"), "2026-10-17 00:30");
  assert.equal(format.formatTaipeiDateTime("2026-10-17T03:05:00Z"), "2026-10-17 11:05");
  assert.equal(format.formatTaipeiTime("2026-10-16T16:30:00+00:00"), "00:30");
  assert.equal(format.formatTaipeiDate("2027-01-14T23:59:59+00:00"), "2027-01-15");
  assert.equal(format.formatTaipeiDateTime("not a date"), "—");
  assert.equal(format.formatTaipeiTime(null), "—");
});

test("即將到期：到期在 7 天內才標；已到期與 7 天以上都不標", () => {
  const now = Date.parse("2026-10-17T00:00:00Z");
  assert.equal(format.isExpiringSoon("2026-10-23T00:00:00Z", now), true);
  assert.equal(format.isExpiringSoon("2026-10-24T00:00:00Z", now), true);
  assert.equal(format.isExpiringSoon("2026-10-25T00:00:00Z", now), false);
  assert.equal(format.isExpiringSoon("2026-10-16T00:00:00Z", now), false);
  assert.equal(format.isExpiringSoon(null, now), false);
});

test("輸出不含 USD 或 quota 字樣", () => {
  const outputs = [
    format.formatAiPoints(1), format.formatSpendPoints(0.5), format.formatTokens(50000),
    format.formatFreeTrialUsage(3, 200), format.formatLatency(1200),
  ];
  for (const text of outputs) {
    assert.doesNotMatch(text, /USD|quota|\$/i, text);
  }
});
