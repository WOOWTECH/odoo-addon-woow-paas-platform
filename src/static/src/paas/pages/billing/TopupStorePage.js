/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { AddFundsModal } from "../../components/modal/AddFundsModal";
import { TopupSuccessModal } from "../../components/modal/TopupSuccessModal";
import { billingService } from "../../services/billing_service";
import { translateTopupError } from "../../services/topup_errors";
import { router } from "../../core/router";
import { formatPoints } from "../../utils/billing_helpers";
import { formatFloat } from "@web/core/utils/numbers";
import { helpUrl } from "../../core/platform_config";

const CATEGORY_TABS = [
  { key: null, label: _t("All Packs") },
  { key: "recommended", label: _t("Recommended") },
  { key: "large_volume", label: _t("Large Volume") },
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
      globalError: null,
      confirming: false,
    });

    onMounted(() => {
      billingService.fetchWallet();
      this.loadPlans();
    });

    onMounted(async () => {
      const q = this.router.params.query || {};
      if (!q.topup_id) return;
      const cleanHash = window.location.hash.split('?')[0];
      window.history.replaceState(null, '', cleanHash);
      let result = await billingService.getTopupStatus(q.topup_id);
      if (result && result.success && result.data.state === 'pending') {
        // Back from 3-D Secure: ECPay's server callback may land a few seconds
        // after the browser. Poll (~30 s) before telling the user to wait.
        this.state.confirming = true;
        const final = await billingService.pollTopupStatus(q.topup_id);
        this.state.confirming = false;
        if (final) {
          result = { success: true, data: final };
        }
      }
      if (result && result.success) {
        const data = result.data;
        if (data.state === 'done') {
          this.state.successData = {
            topup_id: data.topup_id,
            credits_added: data.credits,
            bonus: data.bonus,
            new_balance: data.new_balance,
          };
          this.state.showSuccessModal = true;
          await billingService.fetchWallet();
        } else if (data.state === 'failed') {
          this.state.globalError = translateTopupError(
            data.failure_message, data.failure_code,
          );
        } else if (data.state === 'pending') {
          this.state.globalError =
            _t('Still confirming payment — please refresh in a moment.');
        } else if (data.state === 'cancelled') {
          this.state.globalError =
            _t('Previous top-up was cancelled. You can start a new one.');
        }
      }
    });
  }

  get categoryTabs() {
    return CATEGORY_TABS;
  }

  /** #1453：「說明與支援」網址統一由 core/platform_config 提供。 */
  get helpUrl() {
    return helpUrl();
  }

  get loading() {
    return this.billing.topupPlansLoading;
  }

  get plans() {
    return this.billing.topupPlans;
  }

  get formattedBalance() {
    return formatPoints(this.billing.wallet.balance ?? 0);
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

  // #1310：點數走 formatPoints（原本走已 deprecated 的 formatAmount＋寫死的
  // "Credits"／"Bonus Points" 字樣）；NT$ 售價是真實金額，不是點數，維持 NT$ 前綴。
  formatPlanPoints(credits) {
    return formatPoints(credits, { precision: 0 });
  }

  formatBonus(bonus) {
    return _t("+%(points)s bonus", { points: formatPoints(bonus, { precision: 0 }) });
  }

  formatPrice(amount) {
    return formatFloat(Number(amount ?? 0), { digits: [16, 0] });
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
