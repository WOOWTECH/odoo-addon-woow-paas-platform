/** @odoo-module **/
import { Component, useState, onWillStart } from "@odoo/owl";

/**
 * PlanSelector Component
 *
 * Displays subscription plan cards for user selection.
 *
 * Wave C（#330）：base plan 卡片上若帶 AC 席數變體，選中後於卡片下方
 * 展開第二步「AC 席數」選項；選「不加購」emit base plan id、選 n 席
 * emit 對應變體 plan_id。選中的 base 完全由 `selectedPlanId` 反推
 * （不維護內部 state）——無 variants 時退化為原本「emit plan.id」行為，
 * 與改動前 bit-for-bit 相同（Hermes/postgresql/HA PaaS 零感知）。
 *
 * Props:
 *   - plans (Array): Available plans
 *       [{ id, name, price, duration, unit, has_trial, trial_duration,
 *          trial_unit, variants }]
 *       其中 variants（選填、C1 後端回帶）＝
 *       [{ seats, plan_id, price }, ...]（seats 升序、絕對總價、無變體＝[]）
 *   - selectedPlanId (Number|null): 目前選定 plan id（可為 base 或變體）
 *   - onSelect (Function): 使用者選定時以最終 plan id 呼叫
 */
export class PlanSelector extends Component {
  static template = "woow_paas_platform.PlanSelector";
  static props = {
    plans: { type: Array },
    selectedPlanId: { type: Number, optional: true },
    onSelect: { type: Function },
  };

  _unitLabel(unit) {
    return unit === "month" ? "mo" : unit === "year" ? "yr" : unit;
  }

  formatPrice(plan) {
    return `${plan.price} pts/${this._unitLabel(plan.unit)}`;
  }

  formatTrial(plan) {
    if (!plan.has_trial) return "";
    const unit = plan.trial_unit === "day" ? "day" : plan.trial_unit;
    return `${plan.trial_duration}-${unit} free trial`;
  }

  /**
   * 找出目前 `selectedPlanId` 所屬的 base plan id：
   * 若它本身是某 base 卡片 id → 回該 id；若它是某 base 的變體 plan_id
   * → 回該 base 的 id；找不到（null / 未選）→ 回 null。
   * 讓 base 卡片在選了變體時仍維持高亮，並保持受控元件契約
   * （selectedPlanId 是唯一事實源）。
   */
  _ownerBaseId() {
    const sel = this.props.selectedPlanId;
    if (sel == null) return null;
    for (const plan of this.props.plans) {
      if (plan.id === sel) return plan.id;
      if ((plan.variants || []).some((v) => v.plan_id === sel)) return plan.id;
    }
    return null;
  }

  isSelected(plan) {
    return this._ownerBaseId() === plan.id;
  }

  /**
   * 目前選中、且帶席數變體的 base plan（決定第二步是否顯示）；
   * 無選中／選中的 base 無變體 → 回 null（第二步不渲染）。
   */
  selectedBaseWithVariants() {
    const baseId = this._ownerBaseId();
    if (baseId == null) return null;
    const base = this.props.plans.find((p) => p.id === baseId);
    return base && base.variants && base.variants.length ? base : null;
  }

  /**
   * 席數選項是否為目前選擇。variant=null 代表「不加購」＝base 自身。
   */
  isSeatSelected(basePlan, variant) {
    const target = variant ? variant.plan_id : basePlan.id;
    return this.props.selectedPlanId === target;
  }

  /**
   * 席數選項的價格顯示：一律用 base 的 unit（變體與 base 同 cycle），
   * variant.price 為絕對總價、「不加購」＝base 全價。
   */
  formatSeatPrice(basePlan, variant) {
    const price = variant ? variant.price : basePlan.price;
    return `${price} pts/${this._unitLabel(basePlan.unit)}`;
  }

  onPlanClick(plan) {
    // 選 base 卡片＝回到該方案「不加購」基準（emit base id）；
    // 有變體時第二步隨即出現供加購席數。
    this.props.onSelect(plan.id);
  }

  onSeatSelect(basePlan, variant) {
    // 不加購 → emit base id；選 n 席 → emit 對應變體 plan_id。
    this.props.onSelect(variant ? variant.plan_id : basePlan.id);
  }
}
