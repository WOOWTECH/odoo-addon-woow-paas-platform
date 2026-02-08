/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { HelmValueForm } from "../../components/config/HelmValueForm";
import { cloudService } from "../../services/cloud_service";
import { router } from "../../core/router";
import { generateUUID, md5 } from "../../utils/hash";

/**
 * AppConfigurationPage Component
 * Configuration page for launching a new cloud service from a template
 */
export class AppConfigurationPage extends Component {
    static template = "woow_paas_platform.AppConfigurationPage";
    static components = { WoowCard, WoowIcon, WoowButton, HelmValueForm };
    static props = {
        workspaceId: { type: Number },
        templateId: { type: Number },
    };

    setup() {
        this.state = useState({
            template: null,
            loading: true,
            error: null,
            // Form state
            name: '',
            subdomain: '',
            referenceId: generateUUID(),
            helmValues: {},
            showAdvanced: false,
            // Validation errors
            errors: {},
            // Launch state
            launching: false,
            launchError: null,
        });
        this.router = useState(router);

        onMounted(() => {
            this.loadTemplate();
        });
    }

    /**
     * Load template details from API
     */
    async loadTemplate() {
        this.state.loading = true;
        this.state.error = null;

        const result = await cloudService.getTemplate(this.props.templateId);

        if (result.success) {
            this.state.template = result.data;
            // Initialize helm values with defaults
            this.initializeHelmValues(result.data);
        } else {
            this.state.error = result.error || "Failed to load template";
        }

        this.state.loading = false;
    }

    /**
     * Initialize helm values from template defaults
     * @param {Object} template - The template data
     */
    initializeHelmValues(template) {
        const helmValues = {};
        const specs = template.helm_value_specs || {};
        const defaults = template.helm_default_values || {};

        // Set defaults from helm_default_values
        Object.assign(helmValues, defaults);

        // Override with spec defaults if specified
        if (specs.required) {
            for (const spec of specs.required) {
                if (spec.default !== undefined && helmValues[spec.key] === undefined) {
                    helmValues[spec.key] = spec.default;
                }
            }
        }
        if (specs.optional) {
            for (const spec of specs.optional) {
                if (spec.default !== undefined && helmValues[spec.key] === undefined) {
                    helmValues[spec.key] = spec.default;
                }
            }
        }

        this.state.helmValues = helmValues;
    }

    get template() {
        return this.state.template;
    }

    get helmValueSpecs() {
        return this.template?.helm_value_specs || { required: [], optional: [] };
    }

    get hasOptionalSettings() {
        return (this.helmValueSpecs.optional || []).length > 0;
    }

    /**
     * Get category icon name
     * @returns {string}
     */
    get categoryIcon() {
        const iconMap = {
            database: 'database',
            cache: 'memory',
            messaging: 'message',
            monitoring: 'monitoring',
            storage: 'storage',
            compute: 'developer_board',
            web: 'language',
            other: 'apps',
        };
        return iconMap[this.template?.category] || 'apps';
    }

    /**
     * Get subdomain suffix
     * @returns {string}
     */
    get subdomainSuffix() {
        return `.${cloudService.domain}`;
    }

    /**
     * Handle service name input change
     * @param {Event} event
     */
    onNameChange(event) {
        const value = event.target.value;
        this.state.name = value;
        this.clearError('name');

        // Auto-generate subdomain: paas-{ws_id}-{hash(name)[:8]}
        this.state.subdomain = this.generateSubdomain(value);
    }

    /**
     * Generate subdomain from service name with salt
     * Format: paas-{workspace_id}-{hash(referenceId + name)[:8]}
     * Uses referenceId as salt to prevent subdomain guessing
     * @param {string} name - Service name
     * @returns {string}
     */
    generateSubdomain(name) {
        if (!name || !name.trim()) {
            return '';
        }
        // Use referenceId as salt to prevent subdomain guessing
        const saltedInput = this.state.referenceId + name.trim();
        const nameHash = md5(saltedInput).substring(0, 8);
        return `paas-${this.props.workspaceId}-${nameHash}`;
    }

    /**
     * Handle helm value update from HelmValueForm
     * @param {string} key - The helm value key
     * @param {*} value - The new value
     */
    onHelmValueUpdate(key, value) {
        this.state.helmValues = {
            ...this.state.helmValues,
            [key]: value,
        };
        this.clearError(key);
    }

    /**
     * Toggle advanced settings visibility
     */
    toggleAdvanced() {
        this.state.showAdvanced = !this.state.showAdvanced;
    }

    /**
     * Clear an error for a specific field
     * @param {string} field - The field name
     */
    clearError(field) {
        if (this.state.errors[field]) {
            const newErrors = { ...this.state.errors };
            delete newErrors[field];
            this.state.errors = newErrors;
        }
    }

    /**
     * Validate the entire form
     * @returns {boolean} True if valid
     */
    validateForm() {
        const errors = {};

        // Validate name
        const name = this.state.name.trim();
        if (!name) {
            errors.name = 'Service name is required';
        } else if (name.length < 2) {
            errors.name = 'Service name must be at least 2 characters';
        } else if (name.length > 100) {
            errors.name = 'Service name must be at most 100 characters';
        }

        // Subdomain is auto-generated, no validation needed

        // Validate required helm values
        const requiredSpecs = this.helmValueSpecs.required || [];
        for (const spec of requiredSpecs) {
            const value = this.state.helmValues[spec.key];
            if (spec.required !== false) { // Required by default in required section
                if (value === undefined || value === null || value === '') {
                    errors[spec.key] = `${spec.label} is required`;
                }
            }
        }

        this.state.errors = errors;
        return Object.keys(errors).length === 0;
    }

    /**
     * Handle cancel button click
     */
    onCancel() {
        this.router.navigate(`workspace/${this.props.workspaceId}/marketplace`);
    }

    /**
     * Handle launch button click
     */
    async onLaunch() {
        if (!this.validateForm()) {
            return;
        }

        this.state.launching = true;
        this.state.launchError = null;

        try {
            const result = await cloudService.createService(this.props.workspaceId, {
                template_id: this.props.templateId,
                name: this.state.name.trim(),
                reference_id: this.state.referenceId,
                values: this.state.helmValues,
            });

            if (result.success) {
                // Navigate to service detail page
                const serviceId = result.data.id;
                this.router.navigate(`workspace/${this.props.workspaceId}/services/${serviceId}`);
            } else {
                this.state.launchError = result.error || 'Failed to create service';
            }
        } catch (error) {
            this.state.launchError = error.message || 'An unexpected error occurred';
        } finally {
            this.state.launching = false;
        }
    }

    /**
     * Navigate back to workspaces
     */
    goBack() {
        this.router.navigate(`workspace/${this.props.workspaceId}/marketplace`);
    }
}
