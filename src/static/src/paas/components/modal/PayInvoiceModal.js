/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { formatPoints } from "../../utils/billing_helpers";

/**
 * PayInvoiceModal Component
 *
 * Confirmation modal for paying an invoice using wallet balance.
 *
 * Props:
 *   - invoice (Object): Invoice data { id, name/number, amount_total }
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onPaid (Function): Called after successful payment
 */
export class PayInvoiceModal extends Component {
  static template = "woow_paas_platform.PayInvoiceModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    invoice: { type: Object },
    onClose: { type: Function },
    onPaid: { type: Function },
  };

  setup() {
    this.billing = useState(billingService);
    this.state = useState({
      paying: false,
      error: null,
    });

    onMounted(() => {
      // Ensure wallet balance is fresh
      billingService.fetchWallet();
    });
  }

  get invoiceName() {
    return this.props.invoice.name || this.props.invoice.number || "-";
  }

  get amount() {
    return this.props.invoice.amount_total ?? 0;
  }

  get formattedAmount() {
    return formatPoints(this.amount);
  }

  get walletBalance() {
    return this.billing.wallet.balance ?? 0;
  }

  get formattedBalance() {
    return formatPoints(this.walletBalance);
  }

  get hasSufficientBalance() {
    return this.walletBalance >= this.amount;
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.paying) {
      this.props.onClose();
    }
  }

  async onConfirm() {
    if (this.state.paying || !this.hasSufficientBalance) return;

    this.state.paying = true;
    this.state.error = null;

    const result = await billingService.payInvoice(this.props.invoice.id);

    if (result.success) {
      // Refresh wallet balance after payment
      await billingService.fetchWallet();
      this.props.onPaid();
    } else {
      this.state.error = result.error || _t("Payment failed");
      this.state.paying = false;
    }
  }
}
