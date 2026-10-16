/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";

/**
 * 「AI 模型」分頁的數字與時間格式（DESIGN §6.6〔REV〕42；規則由 tests/ui/test_llm_format.mjs 守）。
 *
 * - 餘額、可用額度一律**無條件捨去**（不高估：0.004 點顯示 0.00）；餘額與月花費 2 位小數、
 *   單筆花費 4 位，不足 0.0001 顯示「<0.0001」；千分位。
 * - 換算只在顯示時做；能拿到整數 quota 時用 `quotaToPointsText`（整數運算，不經浮點）。
 * - tokens 達一萬用「萬」；時間一律換成台北時間（UTC+8，台灣沒有日光節約）。
 * - 畫面上不出現 USD 或 quota。
 *
 * 不用 billing_helpers 的 formatPoints：那支是四捨五入（點數錢包的既有格式），這裡必須捨去。
 */

/** D5：1 點＝15,625 quota（R＝32 永不改；與後端 llm_common.QUOTA_PER_POINT 相同）。 */
export const QUOTA_PER_POINT = 15625;

const TAIPEI_OFFSET_MS = 8 * 60 * 60 * 1000;

function groupThousands(digits) {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** 「最小單位」的整數（例：2 位小數時 1 單位＝0.01 點）→ 帶千分位的字串。 */
function unitsToText(units, digits) {
  const negative = units < 0;
  const abs = negative ? -units : units;
  const scale = 10 ** digits;
  const whole = groupThousands(String(Math.floor(abs / scale)));
  const fraction = digits > 0 ? "." + String(abs % scale).padStart(digits, "0") : "";
  return (negative ? "-" : "") + whole + fraction;
}

/**
 * 非負數 → 十進位的整數部分與小數部分字串。先取 15 位有效數字：消掉二進位浮點的雜訊
 * （0.29 存成 0.28999…，直接乘 100 再 floor 會少 0.01），而 JSON 帶得過來、15 位以內的值都不會變。
 */
function decimalParts(abs) {
  const [mantissa, exponent] = abs.toPrecision(15).split("e");
  const [whole, fraction = ""] = mantissa.split(".");
  let digits = whole + fraction;
  let point = whole.length + (exponent ? Number(exponent) : 0);
  if (point <= 0) {
    digits = "0".repeat(1 - point) + digits;
    point = 1;
  }
  if (point > digits.length) {
    digits += "0".repeat(point - digits.length);
  }
  return { whole: digits.slice(0, point), fraction: digits.slice(point) };
}

/**
 * 數值 → 捨去到 `digits` 位的最小單位整數（往負無限大）；不是有限數字時回 `null`。
 * 用十進位字串截斷，不用 `Math.floor(n * 100)`：後者會被浮點誤差少算，加 epsilon 又會把
 * 0.299999 進位成 0.30（高估）。
 */
function floorUnits(value, digits) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  const { whole, fraction } = decimalParts(Math.abs(n));
  const kept = fraction.slice(0, digits).padEnd(digits, "0");
  let units = Number(whole + kept);
  if (n < 0) {
    units = -units;
    if (/[1-9]/.test(fraction.slice(digits))) {
      units -= 1;
    }
  }
  return units;
}

/** 點數（可能帶小數）→ 捨去後的數字字串（不含單位）；無效值回 `null`。 */
export function floorPointsText(value, digits = 2) {
  const units = floorUnits(value, digits);
  return units === null ? null : unitsToText(units, digits);
}

/**
 * 整數 quota → 點數字串（捨去，負數往負無限大捨去＝不低估透支）。整數運算、不經浮點；
 * 合計一律先加總 quota 再呼叫這支（不逐列捨去後再加，統計卡和明細才對得上）。
 */
export function quotaToPointsText(quota, digits = 2) {
  if (typeof quota !== "number" || !Number.isInteger(quota)) return null;
  const q = BigInt(quota);
  const scale = BigInt(10) ** BigInt(digits);
  const per = BigInt(QUOTA_PER_POINT);
  const scaled = q * scale;
  let units = scaled / per; // BigInt 除法往 0 截斷
  if (scaled % per !== BigInt(0) && scaled < BigInt(0)) {
    units -= BigInt(1);
  }
  return unitsToText(Number(units), digits);
}

/** AI 額度、月花費（點數已換算好的值）：2 位小數、捨去、帶「點」。無效值顯示「—」。 */
export function formatAiPoints(value) {
  const amount = floorPointsText(value, 2);
  return amount === null ? "—" : _t("%(amount)s pts", { amount });
}

/**
 * AI 額度（後端送的**整數 quota**，`/api/llm/state` 的 `account.balance_quota`）：2 位小數、捨去
 * （負數往負無限大＝不低估透支），帶「點」；不是整數時顯示「—」。`balance_points` 是後端先捨去成
 * 整數的點數，拿它顯示 2 位小數只會是假的「.00」（12.99 點顯示成 12.00 點）。
 */
export function formatAiQuota(quota) {
  const amount = quotaToPointsText(quota, 2);
  return amount === null ? "—" : _t("%(amount)s pts", { amount });
}

/** 只有整數點數可用時（沒有 quota）：不帶小數，不顯示假的「.00」。 */
export function formatWholePoints(value) {
  const amount = floorPointsText(value, 0);
  return amount === null ? "—" : _t("%(amount)s pts", { amount });
}

/**
 * 透支了多少點（quota 小於 0 時）：大小無條件**進位**到 0.01 點（不低估透支），不帶負號；
 * 沒有透支或不是整數時回 `null`。
 */
export function overdraftPointsText(quota) {
  if (typeof quota !== "number" || !Number.isInteger(quota) || quota >= 0) return null;
  // quotaToPointsText 對負數往負無限大捨去（-1 quota → "-0.01"），去掉負號就是進位後的大小。
  return quotaToPointsText(quota, 2).replace(/^-/, "");
}

/** 畫面上的 AI 額度是不是「0.00 點」（零額度橫幅用；與卡片顯示同一個捨去規則）。 */
export function isZeroCreditQuota(quota) {
  return typeof quota === "number" && Number.isInteger(quota) && quota >= 0
    && quotaToPointsText(quota, 2) === "0.00";
}

/** 單筆花費：4 位小數、捨去；大於 0 但不足 0.0001 點時顯示「<0.0001」。 */
export function formatSpendPoints(value) {
  const n = Number(value);
  if (value === null || value === undefined || !Number.isFinite(n)) return "—";
  const amount = n > 0 && n < 0.0001 ? "<0.0001" : floorPointsText(n, 4);
  return _t("%(amount)s pts", { amount });
}

/** 次數：整數、千分位。 */
export function formatCount(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return unitsToText(Math.floor(value), 0);
}

/** 「1／200 次」（今日免費試用）；次數未知時顯示「—」而不是 0（0 是一個事實，未知不是）。 */
export function formatFreeTrialUsage(count, limit) {
  return _t("%(count)s / %(limit)s calls", {
    count: typeof count === "number" ? formatCount(count) : "—",
    limit: formatCount(limit),
  });
}

/** 一位小數的捨去字串，整數時不帶「.0」（12.0 萬 → 12 萬）。 */
function oneDecimal(value) {
  const tenths = floorUnits(value, 1);
  const whole = groupThousands(String(Math.floor(tenths / 10)));
  return tenths % 10 === 0 ? whole : whole + "." + (tenths % 10);
}

/**
 * tokens：未滿一萬照常千分位；達一萬改用「萬」（捨去到 1 位小數）。
 * 英文原文用 K：msgid 同時帶兩個參數，zh_TW 譯文改用 `%(tenThousands)s 萬`，
 * 所以兩種語系各自用自己的單位，不必在程式裡判斷語系。
 */
export function formatTokens(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "—";
  if (value < 10000) return formatCount(value);
  return _t("%(thousands)sK", {
    thousands: oneDecimal(value / 1000),
    tenThousands: oneDecimal(value / 10000),
  });
}

/** 耗時（毫秒）→「1.2 秒」。 */
export function formatLatency(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return "—";
  return _t("%(seconds)s s", { seconds: oneDecimal(ms / 1000) });
}

function taipeiParts(isoString) {
  if (typeof isoString !== "string" || !isoString) return null;
  const millis = Date.parse(isoString);
  if (Number.isNaN(millis)) return null;
  const shifted = new Date(millis + TAIPEI_OFFSET_MS);
  const pad = (n) => String(n).padStart(2, "0");
  return {
    date: shifted.getUTCFullYear() + "-" + pad(shifted.getUTCMonth() + 1) + "-" + pad(shifted.getUTCDate()),
    time: pad(shifted.getUTCHours()) + ":" + pad(shifted.getUTCMinutes()),
  };
}

/**
 * 後端 ISO 字串（一律帶 offset，例 `2026-10-17T03:05:00+00:00`）→ 台北時間「2026-10-17 11:05」。
 * 不靠瀏覽器時區：使用者人在哪裡，畫面上都是台北時間（欄位標題寫明「台北時間」）。
 */
export function formatTaipeiDateTime(isoString) {
  const parts = taipeiParts(isoString);
  return parts ? parts.date + " " + parts.time : "—";
}

/** 只要時分（同一天的清單、「資料時間 HH:mm」）。 */
export function formatTaipeiTime(isoString) {
  const parts = taipeiParts(isoString);
  return parts ? parts.time : "—";
}

/** 只要日期（金鑰到期日）。 */
export function formatTaipeiDate(isoString) {
  const parts = taipeiParts(isoString);
  return parts ? parts.date : "—";
}

/** 到期時間在現在之後、7 天以內 ⇒ 標「即將到期」（DESIGN §6.3.2）；已到期的由狀態顯示。 */
export function isExpiringSoon(isoString, nowMillis = Date.now()) {
  if (typeof isoString !== "string" || !isoString) return false;
  const millis = Date.parse(isoString);
  if (Number.isNaN(millis)) return false;
  return millis > nowMillis && millis - nowMillis <= 7 * 24 * 60 * 60 * 1000;
}
