/** @odoo-module **/
import { Component, useState, onMounted, onWillUpdateProps, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { BrandIcon } from "../../components/brand-icon/BrandIcon";
import { templateBrand } from "../../utils/brand_icons";
import { WoowButton } from "../../components/button/WoowButton";
import { HelmValueForm } from "../../components/config/HelmValueForm";
import { OdooAddonSelector } from "../../components/odoo-addon-selector/OdooAddonSelector";
import { PlanSelector } from "../../components/subscription/PlanSelector";
import { PlanTotalBar } from "../../components/subscription/PlanTotalBar";
import { pickDefaultPlan } from "../../components/subscription/plan_selector_utils";
import { getCategoryLabel } from "../../components/marketplace/CategoryFilter";
import { OneTimeCredentialsModal, buildOneTimeCredentials, oneTimeMcpSetupHint } from "../../components/modal/OneTimeCredentialsModal";
import { formatTemplatePrice } from "../../utils/pricing_label";
import { availabilityLabel, isPurchasable } from "../../utils/availability_label";
import { cloudService } from "../../services/cloud_service";
import { settingsErrors } from "../../utils/tailscale_settings";
import { workspaceService } from "../../services/workspace_service";
import { breadcrumbService } from "../../services/breadcrumb_service";
import { router } from "../../core/router";
import { useFormErrorReveal } from "../../utils/form_feedback";

/**
 * AppConfigurationPage Component
 * Configuration page for launching a new cloud service from a template
 */
export class AppConfigurationPage extends Component {
    static template = "woow_paas_platform.AppConfigurationPage";
    static components = { WoowCard, WoowIcon, BrandIcon, WoowButton, HelmValueForm, OdooAddonSelector, PlanSelector, PlanTotalBar, OneTimeCredentialsModal };
    static props = {
        workspaceId: { type: Number },
        templateId: { type: Number },
    };

    getCategoryLabel(key) {
        return getCategoryLabel(key);
    }

    setup() {
        this.state = useState({
            template: null,
            workspace: null,
            accessDenied: false,
            loading: true,
            error: null,
            // Form state
            name: '',
            helmValues: {},
            selectedAddonIds: [],
            showAdvanced: false,
            // Subscription plan
            plans: [],
            selectedPlanId: undefined,
            plansError: null,
            // Validation errors
            errors: {},
            // X-LC-006-3：前端驗證每失敗一次 +1，觸發捲到第一個錯誤並聚焦。
            errorRevealToken: 0,
            // Launch state
            launching: false,
            launchError: null,
            // #530：一次性憑證由單一字串改成具名清單（Admin Password ＋ Gateway
            // API Key）。null＝不開視窗；非空陣列＝開。
            oneTimeCredentials: null,
            // 2026-10-27（#2040 第五輪）：視窗裡「下一步」那一句（oneTimeMcpSetupHint）；""＝不顯示。
            oneTimeSetupHint: "",
            createdServiceId: null,
        });
        // X-LC-002-3／X-LC-006-3：送出失敗時把使用者帶到錯誤（欄位或頁尾的後端錯誤橫幅）。
        useFormErrorReveal(
            "configureRoot",
            () => this.state.errorRevealToken,
            () => this.state.launchError,
        );
        this.router = useState(router);

        this._loadSequence = 0;
        this._unmounted = false;
        onMounted(() => this.loadTemplate());
        onWillUpdateProps((nextProps) => {
            if (nextProps.workspaceId !== this._requestedWorkspaceId ||
                nextProps.templateId !== this._requestedTemplateId) {
                return this.loadTemplate(nextProps);
            }
        });
        onWillUnmount(() => {
            this._unmounted = true;
            this._loadSequence++;
        });
    }

    /**
     * Read the current workspace role before showing a create form. The list
     * cache is not authorization: direct routes may target another workspace.
     * A newer route or unmount invalidates every outstanding read.
     */
    async loadTemplate(props = this.props) {
        const { workspaceId, templateId } = props;
        const sequence = ++this._loadSequence;
        this._requestedWorkspaceId = workspaceId;
        this._requestedTemplateId = templateId;
        const isCurrent = () => !this._unmounted && sequence === this._loadSequence;
        Object.assign(this.state, {
            loading: true, error: null, accessDenied: false,
            workspace: null, template: null,
            name: '', helmValues: {}, selectedAddonIds: [], showAdvanced: false,
            plans: [], selectedPlanId: undefined, plansError: null, errors: {},
            launching: false, launchError: null,
            oneTimeCredentials: null, oneTimeSetupHint: "", createdServiceId: null,
        });

        try {
            const workspaceResult = await workspaceService.getWorkspace(workspaceId);
            if (!isCurrent()) return;
            if (!workspaceResult.success) {
                this.state.error = workspaceResult.error || _t("Failed to load workspace");
                return;
            }
            const workspace = workspaceResult.data;
            if (workspace?.id !== workspaceId ||
                (workspace.role !== 'owner' && workspace.role !== 'admin')) {
                this.state.accessDenied = true;
                return;
            }
            this.state.workspace = workspace;

            const result = await cloudService.fetchTemplate(templateId);
            if (!isCurrent()) return;
            if (!result.success) {
                this.state.error = result.error || _t("Failed to load template");
                return;
            }
            this.state.template = result.data;
            this._publishBreadcrumb(workspaceId);
            this.initializeHelmValues(result.data);
            // Preserve plan selection and backend configuration errors.
            const plansResult = await cloudService.fetchPlans(templateId);
            if (!isCurrent()) return;
            if (plansResult.success) {
                this.state.plans = plansResult.data;
                this.state.selectedPlanId = pickDefaultPlan(this.state.plans);
            } else {
                this.state.plansError = plansResult.error;
            }
        } catch (error) {
            if (isCurrent()) {
                this.state.error = error.message || _t("Failed to load configuration");
            }
        } finally {
            if (isCurrent()) this.state.loading = false;
        }
    }

    /**
     * UI affordance only; the existing create endpoint remains authoritative.
     * Missing roles and data for another route never enable creation.
     */
    get canConfigure() {
        const workspace = this.state.workspace;
        return !this.state.loading && !this.state.error &&
            workspace?.id === this.props.workspaceId &&
            (workspace.role === 'owner' || workspace.role === 'admin') &&
            this.state.template?.id === this.props.templateId;
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

    /**
     * 麵包屑軌跡：Workspaces → {工作區} → App Marketplace(連回市集) → Configure {模板名}。
     * 此頁是從市集點模板進來的設定頁，父層接回市集；工作區名取自本次 API 回覆。
     */
    _publishBreadcrumb(workspaceId) {
        const ws = this.state.workspace;
        breadcrumbService.set("configure", [
            { label: _t("Workspaces"), path: "workspaces" },
            { label: ws?.name || _t("Workspace"), path: `workspace/${workspaceId}` },
            { label: _t("App Marketplace"), path: `workspace/${workspaceId}/services/marketplace` },
            { label: _t("Configure %s", this.state.template.name), path: null },
        ]);
    }

    get template() {
        return this.state.template;
    }

    /**
     * Header 資訊列的價格標籤（#478）。
     *
     * 這裡原本硬寫字面值 `Free`，於是同一個畫面上方說免費、下方的 Subscription
     * Plan 區與總計說 4500 pts/mo，自相矛盾。
     *
     * 顯示的是**商品起價**（與 marketplace 卡片同一個 `formatTemplatePrice`、
     * 同一份後端 `pricing` 資料），而不是「目前選中的方案價」：header 與
     * category／規格並列，講的是這個商品本身；選中方案的精確金額由下方 plan 區
     * 與總計列負責（那兩處本來就正確，本次不動）。單一事實源也讓卡片與 header
     * 不可能再各說各話。
     * @returns {string} 空字串 ⇒ 不渲染標籤
     */
    get formattedPrice() {
        return formatTemplatePrice(this.template?.pricing);
    }

    /**
     * 這個商品現在買不買得到（#501）——與 marketplace 卡片同一個判準來源。
     * @returns {boolean}
     */
    get isPurchasable() {
        return isPurchasable(this.template?.availability);
    }

    /**
     * 不可購買時的狀態標籤（「已售罄」／「暫停販售」）。
     * @returns {string} 空字串 ⇒ 可購買
     */
    get availabilityLabel() {
        return availabilityLabel(this.template?.availability);
    }

    /**
     * Launch 按鈕的 disabled 判準。
     *
     * 收斂成一個 getter 而不是在模板裡寫 `state.launching || !isPurchasable`：
     * OWL 模板的語法錯誤不會被任何 Python 測試攔到、runtime 直接整站白屏
     * （見 docs/tracking/pitfalls/owl-template-crash-class-syntax.md），能放在 JS
     * 的判斷就不要放模板。
     * @returns {boolean}
     */
    get launchDisabled() {
        return !this.canConfigure || this.state.launching || !this.isPurchasable;
    }

    get helmValueSpecs() {
        return this.template?.helm_value_specs || { required: [], optional: [] };
    }

    get isOdooTemplate() {
        return this.template?.slug === 'odoo';
    }

    get hasOptionalSettings() {
        return (this.helmValueSpecs.optional || []).length > 0;
    }

    /**
     * Whether to render the Application Settings section. True when the template has
     * ANY spec (required or optional) — gating on required-only would hide the whole
     * section (and its advanced toggle) for an optional-only template, making its
     * fields unreachable.
     */
    get hasApplicationSettings() {
        const specs = this.helmValueSpecs;
        return (specs.required || []).length + (specs.optional || []).length > 0;
    }

    /**
     * Get category icon name
     * @returns {string}
     */
    get brandKey() {
        return templateBrand(this.state.template?.slug);
    }

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
    onAddonSelectionChange(addonIds) {
        this.state.selectedAddonIds = addonIds;
    }

    onNameChange(event) {
        const value = event.target.value;
        this.state.name = value;
        this.clearError('name');
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
            errors.name = _t('Service name is required');
        } else if (name.length < 2) {
            errors.name = _t('Service name must be at least 2 characters');
        } else if (name.length > 100) {
            errors.name = _t('Service name must be at most 100 characters');
        }

        // Subdomain is auto-generated, no validation needed

        // Validate required helm values
        const requiredSpecs = this.helmValueSpecs.required || [];
        for (const spec of requiredSpecs) {
            const value = this.state.helmValues[spec.key];
            if (spec.required !== false) { // Required by default in required section
                if (value === undefined || value === null || value === '') {
                    errors[spec.key] = _t("%s is required", spec.label);
                }
            }
        }

        // INFRA-TS-005-5／TS-007-1：spec 帶 `validate` 的欄位（VPN 節點的轉發清單、控制平面＋key）。
        // 開通頁還不知道 namespace（null）⇒ 同工作區檢查交給後端。
        const specs = [...(this.helmValueSpecs.required || []), ...(this.helmValueSpecs.optional || [])];
        const checked = settingsErrors(specs, this.state.helmValues, { namespace: null });
        for (const [key, message] of Object.entries(checked)) {
            if (!errors[key]) errors[key] = message;
        }
        if ((this.helmValueSpecs.optional || []).some((spec) => checked[spec.key])) {
            this.state.showAdvanced = true;
        }

        // Validate plan selection
        if (!this.state.selectedPlanId) {
            errors.plan = _t('Please select a subscription plan');
        }

        this.state.errors = errors;
        const valid = Object.keys(errors).length === 0;
        if (!valid) {
            this.state.errorRevealToken++;
        }
        return valid;
    }

    /**
     * Handle cancel button click
     */
    onPlanSelect(planId) {
        this.state.selectedPlanId = planId;
    }

    onCancel() {
        this.router.navigate(`workspace/${this.props.workspaceId}/services/marketplace`);
    }

    /**
     * Handle launch button click
     */
    async onLaunch() {
        if (this.launchDisabled || !this.validateForm()) {
            return;
        }

        const { workspaceId, templateId } = this.props;
        const sequence = this._loadSequence;
        const isCurrent = () => !this._unmounted && sequence === this._loadSequence;
        this.state.launching = true;
        this.state.launchError = null;

        try {
            const payload = {
                template_id: templateId,
                name: this.state.name.trim(),
                values: this.state.helmValues,
                plan_id: this.state.selectedPlanId,
            };
            if (this.isOdooTemplate && this.state.selectedAddonIds.length) {
                payload.odoo_addon_ids = this.state.selectedAddonIds;
            }
            const result = await cloudService.createService(workspaceId, payload);
            if (!isCurrent()) return;

            if (result.success) {
                const serviceId = result.data.id;
                const credentials = buildOneTimeCredentials(result.data);
                if (credentials.length) {
                    this.state.oneTimeCredentials = credentials;
                    // 2026-10-27（#2040 第五輪）：mcp-server 家族多一句「先到 Connection 存好連線」。
                    this.state.oneTimeSetupHint = oneTimeMcpSetupHint(result.data);
                    this.state.createdServiceId = serviceId;
                } else {
                    this.router.navigate(`workspace/${workspaceId}/services/${serviceId}`);
                }
            } else {
                this.state.launchError = result.error || _t('Failed to create service');
            }
        } catch (error) {
            if (isCurrent()) {
                this.state.launchError = error.message || _t('An unexpected error occurred');
            }
        } finally {
            if (isCurrent()) this.state.launching = false;
        }
    }

    /**
     * Navigate back to workspaces
     */
    goBack() {
        this.router.navigate(`workspace/${this.props.workspaceId}/services/marketplace`);
    }

    onCredentialsClose() {
        const serviceId = this.state.createdServiceId;
        this.state.oneTimeCredentials = null;
        this.state.oneTimeSetupHint = "";
        this.state.createdServiceId = null;
        this.router.navigate(`workspace/${this.props.workspaceId}/services/${serviceId}`);
    }
}
