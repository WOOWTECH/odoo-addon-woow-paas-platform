/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { WoowButton } from "../../../components/button/WoowButton";
import { cloudService } from "../../../services/cloud_service";

/**
 * ConfigurationTab Component
 *
 * Displays and allows editing of Helm values for a cloud service.
 * Features:
 * - Display current Helm values in a read-only view
 * - Edit mode for modifying values
 * - Save button to trigger upgrade API
 * - Shows upgrade progress
 *
 * Props:
 *   - service (Object): Service data object with helm_values
 *   - workspaceId (number): Workspace ID for API calls
 *   - onSave (Function): Callback when configuration is saved
 */
export class ConfigurationTab extends Component {
    static template = "woow_paas_platform.ConfigurationTab";
    static components = { WoowCard, WoowIcon, WoowButton };
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
            editedValues: null,
        });
    }

    get currentValues() {
        return this.props.service.helm_values || {};
    }

    get valuesJson() {
        return JSON.stringify(this.currentValues, null, 2);
    }

    get editedValuesJson() {
        if (this.state.editedValues === null) {
            return this.valuesJson;
        }
        return this.state.editedValues;
    }

    get hasChanges() {
        if (this.state.editedValues === null) return false;
        return this.state.editedValues !== this.valuesJson;
    }

    get canEdit() {
        // Disable editing when service is in transitional state
        const disabledStates = ["deploying", "upgrading", "deleting", "pending"];
        return !disabledStates.includes(this.props.service.state);
    }

    startEditing() {
        this.state.editing = true;
        this.state.editedValues = this.valuesJson;
        this.state.error = null;
    }

    cancelEditing() {
        this.state.editing = false;
        this.state.editedValues = null;
        this.state.error = null;
    }

    onValuesChange(ev) {
        this.state.editedValues = ev.target.value;
        this.state.error = null;
    }

    async saveChanges() {
        // Validate JSON
        let parsedValues;
        try {
            parsedValues = JSON.parse(this.state.editedValues);
        } catch (e) {
            this.state.error = "Invalid JSON format. Please check your configuration.";
            return;
        }

        this.state.saving = true;
        this.state.error = null;

        // Call API to update service
        const result = await cloudService.updateService(
            this.props.workspaceId,
            this.props.service.id,
            parsedValues
        );

        if (result.success) {
            this.state.editing = false;
            this.state.editedValues = null;
            if (this.props.onSave) {
                this.props.onSave(parsedValues);
            }
        } else {
            this.state.error = result.error || "Failed to update configuration";
        }

        this.state.saving = false;
    }

    get helmValueSpecs() {
        return this.props.service.template?.helm_value_specs || { required: [], optional: [] };
    }

    get allSpecs() {
        return [...(this.helmValueSpecs.required || []), ...(this.helmValueSpecs.optional || [])];
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
}
