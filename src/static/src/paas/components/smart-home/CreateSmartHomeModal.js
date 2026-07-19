/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { PlanSelector } from "../subscription/PlanSelector";
import { PlanTotalBar } from "../subscription/PlanTotalBar";
import { pickDefaultPlan } from "../subscription/plan_selector_utils";
import { workspaceService } from "../../services/workspace_service";

/**
 * CreateSmartHomeModal Component
 *
 * A modal dialog for creating a new smart home instance.
 * After creation, automatically triggers provisioning.
 *
 * Props:
 *   - workspaceId (number): Parent workspace ID
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onCreated (Function): Called after successful creation with smart home data
 */
export class CreateSmartHomeModal extends Component {
    static template = "woow_paas_platform.CreateSmartHomeModal";
    static components = { WoowButton, WoowIcon, PlanSelector, PlanTotalBar };
    static props = {
        workspaceId: { type: Number },
        onClose: { type: Function },
        onCreated: { type: Function },
    };

    setup() {
        this.state = useState({
            name: "",
            templates: [],
            selectedTemplateId: null,
            plans: [],
            selectedPlanId: undefined,
            loading: false,
            loadingTemplates: true,
            loadingPlans: false,
            provisioning: false,
            error: null,
            // Surface backend fetch failures so admins see the actual
            // error (e.g. "Template has no subscription plans configured")
            // instead of a misleading "No plans available" empty state.
            templatesError: null,
            plansError: null,
            step: "form", // "form" | "provisioning" | "done"
            provisionProgress: 0,
        });

        this.loadTemplates();
    }

    async loadTemplates() {
        this.state.loadingTemplates = true;
        this.state.templatesError = null;
        const result = await workspaceService.fetchSmartHomeTemplates();
        this.state.loadingTemplates = false;
        if (!result.success) {
            this.state.templatesError = result.error;
            this.state.templates = [];
            return;
        }
        this.state.templates = result.data;
        if (this.state.templates.length > 0) {
            // Auto-select the first template; users can switch via the
            // dropdown when more than one is active.
            await this.onTemplateSelect(this.state.templates[0].id);
        }
    }

    async onTemplateSelect(templateId) {
        this.state.selectedTemplateId = templateId;
        this.state.selectedPlanId = undefined;
        this.state.plans = [];
        this.state.error = null;
        this.state.plansError = null;
        this.state.loadingPlans = true;
        const result = await workspaceService.fetchSmartHomePlans(templateId);
        this.state.loadingPlans = false;
        if (!result.success) {
            this.state.plansError = result.error;
            return;
        }
        this.state.plans = result.data;
        this.state.selectedPlanId = pickDefaultPlan(this.state.plans);
    }

    onTemplateChange(ev) {
        const id = parseInt(ev.target.value, 10);
        if (!isNaN(id)) {
            this.onTemplateSelect(id);
        }
    }

    onPlanSelect(planId) {
        this.state.selectedPlanId = planId;
    }

    onNameInput(ev) {
        this.state.name = ev.target.value;
        this.state.error = null;
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget && !this.state.loading && !this.state.provisioning) {
            this.props.onClose();
        }
    }

    async onSubmit() {
        const name = this.state.name.trim();
        if (!name) {
            this.state.error = "Smart home name is required";
            return;
        }

        if (!this.state.selectedTemplateId) {
            this.state.error = "Please select a Smart Home template";
            return;
        }

        if (!this.state.selectedPlanId) {
            this.state.error = "Please select a subscription plan";
            return;
        }

        this.state.loading = true;
        this.state.error = null;

        // Step 1: Create smart home
        // ha_port 不由前端傳入：後端 model default=8123（issue #269，port 壓死）
        const createResult = await workspaceService.createSmartHome(this.props.workspaceId, {
            name,
            plan_id: this.state.selectedPlanId,
            template_id: this.state.selectedTemplateId,
        });

        if (!createResult.success) {
            this.state.error = createResult.error || "Failed to create smart home";
            this.state.loading = false;
            return;
        }

        // Step 2: Start provisioning
        this.state.step = "provisioning";
        this.state.loading = false;
        this.state.provisioning = true;
        this.state.provisionProgress = 30;

        const homeId = createResult.data.id;
        const provisionResult = await workspaceService.provisionSmartHome(
            this.props.workspaceId,
            homeId
        );

        this.state.provisionProgress = 100;

        if (provisionResult.success) {
            this.state.step = "done";
            this.state.provisioning = false;
            // Notify parent after short delay so user sees completion
            setTimeout(() => {
                this.props.onCreated(provisionResult.data || createResult.data);
            }, 800);
        } else {
            this.state.error = provisionResult.error || "Provisioning failed";
            this.state.provisioning = false;
            // Still notify parent with the created data, even though provisioning failed
            this.props.onCreated(createResult.data);
        }
    }
}
