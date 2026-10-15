/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { translateTopupError } from "../../services/topup_errors";
import { router } from "../../core/router";
import { formatFloat } from "@web/core/utils/numbers";
import { formatPoints } from "../../utils/billing_helpers";

/**
 * AddFundsModal Component
 *
 * Confirmation modal for purchasing a top-up plan with a credit card.
 *
 * Props:
 *   - plan (Object): Top-up plan data { id, name, amount, credits, bonus }
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onSuccess (Function): Called after successful top-up with result data
 */
export class AddFundsModal extends Component {
  static template = "woow_paas_platform.AddFundsModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    plan: { type: Object },
    onClose: { type: Function },
    onSuccess: { type: Function },
  };

  setup() {
    this.billing = useState(billingService);
    this.state = useState({
      processing: false,
      pending: false,
      error: null,
    });

    onMounted(() => {
      billingService.fetchWallet();
    });
  }

  get plan() {
    return this.props.plan;
  }

  get formattedAmount() {
    return formatFloat(this.plan.amount ?? 0, { digits: [16, 0] });
  }

  get formattedCredits() {
    return formatFloat(this.plan.credits ?? 0, { digits: [16, 0] });
  }

  get totalCredits() {
    return (this.plan.credits ?? 0) + (this.plan.bonus ?? 0);
  }

  // #1310：點數一律走 formatPoints（原本 XML 自己接 " pts"、bonus 是未格式化的原始數字）。
  get formattedTotalCredits() {
    return formatPoints(this.totalCredits, { precision: 0 });
  }

  get bonusNote() {
    return _t("(incl. %(bonus)s bonus)", {
      bonus: formatPoints(this.plan.bonus ?? 0, { precision: 0 }),
    });
  }

  get hasCard() {
    return this.billing.wallet.cardBound;
  }

  get cardDisplay() {
    if (!this.hasCard) return "";
    const bank = this.billing.wallet.cardBank || _t("Card");
    return `${bank} \u2022\u2022\u2022\u2022 ${this.billing.wallet.cardLast4}`;
  }

  get cardExpiry() {
    return this.billing.wallet.cardExpiry || "";
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.processing) {
      this.props.onClose();
    }
  }

  onAddPaymentMethod() {
    this.props.onClose();
    router.navigate("settings/payment-methods/add");
  }

  async onConfirm() {
    if (this.state.processing || !this.hasCard) return;
    this.state.processing = true;
    this.state.error = null;
    this.state.pending = false;

    const result = await billingService.topup(this.plan.id);
    if (result.success && result.data && result.data.redirect_url) {
      window.location.href = result.data.redirect_url;
      return;  // Browser is leaving; no further UI work
    }
    const topupId = result.data?.topup_id;
    const state = result.data?.state;
    // Success is shown ONLY for a top-up that is actually `done`. A pending
    // top-up (still waiting for ECPay, or a previous one still in flight) is
    // polled; anything else is an error.
    if (topupId && state === "pending") {
      await this._waitForResult(topupId);
      return;
    }
    if (!result.success || state !== "done" || !topupId) {
      this.state.error = translateTopupError(result.error, result.code);
      this.state.processing = false;
      return;
    }
    await this._finishWithStatus(topupId);
  }

  async _finishWithStatus(topupId) {
    const status = await billingService.getTopupStatus(topupId);
    if (!status?.success || status.data.state !== "done") {
      this.state.error = translateTopupError(
        status?.data?.failure_message, status?.data?.failure_code || status?.code,
      );
      this.state.processing = false;
      return;
    }
    await billingService.fetchWallet();
    this.props.onSuccess({
      topup_id: status.data.topup_id,
      credits_added: status.data.credits,
      bonus: status.data.bonus,
      new_balance: status.data.new_balance,
    });
  }

  async _waitForResult(topupId) {
    this.state.pending = true;
    const final = await billingService.pollTopupStatus(topupId);
    this.state.pending = false;
    if (final?.state === "done") {
      await this._finishWithStatus(topupId);
      return;
    }
    if (final?.state === "failed" || final?.state === "cancelled") {
      this.state.error = translateTopupError(final.failure_message, final.failure_code);
    } else {
      this.state.error = translateTopupError(null, "topup_pending");
    }
    this.state.processing = false;
  }
}
