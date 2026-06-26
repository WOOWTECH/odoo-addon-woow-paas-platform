/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { CreditHistoryModal } from "../../components/modal/CreditHistoryModal";
import { ManagePaymentMethodsModal } from "../../components/modal/ManagePaymentMethodsModal";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";
import { formatDate, formatPoints } from "../../utils/billing_helpers";

const STATUS_MAP = {
  draft: { label: "Draft", color: "gray" },
  posted: { label: "Waiting", color: "blue" },
  paid: { label: "Paid", color: "green" },
  overdue: { label: "Overdue", color: "red" },
};

const SUB_STATE_MAP = {
  in_progress: { label: "In Progress", color: "green" },
  draft: { label: "Draft", color: "gray" },
  expire: { label: "Expired", color: "gray" },
  close: { label: "Cancelled", color: "gray" },
  cancel: { label: "Cancelled", color: "gray" },
};

export class BillingOverviewPage extends Component {
  static template = "woow_paas_platform.BillingOverviewPage";
  static components = { WoowCard, WoowIcon, WoowButton, CreditHistoryModal, ManagePaymentMethodsModal };

  setup() {
    this.billing = useState(billingService);
    this.router = useState(router);
    this.state = useState({
      showCreditHistoryModal: false,
      showManagePaymentMethods: false,
      redeemCode: "",
      redeeming: false,
      redeemError: null,
      redeemSuccess: null,
    });

    onMounted(() => {
      this.loadWallet();
      billingService.fetchInvoices(1, 3);
      billingService.fetchSubscriptions("active", 1, 3);
      billingService.fetchUpcomingRenewals();
    });
  }

  async loadWallet() {
    await billingService.fetchWallet();
  }

  get loading() {
    return this.billing.walletLoading;
  }

  get invoicesLoading() {
    return this.billing.invoicesLoading;
  }

  get recentInvoices() {
    return this.billing.invoices;
  }

  get formattedBalance() {
    return formatPoints(this.billing.wallet.balance ?? 0);
  }

  get cardInfo() {
    const w = this.billing.wallet;
    if (!w.cardBound) {
      return null;
    }
    return {
      last4: w.cardLast4,
      expiry: w.cardExpiry,
    };
  }

  getStatusInfo(invoice) {
    const status = invoice.payment_state || invoice.state || "draft";
    return STATUS_MAP[status] || STATUS_MAP.draft;
  }

  getStatusLabel(invoice) {
    return this.getStatusInfo(invoice).label;
  }

  getStatusClass(invoice) {
    return `o_woow_invoice_status_${this.getStatusInfo(invoice).color}`;
  }

  formatDate(dateStr) {
    return formatDate(dateStr);
  }

  formatPoints(amount) {
    return formatPoints(amount);
  }

  get subscriptionsLoading() {
    return this.billing.subscriptionsLoading;
  }

  get activeSubscriptions() {
    return this.billing.subscriptions;
  }

  get hasSubscriptions() {
    return this.activeSubscriptions.length > 0;
  }

  getSubStateInfo(sub) {
    return SUB_STATE_MAP[sub.state] || SUB_STATE_MAP.draft;
  }

  getSubStateLabel(sub) {
    if (sub.is_trial) return "Trial";
    return this.getSubStateInfo(sub).label;
  }

  getSubStateClass(sub) {
    if (sub.is_trial) return "o_woow_sub_status_blue";
    return `o_woow_sub_status_${this.getSubStateInfo(sub).color}`;
  }

  getPaymentMethodLabel() {
    const w = this.billing.wallet;
    if (w.cardBound && w.cardLast4) {
      return `Visa •••• ${w.cardLast4}`;
    }
    return "No card bound";
  }

  get hasLowBalanceWarning() {
    return this.billing.upcomingRenewals.has_low_balance;
  }

  get lowBalanceShortfall() {
    return this.billing.upcomingRenewals.shortfall;
  }

  showCreditHistory() {
    this.state.showCreditHistoryModal = true;
  }

  hideCreditHistory() {
    this.state.showCreditHistoryModal = false;
  }

  openManagePaymentMethods() {
    this.state.showManagePaymentMethods = true;
  }

  closeManagePaymentMethods() {
    this.state.showManagePaymentMethods = false;
  }

  onPaymentMethodsUpdated() {
    billingService.fetchWallet();
  }

  onRedeemInput(ev) {
    this.state.redeemCode = ev.target.value;
    // Clear stale feedback as soon as the user edits the code again.
    this.state.redeemError = null;
    this.state.redeemSuccess = null;
  }

  onRedeemKeydown(ev) {
    if (ev.key === "Enter") {
      this.redeem();
    }
  }

  async redeem() {
    const code = (this.state.redeemCode || "").trim();
    if (this.state.redeeming || !code) {
      return;
    }
    this.state.redeeming = true;
    this.state.redeemError = null;
    this.state.redeemSuccess = null;

    const result = await billingService.redeemCode(code);
    if (!result.success) {
      this.state.redeemError = result.error || "Redeem failed. Please try again.";
      this.state.redeeming = false;
      return;
    }

    // Refresh the wallet balance so the Credit Balance card reflects the grant.
    await this.loadWallet();
    const granted = result.data ? result.data.points_granted : 0;
    this.state.redeemSuccess = `Successfully redeemed ${this.formatPoints(granted)} to your wallet.`;
    this.state.redeemCode = "";
    this.state.redeeming = false;
  }

  goToTopup() {
    this.router.navigate("billing/topup");
  }

  goToInvoices() {
    this.router.navigate("billing/invoices");
  }

  goToInvoiceDetail(id) {
    this.router.navigate(`billing/invoices/${id}`);
  }

  goToSubscriptions() {
    this.router.navigate("billing/subscriptions");
  }
}
