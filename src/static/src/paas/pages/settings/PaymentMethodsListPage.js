/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowButton } from "../../components/button/WoowButton";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";

/**
 * PaymentMethodsListPage — lists ECPG-bound cards, supports delete & set default.
 * On mount, reads URL query for post-3DS status (`bind_status=ok`) to surface a toast.
 */
export class PaymentMethodsListPage extends Component {
    static template = "woow_paas_platform.PaymentMethodsListPage";
    static components = { WoowCard, WoowButton, WoowIcon };
    static props = {};

    setup() {
        this.router = router;
        this.billing = useState(billingService);
        this.state = useState({
            bindSuccess: false,
            deleting: null,       // token id being deleted
            settingDefault: null, // token id being set default
            error: null,
        });

        onMounted(async () => {
            // Detect post-3DS redirect via router query params
            const q = this.router.params.query || {};
            if (q.bind_status === "ok") {
                this.state.bindSuccess = true;
                // Clean the query so reload doesn't retrigger.
                // Hash path without query: "#/settings/payment-methods"
                const cleanHash = window.location.hash.split("?")[0];
                window.history.replaceState(null, "", cleanHash);
            }
            await billingService.fetchPaymentMethods();
        });
    }

    get cards() {
        return this.billing.paymentMethods;
    }

    get isLoading() {
        return this.billing.paymentMethodsLoading;
    }

    goToAdd() {
        this.router.navigate("settings/payment-methods/add");
    }

    async onDelete(tokenId) {
        if (!confirm("Remove this card?")) return;
        this.state.deleting = tokenId;
        this.state.error = null;
        const result = await billingService.deletePaymentMethod(tokenId);
        if (!result.success) {
            this.state.error = result.error || "Delete failed";
        }
        this.state.deleting = null;
    }

    async onSetDefault(tokenId) {
        this.state.settingDefault = tokenId;
        this.state.error = null;
        const result = await billingService.setDefaultPaymentMethod(tokenId);
        if (result.success) {
            await billingService.fetchPaymentMethods();
        } else {
            this.state.error = result.error || "Set default failed";
        }
        this.state.settingDefault = null;
    }

    dismissBindSuccess() {
        this.state.bindSuccess = false;
    }
}
