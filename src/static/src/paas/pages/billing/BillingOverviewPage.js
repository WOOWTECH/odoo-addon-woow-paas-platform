/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { CreditHistoryModal } from "../../components/modal/CreditHistoryModal";
import { ManagePaymentMethodsModal } from "../../components/modal/ManagePaymentMethodsModal";
import { RenewSubscriptionModal } from "../../components/modal/RenewSubscriptionModal";
import { billingService } from "../../services/billing_service";
import { cloudService } from "../../services/cloud_service";
import { router } from "../../core/router";
import { formatDate, formatPoints } from "../../utils/billing_helpers";
import { formatUserDate } from "../../utils/datetime_helpers";

const STATUS_MAP = {
  draft: { label: _t("Draft"), color: "gray" },
  posted: { label: _t("Waiting"), color: "blue" },
  paid: { label: _t("Paid"), color: "green" },
  overdue: { label: _t("Overdue"), color: "red" },
};

const SUB_STATE_MAP = {
  in_progress: { label: _t("In Progress"), color: "green" },
  draft: { label: _t("Draft"), color: "gray" },
  expire: { label: _t("Expired"), color: "gray" },
  close: { label: _t("Cancelled"), color: "gray" },
  cancel: { label: _t("Cancelled"), color: "gray" },
};

export class BillingOverviewPage extends Component {
  static template = "woow_paas_platform.BillingOverviewPage";
  static components = {
    WoowCard, WoowIcon, WoowButton, CreditHistoryModal, ManagePaymentMethodsModal,
    RenewSubscriptionModal,
  };

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
      // #1611：可以自助續訂的服務（停權說明頁的續訂連結指到本頁）。
      renewals: [],
      renewTarget: null,
    });

    onMounted(() => {
      this.loadWallet();
      billingService.fetchInvoices(1, 3);
      billingService.fetchSubscriptions("active", 1, 3);
      billingService.fetchUpcomingRenewals();
      this.loadRenewals();
    });
  }

  // ---------------------------------------------------------------------------
  // #1611 待續訂的服務
  // ---------------------------------------------------------------------------

  async loadRenewals() {
    const result = await cloudService.fetchRenewals();
    this.state.renewals = result.success ? result.data : [];
  }

  renewalMeta(item) {
    const parts = [item.workspace_name];
    if (item.plan_name) {
      parts.push(item.plan_name);
    }
    if (item.renewal?.retention_until) {
      parts.push(_t("Renew by %(date)s", {
        date: formatUserDate(item.renewal.retention_until, ""),
      }));
    }
    return parts.join(" · ");
  }

  renewalStateLabel(item) {
    return item.state === "suspended" ? _t("Suspended") : _t("Subscription ended");
  }

  openRenew(item) {
    this.state.renewTarget = item;
  }

  closeRenew() {
    this.state.renewTarget = null;
  }

  loadRenewEstimate = () => {
    return cloudService.getRenewEstimate(this.state.renewTarget.id);
  };

  confirmRenew = (planId) => {
    return cloudService.renew(this.state.renewTarget.id, planId);
  };

  onRenewed() {
    const target = this.state.renewTarget;
    this.state.renewTarget = null;
    this.router.navigate(`workspace/${target.workspace_id}/services/${target.id}`);
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
    if (sub.is_trial) return _t("Trial");
    return this.getSubStateInfo(sub).label;
  }

  getSubStateClass(sub) {
    if (sub.is_trial) return "o_woow_sub_status_blue";
    return `o_woow_sub_status_${this.getSubStateInfo(sub).color}`;
  }

  /**
   * #1310：原本不論卡別一律寫死「Visa」；未綁卡時顯示「尚未綁定信用卡」，
   * 但訂閱其實是從點數扣款，容易讓人以為一定要綁卡。改用實際發卡行
   * （同 AddFundsModal.cardDisplay），未綁卡時標示以點數支付。
   */
  getPaymentMethodLabel() {
    const w = this.billing.wallet;
    if (w.cardBound && w.cardLast4) {
      return _t("%(card)s •••• %(last4)s", { card: w.cardBank || _t("Card"), last4: w.cardLast4 });
    }
    return _t("Paid with points");
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
      this.state.redeemError = result.error || _t("Redeem failed. Please try again.");
      this.state.redeeming = false;
      return;
    }

    // Refresh the wallet balance so the Credit Balance card reflects the grant.
    await this.loadWallet();
    const granted = result.data ? result.data.points_granted : 0;
    this.state.redeemSuccess = _t(
      "Successfully redeemed %s to your wallet.", this.formatPoints(granted));
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
