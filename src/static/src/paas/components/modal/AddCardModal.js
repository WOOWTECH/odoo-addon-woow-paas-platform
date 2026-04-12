/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";

/**
 * AddCardModal Component
 *
 * Modal form for adding/binding a credit card.
 * Currently ECPay integration is stubbed, so save will return an error,
 * but the UI form is fully functional.
 *
 * Props:
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onAdded (Function): Called after successful card addition with card info
 */
export class AddCardModal extends Component {
  static template = "woow_paas_platform.AddCardModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    onClose: { type: Function },
    onAdded: { type: Function },
  };

  setup() {
    this.state = useState({
      cardNumber: "",
      expiry: "",
      cvc: "",
      cardholderName: "",
      setDefault: true,
      saving: false,
      error: null,
    });
  }

  get isFormValid() {
    return (
      this.state.cardNumber.replace(/\s/g, "").length >= 13 &&
      this.state.expiry.length >= 4 &&
      this.state.cvc.length >= 3 &&
      this.state.cardholderName.trim().length > 0
    );
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.saving) {
      this.props.onClose();
    }
  }

  onCardNumberInput(ev) {
    // Format card number with spaces every 4 digits
    let value = ev.target.value.replace(/\D/g, "").substring(0, 16);
    value = value.replace(/(\d{4})(?=\d)/g, "$1 ");
    this.state.cardNumber = value;
    ev.target.value = value;
  }

  onExpiryInput(ev) {
    // Format as MM/YY
    let value = ev.target.value.replace(/\D/g, "").substring(0, 4);
    if (value.length >= 3) {
      value = value.substring(0, 2) + "/" + value.substring(2);
    }
    this.state.expiry = value;
    ev.target.value = value;
  }

  onCvcInput(ev) {
    const value = ev.target.value.replace(/\D/g, "").substring(0, 4);
    this.state.cvc = value;
    ev.target.value = value;
  }

  onCardholderInput(ev) {
    this.state.cardholderName = ev.target.value;
  }

  onSetDefaultChange(ev) {
    this.state.setDefault = ev.target.checked;
  }

  async onSave() {
    if (this.state.saving || !this.isFormValid) return;

    this.state.saving = true;
    this.state.error = null;

    const result = await billingService.addPaymentMethod({
      card_number: this.state.cardNumber.replace(/\s/g, ""),
      expiry: this.state.expiry,
      cvc: this.state.cvc,
      cardholder_name: this.state.cardholderName,
    });

    if (result.success) {
      this.props.onAdded({
        last4: this.state.cardNumber.replace(/\s/g, "").slice(-4),
        type: "visa",
      });
    } else {
      this.state.error = result.error || "Failed to add card";
      this.state.saving = false;
    }
  }
}
