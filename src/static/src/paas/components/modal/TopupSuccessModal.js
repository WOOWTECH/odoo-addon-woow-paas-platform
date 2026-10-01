/** @odoo-module **/
import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { formatPoints } from "../../utils/billing_helpers";

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

  // #1310：原本訊息混用 "credits"／"bonus points"／"pts" 三種說法、且句子被
  // <strong> 切成三段 msgid（無法依中文語序翻譯）⇒ 整句在 JS 組好，點數一律走 formatPoints。
  get successMessage() {
    return _t("Your pre-payment has been processed, and %(points)s have been added to your account.", {
      points: formatPoints(this.totalAdded, { precision: 0 }),
    });
  }

  get bonusMessage() {
    return _t("Includes a %(points)s bonus — enjoy!", {
      points: formatPoints(this.bonus, { precision: 0 }),
    });
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
