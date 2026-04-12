/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { PayInvoiceModal } from "../../components/modal/PayInvoiceModal";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";

const STATUS_MAP = {
  draft: { label: "Draft", color: "gray" },
  waiting: { label: "Waiting for Payment", color: "blue" },
  paid: { label: "Paid", color: "green" },
  overdue: { label: "Overdue", color: "red" },
};

export class InvoiceDetailPage extends Component {
  static template = "woow_paas_platform.InvoiceDetailPage";
  static components = { WoowCard, WoowIcon, WoowButton, PayInvoiceModal };
  static props = {
    invoiceId: { type: Number },
  };

  setup() {
    this.billing = useState(billingService);
    this.router = router;
    this.state = useState({
      showPayModal: false,
    });

    onMounted(() => {
      billingService.fetchInvoice(this.props.invoiceId);
      billingService.fetchWallet();
    });
  }

  get loading() {
    return this.billing.invoiceDetailLoading;
  }

  get invoice() {
    return this.billing.invoiceDetail;
  }

  get statusInfo() {
    if (!this.invoice) return STATUS_MAP.draft;
    const status = this.invoice.status_label || "draft";
    return STATUS_MAP[status] || STATUS_MAP.draft;
  }

  get statusClass() {
    return `o_woow_invoice_status_${this.statusInfo.color}`;
  }

  get canPay() {
    if (!this.invoice) return false;
    const status = this.invoice.status_label || "draft";
    return status === "waiting" || status === "overdue";
  }

  get dueLabel() {
    if (!this.invoice) return "";
    const status = this.invoice.status_label || "draft";
    if (status === "paid") return "Paid";
    if (status === "overdue") return "Overdue";
    if (!this.invoice.invoice_date_due) return "";
    try {
      const due = new Date(this.invoice.invoice_date_due);
      const now = new Date();
      const diff = Math.ceil((due - now) / (1000 * 60 * 60 * 24));
      if (diff < 0) return "Overdue";
      if (diff === 0) return "Due today";
      return `Due in ${diff} day${diff > 1 ? "s" : ""}`;
    } catch {
      return "";
    }
  }

  get dueLabelClass() {
    if (!this.invoice) return "";
    const status = this.invoice.status_label || "draft";
    if (status === "paid") return "o_woow_due_paid";
    if (status === "overdue") return "o_woow_due_overdue";
    return "o_woow_due_pending";
  }

  get downloadUrl() {
    if (!this.invoice) return "#";
    return `/report/pdf/account.account_invoices/${this.invoice.id}`;
  }

  get lines() {
    return this.invoice?.lines || [];
  }

  formatAmount(amount) {
    return (amount ?? 0).toLocaleString(undefined, {
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    });
  }

  formatAmountDecimal(amount) {
    return (amount ?? 0).toLocaleString(undefined, {
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
      });
    } catch {
      return dateStr;
    }
  }

  showPayDialog() {
    this.state.showPayModal = true;
  }

  hidePayDialog() {
    this.state.showPayModal = false;
  }

  onPayComplete() {
    this.state.showPayModal = false;
    billingService.fetchInvoice(this.props.invoiceId);
  }

  goBack() {
    this.router.navigate("billing/invoices");
  }
}
