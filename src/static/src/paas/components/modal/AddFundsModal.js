/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";

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
    return (this.plan.amount ?? 0).toLocaleString();
  }

  get formattedCredits() {
    return (this.plan.credits ?? 0).toLocaleString();
  }

  get totalCredits() {
    return (this.plan.credits ?? 0) + (this.plan.bonus ?? 0);
  }

  get formattedTotalCredits() {
    return this.totalCredits.toLocaleString();
  }

  get hasCard() {
    return this.billing.wallet.cardBound;
  }

  get cardDisplay() {
    if (!this.hasCard) return "";
    return `Visa \u2022\u2022\u2022\u2022 ${this.billing.wallet.cardLast4}`;
  }

  get cardExpiry() {
    return this.billing.wallet.cardExpiry || "";
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.processing) {
      this.props.onClose();
    }
  }

  async onConfirm() {
    if (this.state.processing || !this.hasCard) return;

    this.state.processing = true;
    this.state.error = null;

    const result = await billingService.topup(this.plan.id);

    if (result.success) {
      await billingService.fetchWallet();
      this.props.onSuccess(result.data);
    } else {
      this.state.error = result.error || "Top-up failed";
      this.state.processing = false;
    }
  }
}
