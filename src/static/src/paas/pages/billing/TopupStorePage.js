/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { AddFundsModal } from "../../components/modal/AddFundsModal";
import { TopupSuccessModal } from "../../components/modal/TopupSuccessModal";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";

const CATEGORY_TABS = [
  { key: null, label: "All Packs" },
  { key: "recommended", label: "Recommended" },
  { key: "large_volume", label: "Large Volume" },
];

export class TopupStorePage extends Component {
  static template = "woow_paas_platform.TopupStorePage";
  static components = { WoowCard, WoowIcon, WoowButton, AddFundsModal, TopupSuccessModal };

  setup() {
    this.billing = useState(billingService);
    this.router = router;
    this.state = useState({
      activeCategory: null,
      showAddFundsModal: false,
      showSuccessModal: false,
      selectedPlan: null,
      successData: null,
    });

    onMounted(() => {
      billingService.fetchWallet();
      this.loadPlans();
    });
  }

  get categoryTabs() {
    return CATEGORY_TABS;
  }

  get loading() {
    return this.billing.topupPlansLoading;
  }

  get plans() {
    return this.billing.topupPlans;
  }

  get formattedBalance() {
    const balance = this.billing.wallet.balance ?? 0;
    return balance.toLocaleString();
  }

  async loadPlans() {
    await billingService.fetchTopupPlans(this.state.activeCategory);
  }

  onCategoryChange(categoryKey) {
    this.state.activeCategory = categoryKey;
    this.loadPlans();
  }

  isCategoryActive(categoryKey) {
    return this.state.activeCategory === categoryKey;
  }

  formatAmount(amount) {
    return (amount ?? 0).toLocaleString(undefined, {
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    });
  }

  onPurchaseClick(plan) {
    this.state.selectedPlan = plan;
    this.state.showAddFundsModal = true;
  }

  onAddFundsClose() {
    this.state.showAddFundsModal = false;
    this.state.selectedPlan = null;
  }

  onAddFundsSuccess(data) {
    this.state.showAddFundsModal = false;
    this.state.selectedPlan = null;
    this.state.successData = data;
    this.state.showSuccessModal = true;
  }

  onSuccessClose() {
    this.state.showSuccessModal = false;
    this.state.successData = null;
  }

  goBack() {
    this.router.navigate("billing");
  }
}
