/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { HelmValueForm } from "../../components/config/HelmValueForm";
import { cloudService } from "../../services/cloud_service";
import { router } from "../../core/router";

/**
 * Generate a UUID v4
 * @returns {string}
 */
function generateUUID() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
        const r = Math.random() * 16 | 0;
        const v = c === 'x' ? r : (r & 0x3 | 0x8);
        return v.toString(16);
    });
}

/**
 * MD5 hash implementation for subdomain generation
 * Matches Python's hashlib.md5() output
 * @param {string} str - Input string
 * @returns {string} - MD5 hash hex string
 */
function md5(str) {
    function rotateLeft(x, n) {
        return (x << n) | (x >>> (32 - n));
    }

    function addUnsigned(x, y) {
        const x8 = x & 0x80000000;
        const y8 = y & 0x80000000;
        const x4 = x & 0x40000000;
        const y4 = y & 0x40000000;
        const result = (x & 0x3FFFFFFF) + (y & 0x3FFFFFFF);
        if (x4 & y4) return result ^ 0x80000000 ^ x8 ^ y8;
        if (x4 | y4) {
            if (result & 0x40000000) return result ^ 0xC0000000 ^ x8 ^ y8;
            return result ^ 0x40000000 ^ x8 ^ y8;
        }
        return result ^ x8 ^ y8;
    }

    function F(x, y, z) { return (x & y) | (~x & z); }
    function G(x, y, z) { return (x & z) | (y & ~z); }
    function H(x, y, z) { return x ^ y ^ z; }
    function I(x, y, z) { return y ^ (x | ~z); }

    function FF(a, b, c, d, x, s, ac) {
        a = addUnsigned(a, addUnsigned(addUnsigned(F(b, c, d), x), ac));
        return addUnsigned(rotateLeft(a, s), b);
    }
    function GG(a, b, c, d, x, s, ac) {
        a = addUnsigned(a, addUnsigned(addUnsigned(G(b, c, d), x), ac));
        return addUnsigned(rotateLeft(a, s), b);
    }
    function HH(a, b, c, d, x, s, ac) {
        a = addUnsigned(a, addUnsigned(addUnsigned(H(b, c, d), x), ac));
        return addUnsigned(rotateLeft(a, s), b);
    }
    function II(a, b, c, d, x, s, ac) {
        a = addUnsigned(a, addUnsigned(addUnsigned(I(b, c, d), x), ac));
        return addUnsigned(rotateLeft(a, s), b);
    }

    function convertToWordArray(str) {
        const lWordCount = (((str.length + 8) - ((str.length + 8) % 64)) / 64 + 1) * 16;
        const lWordArray = new Array(lWordCount - 1);
        let lByteCount = 0, lWordIdx = 0;
        while (lByteCount < str.length) {
            lWordIdx = (lByteCount - (lByteCount % 4)) / 4;
            const lBytePosition = (lByteCount % 4) * 8;
            lWordArray[lWordIdx] = (lWordArray[lWordIdx] | (str.charCodeAt(lByteCount) << lBytePosition));
            lByteCount++;
        }
        lWordIdx = (lByteCount - (lByteCount % 4)) / 4;
        const lBytePosition = (lByteCount % 4) * 8;
        lWordArray[lWordIdx] = lWordArray[lWordIdx] | (0x80 << lBytePosition);
        lWordArray[lWordCount - 2] = str.length << 3;
        lWordArray[lWordCount - 1] = str.length >>> 29;
        return lWordArray;
    }

    function wordToHex(value) {
        let hex = '', byte, i;
        for (i = 0; i <= 3; i++) {
            byte = (value >>> (i * 8)) & 255;
            hex = hex + ('0' + byte.toString(16)).slice(-2);
        }
        return hex;
    }

    const x = convertToWordArray(str);
    let a = 0x67452301, b = 0xEFCDAB89, c = 0x98BADCFE, d = 0x10325476;
    const S11 = 7, S12 = 12, S13 = 17, S14 = 22;
    const S21 = 5, S22 = 9, S23 = 14, S24 = 20;
    const S31 = 4, S32 = 11, S33 = 16, S34 = 23;
    const S41 = 6, S42 = 10, S43 = 15, S44 = 21;

    for (let k = 0; k < x.length; k += 16) {
        const AA = a, BB = b, CC = c, DD = d;
        a = FF(a, b, c, d, x[k+0], S11, 0xD76AA478);
        d = FF(d, a, b, c, x[k+1], S12, 0xE8C7B756);
        c = FF(c, d, a, b, x[k+2], S13, 0x242070DB);
        b = FF(b, c, d, a, x[k+3], S14, 0xC1BDCEEE);
        a = FF(a, b, c, d, x[k+4], S11, 0xF57C0FAF);
        d = FF(d, a, b, c, x[k+5], S12, 0x4787C62A);
        c = FF(c, d, a, b, x[k+6], S13, 0xA8304613);
        b = FF(b, c, d, a, x[k+7], S14, 0xFD469501);
        a = FF(a, b, c, d, x[k+8], S11, 0x698098D8);
        d = FF(d, a, b, c, x[k+9], S12, 0x8B44F7AF);
        c = FF(c, d, a, b, x[k+10], S13, 0xFFFF5BB1);
        b = FF(b, c, d, a, x[k+11], S14, 0x895CD7BE);
        a = FF(a, b, c, d, x[k+12], S11, 0x6B901122);
        d = FF(d, a, b, c, x[k+13], S12, 0xFD987193);
        c = FF(c, d, a, b, x[k+14], S13, 0xA679438E);
        b = FF(b, c, d, a, x[k+15], S14, 0x49B40821);
        a = GG(a, b, c, d, x[k+1], S21, 0xF61E2562);
        d = GG(d, a, b, c, x[k+6], S22, 0xC040B340);
        c = GG(c, d, a, b, x[k+11], S23, 0x265E5A51);
        b = GG(b, c, d, a, x[k+0], S24, 0xE9B6C7AA);
        a = GG(a, b, c, d, x[k+5], S21, 0xD62F105D);
        d = GG(d, a, b, c, x[k+10], S22, 0x2441453);
        c = GG(c, d, a, b, x[k+15], S23, 0xD8A1E681);
        b = GG(b, c, d, a, x[k+4], S24, 0xE7D3FBC8);
        a = GG(a, b, c, d, x[k+9], S21, 0x21E1CDE6);
        d = GG(d, a, b, c, x[k+14], S22, 0xC33707D6);
        c = GG(c, d, a, b, x[k+3], S23, 0xF4D50D87);
        b = GG(b, c, d, a, x[k+8], S24, 0x455A14ED);
        a = GG(a, b, c, d, x[k+13], S21, 0xA9E3E905);
        d = GG(d, a, b, c, x[k+2], S22, 0xFCEFA3F8);
        c = GG(c, d, a, b, x[k+7], S23, 0x676F02D9);
        b = GG(b, c, d, a, x[k+12], S24, 0x8D2A4C8A);
        a = HH(a, b, c, d, x[k+5], S31, 0xFFFA3942);
        d = HH(d, a, b, c, x[k+8], S32, 0x8771F681);
        c = HH(c, d, a, b, x[k+11], S33, 0x6D9D6122);
        b = HH(b, c, d, a, x[k+14], S34, 0xFDE5380C);
        a = HH(a, b, c, d, x[k+1], S31, 0xA4BEEA44);
        d = HH(d, a, b, c, x[k+4], S32, 0x4BDECFA9);
        c = HH(c, d, a, b, x[k+7], S33, 0xF6BB4B60);
        b = HH(b, c, d, a, x[k+10], S34, 0xBEBFBC70);
        a = HH(a, b, c, d, x[k+13], S31, 0x289B7EC6);
        d = HH(d, a, b, c, x[k+0], S32, 0xEAA127FA);
        c = HH(c, d, a, b, x[k+3], S33, 0xD4EF3085);
        b = HH(b, c, d, a, x[k+6], S34, 0x4881D05);
        a = HH(a, b, c, d, x[k+9], S31, 0xD9D4D039);
        d = HH(d, a, b, c, x[k+12], S32, 0xE6DB99E5);
        c = HH(c, d, a, b, x[k+15], S33, 0x1FA27CF8);
        b = HH(b, c, d, a, x[k+2], S34, 0xC4AC5665);
        a = II(a, b, c, d, x[k+0], S41, 0xF4292244);
        d = II(d, a, b, c, x[k+7], S42, 0x432AFF97);
        c = II(c, d, a, b, x[k+14], S43, 0xAB9423A7);
        b = II(b, c, d, a, x[k+5], S44, 0xFC93A039);
        a = II(a, b, c, d, x[k+12], S41, 0x655B59C3);
        d = II(d, a, b, c, x[k+3], S42, 0x8F0CCC92);
        c = II(c, d, a, b, x[k+10], S43, 0xFFEFF47D);
        b = II(b, c, d, a, x[k+1], S44, 0x85845DD1);
        a = II(a, b, c, d, x[k+8], S41, 0x6FA87E4F);
        d = II(d, a, b, c, x[k+15], S42, 0xFE2CE6E0);
        c = II(c, d, a, b, x[k+6], S43, 0xA3014314);
        b = II(b, c, d, a, x[k+13], S44, 0x4E0811A1);
        a = II(a, b, c, d, x[k+4], S41, 0xF7537E82);
        d = II(d, a, b, c, x[k+11], S42, 0xBD3AF235);
        c = II(c, d, a, b, x[k+2], S43, 0x2AD7D2BB);
        b = II(b, c, d, a, x[k+9], S44, 0xEB86D391);
        a = addUnsigned(a, AA);
        b = addUnsigned(b, BB);
        c = addUnsigned(c, CC);
        d = addUnsigned(d, DD);
    }
    return wordToHex(a) + wordToHex(b) + wordToHex(c) + wordToHex(d);
}

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
                subdomain: this.state.subdomain,
                helm_values: this.state.helmValues,
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
