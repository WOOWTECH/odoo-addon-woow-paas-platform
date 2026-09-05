/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";

// 🔴 後端 `_check_allow_policy_no_bulk_grant` 的 `_BULK_GRANT_RULE_TYPES`
// (src/models/security_access_access_policy.py) 在前端的**唯一**鏡像：allow 政策
// 的 include 群組不得使用的選擇器（#352 收 email_domain／everyone，#521 P-J 收
// ip_range／country）。四型都「放行無限多人卻計 0 人」，而登入者照樣吃 Cloudflare
// Access 的全平台共用席位池。
//
// 這裡是 builder 端的**便利過濾**，真正的 gate 在後端；但兩份漂開的症狀是「UI 讓
// 租戶選得到、送出必被拒」，租戶端沒有任何線索可循 ⇒ 由 repo-lint
// `bulk-grant-ruler-sync` 機械對帳（#543 的根因正是同一種「兩份清單分離」）。
// ⛔ 不要在本檔別處再寫一份型別字面量——`selectorOptionsFor` 與 `setDecision`
// 都必須讀這一份。
const BULK_GRANT_RULE_TYPES = ["email_domain", "everyone", "ip_range", "country"];

/**
 * AddPolicyModal Component
 *
 * A modal dialog for creating or editing an access policy.
 * P-rule: a policy is a CONTAINER of selector rules split into include (OR) /
 * require (AND) / exclude (NOT) groups, plus a decision (allow / deny / bypass).
 * Mirrors Cloudflare's native policy builder.
 *
 * When a `policy` prop is passed the modal is in edit mode (PUT), otherwise
 * create mode (POST). On success the parent reloads its detail via onSaved.
 *
 * #746：原本直接 import `securityAccessService`，等於把「policy 長什麼樣子」
 * （這個元件的職責）和「policy 存去哪裡」（呼叫端的職責）綁死在一起——Cloud
 * Service 的 policy 走的是 service-pinned 端點，同一支 hard import 服務不了它。
 * 改成注入單一 `savePolicy` callback：**這個元件不再認識任何 endpoint**，兩個
 * surface 各自把自己的 service wrapper 包成同一個簽章傳進來。
 *
 * Props:
 *   - policy (Object | null): existing policy when editing (include/require/
 *       exclude groups from to_dict)
 *   - savePolicy (Function): (vals, policyId | null) => Promise<{success, ...}>
 *       policyId 由本元件從 `policy` prop 推出來明給，不讓呼叫端從自己的 state
 *       再推一次（兩份「現在是不是編輯模式」的判準必然漂移）
 *   - onClose (Function) / onSaved (Function)
 */
export class AddPolicyModal extends Component {
  static template = "woow_paas_platform.AddPolicyModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    policy: { type: [Object, { value: null }], optional: true },
    savePolicy: { type: Function },
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
        label: _t("Include — match any (OR)"),
        hint: _t("Visitor passes this policy if they match ANY include rule."),
      },
      {
        key: "require",
        label: _t("Require — match all (AND)"),
        hint: _t("Visitor must additionally match EVERY require rule."),
      },
      {
        key: "exclude",
        label: _t("Exclude — match none (NOT)"),
        hint: _t("Visitor is rejected if they match ANY exclude rule."),
      },
    ];
  }

  // Selector options; identity-based ones (email) are hidden for bypass
  // policies because Cloudflare rejects them.
  get selectorOptions() {
    const all = [
      { value: "email_domain", label: _t("Emails ending in a domain"), identity: true },
      { value: "email_list", label: _t("List of emails"), identity: true },
      { value: "ip_range", label: _t("IP range (CIDR)"), identity: false },
      { value: "everyone", label: _t("Everyone"), identity: false },
      { value: "country", label: _t("Country"), identity: false },
    ];
    return this.state.decision === "bypass"
      ? all.filter((o) => !o.identity)
      : all;
  }

  // #352 / #521 P-J: an Allow policy's INCLUDE rules must not admit an
  // unlimited, uncounted crowd — seat-based Access plans count people by the
  // addresses in "List of emails" rules, while a whole-domain, everyone,
  // IP-range or country rule counts as zero yet still draws on the Cloudflare
  // Access seat pool shared by the entire platform. Mirrors the backend
  // constraint (_check_allow_policy_no_bulk_grant), which is the real gate —
  // this only keeps the builder from offering a choice the server will reject.
  // ⇒ after P-J the only Include selector left for an Allow policy is
  // "List of emails"; that narrowing is deliberate, not a bug.
  //
  // Scoped to include (OR = grants access). Require (AND) and Exclude (NOT)
  // only NARROW access, so "allow everyone at acme.com except evil.com" stays
  // expressible. Block/Bypass policies are unaffected — a Bypass policy skips
  // authentication and therefore consumes no seat.
  selectorOptionsFor(groupKey) {
    const options = this.selectorOptions;
    if (this.state.decision !== "allow" || groupKey !== "include") {
      return options;
    }
    return options.filter((o) => !BULK_GRANT_RULE_TYPES.includes(o.value));
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
        return { show: true, prefix: "@", placeholder: _t("acme.com"),
          hint: _t("Anyone with an email at this domain.") };
      case "email_list":
        return { show: true, prefix: "", placeholder: _t("a@x.com, b@x.com"),
          hint: _t("Comma-separated email addresses.") };
      case "ip_range":
        return { show: true, prefix: "", placeholder: "203.0.113.0/24",   // 純數字位址範例：Odoo 的抽取器丟棄不含字母的 msgid，包 _t() 只會是空包彈
          hint: _t("Visitors from this CIDR range.") };
      case "country":
        return { show: true, prefix: "", placeholder: _t("TW, US"),
          hint: _t("Comma-separated ISO-2 country codes.") };
      case "everyone":
        return { show: false, prefix: "", placeholder: "",
          hint: _t("Matches all visitors.") };
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
      // #352 / #521 P-J: switching Block/Bypass → Allow can leave a bulk-grant
      // selector sitting in the include group, which the backend rejects.
      // Rewrite it the same way the bypass branch below rewrites identity
      // selectors, so the builder state stays submittable.
      // ⛔ Reads BULK_GRANT_RULE_TYPES — do NOT re-list the types here: a
      // Bypass policy's include group is typically ip_range, so a stale literal
      // list would leave exactly the Bypass → Allow path producing a payload
      // the server refuses.
      for (const row of this.state.include) {
        if (BULK_GRANT_RULE_TYPES.includes(row.rule_type)) {
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
      this.state.error = _t("Policy name is required");
      return;
    }
    const include = this._buildGroup(this.state.include);
    const require = this._buildGroup(this.state.require);
    const exclude = this._buildGroup(this.state.exclude);
    if (!include.length) {
      this.state.error = _t("At least one Include rule with a value is required");
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
    const res = await this.props.savePolicy(
      vals, this.isEdit ? this.props.policy.id : null);
    this.state.saving = false;
    if (res.success) {
      this.props.onSaved();
    } else {
      this.state.error = res.error;
    }
  }
}
