/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";

/**
 * ManagePaymentMethodsModal — quick-access modal from BillingOverview.
 * Lists bound cards, supports delete. Adding a card routes to the full page.
 */
export class ManagePaymentMethodsModal extends Component {
    static template = "woow_paas_platform.ManagePaymentMethodsModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        onClose: { type: Function },
        onUpdated: { type: Function },
    };

    setup() {
        this.router = router;
        this.billing = useState(billingService);
        this.state = useState({
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
        if (ev.target === ev.currentTarget && !this.state.deleting) {
            this.props.onClose();
        }
    }

    onAddCard() {
        this.props.onClose();
        this.router.navigate("settings/payment-methods/add");
    }

    async onDeleteMethod(methodId) {
        if (this.state.deleting) return;
        if (!confirm(_t("Are you sure you want to remove this payment method?"))) return;

        this.state.deleting = true;
        this.state.deleteError = null;

        const result = await billingService.deletePaymentMethod(methodId);

        if (result.success) {
            this.props.onUpdated();
        } else {
            this.state.deleteError = result.error || _t("Failed to remove card");
        }

        this.state.deleting = false;
    }
}
