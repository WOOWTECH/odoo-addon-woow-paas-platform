/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { translateTopupError } from "../../services/topup_errors";
import { router } from "../../core/router";
import { formatFloat } from "@web/core/utils/numbers";

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

  get formattedTotalCredits() {
    return formatFloat(this.totalCredits, { digits: [16, 0] });
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

    const result = await billingService.topup(this.plan.id);
    if (!result.success) {
      this.state.error = translateTopupError(result.error, result.code);
      this.state.processing = false;
      return;
    }
    if (result.data && result.data.redirect_url) {
      window.location.href = result.data.redirect_url;
      return;  // Browser is leaving; no further UI work
    }
    // Synchronous done (non-3DS path). Fetch full topup status so the
    // success modal receives credits_added / bonus / new_balance.
    const topupId = result.data?.topup_id;
    let successPayload = result.data;
    if (topupId) {
      const status = await billingService.getTopupStatus(topupId);
      if (status?.success) {
        successPayload = {
          topup_id: status.data.topup_id,
          credits_added: status.data.credits,
          bonus: status.data.bonus,
          new_balance: status.data.new_balance,
        };
      }
    }
    await billingService.fetchWallet();
    this.props.onSuccess(successPayload);
  }
}
