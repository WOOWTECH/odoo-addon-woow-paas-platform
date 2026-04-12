/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";
import {
  formatDateTime,
  formatAmountDecimal,
  getTxnDisplay,
  computePageNumbers,
} from "../../utils/billing_helpers";

const ITEMS_PER_PAGE = 20;

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
    return computePageNumbers(this.currentPage, this.totalPages);
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
    const formatted = formatAmountDecimal(abs);
    return `${display.sign}$${formatted}`;
  }

  getAmountClass(txn) {
    return txn.amount >= 0 ? "o_woow_txn_amount_credit" : "o_woow_txn_amount_debit";
  }

  formatBalance(txn) {
    if (txn.balance_after == null) return "";
    return formatAmountDecimal(txn.balance_after);
  }

  formatDate(dateStr) {
    return formatDateTime(dateStr);
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
