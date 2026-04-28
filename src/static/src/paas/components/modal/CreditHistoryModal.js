/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";
import {
  formatDateTimeShort,
  formatAmountDecimal,
  formatTopupDescription,
  getTxnDisplay,
} from "../../utils/billing_helpers";

/**
 * CreditHistoryModal Component
 *
 * Quick-view modal for recent credit point transactions.
 *
 * Props:
 *   - onClose (Function): Called when modal is closed
 */
export class CreditHistoryModal extends Component {
  static template = "woow_paas_platform.CreditHistoryModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    onClose: { type: Function },
  };

  setup() {
    this.billing = useState(billingService);

    onMounted(() => {
      billingService.fetchTransactions(1, 10);
      billingService.fetchWallet();
    });
  }

  get loading() {
    return this.billing.transactionsLoading;
  }

  get transactions() {
    return this.billing.transactions;
  }

  get formattedBalance() {
    const balance = this.billing.wallet.balance ?? 0;
    return balance.toLocaleString();
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
    return formatDateTimeShort(dateStr);
  }

  formatDescription(txn) {
    return formatTopupDescription(txn) || txn.name || "-";
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget) {
      this.props.onClose();
    }
  }

  goToTopup() {
    this.props.onClose();
    router.navigate("billing/topup");
  }

  goToAllTransactions() {
    this.props.onClose();
    router.navigate("billing/transactions");
  }
}
