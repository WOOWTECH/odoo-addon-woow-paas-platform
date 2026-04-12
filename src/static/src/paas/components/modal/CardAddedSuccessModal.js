/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";

/**
 * CardAddedSuccessModal Component
 *
 * Displays a success confirmation after a credit card has been added.
 *
 * Props:
 *   - cardInfo (Object): { last4, type }
 *   - onClose (Function): Called when Done is clicked
 */
export class CardAddedSuccessModal extends Component {
  static template = "woow_paas_platform.CardAddedSuccessModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    cardInfo: { type: Object },
    onClose: { type: Function },
  };

  get cardTypeLabel() {
    const types = {
      visa: "Visa",
      mastercard: "Mastercard",
      amex: "Amex",
    };
    return types[this.props.cardInfo.type] || "Card";
  }

  get last4() {
    return this.props.cardInfo.last4 || "****";
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget) {
      this.props.onClose();
    }
  }
}
