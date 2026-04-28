/** @odoo-module **/
import { Component, onMounted, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { cloudService } from "../../services/cloud_service";

/**
 * SkipTrialModal Component
 *
 * Confirmation modal for skipping trial period and starting paid billing.
 * On mount, calls `cloudService.getSkipTrialEstimate` to fetch the
 * upcoming-invoice breakdown (untaxed / tax / total) + wallet sufficiency
 * info, then renders the breakdown and gates the confirm button on
 * `state.estimate?.sufficient`.
 *
 * Props:
 *   - service (Object): Service data { id, name, subscription }
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onSkipped (Function): Called after successful skip
 */
export class SkipTrialModal extends Component {
    static template = "woow_paas_platform.SkipTrialModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        service: { type: Object },
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
        const result = await cloudService.getSkipTrialEstimate(this.props.service.id);
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

        const result = await cloudService.skipTrial(this.props.service.id);

        if (result.success) {
            this.props.onSkipped();
        } else {
            this.state.error = result.error || "Failed to skip trial";
            this.state.skipping = false;
        }
    }
}
