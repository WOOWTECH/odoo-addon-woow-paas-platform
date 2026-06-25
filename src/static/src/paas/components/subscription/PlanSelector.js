/** @odoo-module **/
import { Component, useState, onWillStart } from "@odoo/owl";

/**
 * PlanSelector Component
 *
 * Displays subscription plan cards for user selection.
 *
 * Props:
 *   - plans (Array): Available plans [{ id, name, price, duration, unit, has_trial, trial_duration, trial_unit }]
 *   - selectedPlanId (Number|null): Currently selected plan ID
 *   - onSelect (Function): Called with plan ID when user selects a plan
 */
export class PlanSelector extends Component {
  static template = "woow_paas_platform.PlanSelector";
  static props = {
    plans: { type: Array },
    selectedPlanId: { type: Number, optional: true },
    onSelect: { type: Function },
  };

  formatPrice(plan) {
    const unitLabel = plan.unit === "month" ? "mo" : plan.unit === "year" ? "yr" : plan.unit;
    return `${plan.price} pts/${unitLabel}`;
  }

  formatTrial(plan) {
    if (!plan.has_trial) return "";
    const unit = plan.trial_unit === "day" ? "day" : plan.trial_unit;
    return `${plan.trial_duration}-${unit} free trial`;
  }

  isSelected(plan) {
    return this.props.selectedPlanId === plan.id;
  }

  onPlanClick(plan) {
    this.props.onSelect(plan.id);
  }
}
