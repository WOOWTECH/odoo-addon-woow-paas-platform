/** @odoo-module **/
import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

/**
 * SubscriptionBadge Component
 *
 * Displays a color-coded badge for subscription status.
 *
 * Props:
 *   - subscription (Object|null): Subscription info from API
 *     { state, is_trial, plan_name, next_payment, ... }
 */
export class SubscriptionBadge extends Component {
  static template = "woow_paas_platform.SubscriptionBadge";
  static props = {
    subscription: { type: Object, optional: true },
  };

  get label() {
    const sub = this.props.subscription;
    if (!sub) return "";
    if (sub.state === "in_progress" && sub.is_trial) return _t("Trial");
    if (sub.state === "in_progress") return _t("Active");
    if (sub.state === "expire" || sub.state === "close") return _t("Expired");
    if (sub.state === "cancel") return _t("Cancelled");
    if (sub.state === "renewed") return _t("Renewed");
    if (sub.state === "draft") return _t("Draft");
    return sub.state || "";
  }

  get badgeClass() {
    const sub = this.props.subscription;
    if (!sub) return "";
    if (sub.state === "in_progress" && sub.is_trial) return "o_woow_sub_badge--trial";
    if (sub.state === "in_progress") return "o_woow_sub_badge--active";
    if (sub.state === "expire" || sub.state === "close") return "o_woow_sub_badge--expired";
    if (sub.state === "cancel") return "o_woow_sub_badge--cancelled";
    if (sub.state === "renewed") return "o_woow_sub_badge--active";
    return "o_woow_sub_badge--default";
  }
}
