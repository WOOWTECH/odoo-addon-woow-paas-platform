/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { AddCardModal } from "./AddCardModal";
import { CardAddedSuccessModal } from "./CardAddedSuccessModal";
import { billingService } from "../../services/billing_service";

/**
 * ManagePaymentMethodsModal Component
 *
 * Displays bound payment methods and allows adding/deleting cards.
 *
 * Props:
 *   - onClose (Function): Called when modal is closed
 *   - onUpdated (Function): Called when payment methods are changed
 */
export class ManagePaymentMethodsModal extends Component {
  static template = "woow_paas_platform.ManagePaymentMethodsModal";
  static components = { WoowButton, WoowIcon, AddCardModal, CardAddedSuccessModal };
  static props = {
    onClose: { type: Function },
    onUpdated: { type: Function },
  };

  setup() {
    this.billing = useState(billingService);
    this.state = useState({
      showAddCardModal: false,
      showCardAddedSuccess: null, // null or { last4, type }
      deleting: false,
      deleteError: null,
    });

    onMounted(() => {
      billingService.fetchPaymentMethods();
    });
  }

  get methods() {
    return this.billing.paymentMethods;
  }

  get defaultMethod() {
    return this.methods.find((m) => m.is_default) || null;
  }

  get otherMethods() {
    return this.methods.filter((m) => !m.is_default);
  }

  get isLoading() {
    return this.billing.paymentMethodsLoading;
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.deleting && !this.state.showAddCardModal) {
      this.props.onClose();
    }
  }

  onAddCard() {
    this.state.showAddCardModal = true;
  }

  onAddCardClose() {
    this.state.showAddCardModal = false;
  }

  onCardAdded(cardInfo) {
    this.state.showAddCardModal = false;
    this.state.showCardAddedSuccess = cardInfo;
    billingService.fetchPaymentMethods();
    this.props.onUpdated();
  }

  onCardAddedSuccessClose() {
    this.state.showCardAddedSuccess = null;
  }

  async onDeleteMethod(methodId) {
    if (this.state.deleting) return;

    // Simple confirm
    if (!confirm("Are you sure you want to remove this payment method?")) return;

    this.state.deleting = true;
    this.state.deleteError = null;

    const result = await billingService.deletePaymentMethod(methodId);

    if (result.success) {
      await billingService.fetchWallet();
      this.props.onUpdated();
    } else {
      this.state.deleteError = result.error || "Failed to remove card";
    }

    this.state.deleting = false;
  }
}
