/** @odoo-module **/
import { Component, onMounted, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { formatPoints } from "../../utils/billing_helpers";

/**
 * SkipTrialModal Component
 *
 * Confirmation modal for skipping a subscription's trial period and
 * starting paid billing immediately. The modal is service-type-agnostic:
 * the caller supplies `loadEstimate` / `confirmSkip` callbacks pointing at
 * whichever backend the parent page is for (Cloud Service, Smart Home, …),
 * so the same UI handles every service type without duplication.
 *
 * Props:
 *   - service (Object): { id, name, subscription } — display data only.
 *     The modal never derives the request target from `service.id`; the
 *     callbacks already know what to call. `service` exists purely for
 *     name/trial-end-date rendering.
 *   - loadEstimate (Function): () => Promise<ApiResponse<EstimateData>>.
 *     Called on mount to populate the breakdown. Should be side-effect-free.
 *   - confirmSkip (Function): () => Promise<ApiResponse<Object>>.
 *     Called when the user confirms; mutates the subscription.
 *   - onClose (Function): Called when modal is closed/cancelled.
 *   - onSkipped (Function): Called after a successful confirmSkip.
 */
export class SkipTrialModal extends Component {
    static template = "woow_paas_platform.SkipTrialModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        service: { type: Object },
        loadEstimate: { type: Function },
        confirmSkip: { type: Function },
        onClose: { type: Function },
        onSkipped: { type: Function },
    };

    setup() {
        this.state = useState({
            skipping: false,
            error: null,
            loadingEstimate: true,
            estimateError: null,
            estimate: null,
        });
        onMounted(() => this.loadEstimate());
    }

    async loadEstimate() {
        this.state.loadingEstimate = true;
        this.state.estimateError = null;
        const result = await this.props.loadEstimate();
        if (result.success) {
            this.state.estimate = result.data;
        } else {
            this.state.estimateError = result.error || "Failed to load invoice breakdown";
        }
        this.state.loadingEstimate = false;
    }

    get planPrice() {
        return this.props.service.subscription?.plan_price || 0;
    }

    formatPoints(amount) {
        return formatPoints(amount);
    }

    get trialEndDate() {
        return this.props.service.subscription?.trial_end_date || "";
    }

    get canConfirm() {
        return (
            !this.state.loadingEstimate &&
            !this.state.estimateError &&
            !!this.state.estimate &&
            this.state.estimate.sufficient &&
            !this.state.skipping
        );
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget && !this.state.skipping) {
            this.props.onClose();
        }
    }

    async onConfirm() {
        if (!this.canConfirm) return;

        this.state.skipping = true;
        this.state.error = null;

        const result = await this.props.confirmSkip();

        if (result.success) {
            this.props.onSkipped();
        } else {
            this.state.error = result.error || "Failed to skip trial";
            this.state.skipping = false;
        }
    }
}
