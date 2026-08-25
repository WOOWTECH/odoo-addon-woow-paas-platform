/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";

/**
 * CancelSubscriptionModal Component
 *
 * Confirmation modal for cancelling a subscription.
 *
 * Props:
 *   - subscription (Object): Subscription data { id, name, plan_name }
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onCancelled (Function): Called after successful cancellation
 */
export class CancelSubscriptionModal extends Component {
  static template = "woow_paas_platform.CancelSubscriptionModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    subscription: { type: Object },
    onClose: { type: Function },
    onCancelled: { type: Function },
  };

  setup() {
    this.state = useState({
      reason: "",
      cancelling: false,
      error: null,
    });
  }

  get planName() {
    return this.props.subscription.plan_name || this.props.subscription.name || _t("this subscription");
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.cancelling) {
      this.props.onClose();
    }
  }

  onReasonInput(ev) {
    this.state.reason = ev.target.value;
  }

  async onConfirm() {
    if (this.state.cancelling) return;

    this.state.cancelling = true;
    this.state.error = null;

    const result = await billingService.cancelSubscription(
      this.props.subscription.id,
      this.state.reason,
    );

    if (result.success) {
      this.props.onCancelled();
    } else {
      this.state.error = result.error || _t("Cancellation failed");
      this.state.cancelling = false;
    }
  }
}
