/** @odoo-module **/
import { Component, onMounted, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { formatPoints } from "../../utils/billing_helpers";
import { formatUserDate } from "../../utils/datetime_helpers";

/**
 * RenewSubscriptionModal（#1611）
 *
 * 訂閱被取消（欠費）或到期、服務還在保留期內時的自助續訂確認框：選方案 → 看首期金額與
 * 錢包餘額 → 「續訂並付款」。與 SkipTrialModal 同一個形狀：父頁注入 `loadEstimate` /
 * `confirmRenew` callback（指向 `/renew-estimate`、`/renew`），modal 自己不知道打哪支。
 *
 * 能不能付款、夠不夠錢、付款人是誰全部讀後端 estimate 的欄位（`payable_via_woow`、
 * `wallet_balance`、`payer_is_viewer`、各方案的 `total`），前端不自己推導（fail-closed：
 * 欄位缺席一律當作不能付款）。真正的判斷在後端 `_renew_lapsed_subscription` 會再做一次。
 *
 * Props:
 *   - serviceName (String): 顯示用。
 *   - loadEstimate (Function): () => Promise<ApiResponse<Estimate>>，onMounted 呼叫，無副作用。
 *   - confirmRenew (Function): (planId) => Promise<ApiResponse<Object>>，使用者確認時呼叫。
 *   - onClose (Function): 取消／關閉。
 *   - onRenewed (Function): 續訂成功後呼叫（帶後端回傳 data）；父頁負責關 modal 與重載。
 */
export class RenewSubscriptionModal extends Component {
  static template = "woow_paas_platform.RenewSubscriptionModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    serviceName: { type: String },
    loadEstimate: { type: Function },
    confirmRenew: { type: Function },
    onClose: { type: Function },
    onRenewed: { type: Function },
  };

  setup() {
    this.state = useState({
      loading: true,
      estimateError: null,
      estimate: null,
      selectedPlanId: undefined,
      renewing: false,
      error: null,
    });
    onMounted(() => this.loadEstimate());
  }

  async loadEstimate() {
    this.state.loading = true;
    this.state.estimateError = null;
    const result = await this.props.loadEstimate();
    if (result.success) {
      this.state.estimate = result.data;
      const plans = result.data.plans || [];
      const chosen =
        plans.find((p) => p.plan_id === this.state.selectedPlanId) ||
        plans.find((p) => p.is_current) ||
        plans[0];
      this.state.selectedPlanId = chosen ? chosen.plan_id : undefined;
    } else {
      this.state.estimateError = result.error || _t("Failed to load the renewal details");
    }
    this.state.loading = false;
  }

  get plans() {
    return this.state.estimate?.plans || [];
  }

  get selectedPlan() {
    return this.plans.find((p) => p.plan_id === this.state.selectedPlanId) || null;
  }

  get walletBalance() {
    return this.state.estimate?.wallet_balance ?? 0;
  }

  get shortage() {
    const plan = this.selectedPlan;
    return plan ? Math.max(0, plan.total - this.walletBalance) : 0;
  }

  get sufficient() {
    return !!this.selectedPlan && this.shortage <= 0;
  }

  get isPaymentBlocked() {
    return !!this.state.estimate && this.state.estimate.payable_via_woow !== true;
  }

  get payerIsViewer() {
    return this.state.estimate?.payer_is_viewer === true;
  }

  get retentionUntil() {
    return formatUserDate(this.state.estimate?.renewal?.retention_until, "");
  }

  get canConfirm() {
    return (
      !this.state.loading &&
      !this.state.estimateError &&
      !!this.selectedPlan &&
      !this.isPaymentBlocked &&
      this.sufficient &&
      !this.state.renewing
    );
  }

  formatPoints(amount) {
    return formatPoints(amount);
  }

  formatDate(value) {
    return formatUserDate(value, "-");
  }

  get leadText() {
    return _t("Renew the subscription of %(name)s to bring the service back.", {
      name: this.props.serviceName,
    });
  }

  get retentionText() {
    return _t("Renew by %(date)s; after that the service and its data are removed.", {
      date: this.retentionUntil,
    });
  }

  get taxLabel() {
    const rate = ((this.selectedPlan?.tax_rate || 0) * 100).toFixed(0);
    return _t("Tax (%(rate)s)", { rate: `${rate}%` });
  }

  get periodText() {
    const plan = this.selectedPlan;
    if (!plan) return "";
    return `${this.formatDate(plan.billing_cycle_start)} – ${this.formatDate(plan.billing_cycle_end)}`;
  }

  get shortageText() {
    return _t("Insufficient balance. You need %(amount)s more to renew.", {
      amount: this.formatPoints(this.shortage),
    });
  }

  selectPlan(planId) {
    if (this.state.renewing) return;
    this.state.selectedPlanId = planId;
    this.state.error = null;
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.renewing) {
      this.props.onClose();
    }
  }

  async onConfirm() {
    if (!this.canConfirm) return;
    this.state.renewing = true;
    this.state.error = null;
    const result = await this.props.confirmRenew(this.state.selectedPlanId);
    if (result.success) {
      // 頁首的點數餘額是 billingService 的快取：付款之後重讀（同 PayInvoiceModal）。
      billingService.fetchWallet();
      this.props.onRenewed(result.data);
      return;
    }
    this.state.error = result.error || _t("Failed to renew the subscription");
    this.state.renewing = false;
    // 錢包餘額可能已經變了（例如另一個分頁剛付過款）：重讀一次，畫面上的數字才對得上。
    if (result.code === "insufficient_balance") {
      await this.loadEstimate();
      this.state.error = result.error;
    }
  }
}
