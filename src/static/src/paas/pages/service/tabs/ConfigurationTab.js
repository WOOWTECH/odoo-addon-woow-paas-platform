/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
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
            // #1455：勾選要清除的憑證欄位（spec key 陣列）。
            clearKeys: [],
        });
    }

    get currentValues() {
        return this.props.service.helm_values || {};
    }

    get helmValueSpecs() {
        return this.props.service.template?.helm_value_specs || { required: [], optional: [] };
    }

    /**
     * Specs that are actually reconfigurable. `_init.*` specs are one-time
     * provisioning params applied at creation (stored on the service record, never
     * in helm_values), so they must not be rendered nor validated here — otherwise
     * the owner is forced to re-type the admin password on every reconfigure and the
     * value gets written into helm_values in plaintext on save.
     */
    get configurableSpecs() {
        const notInit = (s) => !String(s.key || "").startsWith("_init.");
        const base = this.helmValueSpecs;
        return {
            required: (base.required || []).filter(notInit),
            optional: (base.optional || []).filter(notInit),
        };
    }

    get allSpecs() {
        return [...this.configurableSpecs.required, ...this.configurableSpecs.optional];
    }

    get hasOptionalSettings() {
        return this.configurableSpecs.optional.length > 0;
    }

    get canEdit() {
        // #1456：沒有任何可調設定（HA、pi-agent…）時不給「修改配置」——原本照樣能按
        // 「儲存變更」，送出一次空的 helm upgrade（51–65 秒「升級中」、鎖住其他操作）。
        if (this.allSpecs.length === 0) {
            return false;
        }
        const disabledStates = ["deploying", "initializing", "upgrading", "deleting", "pending"];
        if (disabledStates.includes(this.props.service.state)) {
            return false;
        }
        // #1159 Q30：error 只有 init 額度出口可以改設定（算新一代）；#1552：背景 upgrade
        // 沒有確認套用的 error 也可以重新送出。等待逾時等其他 error 後端照擋。讀後端同一
        // 判準的鏡射欄位，前端不自建。
        return (
            this.props.service.state !== "error" ||
            !!this.props.service.init_reconfigure_allowed ||
            !!this.props.service.upgrade_retry_allowed
        );
    }

    getSpecValue(spec) {
        const values = this.currentValues;
        // Try flat key first (backward compatibility)
        const flatValue = values[spec.key];
        if (flatValue !== undefined) return flatValue;
        // Try nested lookup via dot-path
        const parts = spec.key.split('.');
        if (parts.length > 1) {
            let value = values;
            for (const part of parts) {
                if (value === undefined || value === null || typeof value !== 'object') {
                    value = undefined;
                    break;
                }
                value = value[part];
            }
            if (value !== undefined) return value;
        }
        return spec.default !== undefined ? spec.default : "";
    }

    /**
     * #1455：憑證欄位有沒有設定——讀後端的 `password_specs_set`（布林，不含值）。
     * 舊後端沒有這個欄位時退回「helm_values 裡有沒有非空值」（遮罩後仍是非空）。
     */
    isSecretSet(spec) {
        const states = this.props.service.password_specs_set;
        if (states && Object.prototype.hasOwnProperty.call(states, spec.key)) {
            return !!states[spec.key];
        }
        const value = this.getSpecValue(spec);
        return value !== undefined && value !== null && value !== "";
    }

    formatSpecValue(spec) {
        const value = this.getSpecValue(spec);
        // #1455：從沒設定過的金鑰原本也顯示成 ********，租戶以為已設定。
        if (spec.type === "password") return this.isSecretSet(spec) ? "********" : _t("Not set");
        if (spec.type === "boolean") return value ? _t("Enabled") : _t("Disabled");
        if (value === null || value === undefined) return "-";
        return String(value);
    }

    /** 是否為憑證欄位（`type: password`）。三處回填／驗證／送出共用同一判準。 */
    isPasswordSpec(spec) {
        return spec.type === "password";
    }

    /** #1435：有憑證欄位的服務才顯示「改金鑰會重新啟動」提示。 */
    get hasSecretSpecs() {
        return this.allSpecs.some((spec) => this.isPasswordSpec(spec));
    }

    /**
     * #1455：可清除的憑證欄位＝optional、目前有設定的 password spec。
     * 與後端 `_clearable_password_keys` 同一判準（required 的清掉服務就壞了）。
     */
    get clearableSecretSpecs() {
        return this.configurableSpecs.optional.filter(
            (spec) => this.isPasswordSpec(spec) && this.isSecretSet(spec)
        );
    }

    isClearChecked(spec) {
        return this.state.clearKeys.includes(spec.key);
    }

    toggleClear(spec) {
        if (this.isClearChecked(spec)) {
            this.state.clearKeys = this.state.clearKeys.filter((k) => k !== spec.key);
        } else {
            this.state.clearKeys = [...this.state.clearKeys, spec.key];
        }
    }

    /**
     * 進入編輯模式：`type: password` 的欄位**空白起始**，不回填。
     *
     * #535：後端 API 回來的密碼是遮罩後的 `'***'`（`_sanitize_helm_values`），
     * 而這裡原本對「每一條」spec 都做 `values[spec.key] = this.getSpecValue(spec)`
     * ⇒ 星號被當成使用者的輸入回填進表單 ⇒ `saveChanges()` 整包送出 ⇒ 後端
     * `_deep_merge` 讓它覆蓋既有值。真瀏覽器實測的結果是**平台把租戶的 Odoo
     * 管理員密碼改成 `'***'`**（helm_values.odooPassword: PLAIN → '***'）。
     *
     * read 端遮罩與 write 端回填是同一個契約，不能只動一半。
     */
    startEditing() {
        const values = {};
        for (const spec of this.allSpecs) {
            // 密碼欄留白 ＝「不變更」。使用者真的要換密碼時自己輸入，
            // 那時才會經 onHelmValueUpdate 進 editedValues。
            if (this.isPasswordSpec(spec)) continue;
            values[spec.key] = this.getSpecValue(spec);
        }
        this.state.editedValues = values;
        this.state.editing = true;
        this.state.error = null;
        this.state.errors = {};
        this.state.clearKeys = [];
    }

    cancelEditing() {
        this.state.editing = false;
        this.state.editedValues = {};
        this.state.error = null;
        this.state.errors = {};
        this.state.showAdvanced = false;
        this.state.clearKeys = [];
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
        for (const spec of this.configurableSpecs.required) {
            // #535：required 的密碼欄留白代表「不變更」，不是漏填。這是重新設定
            // 頁面，密碼在建立服務時就已經設過了（建立頁 AppConfigurationPage
            // 走自己的驗證，那裡的 required password 仍然必填）。
            if (this.isPasswordSpec(spec)) continue;
            const value = this.state.editedValues[spec.key];
            if (value === undefined || value === null || value === "") {
                errors[spec.key] = _t("%s is required", spec.label || spec.key);
            }
        }
        this.state.errors = errors;
        return Object.keys(errors).length === 0;
    }

    /**
     * 送出前把「沒有輸入」的密碼欄從 payload 拿掉。
     *
     * #535：使用者可能先輸入再清空，那時 `editedValues[key]` 會是 `""`。整包送出
     * 會讓後端把憑證覆蓋成空字串——與送星號一樣是憑證破壞。缺 key 在後端是
     * 「保留既有」（`_deep_merge` 不動沒被覆寫的鍵），所以直接拿掉即可。
     */
    buildSavePayload() {
        const payload = { ...this.state.editedValues };
        for (const spec of this.allSpecs) {
            if (!this.isPasswordSpec(spec)) continue;
            const value = payload[spec.key];
            if (value === undefined || value === null || value === "") {
                delete payload[spec.key];
            }
        }
        // #1455：勾了清除的欄位不送值（後端也是「清除優先」）。
        for (const key of this.state.clearKeys) {
            delete payload[key];
        }
        return payload;
    }

    /**
     * #1456：使用者沒有改任何值（密碼欄留白＝不變更，已由 buildSavePayload 拿掉）時
     * 不送出——每次送出都是一次 helm upgrade（服務短暫進入「升級中」）。
     * 比對基準是進入編輯時的回填值（startEditing 用的同一個 getSpecValue）。
     */
    hasChanges(payload) {
        for (const [key, value] of Object.entries(payload)) {
            const spec = this.allSpecs.find((s) => s.key === key);
            if (!spec || this.isPasswordSpec(spec)) {
                return true;
            }
            if (String(value ?? "") !== String(this.getSpecValue(spec) ?? "")) {
                return true;
            }
        }
        return false;
    }

    async saveChanges() {
        if (this.state.saving) return;
        if (!this.validateForm()) return;

        const payload = this.buildSavePayload();
        // #1455：只勾「清除」時 payload 是空的，但仍要送出。
        if (!this.hasChanges(payload) && !this.state.clearKeys.length) {
            this.cancelEditing();
            return;
        }

        this.state.saving = true;
        this.state.error = null;

        try {
            const result = await cloudService.updateService(
                this.props.workspaceId,
                this.props.service.id,
                payload,
                null,
                this.state.clearKeys
            );

            if (result.success) {
                this.state.editing = false;
                this.state.editedValues = {};
                this.state.showAdvanced = false;
                this.state.clearKeys = [];
                if (this.props.onSave) {
                    this.props.onSave(result.data);
                }
            } else {
                this.state.error = result.error || _t("Failed to update configuration");
            }
        } catch (err) {
            this.state.error = err.message || _t("Failed to update configuration");
        } finally {
            this.state.saving = false;
        }
    }
}
