/** @odoo-module **/

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
 * Format amount with locale separators, no decimals (e.g. "1,234").
 *
 * @param {number|null|undefined} amount
 * @returns {string}
 */
export function formatAmount(amount) {
  return (amount ?? 0).toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  });
}

/**
 * Format amount with 2 decimal places (e.g. "1,234.00").
 *
 * @param {number|null|undefined} amount
 * @returns {string}
 */
export function formatAmountDecimal(amount) {
  return (amount ?? 0).toLocaleString(undefined, {
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
  draft: { label: "Draft", color: "gray" },
  waiting: { label: "Waiting for Payment", color: "blue" },
  paid: { label: "Paid", color: "green" },
  overdue: { label: "Overdue", color: "red" },
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
