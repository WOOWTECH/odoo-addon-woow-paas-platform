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

  // 依 rule_type 給 rule value 的動態 label/prefix/placeholder/hint（'@' 純視覺前綴，不進 value）
  get ruleValueConfig() {
    switch (this.state.rule_type) {
      case "email_domain":
        return { label: "Emails ending in", prefix: "@", placeholder: "acme.com",
          hint: "Anyone with an email at this domain matches." };
      case "email_list":
        return { label: "Emails", prefix: "", placeholder: "a@x.com, b@x.com",
          hint: "Comma-separated email addresses match." };
      case "ip_range":
        return { label: "IP range (CIDR)", prefix: "", placeholder: "203.0.113.0/24",
          hint: "Visitors from this CIDR range match." };
      default:
        return { label: "Rule value", prefix: "", placeholder: "", hint: "" };
    }
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
