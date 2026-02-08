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

    formatKey(key) {
        // Convert camelCase or snake_case to Title Case
        return key
            .replace(/([A-Z])/g, " $1")
            .replace(/_/g, " ")
            .replace(/^./, (str) => str.toUpperCase())
            .trim();
    }

    isObject(value) {
        return typeof value === "object" && value !== null && !Array.isArray(value);
    }

    isArray(value) {
        return Array.isArray(value);
    }

    formatValue(value) {
        if (value === null || value === undefined) return "null";
        if (typeof value === "boolean") return value ? "true" : "false";
        if (typeof value === "number") return String(value);
        if (typeof value === "string") return value;
        return JSON.stringify(value);
    }

    get flattenedValues() {
        // Flatten nested objects for display
        const result = [];
        const flatten = (obj, prefix = "") => {
            for (const [key, value] of Object.entries(obj)) {
                const fullKey = prefix ? `${prefix}.${key}` : key;
                if (this.isObject(value) && Object.keys(value).length > 0) {
                    flatten(value, fullKey);
                } else {
                    result.push({
                        key: fullKey,
                        label: this.formatKey(key),
                        value: this.formatValue(value),
                        isSecret: key.toLowerCase().includes("password") ||
                                  key.toLowerCase().includes("secret") ||
                                  key.toLowerCase().includes("key"),
                    });
                }
            }
        };
        flatten(this.currentValues);
        return result;
    }
}
