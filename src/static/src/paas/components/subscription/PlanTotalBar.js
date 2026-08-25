/** @odoo-module **/
import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { resolveSelected } from "./plan_selector_utils";

/**
 * PlanTotalBar — 展示型共用元件，顯示目前選擇的絕對總價。
 * 4 個嵌入點（AppConfigurationPage sticky footer / 三個 modal footer）內嵌它，
 * 總價「呈現」集中一份，避免 4× markup 漂移。無 state、純由 props 反推。
 *
 * Props:
 *   - plans (Array): 同 PlanSelector 的 plans
 *   - selectedPlanId (Number, optional): 目前選擇（base 或變體 plan_id）
 *   - mode (String, optional): 'change'＝改方案情境（label 用「新方案總計」）
 */
export class PlanTotalBar extends Component {
  static template = "woow_paas_platform.PlanTotalBar";
  static props = {
    plans: { type: Array },
    selectedPlanId: { type: Number, optional: true },
    mode: { type: String, optional: true },
  };

  _unitLabel(unit) {
    return unit === "month" ? _t("mo") : unit === "year" ? _t("yr") : unit;
  }

  get selected() {
    return resolveSelected(this.props.plans, this.props.selectedPlanId);
  }
}
