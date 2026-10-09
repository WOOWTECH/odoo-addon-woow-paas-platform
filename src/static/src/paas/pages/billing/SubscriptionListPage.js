/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { workspaceService } from "../../services/workspace_service";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { BrandIcon } from "../../components/brand-icon/BrandIcon";
import { subscriptionBrand } from "../../utils/brand_icons";
import { WoowButton } from "../../components/button/WoowButton";
import { CancelSubscriptionModal } from "../../components/modal/CancelSubscriptionModal";
import { SubscribedServicesModal } from "../../components/modal/SubscribedServicesModal";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";
import { formatDate, formatPoints, computePageNumbers } from "../../utils/billing_helpers";

const ITEMS_PER_PAGE = 10;

const STATE_MAP = {
  in_progress: { label: _t("In Progress"), color: "green" },
  draft: { label: _t("Draft"), color: "gray" },
  expire: { label: _t("Expired"), color: "gray" },
  close: { label: _t("Cancelled"), color: "gray" },
  cancel: { label: _t("Cancelled"), color: "gray" },
};

const FILTER_TABS = [
  { key: "active", label: _t("Active") },
  { key: "past", label: _t("Past") },
  { key: "all", label: _t("All") },
];

export class SubscriptionListPage extends Component {
  static template = "woow_paas_platform.SubscriptionListPage";
  static components = {
    WoowCard,
    WoowIcon, BrandIcon,
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
    if (sub.is_trial) return _t("Trial");
    return this.getStateInfo(sub).label;
  }

  getStateClass(sub) {
    if (sub.is_trial) return "o_woow_sub_status_blue";
    return `o_woow_sub_status_${this.getStateInfo(sub).color}`;
  }

  getSubBrand(sub) {
    return subscriptionBrand(sub);
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
    return formatPoints(price);
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

  /**
   * #1310：原本一律導到工作區清單，與「New Subscription」字面不符。
   * 只有一個工作區時直接進它的應用市集；多個工作區才讓使用者先選工作區。
   */
  async goToMarketplace() {
    if (!workspaceService.workspaces.length) {
      await workspaceService.fetchWorkspaces();
    }
    const active = workspaceService.workspaces;
    if (active.length === 1) {
      this.router.navigate(`workspace/${active[0].id}/services/marketplace`);
    } else {
      this.router.navigate("workspaces");
    }
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
