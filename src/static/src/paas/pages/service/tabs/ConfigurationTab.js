/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { WoowButton } from "../../../components/button/WoowButton";
import { HelmValueForm } from "../../../components/config/HelmValueForm";
import { cloudService } from "../../../services/cloud_service";

/**
 * ConfigurationTab Component
 *
 * Displays and allows editing of Helm values for a cloud service.
 * Uses HelmValueForm for structured editing based on template specs.
 *
 * Props:
 *   - service (Object): Service data object with helm_values and template.helm_value_specs
 *   - workspaceId (number): Workspace ID for API calls
 *   - onSave (Function): Callback when configuration is saved
 */
export class ConfigurationTab extends Component {
    static template = "woow_paas_platform.ConfigurationTab";
    static components = { WoowCard, WoowIcon, WoowButton, HelmValueForm };
    static props = {
        service: { type: Object },
        workspaceId: { type: Number },
        onSave: { type: Function, optional: true },
    };

    setup() {
        this.state = useState({
            editing: false,
            saving: false,
            error: null,
            editedValues: {},
            showAdvanced: false,
            errors: {},
        });
    }

    get currentValues() {
        return this.props.service.helm_values || {};
    }

    get helmValueSpecs() {
        return this.props.service.template?.helm_value_specs || { required: [], optional: [] };
    }

    get allSpecs() {
        return [...(this.helmValueSpecs.required || []), ...(this.helmValueSpecs.optional || [])];
    }

    get hasOptionalSettings() {
        return (this.helmValueSpecs.optional || []).length > 0;
    }

    get canEdit() {
        const disabledStates = ["deploying", "upgrading", "deleting", "pending"];
        return !disabledStates.includes(this.props.service.state);
    }

    getSpecValue(spec) {
        const values = this.currentValues;
        const value = values[spec.key];
        if (value !== undefined) return value;
        return spec.default !== undefined ? spec.default : "";
    }

    formatSpecValue(spec) {
        const value = this.getSpecValue(spec);
        if (spec.type === "password") return "********";
        if (spec.type === "boolean") return value ? "Enabled" : "Disabled";
        if (value === null || value === undefined) return "-";
        return String(value);
    }

    startEditing() {
        const values = {};
        for (const spec of this.allSpecs) {
            values[spec.key] = this.getSpecValue(spec);
        }
        this.state.editedValues = values;
        this.state.editing = true;
        this.state.error = null;
        this.state.errors = {};
    }

    cancelEditing() {
        this.state.editing = false;
        this.state.editedValues = {};
        this.state.error = null;
        this.state.errors = {};
        this.state.showAdvanced = false;
    }

    onHelmValueUpdate(key, value) {
        this.state.editedValues = { ...this.state.editedValues, [key]: value };
        if (this.state.errors[key]) {
            const newErrors = { ...this.state.errors };
            delete newErrors[key];
            this.state.errors = newErrors;
        }
    }

    toggleAdvanced() {
        this.state.showAdvanced = !this.state.showAdvanced;
    }

    validateForm() {
        const errors = {};
        for (const spec of (this.helmValueSpecs.required || [])) {
            const value = this.state.editedValues[spec.key];
            if (value === undefined || value === null || value === "") {
                errors[spec.key] = `${spec.label || spec.key} is required`;
            }
        }
        this.state.errors = errors;
        return Object.keys(errors).length === 0;
    }

    async saveChanges() {
        if (!this.validateForm()) return;

        this.state.saving = true;
        this.state.error = null;

        const result = await cloudService.updateService(
            this.props.workspaceId,
            this.props.service.id,
            this.state.editedValues
        );

        if (result.success) {
            this.state.editing = false;
            this.state.editedValues = {};
            this.state.showAdvanced = false;
            if (this.props.onSave) {
                this.props.onSave(result.data);
            }
        } else {
            this.state.error = result.error || "Failed to update configuration";
        }

        this.state.saving = false;
    }
}
