/** @odoo-module **/
import { Component, useState, onWillStart } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { PlanSelector } from "../subscription/PlanSelector";
import { PlanTotalBar } from "../subscription/PlanTotalBar";
import { pickDefaultPlan } from "../subscription/plan_selector_utils";
import { securityAccessService } from "../../services/security_access";

/**
 * CreateSecurityAccessModal Component
 *
 * A modal dialog for creating a new Security Access (Zero Trust Tunnel)
 * instance. Mirrors CreateSmartHomeModal: loads platform-level templates
 * and plans, lets the user pick a plan, creates the record, then
 * automatically chains provisioning (Cloudflare Tunnel creation) in-modal
 * with a progress step. On success/failure the parent is notified and
 * navigates to the detail page (active, or error with a retry button).
 *
 * Props:
 *   - workspaceId (number): Parent workspace ID
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onCreated (Function): Called after successful creation with SA data
 */
export class CreateSecurityAccessModal extends Component {
  static template = "woow_paas_platform.CreateSecurityAccessModal";
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
      templateId: null,
      plans: [],
      selectedPlanId: undefined,
      loading: false,
      error: null,
      // provision 串接 step machine（鏡像 CreateSmartHomeModal）：
      // loading 管 create 階段的按鈕 spinner，provisioning 管 provision 階段。
      provisioning: false,
      step: "form", // "form" | "provisioning" | "done"
      provisionProgress: 0,
    });
    onWillStart(async () => {
      const res = await securityAccessService.fetchTemplates();
      if (res.success && res.data.length) {
        this.state.templates = res.data;
        this.state.templateId = res.data[0].id;
        await this.loadPlans();
      } else {
        this.state.error =
          res.error || _t("No Security Access template available");
      }
    });
  }

  async loadPlans() {
    const res = await securityAccessService.fetchPlans(this.state.templateId);
    if (res.success) {
      this.state.plans = res.data;
      this.state.selectedPlanId = pickDefaultPlan(res.data);
    } else {
      this.state.error = res.error;
    }
  }

  onSelectPlan(planId) {
    this.state.selectedPlanId = planId;
  }

  onNameInput(ev) {
    this.state.name = ev.target.value;
    this.state.error = null;
  }

  onBackdropClick(ev) {
    if (
      ev.target === ev.currentTarget &&
      !this.state.loading &&
      !this.state.provisioning
    ) {
      this.props.onClose();
    }
  }

  async confirm() {
    if (!this.state.name.trim()) {
      this.state.error = _t("Please enter a name");
      return;
    }
    if (!this.state.selectedPlanId) {
      this.state.error = _t("Please select a plan");
      return;
    }
    this.state.loading = true;
    this.state.error = null;

    // Step 1: 建立 SA 記錄
    const createResult = await securityAccessService.create(
      this.props.workspaceId,
      this.state.name.trim(),
      this.state.selectedPlanId,
      this.state.templateId,
    );

    if (!createResult.success) {
      this.state.error =
        createResult.error || _t("Failed to create security access");
      this.state.loading = false;
      return;
    }

    // Step 2: 自動 provision（建 Cloudflare tunnel），進度顯示在 modal 內
    this.state.step = "provisioning";
    this.state.loading = false;
    this.state.provisioning = true;
    this.state.provisionProgress = 30;

    const saId = createResult.data.id;
    const provisionResult = await securityAccessService.provision(
      this.props.workspaceId,
      saId,
    );

    this.state.provisionProgress = 100;

    if (provisionResult.success) {
      this.state.step = "done";
      this.state.provisioning = false;
      // 短暫顯示完成畫面後通知父層（父層關閉 modal 並導去 detail page）
      setTimeout(() => {
        this.props.onCreated(provisionResult.data || createResult.data);
      }, 800);
    } else {
      // provision 失敗：仍通知父層導去 detail page，SA 處於 error state，
      // detail page 有 Provision 重試鈕接住。
      this.state.error = provisionResult.error || _t("Provisioning failed");
      this.state.provisioning = false;
      this.props.onCreated(createResult.data);
    }
  }
}
