/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { CancelSubscriptionModal } from "../../components/modal/CancelSubscriptionModal";
import { SubscribedServicesModal } from "../../components/modal/SubscribedServicesModal";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";
import { formatDate, formatAmount, computePageNumbers } from "../../utils/billing_helpers";

const ITEMS_PER_PAGE = 10;

const STATE_MAP = {
  in_progress: { label: "In Progress", color: "green" },
  draft: { label: "Draft", color: "gray" },
  expire: { label: "Expired", color: "gray" },
  close: { label: "Cancelled", color: "gray" },
  cancel: { label: "Cancelled", color: "gray" },
};

const FILTER_TABS = [
  { key: "active", label: "Active" },
  { key: "past", label: "Past" },
  { key: "all", label: "All" },
];

export class SubscriptionListPage extends Component {
  static template = "woow_paas_platform.SubscriptionListPage";
  static components = {
    WoowCard,
    WoowIcon,
    WoowButton,
    CancelSubscriptionModal,
    SubscribedServicesModal,
  };

  setup() {
    this.billing = useState(billingService);
    this.router = router;
    this.state = useState({
      filter: "active",
      page: 1,
      showCancelModal: false,
      showServicesModal: false,
      selectedSubscription: null,
    });

    onMounted(() => {
      this.loadSubscriptions();
    });
  }

  async loadSubscriptions() {
    await billingService.fetchSubscriptions(
      this.state.filter,
      this.state.page,
      ITEMS_PER_PAGE,
    );
  }

  get loading() {
    return this.billing.subscriptionsLoading;
  }

  get subscriptions() {
    return this.billing.subscriptions;
  }

  get totalSubscriptions() {
    return this.billing.subscriptionsTotal;
  }

  get currentPage() {
    return this.state.page;
  }

  get totalPages() {
    return Math.max(1, Math.ceil(this.totalSubscriptions / ITEMS_PER_PAGE));
  }

  get showingFrom() {
    if (this.totalSubscriptions === 0) return 0;
    return (this.currentPage - 1) * ITEMS_PER_PAGE + 1;
  }

  get showingTo() {
    return Math.min(this.currentPage * ITEMS_PER_PAGE, this.totalSubscriptions);
  }

  get pageNumbers() {
    return computePageNumbers(this.currentPage, this.totalPages);
  }

  get filterTabs() {
    return FILTER_TABS;
  }

  getStateInfo(sub) {
    return STATE_MAP[sub.state] || STATE_MAP.draft;
  }

  getStateLabel(sub) {
    if (sub.is_trial) return "Trial";
    return this.getStateInfo(sub).label;
  }

  getStateClass(sub) {
    if (sub.is_trial) return "o_woow_sub_status_blue";
    return `o_woow_sub_status_${this.getStateInfo(sub).color}`;
  }

  getSubIcon(sub) {
    if (sub.state === "in_progress") return "diamond";
    return "history";
  }

  isActive(sub) {
    return sub.state === "in_progress";
  }

  formatDate(dateStr) {
    return formatDate(dateStr);
  }

  formatPrice(price) {
    return formatAmount(price);
  }

  onFilterChange(filterKey) {
    this.state.filter = filterKey;
    this.state.page = 1;
    this.loadSubscriptions();
  }

  goToPage(page) {
    if (page < 1 || page > this.totalPages || page === this.currentPage) return;
    this.state.page = page;
    this.loadSubscriptions();
  }

  goToMarketplace() {
    this.router.navigate("workspaces");
  }

  goBack() {
    this.router.navigate("billing");
  }

  showManage(ev, sub) {
    ev.stopPropagation();
    this.state.selectedSubscription = sub;
    this.state.showServicesModal = true;
  }

  showCancel(ev, sub) {
    ev.stopPropagation();
    this.state.selectedSubscription = sub;
    this.state.showCancelModal = true;
  }

  hideServicesModal() {
    this.state.showServicesModal = false;
    this.state.selectedSubscription = null;
  }

  hideCancelModal() {
    this.state.showCancelModal = false;
    this.state.selectedSubscription = null;
  }

  onCancelled() {
    this.state.showCancelModal = false;
    this.state.selectedSubscription = null;
    this.loadSubscriptions();
  }
}
