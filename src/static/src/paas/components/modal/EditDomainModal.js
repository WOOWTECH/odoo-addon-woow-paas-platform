/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { cloudService } from "../../services/cloud_service";

/**
 * EditDomainModal Component
 *
 * Allows users to configure a custom domain for their service.
 * Shows CNAME setup instructions for DNS configuration.
 *
 * Props:
 *   - service (Object): Service data { id, workspace_id, subdomain, custom_domain }
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onSaved (Function): Called after successful save
 */
export class EditDomainModal extends Component {
    static template = "woow_paas_platform.EditDomainModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        service: { type: Object },
        onClose: { type: Function },
        onSaved: { type: Function },
    };

    setup() {
        this.state = useState({
            customDomain: this.props.service.custom_domain || "",
            saving: false,
            error: null,
            validationError: null,
        });
    }

    get subdomain() {
        return this.props.service.subdomain || "";
    }

    get fullSubdomain() {
        return this.subdomain ? `${this.subdomain}.woowtech.com` : "";
    }

    get hasChanges() {
        const original = this.props.service.custom_domain || "";
        return this.state.customDomain.trim() !== original;
    }

    get canSave() {
        return this.hasChanges && !this.state.validationError && !this.state.saving;
    }

    validateDomain(domain) {
        if (!domain) {
            // Empty is valid - removes custom domain
            return null;
        }

        // Domain validation pattern
        const pattern = /^([a-z0-9]+(-[a-z0-9]+)*\.)+[a-z]{2,}$/i;
        if (!pattern.test(domain)) {
            return "Invalid domain format. Example: app.yourdomain.com";
        }

        // Check for common mistakes
        if (domain.startsWith("http://") || domain.startsWith("https://")) {
            return "Enter domain without http:// or https://";
        }

        if (domain.includes(" ")) {
            return "Domain cannot contain spaces";
        }

        return null;
    }

    onDomainInput(ev) {
        const domain = ev.target.value.toLowerCase().trim();
        this.state.customDomain = ev.target.value;
        this.state.validationError = this.validateDomain(domain);
        this.state.error = null;
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget && !this.state.saving) {
            this.props.onClose();
        }
    }

    async onSave() {
        const domain = this.state.customDomain.trim().toLowerCase();
        const validationError = this.validateDomain(domain);

        if (validationError) {
            this.state.validationError = validationError;
            return;
        }

        if (!this.hasChanges) {
            this.props.onClose();
            return;
        }

        this.state.saving = true;
        this.state.error = null;

        const result = await cloudService.updateService(
            this.props.service.workspace_id,
            this.props.service.id,
            { custom_domain: domain || null }
        );

        if (result.success) {
            this.props.onSaved();
        } else {
            this.state.error = result.error || "Failed to update domain";
            this.state.saving = false;
        }
    }

    clearDomain() {
        this.state.customDomain = "";
        this.state.validationError = null;
        this.state.error = null;
    }
}
