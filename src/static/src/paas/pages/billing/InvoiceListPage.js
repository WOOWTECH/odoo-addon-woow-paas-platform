/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { PayInvoiceModal } from "../../components/modal/PayInvoiceModal";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";
import {
  formatDate,
  formatAmount,
  getInvoiceStatusInfo,
  computePageNumbers,
} from "../../utils/billing_helpers";

const ITEMS_PER_PAGE = 10;

export class InvoiceListPage extends Component {
  static template = "woow_paas_platform.InvoiceListPage";
  static components = { WoowCard, WoowIcon, WoowButton, PayInvoiceModal };

  setup() {
    this.billing = useState(billingService);
    this.router = router;
    this.state = useState({
      sortBy: "invoice_date",
      sortOrder: "desc",
      statusFilter: null,
      showPayModal: false,
      selectedInvoice: null,
    });

    onMounted(() => {
      this.loadInvoices();
    });
  }

  async loadInvoices() {
    await billingService.fetchInvoices(
      this.billing.invoicesPage,
      ITEMS_PER_PAGE,
      this.state.sortBy,
      this.state.sortOrder,
      this.state.statusFilter,
    );
  }

  get loading() {
    return this.billing.invoicesLoading;
  }

  get invoices() {
    return this.billing.invoices;
  }

  get totalInvoices() {
    return this.billing.invoicesTotal;
  }

  get currentPage() {
    return this.billing.invoicesPage;
  }

  get totalPages() {
    return Math.max(1, Math.ceil(this.totalInvoices / ITEMS_PER_PAGE));
  }

  get showingFrom() {
    if (this.totalInvoices === 0) return 0;
    return (this.currentPage - 1) * ITEMS_PER_PAGE + 1;
  }

  get showingTo() {
    return Math.min(this.currentPage * ITEMS_PER_PAGE, this.totalInvoices);
  }

  get pageNumbers() {
    return computePageNumbers(this.currentPage, this.totalPages);
  }

  getStatusInfo(invoice) {
    return getInvoiceStatusInfo(invoice);
  }

  getStatusLabel(invoice) {
    return this.getStatusInfo(invoice).label;
  }

  getStatusClass(invoice) {
    return `o_woow_invoice_status_${this.getStatusInfo(invoice).color}`;
  }

  canPay(invoice) {
    const status = invoice.status_label || "draft";
    return status === "waiting" || status === "overdue";
  }

  formatAmount(amount) {
    return formatAmount(amount);
  }

  formatDate(dateStr) {
    return formatDate(dateStr);
  }

  onSortChange(ev) {
    const value = ev.target.value;
    if (value === "date") {
      this.state.sortBy = "invoice_date";
      this.state.sortOrder = "desc";
    } else if (value === "amount") {
      this.state.sortBy = "amount_total";
      this.state.sortOrder = "desc";
    }
    billingService.invoicesPage = 1;
    this.loadInvoices();
  }

  onFilterChange(ev) {
    const value = ev.target.value;
    this.state.statusFilter = value === "all" ? null : value;
    billingService.invoicesPage = 1;
    this.loadInvoices();
  }

  goToPage(page) {
    if (page < 1 || page > this.totalPages || page === this.currentPage) return;
    billingService.invoicesPage = page;
    this.loadInvoices();
  }

  onInvoiceClick(invoice) {
    this.router.navigate(`billing/invoices/${invoice.id}`);
  }

  showPay(ev, invoice) {
    ev?.stopPropagation();
    this.state.selectedInvoice = invoice;
    this.state.showPayModal = true;
  }

  hidePay() {
    this.state.showPayModal = false;
    this.state.selectedInvoice = null;
  }

  onPayComplete() {
    this.state.showPayModal = false;
    this.state.selectedInvoice = null;
    this.loadInvoices();
  }

  goBack() {
    this.router.navigate("billing");
  }
}
