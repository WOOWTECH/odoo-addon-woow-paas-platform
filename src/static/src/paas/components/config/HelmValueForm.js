/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowIcon } from "../icon/WoowIcon";

/**
 * HelmValueForm Component
 * Renders a dynamic form for Helm values configuration based on specs
 *
 * @example
 * <HelmValueForm
 *     specs="{ required: [...], optional: [...] }"
 *     values="state.helmValues"
 *     onUpdate.bind="onHelmValueUpdate"
 *     showAdvanced="state.showAdvanced"
 * />
 */
export class HelmValueForm extends Component {
    static template = "woow_paas_platform.HelmValueForm";
    static components = { WoowIcon };
    static props = {
        specs: { type: Object },         // { required: [...], optional: [...] }
        values: { type: Object },         // current values
        onUpdate: { type: Function },     // callback when value changes
        showAdvanced: { type: Boolean, optional: true },
        errors: { type: Object, optional: true }, // validation errors by key
    };

    setup() {
        this.state = useState({
            passwordVisibility: {}, // Track password visibility per field
        });
    }

    get requiredSpecs() {
        return this.props.specs?.required || [];
    }

    get optionalSpecs() {
        return this.props.specs?.optional || [];
    }

    get hasOptionalSpecs() {
        return this.optionalSpecs.length > 0;
    }

    /**
     * Get the current value for a spec key, using default if not set
     * @param {Object} spec - The spec object
     * @returns {*} The current value
     */
    getValue(spec) {
        const currentValue = this.props.values[spec.key];
        if (currentValue !== undefined) {
            return currentValue;
        }
        return spec.default !== undefined ? spec.default : '';
    }

    /**
     * Get the string value for display (handles boolean conversion)
     * @param {Object} spec - The spec object
     * @returns {string} The display value
     */
    getDisplayValue(spec) {
        const value = this.getValue(spec);
        if (spec.type === 'boolean') {
            return value ? 'true' : 'false';
        }
        return value === null || value === undefined ? '' : String(value);
    }

    /**
     * Check if a field has an error
     * @param {string} key - The field key
     * @returns {boolean}
     */
    hasError(key) {
        return this.props.errors && this.props.errors[key];
    }

    /**
     * Get error message for a field
     * @param {string} key - The field key
     * @returns {string|null}
     */
    getError(key) {
        return this.props.errors && this.props.errors[key];
    }

    /**
     * Handle input change for text, password, number fields
     * @param {Object} spec - The spec object
     * @param {Event} event - The input event
     */
    onInputChange(spec, event) {
        let value = event.target.value;

        // Convert value based on type
        if (spec.type === 'number') {
            value = value === '' ? '' : Number(value);
        }

        this.props.onUpdate(spec.key, value);
    }

    /**
     * Handle checkbox change for boolean fields
     * @param {Object} spec - The spec object
     * @param {Event} event - The change event
     */
    onCheckboxChange(spec, event) {
        this.props.onUpdate(spec.key, event.target.checked);
    }

    /**
     * Handle select change
     * @param {Object} spec - The spec object
     * @param {Event} event - The change event
     */
    onSelectChange(spec, event) {
        this.props.onUpdate(spec.key, event.target.value);
    }

    /**
     * Toggle password visibility for a specific field
     * @param {string} key - The field key
     */
    togglePasswordVisibility(key) {
        this.state.passwordVisibility[key] = !this.state.passwordVisibility[key];
    }

    /**
     * Check if password is visible for a field
     * @param {string} key - The field key
     * @returns {boolean}
     */
    isPasswordVisible(key) {
        return this.state.passwordVisibility[key] || false;
    }

    /**
     * Get input type for a spec
     * @param {Object} spec - The spec object
     * @returns {string}
     */
    getInputType(spec) {
        if (spec.type === 'password') {
            return this.isPasswordVisible(spec.key) ? 'text' : 'password';
        }
        if (spec.type === 'number') {
            return 'number';
        }
        return 'text';
    }

    /**
     * Check if spec is a text-like input (text, password, number)
     * @param {Object} spec - The spec object
     * @returns {boolean}
     */
    isTextInput(spec) {
        return ['text', 'password', 'number'].includes(spec.type);
    }

    /**
     * Check if spec is a password field
     * @param {Object} spec - The spec object
     * @returns {boolean}
     */
    isPasswordField(spec) {
        return spec.type === 'password';
    }

    /**
     * Check if spec is a boolean/checkbox field
     * @param {Object} spec - The spec object
     * @returns {boolean}
     */
    isBooleanField(spec) {
        return spec.type === 'boolean';
    }

    /**
     * Check if spec is a select field
     * @param {Object} spec - The spec object
     * @returns {boolean}
     */
    isSelectField(spec) {
        return spec.type === 'select';
    }

    /**
     * Get options for a select field
     * @param {Object} spec - The spec object
     * @returns {Array<{value: string, label: string}>}
     */
    getSelectOptions(spec) {
        if (!spec.options) return [];
        // Options can be an array of strings or objects with value/label
        return spec.options.map(opt => {
            if (typeof opt === 'string') {
                return { value: opt, label: opt };
            }
            return opt;
        });
    }

    /**
     * Check if a select option is selected
     * @param {Object} spec - The spec object
     * @param {string} optionValue - The option value
     * @returns {boolean}
     */
    isOptionSelected(spec, optionValue) {
        return this.getValue(spec) === optionValue;
    }
}
