/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { securityAccessService } from "../../services/security_access";

/**
 * AddPolicyModal Component
 *
 * A modal dialog for creating or editing a Security Access access policy.
 * Mirrors CreateSecurityAccessModal's `o_woow_modal_*` structure. When a
 * `policy` prop is passed the modal is in edit mode (PUT), otherwise create
 * mode (POST). On success the parent reloads the SA detail via onSaved.
 *
 * Props:
 *   - workspaceId (number): Parent workspace ID
 *   - saId (number): Security Access ID
 *   - route (Object): The Application Route this policy belongs to (P2.5
 *       per-route: a policy binds to one route; create passes route.id)
 *   - policy (Object | null): Existing policy when editing
 *   - onClose (Function): Called when the modal is closed/cancelled
 *   - onSaved (Function): Called after a successful create/update
 */
export class AddPolicyModal extends Component {
  static template = "woow_paas_platform.AddPolicyModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    workspaceId: { type: Number },
    saId: { type: Number },
    route: { type: Object },
    policy: { type: [Object, { value: null }], optional: true },
    onClose: { type: Function },
    onSaved: { type: Function },
  };

  setup() {
    const p = this.props.policy;
    this.state = useState({
      name: p ? p.name : "",
      decision: p ? p.decision : "allow",
      rule_type: p ? p.rule_type : "email_domain",
      rule_value: p ? p.rule_value : "",
      saving: false,
      error: null,
    });
  }

  get isEdit() {
    return !!this.props.policy;
  }

  async confirm() {
    if (!this.state.name.trim() || !this.state.rule_value.trim()) {
      this.state.error = "Name and rule value are required";
      return;
    }
    this.state.saving = true;
    this.state.error = null;
    const vals = {
      name: this.state.name.trim(),
      decision: this.state.decision,
      rule_type: this.state.rule_type,
      rule_value: this.state.rule_value.trim(),
    };
    const res = this.isEdit
      ? await securityAccessService.updatePolicy(
          this.props.workspaceId, this.props.saId, this.props.policy.id, vals)
      : await securityAccessService.createPolicy(
          this.props.workspaceId, this.props.saId, this.props.route.id, vals);
    this.state.saving = false;
    if (res.success) {
      this.props.onSaved();
    } else {
      this.state.error = res.error;
    }
  }
}
