/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";

const STATUS_MAP = {
  draft: { label: "Draft", color: "gray" },
  posted: { label: "Waiting", color: "blue" },
  paid: { label: "Paid", color: "green" },
  overdue: { label: "Overdue", color: "red" },
};

export class BillingOverviewPage extends Component {
  static template = "woow_paas_platform.BillingOverviewPage";
  static components = { WoowCard, WoowIcon, WoowButton };

  setup() {
    this.billing = useState(billingService);
    this.router = useState(router);

    onMounted(() => {
      this.loadWallet();
      billingService.fetchInvoices(1, 3);
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
    const balance = this.billing.wallet.balance ?? 0;
    return balance.toLocaleString();
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

  formatAmount(amount) {
    return (amount ?? 0).toLocaleString(undefined, {
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    });
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
