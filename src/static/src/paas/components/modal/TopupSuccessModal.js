/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { formatPoints } from "../../utils/billing_helpers";
import { formatFloat } from "@web/core/utils/numbers";

/**
 * TopupSuccessModal Component
 *
 * Displays a success confirmation after a wallet top-up.
 *
 * Props:
 *   - data (Object): { credits_added, bonus, new_balance }
 *   - onClose (Function): Called when Done is clicked
 */
export class TopupSuccessModal extends Component {
  static template = "woow_paas_platform.TopupSuccessModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    data: { type: Object },
    onClose: { type: Function },
  };

  get creditsAdded() {
    return this.props.data.credits_added ?? 0;
  }

  get bonus() {
    return this.props.data.bonus ?? 0;
  }

  get totalAdded() {
    return this.creditsAdded + this.bonus;
  }

  get newBalance() {
    return this.props.data.new_balance ?? 0;
  }

  get formattedCredits() {
    return formatFloat(this.creditsAdded, { digits: [16, 0] });
  }

  get formattedTotal() {
    return formatFloat(this.totalAdded, { digits: [16, 0] });
  }

  get formattedBalance() {
    return formatPoints(this.newBalance);
  }

  get hasBonus() {
    return this.bonus > 0;
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget) {
      this.onDone();
    }
  }

  async onDone() {
    await billingService.fetchWallet();
    this.props.onClose();
  }
}
