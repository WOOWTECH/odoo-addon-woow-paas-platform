/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { cloudService } from "../../services/cloud_service";

/**
 * SkipTrialModal Component
 *
 * Confirmation modal for skipping trial period and starting paid billing.
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
        });
    }

    get planPrice() {
        return this.props.service.subscription?.plan_price || 0;
    }

    get trialEndDate() {
        return this.props.service.subscription?.trial_end_date || "";
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget && !this.state.skipping) {
            this.props.onClose();
        }
    }

    async onConfirm() {
        if (this.state.skipping) return;

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
