/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";

const ITEMS_PER_PAGE = 20;

/**
 * Determine transaction display properties based on amount and description.
 *
 * @param {Object} txn - Transaction object
 * @returns {{ icon: string, colorClass: string, sign: string }}
 */
function getTxnDisplay(txn) {
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

export class TransactionListPage extends Component {
  static template = "woow_paas_platform.TransactionListPage";
  static components = { WoowCard, WoowIcon };

  setup() {
    this.billing = useState(billingService);
    this.router = router;
    this.localState = useState({ page: 1 });

    onMounted(() => {
      this.loadTransactions();
    });
  }

  async loadTransactions() {
    await billingService.fetchTransactions(this.localState.page, ITEMS_PER_PAGE);
  }

  get loading() {
    return this.billing.transactionsLoading;
  }

  get transactions() {
    return this.billing.transactions;
  }

  get totalTransactions() {
    return this.billing.transactionsTotal;
  }

  get currentPage() {
    return this.localState.page;
  }

  get totalPages() {
    return Math.max(1, Math.ceil(this.totalTransactions / ITEMS_PER_PAGE));
  }

  get showingFrom() {
    if (this.totalTransactions === 0) return 0;
    return (this.currentPage - 1) * ITEMS_PER_PAGE + 1;
  }

  get showingTo() {
    return Math.min(this.currentPage * ITEMS_PER_PAGE, this.totalTransactions);
  }

  get pageNumbers() {
    const pages = [];
    const total = this.totalPages;
    const current = this.currentPage;
    const maxVisible = 5;

    let start = Math.max(1, current - Math.floor(maxVisible / 2));
    let end = Math.min(total, start + maxVisible - 1);
    if (end - start + 1 < maxVisible) {
      start = Math.max(1, end - maxVisible + 1);
    }

    for (let i = start; i <= end; i++) {
      pages.push(i);
    }
    return pages;
  }

  getTxnIcon(txn) {
    return getTxnDisplay(txn).icon;
  }

  getTxnColorClass(txn) {
    return getTxnDisplay(txn).colorClass;
  }

  formatAmount(txn) {
    const display = getTxnDisplay(txn);
    const abs = Math.abs(txn.amount ?? 0);
    const formatted = abs.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    return `${display.sign}$${formatted}`;
  }

  getAmountClass(txn) {
    return txn.amount >= 0 ? "o_woow_txn_amount_credit" : "o_woow_txn_amount_debit";
  }

  formatBalance(txn) {
    if (txn.balance_after == null) return "";
    return txn.balance_after.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  formatDate(dateStr) {
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

  goToPage(page) {
    if (page < 1 || page > this.totalPages || page === this.currentPage) return;
    this.localState.page = page;
    this.loadTransactions();
  }

  goBack() {
    this.router.navigate("billing");
  }
}
