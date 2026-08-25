/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";

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
 * Format ISO date string to locale display (e.g. "Apr 12, 2026").
 *
 * @param {string|null|undefined} dateStr
 * @returns {string}
 */
export function formatDate(dateStr) {
  if (!dateStr) return "-";
  try {
    const d = new Date(dateStr);
    return d.toLocaleDateString("en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return dateStr;
  }
}

/**
 * Format ISO date string including time (e.g. "Apr 12, 2026, 02:30 PM").
 * Used by transaction / credit-history views that need timestamp precision.
 *
 * @param {string|null|undefined} dateStr
 * @returns {string}
 */
export function formatDateTime(dateStr) {
  if (!dateStr) return "-";
  try {
    const d = new Date(dateStr);
    return d.toLocaleDateString("en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return dateStr;
  }
}

/**
 * Format ISO date string as short date+time without year
 * (e.g. "Apr 12, 02:30 PM").
 * Used by CreditHistoryModal compact rows.
 *
 * @param {string|null|undefined} dateStr
 * @returns {string}
 */
export function formatDateTimeShort(dateStr) {
  if (!dateStr) return "-";
  try {
    const d = new Date(dateStr);
    return d.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return dateStr;
  }
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
  const n = Number(value ?? 0);
  const abs = Math.abs(n);
  const body = abs.toLocaleString(undefined, {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  });
  let prefix = "";
  if (signed && n > 0) prefix = "+";
  else if (n < 0) prefix = "-";
  return _t("%(amount)s pts", { amount: `${prefix}${body}` });
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
  return Number(amount ?? 0).toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  });
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
  return Number(amount ?? 0).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
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
    bonus: bonus.toLocaleString(),
    total: total.toLocaleString(),
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
