/** @odoo-module **/
import { Component, useState, onWillStart } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { PlanSelector } from "../subscription/PlanSelector";
import { securityAccessService } from "../../services/security_access";

/**
 * CreateSecurityAccessModal Component
 *
 * A modal dialog for creating a new Security Access (Zero Trust Tunnel)
 * instance. Mirrors CreateSmartHomeModal: loads platform-level templates
 * and plans, lets the user pick a plan, then creates the record. The
 * parent navigates to the detail page (where provisioning progress polls)
 * after creation.
 *
 * Props:
 *   - workspaceId (number): Parent workspace ID
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onCreated (Function): Called after successful creation with SA data
 */
export class CreateSecurityAccessModal extends Component {
  static template = "woow_paas_platform.CreateSecurityAccessModal";
  static components = { WoowButton, WoowIcon, PlanSelector };
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
      selectedPlanId: null,
      loading: false,
      error: null,
    });
    onWillStart(async () => {
      const res = await securityAccessService.fetchTemplates();
      if (res.success && res.data.length) {
        this.state.templates = res.data;
        this.state.templateId = res.data[0].id;
        await this.loadPlans();
      } else {
        this.state.error =
          res.error || "No Security Access template available";
      }
    });
  }

  async loadPlans() {
    const res = await securityAccessService.fetchPlans(this.state.templateId);
    if (res.success) {
      this.state.plans = res.data;
      this.state.selectedPlanId = res.data.length ? res.data[0].id : null;
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
    if (ev.target === ev.currentTarget && !this.state.loading) {
      this.props.onClose();
    }
  }

  async confirm() {
    if (!this.state.name.trim()) {
      this.state.error = "Please enter a name";
      return;
    }
    if (!this.state.selectedPlanId) {
      this.state.error = "Please select a plan";
      return;
    }
    this.state.loading = true;
    this.state.error = null;
    const res = await securityAccessService.create(
      this.props.workspaceId,
      this.state.name.trim(),
      this.state.selectedPlanId,
      this.state.templateId,
    );
    this.state.loading = false;
    if (res.success) {
      this.props.onCreated(res.data);
      this.props.onClose();
    } else {
      this.state.error = res.error;
    }
  }
}
