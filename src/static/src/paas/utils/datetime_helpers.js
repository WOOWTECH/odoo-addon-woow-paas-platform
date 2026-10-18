/** @odoo-module **/

import {
  formatDate as odooFormatDate,
  formatDateTime as odooFormatDateTime,
} from "@web/core/l10n/dates";
import { localization } from "@web/core/l10n/localization";

const { DateTime } = luxon;

/**
 * 日期／時間顯示格式的單一入口（#107）。
 *
 * 目的：讓 `/woow` 的日期／時間顯示**跟隨 Odoo 使用者語系**（`res.lang.date_format`
 * / `time_format`），而不是跟隨瀏覽器語言。
 *
 * 為什麼需要這一層，而不是各處直接呼叫 `@web/core/l10n/dates`：
 *
 * 1. **型別**：Odoo 的 `formatDate` / `formatDateTime` 吃的是 luxon `DateTime`，
 *    而本 app 手上全部是後端來的**字串**，中間一定要有一次轉換。
 * 2. **格式不只一種**：後端送來的日期字串有四種長相（見下方 SHAPE 表），
 *    Odoo 現成的 `deserializeDate` / `deserializeDateTime` 內部走 `DateTime.fromSQL`，
 *    **只吃 SQL 空格格式**，對用量最大的那一種（帶 `T`）一律回 invalid。
 * 3. **invalid 是 truthy**：luxon 的 invalid DateTime 為真值，而 `formatDate()`
 *    只在 `!value` 時早退 ⇒ 餵錯格式不會得到空字串，而是把字串 `"Invalid DateTime"`
 *    顯示給使用者。因此本層的 `parseBackendDate()` **解析失敗一律回 `null`**，
 *    由呼叫端決定要顯示什麼 fallback。
 *
 * ⚠ Odoo 的 `parseDateTime()` 看起來也能救場（其 docstring 自稱會試 ISO8601 與 SQL），
 * 但它是「解析使用者鍵入字串」的工具、不是 wire-format deserializer：它的成功分支
 * 會因語系而異（zh_TW 的純數字格式會讓 shorthand 分支提前吃掉輸入），
 * ⇒ 用它寫出來的 bug 可能只在 zh_TW 下發生、en_US 下永遠測不出來。**本層不使用它。**
 *
 * 後端序列化格式本身的不一致另案處理（#916）——那是跨 repo 契約，不在 i18n 批次內改。
 */

// ---------------------------------------------------------------------------
// 後端日期字串的四種長相（#107 盤點；來源與逐一出處見 #916）
// ---------------------------------------------------------------------------
//   SHAPE-1  只有日期            "2026-08-26"                    fields.Date + isoformat()
//   SHAPE-2  帶 T、不帶 offset   "2026-08-26T12:34:56.123456"    fields.Datetime + isoformat()（用量最大）
//   SHAPE-3  SQL 空格            "2026-08-26 12:34:56"           Odoo json_default 的預設序列化
//            ⚠ 已知的唯一來源（`last_release_sync_at` 漏 `.isoformat()`）已於 #917 在後端修掉，
//            但本層**刻意保留**對這個形狀的支援：那不是為了那一格，是為了**還沒被發現的
//            其他漏網欄位**——只要有人在 controller 的回應 dict 裡放裸 datetime，
//            Odoo 就會替他序列化成這個形狀，而且沒有任何訊號。
//   SHAPE-4  帶 offset 或 Z      "2026-08-26T10:44:22.962052+08:00"
//                                                                helm history --output json 的 `updated`（已實測）
//                                "2026-08-26T12:34:56.789Z"      前端自產的 toISOString()

/**
 * 後端送來、**不帶時區資訊**的時間字串要採用哪一種讀法。
 *
 * - `"utc-wire-format"`（目前值，#914 起）：把不帶 offset 的值一律視為 UTC 的線上格式
 *   （Odoo ORM 的實際語意：`fields.Datetime` 在 DB 與 `.isoformat()` 都是 naive UTC），
 *   再轉成顯示時區；只有日期（SHAPE-1）視為**日曆日**，不做任何時區位移。
 * - `"ecmascript-date"`（#914 之前）：交給 JS 引擎的 `new Date()` 依 ECMA-262 判讀。
 *   規格對兩種形狀的規定並不對稱：只有日期當 **UTC 午夜**（UTC 以西少一天），
 *   帶 `T` 但不帶 offset 當 **local time**（台北差 8 小時）——X-LC-006-1／-2 的根因。
 *   保留這個分支只為了必要時能一行回退。
 *
 * 跨時區、跨粒度的驗證在 `tests/ui/test_datetime_tz_914.mjs`（Taipei／UTC／Los_Angeles／
 * Pago_Pago／Kiritimati 各跑一次真的 luxon）。
 *
 * @type {"ecmascript-date"|"utc-wire-format"}
 */
export const BACKEND_NAIVE_DATETIME_READING = "utc-wire-format";

const HAS_EXPLICIT_OFFSET_RE = /(?:Z|[+-]\d{2}:?\d{2})$/;
const DATE_ONLY_RE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * 把後端（或前端自產）的日期／時間值解析成 luxon `DateTime`。
 *
 * 四種 SHAPE 全吃。⛔ **解析失敗回 `null`，絕不回 invalid DateTime**——
 * 呼叫端據此提供自己的 fallback 文字。
 *
 * @param {string|Date|null|undefined} value
 * @returns {DateTime|null}
 */
export function parseBackendDate(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }

  if (value instanceof Date) {
    return validOrNull(DateTime.fromJSDate(value));
  }

  if (typeof value !== "string") {
    return null;
  }

  const raw = value.trim();
  if (!raw) {
    return null;
  }

  if (BACKEND_NAIVE_DATETIME_READING === "utc-wire-format") {
    return parseAsUtcWireFormat(raw);
  }

  // "ecmascript-date"：與本批之前各 call site 使用的 `new Date(dateStr)` 逐形狀等價，
  // 包含 `new Date()` 能吃、但 luxon 的 fromISO/fromSQL 不吃的非標準形狀
  // ⇒ 本批不會因為換 parser 而讓任何原本顯示得出來的值變成 fallback。
  const native = DateTime.fromJSDate(new Date(raw));
  if (native.isValid) {
    return native;
  }

  // SQL 空格格式（SHAPE-3）在主流引擎的 `new Date()` 都吃得下，但那是**非標準**行為
  // （ECMA-262 只規定 ISO 8601 形狀，其餘由實作自訂）⇒ 不能靠它。
  // 顯式補一支 `fromSQL`，讓這個形狀的支援不依賴瀏覽器實作細節。
  return validOrNull(DateTime.fromSQL(raw, { numberingSystem: "latn" }));
}

/**
 * `"utc-wire-format"` 讀法：不帶 offset 的值視為 UTC，帶 offset 的照其 offset 解讀，
 * 兩者都轉成顯示時區。只有日期的值視為日曆日（不做時區位移）。
 *
 * @param {string} raw
 * @returns {DateTime|null}
 */
function parseAsUtcWireFormat(raw) {
  if (DATE_ONLY_RE.test(raw)) {
    return validOrNull(DateTime.fromISO(raw, { numberingSystem: "latn" }));
  }

  const zone = HAS_EXPLICIT_OFFSET_RE.test(raw) ? undefined : "utc";
  const opts = { numberingSystem: "latn", setZone: true, ...(zone ? { zone } : {}) };

  const iso = DateTime.fromISO(raw, opts);
  if (iso.isValid) {
    return iso.setZone("default");
  }

  const sql = DateTime.fromSQL(raw, opts);
  return sql.isValid ? sql.setZone("default") : null;
}

/**
 * @param {DateTime} dt
 * @returns {DateTime|null}
 */
function validOrNull(dt) {
  return dt && dt.isValid ? dt : null;
}

/**
 * 依 Odoo 使用者語系格式化日期（`res.lang.date_format`）。
 *
 * @param {string|Date|null|undefined} value
 * @param {string} [fallback=""] 值為空或無法解析時回傳的字串
 * @returns {string}
 */
export function formatUserDate(value, fallback = "") {
  const dt = parseBackendDate(value);
  return dt ? odooFormatDate(dt) : fallback;
}

/**
 * 依 Odoo 使用者語系格式化日期＋時間（`date_format` ＋ `time_format`）。
 *
 * @param {string|Date|null|undefined} value
 * @param {{showSeconds?: boolean, condensed?: boolean}} [options]
 *   `showSeconds` 預設 `false`（改用 `res.lang.short_time_format`）；
 *   `condensed` 讓月／日／時去掉前導零。
 * @param {string} [fallback=""]
 * @returns {string}
 */
export function formatUserDateTime(value, options = {}, fallback = "") {
  const dt = parseBackendDate(value);
  if (!dt) {
    return fallback;
  }
  const { showSeconds = false, condensed = false } = options;
  return odooFormatDateTime(dt, { showSeconds, condensed });
}

/**
 * 依 Odoo 使用者語系只格式化時間部分（`res.lang.short_time_format`）。
 *
 * @param {string|Date|null|undefined} value
 * @param {string} [fallback=""]
 * @returns {string}
 */
export function formatUserTime(value, fallback = "") {
  const dt = parseBackendDate(value);
  if (!dt) {
    return fallback;
  }
  // ⚠ `localization` 是 Proxy，service 尚未 start 完成時存取會 throw
  // ⇒ 只能在呼叫當下取值，⛔ 不可在 module 頂層先取出來存成常數。
  return odooFormatDateTime(dt, { format: localization.shortTimeFormat });
}

/**
 * 值所在的**當地日曆日**距離今天幾天（今天＝0、明天＝1、昨天＝-1）。
 *
 * 給「到期日／截止日」判斷用（發票到期、任務逾期）：只有日期的欄位是日曆日，
 * ⛔ 不可用 `new Date("YYYY-MM-DD")`（那是 UTC 午夜，UTC 以西會提早一天判成逾期，X-LC-006-2）。
 *
 * @param {string|Date|null|undefined} value
 * @param {{today?: string}} [options] `today`：測試用的「今天」（`YYYY-MM-DD`，當地日曆日）
 * @returns {number|null} 無法解析時回 `null`
 */
export function calendarDaysFromToday(value, options = {}) {
  const dt = parseBackendDate(value);
  if (!dt) {
    return null;
  }
  const today = options.today
    ? DateTime.fromISO(options.today, { numberingSystem: "latn" })
    : DateTime.local();
  const target = DateTime.fromObject({ year: dt.year, month: dt.month, day: dt.day });
  const base = DateTime.fromObject({ year: today.year, month: today.month, day: today.day });
  return Math.round(target.diff(base, "days").days);
}
