/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { formatFloat } from "@web/core/utils/numbers";

import { formatUserDate, formatUserDateTime } from "./datetime_helpers";

/**
 * Shared billing helper utilities.
 *
 * Extracted from InvoiceListPage, InvoiceDetailPage, BillingOverviewPage,
 * TransactionListPage, SubscriptionListPage and CreditHistoryModal to
 * eliminate duplicated formatting / display logic.
 */

// ---------------------------------------------------------------------------
// Date formatting
// ---------------------------------------------------------------------------

/**
 * 依 Odoo 使用者語系格式化日期（zh_TW 下為 `2026年08月26日`）。
 *
 * ⚠ #107 前寫死 `en-US`，與 `services/utils.js` 寫死的 `zh-TW` 並存
 * ⇒ 同一個畫面的日期格式取決於走哪條 code path。現已統一走 `res.lang.date_format`。
 *
 * @param {string|null|undefined} dateStr
 * @returns {string}
 */
export function formatDate(dateStr) {
  return formatUserDate(dateStr, "-");
}

/**
 * 依 Odoo 使用者語系格式化日期＋時間（不含秒）。
 * 交易／點數歷史等需要時間精度的畫面使用。
 *
 * @param {string|null|undefined} dateStr
 * @returns {string}
 */
export function formatDateTime(dateStr) {
  return formatUserDateTime(dateStr, { showSeconds: false }, "-");
}

/**
 * 依 Odoo 使用者語系格式化日期＋時間的精簡版（月／日／時去前導零）。
 * CreditHistoryModal 的密集列與 Dashboard 活動列使用。
 *
 * ⚠ #107 之前這裡刻意不顯示年份（`Apr 12, 02:30 PM`）。改走 Odoo 語系後
 * **年份會出現**：Odoo 的 localization 只有「完整日期格式」一種概念，
 * `condensed` 只去前導零（見 `@web/core/l10n/dates` 的 `getCondensedFormat`），
 * 不會移除年份；要移除就得自己對 `localization.dateFormat` 做語系相依的字串裁切，
 * 那是新造一個故障面。⇒ 接受年份出現，密度損失若需處理另議。
 *
 * @param {string|null|undefined} dateStr
 * @returns {string}
 */
export function formatDateTimeShort(dateStr) {
  return formatUserDateTime(dateStr, { showSeconds: false, condensed: true }, "-");
}

// ---------------------------------------------------------------------------
// Amount formatting
// ---------------------------------------------------------------------------

/**
 * Format a points / credits value for display.
 *
 * Conventions:
 * - Always two decimals (eliminates "574 vs 573.85" precision mismatch).
 * - Always `pts` suffix (replaces the prior $ / Points / Credits zoo).
 * - Optional `+` / `-` sign prefix for transaction rows so debits and
 *   credits are visually distinct.
 *
 * Examples:
 *   formatPoints(573.85)                    → "573.85 pts"
 *   formatPoints(573.85, { signed: true })  → "+573.85 pts"
 *   formatPoints(-573.85, { signed: true }) → "-573.85 pts"
 *   formatPoints(0, { signed: true })       → "0.00 pts"  (no sign on zero)
 *   formatPoints(null)                      → "0.00 pts"
 *   formatPoints(1234.5, { precision: 0 })  → "1,235 pts" (rounded)
 *
 * @param {number|null|undefined} value
 * @param {{signed?: boolean, precision?: number}} [opts]
 * @returns {string}
 */
export function formatPoints(value, { signed = false, precision = 2 } = {}) {
  return _t("%(amount)s pts", { amount: _pointsNumber(value, { signed, precision }) });
}

/**
 * 點數數字本體（含正負號、不含單位）。formatPoints／formatPointsPerUnit 共用。
 *
 * @param {number|null|undefined} value
 * @param {{signed?: boolean, precision?: number}} [opts]
 * @returns {string}
 */
function _pointsNumber(value, { signed = false, precision = 2 } = {}) {
  const n = Number(value ?? 0);
  const abs = Math.abs(n);
  // #107：改走 Odoo 的 formatFloat ⇒ 千分位與小數點跟隨 res.lang
  // （`localization.thousandsSep` / `decimalPoint` / `grouping`），
  // 而非原本那個不傳 locale、實際跟隨瀏覽器語言的 Intl 呼叫。
  const body = formatFloat(abs, { digits: [16, precision] });
  let prefix = "";
  if (signed && n > 0) prefix = "+";
  else if (n < 0) prefix = "-";
  return `${prefix}${body}`;
}

/**
 * 方案計費週期單位的縮寫（#1310：從 PlanSelector／PlanTotalBar 各自一份的
 * `_unitLabel` 收斂到這裡）。month → `mo`（月）、year → `yr`（年），
 * 其餘（day／week 等實務上沒有 seed 的週期）原字串顯示。
 *
 * @param {string|null|undefined} unit `sh.subscription.plan` 的 `sh_unit`
 * @returns {string}
 */
export function planUnitLabel(unit) {
  if (unit === "month") return _t("mo");
  if (unit === "year") return _t("yr");
  return unit || "";
}

/**
 * 方案價格「點數／週期」的單一格式化來源（#1310）。
 *
 * 方案價是整數點時不顯示小數（`1,500 pts/mo`、`1,500 點/月`），有小數才顯示兩位
 * ——與 formatPoints 共用千分位與正負號規則。
 *
 * Examples:
 *   formatPointsPerUnit(1500, "month")                   → "1,500 pts/mo"
 *   formatPointsPerUnit(99.5, "year")                    → "99.50 pts/yr"
 *   formatPointsPerUnit(300, "month", { signed: true })  → "+300 pts/mo"
 *
 * @param {number|null|undefined} value
 * @param {string|null|undefined} unit
 * @param {{signed?: boolean}} [opts]
 * @returns {string}
 */
export function formatPointsPerUnit(value, unit, { signed = false } = {}) {
  const n = Number(value ?? 0);
  const precision = Number.isInteger(n) ? 0 : 2;
  return _t("%(amount)s pts/%(unit)s", {
    amount: _pointsNumber(n, { signed, precision }),
    unit: planUnitLabel(unit),
  });
}

/**
 * Format amount with locale separators, no decimals (e.g. "1,234").
 *
 * @deprecated Use `formatPoints(amount, { precision: 0 })` for new code.
 *   Kept as a backward-compatibility shim — preserves the original contract
 *   (no `pts` suffix, no decimals) so any caller missed by the G1 sweep
 *   continues to render correctly.
 *
 * @param {number|null|undefined} amount
 * @returns {string}
 */
export function formatAmount(amount) {
  return formatFloat(Number(amount ?? 0), { digits: [16, 0] });
}

/**
 * Format amount with 2 decimal places (e.g. "1,234.00").
 *
 * @deprecated Use `formatPoints(amount)` for new code.
 *   Kept as a backward-compatibility shim — preserves the original contract
 *   (no `pts` suffix, two decimals) so any caller missed by the G1 sweep
 *   continues to render correctly.
 *
 * @param {number|null|undefined} amount
 * @returns {string}
 */
export function formatAmountDecimal(amount) {
  return formatFloat(Number(amount ?? 0), { digits: [16, 2] });
}

// ---------------------------------------------------------------------------
// Invoice status
// ---------------------------------------------------------------------------

/**
 * Invoice status map: status key -> { label, color }.
 */
export const INVOICE_STATUS_MAP = {
  draft: { label: _t("Draft"), color: "gray" },
  waiting: { label: _t("Waiting for Payment"), color: "blue" },
  paid: { label: _t("Paid"), color: "green" },
  overdue: { label: _t("Overdue"), color: "red" },
};

/**
 * Get invoice status display info from an invoice object.
 *
 * @param {Object} invoice
 * @returns {{ label: string, color: string }}
 */
export function getInvoiceStatusInfo(invoice) {
  const status = invoice.status_label || "draft";
  return INVOICE_STATUS_MAP[status] || INVOICE_STATUS_MAP.draft;
}

// ---------------------------------------------------------------------------
// Transaction display
// ---------------------------------------------------------------------------

/**
 * Render a top-up description with bonus breakdown when applicable.
 *
 * For top-up transactions that include a bonus, expands the stored
 * description (e.g. `"Top-up: Plan 1000"`) into
 * `"Top-up: Plan 1000 + 100 bonus = 1,100 pts"` so users can see what
 * they paid for vs what they earned. Non-topup rows, legacy rows that
 * predate the metadata migration, and zero-bonus top-ups all fall back
 * to the unchanged `description` string.
 *
 * @param {Object} txn - Transaction object with `description`, `base_amount`,
 *   `bonus_amount`
 * @returns {string}
 */
export function formatTopupDescription(txn) {
  if (!txn) return "";
  const bonus = txn.bonus_amount || 0;
  if (bonus <= 0) return txn.description || "";
  const base = txn.base_amount || 0;
  const total = base + bonus;
  // Defensive against a falsy description: avoid interpolating literal "undefined".
  const desc = txn.description || "";
  return _t("%(description)s + %(bonus)s bonus = %(total)s pts", {
    description: desc,
    bonus: formatFloat(bonus, { digits: [16, 0] }),
    total: formatFloat(total, { digits: [16, 0] }),
  });
}

/**
 * Determine transaction display properties based on amount and description.
 *
 * @param {Object} txn - Transaction object with `amount` and `description`
 * @returns {{ icon: string, colorClass: string, sign: string }}
 */
export function getTxnDisplay(txn) {
  const desc = (txn.description || "").toLowerCase();
  const isPromotion = desc.includes("promotion") || desc.includes("bonus");

  if (txn.amount >= 0) {
    if (isPromotion) {
      return { icon: "redeem", colorClass: "o_woow_txn_purple", sign: "+" };
    }
    return { icon: "account_balance_wallet", colorClass: "o_woow_txn_blue", sign: "+" };
  }
  return { icon: "arrow_outward", colorClass: "o_woow_txn_gray", sign: "" };
}

// ---------------------------------------------------------------------------
// Pagination
// ---------------------------------------------------------------------------

/**
 * Compute a sliding-window array of page numbers for pagination controls.
 *
 * @param {number} currentPage - 1-based current page
 * @param {number} totalPages  - total number of pages
 * @param {number} [maxVisible=5] - max page buttons to show
 * @returns {number[]}
 */
export function computePageNumbers(currentPage, totalPages, maxVisible = 5) {
  const pages = [];
  let start = Math.max(1, currentPage - Math.floor(maxVisible / 2));
  let end = Math.min(totalPages, start + maxVisible - 1);
  if (end - start + 1 < maxVisible) {
    start = Math.max(1, end - maxVisible + 1);
  }
  for (let i = start; i <= end; i++) {
    pages.push(i);
  }
  return pages;
}
