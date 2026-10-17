/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { PayInvoiceModal } from "../../components/modal/PayInvoiceModal";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";
import {
  formatDate,
  formatPoints,
  INVOICE_STATUS_MAP,
} from "../../utils/billing_helpers";

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
    if (!this.invoice) return INVOICE_STATUS_MAP.draft;
    const status = this.invoice.status_label || "draft";
    return INVOICE_STATUS_MAP[status] || INVOICE_STATUS_MAP.draft;
  }

  get statusClass() {
    return `o_woow_invoice_status_${this.statusInfo.color}`;
  }

  get isPaymentDue() {
    if (!this.invoice) return false;
    const status = this.invoice.status_label || "draft";
    return status === "waiting" || status === "overdue";
  }

  get canPay() {
    return this.isPaymentDue && this.invoice?.payable_via_woow === true;
  }

  get isPaymentBlocked() {
    return (
      this.isPaymentDue &&
      this.invoice?.payable_via_woow !== true &&
      !!this.invoice?.pay_blocked_reason
    );
  }

  get dueLabel() {
    if (!this.invoice) return "";
    const status = this.invoice.status_label || "draft";
    if (status === "paid") return _t("Paid");
    if (status === "overdue") return _t("Overdue");
    if (!this.invoice.invoice_date_due) return "";
    try {
      const due = new Date(this.invoice.invoice_date_due);
      const now = new Date();
      const diff = Math.ceil((due - now) / (1000 * 60 * 60 * 24));
      if (diff < 0) return _t("Overdue");
      if (diff === 0) return _t("Due today");
      return diff > 1 ? _t("Due in %s days", diff) : _t("Due in %s day", diff);
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
    // AUDITFIX tax-invoice-pdf-403：原生 /report/pdf 以租戶身分渲染，含稅發票讀不到
    // account.tax 會 403；平台端點先驗擁有權再以平台身分渲染。
    return `/api/billing/invoices/${this.invoice.id}/pdf`;
  }

  get lines() {
    return this.invoice?.lines || [];
  }

  formatPoints(amount) {
    return formatPoints(amount);
  }

  formatDate(dateStr) {
    return formatDate(dateStr);
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
