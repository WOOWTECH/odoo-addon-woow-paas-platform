/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { securityAccessService } from "../../services/security_access";

/**
 * AddPolicyModal Component
 *
 * A modal dialog for creating or editing a Security Access access policy.
 * P-rule: a policy is a CONTAINER of selector rules split into include (OR) /
 * require (AND) / exclude (NOT) groups, plus a decision (allow / deny / bypass).
 * Mirrors Cloudflare's native policy builder.
 *
 * When a `policy` prop is passed the modal is in edit mode (PUT), otherwise
 * create mode (POST). On success the parent reloads the SA detail via onSaved.
 *
 * Props:
 *   - workspaceId (number) / saId (number) / route (Object)
 *   - policy (Object | null): existing policy when editing (include/require/
 *       exclude groups from to_dict)
 *   - onClose (Function) / onSaved (Function)
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
    this._ruleUid = 0;
    this.state = useState({
      name: p ? p.name : "",
      decision: p ? p.decision : "allow",
      include: p ? this._fromGroup(p.include) : [],
      require: p ? this._fromGroup(p.require) : [],
      exclude: p ? this._fromGroup(p.exclude) : [],
      saving: false,
      error: null,
    });
    // Never present an empty Include group (a policy needs ≥1 include).
    //
    // #352: the seed row used to be hardcoded "email_domain" — the builder
    // handed every new Allow policy a bulk-grant rule out of the box. Derive it
    // from what the current decision actually permits in the include group
    // instead (needs this.state, hence after useState).
    if (!this.state.include.length) {
      this.state.include = [this._mkRow(this._defaultRuleType("include"))];
    }
  }

  get isEdit() {
    return !!this.props.policy;
  }

  _mkRow(ruleType, ruleValue) {
    return {
      _key: this._ruleUid++,
      rule_type: ruleType || "email_list",
      rule_value: ruleValue || "",
    };
  }

  _fromGroup(rules) {
    return (rules || []).map((r) => this._mkRow(r.rule_type, r.rule_value));
  }

  // The three rule groups rendered as builder sections.
  get groupDefs() {
    return [
      {
        key: "include",
        label: "Include — match any (OR)",
        hint: "Visitor passes this policy if they match ANY include rule.",
      },
      {
        key: "require",
        label: "Require — match all (AND)",
        hint: "Visitor must additionally match EVERY require rule.",
      },
      {
        key: "exclude",
        label: "Exclude — match none (NOT)",
        hint: "Visitor is rejected if they match ANY exclude rule.",
      },
    ];
  }

  // Selector options; identity-based ones (email) are hidden for bypass
  // policies because Cloudflare rejects them.
  get selectorOptions() {
    const all = [
      { value: "email_domain", label: "Emails ending in a domain", identity: true, bulk: true },
      { value: "email_list", label: "List of emails", identity: true, bulk: false },
      { value: "ip_range", label: "IP range (CIDR)", identity: false, bulk: false },
      { value: "everyone", label: "Everyone", identity: false, bulk: true },
      { value: "country", label: "Country", identity: false, bulk: false },
    ];
    return this.state.decision === "bypass"
      ? all.filter((o) => !o.identity)
      : all;
  }

  // #352: an Allow policy's INCLUDE rules must not admit an unlimited,
  // uncounted crowd — seat-based Access plans count people by the addresses in
  // "List of emails" rules, while a whole-domain / everyone rule counts as zero
  // yet still burns Cloudflare seats from the 50-seat pool shared by the entire
  // platform. Mirrors the backend constraint
  // (_check_allow_policy_no_bulk_grant), which is the real gate — this only
  // keeps the builder from offering a choice the server will reject.
  //
  // Scoped to include (OR = grants access). Require (AND) and Exclude (NOT)
  // only NARROW access, so "allow everyone at acme.com except evil.com" stays
  // expressible. Block/Bypass policies are unaffected.
  selectorOptionsFor(groupKey) {
    const options = this.selectorOptions;
    if (this.state.decision !== "allow" || groupKey !== "include") {
      return options;
    }
    return options.filter((o) => !o.bulk);
  }

  _defaultRuleType(groupKey) {
    const options = this.selectorOptionsFor(groupKey);
    return options.length ? options[0].value : "email_list";
  }

  // Per-selector value-field config. `show:false` selectors (everyone) take no
  // value input.
  valueConfig(ruleType) {
    switch (ruleType) {
      case "email_domain":
        return { show: true, prefix: "@", placeholder: "acme.com",
          hint: "Anyone with an email at this domain." };
      case "email_list":
        return { show: true, prefix: "", placeholder: "a@x.com, b@x.com",
          hint: "Comma-separated email addresses." };
      case "ip_range":
        return { show: true, prefix: "", placeholder: "203.0.113.0/24",
          hint: "Visitors from this CIDR range." };
      case "country":
        return { show: true, prefix: "", placeholder: "TW, US",
          hint: "Comma-separated ISO-2 country codes." };
      case "everyone":
        return { show: false, prefix: "", placeholder: "",
          hint: "Matches all visitors." };
      default:
        return { show: true, prefix: "", placeholder: "", hint: "" };
    }
  }

  addRule(group) {
    this.state[group].push(this._mkRow(this._defaultRuleType(group)));
  }

  removeRule(group, index) {
    this.state[group].splice(index, 1);
  }

  setRuleType(group, index, value) {
    const row = this.state[group][index];
    row.rule_type = value;
    if (value === "everyone") {
      row.rule_value = "";
    }
  }

  setRuleValue(group, index, value) {
    this.state[group][index].rule_value = value;
  }

  setDecision(decision) {
    this.state.decision = decision;
    if (decision === "allow") {
      // #352: switching Block/Bypass → Allow can leave a bulk-grant selector
      // sitting in the include group, which the backend now rejects. Rewrite
      // it the same way the bypass branch below rewrites identity selectors,
      // so the builder state stays submittable.
      for (const row of this.state.include) {
        if (row.rule_type === "email_domain" || row.rule_type === "everyone") {
          row.rule_type = "email_list";
          row.rule_value = "";
        }
      }
    }
    if (decision === "bypass") {
      // Bypass cannot use identity selectors — rewrite any email rule to a
      // non-identity selector so the builder state stays valid.
      for (const group of ["include", "require", "exclude"]) {
        for (const row of this.state[group]) {
          if (row.rule_type === "email_domain" || row.rule_type === "email_list") {
            row.rule_type = "ip_range";
            row.rule_value = "";
          }
        }
      }
    }
  }

  _buildGroup(rows) {
    return rows
      .filter((r) => r.rule_type === "everyone" || r.rule_value.trim())
      .map((r) => ({
        rule_type: r.rule_type,
        rule_value: r.rule_type === "everyone" ? "" : r.rule_value.trim(),
      }));
  }

  async confirm() {
    if (!this.state.name.trim()) {
      this.state.error = "Policy name is required";
      return;
    }
    const include = this._buildGroup(this.state.include);
    const require = this._buildGroup(this.state.require);
    const exclude = this._buildGroup(this.state.exclude);
    if (!include.length) {
      this.state.error = "At least one Include rule with a value is required";
      return;
    }
    this.state.saving = true;
    this.state.error = null;
    const vals = {
      name: this.state.name.trim(),
      decision: this.state.decision,
      include,
      require,
      exclude,
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
