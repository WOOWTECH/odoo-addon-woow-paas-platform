/** @odoo-module **/
import { Component, useState, onWillUpdateProps } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { groupPlansByCycle } from "./plan_selector_utils";

/**
 * PlanSelector（#368 重設計）
 *
 * 頂部常駐 cycle toggle（月|年）＝view-state；只顯示當前 cycle 的每 tier 一張卡。
 * 選中帶 AC 席數變體的 base → 展開第二步席數（增量 chip；{0,1} 降級 checkbox）。
 * selectedPlanId（props）是唯一「選擇」事實源；state.cycle 是唯一 view-state，
 * 用 onWillUpdateProps 同步避免與 selectedPlanId desync。無 variants 行為與
 * 改動前 bit-for-bit 相同。退化規則 R1/R2/R3 全由資料驅動、零 service_type 分支。
 *
 * Props:
 *   - plans (Array): [{id,name,price,unit,group_key,specs,variants,...}]
 *   - selectedPlanId (Number|null): 目前選擇（base 或變體 plan_id）
 *   - onSelect (Function): 使用者選定時以最終 plan_id 呼叫
 *   - currentPlanId (Number, optional): PR-3 用（本版僅佔位、不消費）
 */
export class PlanSelector extends Component {
  static template = "woow_paas_platform.PlanSelector";
  static props = {
    plans: { type: Array },
    selectedPlanId: { type: Number, optional: true },
    onSelect: { type: Function },
    currentPlanId: { type: Number, optional: true },
    // #773：這台服務目前已用的席位數。optional ⇒ 建立流程（還沒有服務、必然 0 用量）
    // 的呼叫端不必傳，行為與本 PR 之前完全相同。
    usedSeats: { type: Number, optional: true },
  };

  setup() {
    // cycle 是唯一 view-state：初始由選中 plan 反推，無選中則 'month'。
    this.state = useState({
      cycle: this._cycleOf(this.props.selectedPlanId) || "month",
    });
    // desync 守線：父層在不 remount 下改 selectedPlanId 到另一 cycle 的 plan
    // （如 CreateSmartHomeModal 切模板、PR-3 定位目前方案）時同步 state.cycle。
    onWillUpdateProps((next) => {
      if (next.selectedPlanId !== this.props.selectedPlanId) {
        const c = this._cycleOf(next.selectedPlanId, next.plans);
        if (c) this.state.cycle = c;
      }
    });
  }

  // ---- cycle 反推（view-state 來源）----
  _cycleOf(planId, plans = this.props.plans) {
    if (planId == null) return null;
    for (const p of plans) {
      if (p.id === planId) return p.unit;
      if ((p.variants || []).some((v) => v.plan_id === planId)) return p.unit;
    }
    return null;
  }

  get cycles() {
    return [...new Set(this.props.plans.map((p) => p.unit))];
  }
  get showCycleToggle() {
    return this.cycles.length > 1; // R1：單一 cycle 不渲染 toggle
  }

  // 當前 cycle 的每 group 一張卡（groupPlansByCycle 已 by price 升序）
  get visibleGroups() {
    return groupPlansByCycle(this.props.plans)
      .map((g) => g.byCycle[this.state.cycle])
      .filter(Boolean);
  }

  onCycleToggle(cycle) {
    this.state.cycle = cycle;
    const sel = this.props.selectedPlanId;
    if (sel == null) return; // 未選態：只切顯示、不 emit（不 arm canConfirm）
    const owner = this._ownerBase(sel);
    if (!owner) return;
    const seats = this._selectedSeats(sel, owner);
    const target = this._findInCycle(owner.group_key, cycle, seats);
    if (target != null) this.props.onSelect(target); // 保留檔位（同 tier 同席數）
  }

  // ---- 選中反推（受控，selectedPlanId 是唯一事實源）----
  _ownerBase(planId) {
    if (planId == null) return null;
    for (const p of this.props.plans) {
      if (p.id === planId) return p;
      if ((p.variants || []).some((v) => v.plan_id === planId)) return p;
    }
    return null;
  }
  _selectedSeats(planId, base) {
    if (planId === base.id) return 0;
    const v = (base.variants || []).find((x) => x.plan_id === planId);
    return v ? v.seats : 0;
  }
  _findInCycle(groupKey, cycle, seats) {
    if (!groupKey) return null; // 無 xmlid 的獨立 group 不支援跨 cycle 對映
    const target = this.props.plans.find(
      (p) => p.group_key === groupKey && p.unit === cycle);
    if (!target) return null; // 該 group 在此 cycle 無 base（R1 已擋，理論不達）
    if (seats === 0) return target.id;
    const v = (target.variants || []).find((x) => x.seats === seats);
    return v ? v.plan_id : target.id; // 該席數在新 cycle 不存在 → 退 0 席 base
  }

  isSelected(base) {
    return this._ownerBase(this.props.selectedPlanId)?.id === base.id;
  }

  // 選中且帶變體的 base（決定席數第二步是否顯示）；R3 退化在此與 template 判斷
  selectedBaseWithVariants() {
    const owner = this._ownerBase(this.props.selectedPlanId);
    return owner && owner.variants && owner.variants.length ? owner : null;
  }

  // R3：variants 只有 1 席且 seats===1（mcp）→ 降級 checkbox
  isSeatCheckbox(base) {
    return (
      base.variants &&
      base.variants.length === 1 &&
      base.variants[0].seats === 1
    );
  }
  isSeatSelected(base, variant) {
    const target = variant ? variant.plan_id : base.id;
    return this.props.selectedPlanId === target;
  }

  _unitLabel(unit) {
    return unit === "month" ? _t("mo") : unit === "year" ? _t("yr") : unit;
  }
  formatPrice(plan) {
    return _t("%(amount)s pts/%(unit)s", {
      amount: plan.price,
      unit: this._unitLabel(plan.unit),
    });
  }
  formatTrial(plan) {
    if (!plan.has_trial) return "";
    // 「天」是唯一實際出現的試用單位，整句成為一個 msgid（CJK 語序需要），
    // 其他單位走泛用句型，輸出與改寫前逐字相同。
    if (plan.trial_unit === "day") {
      return _t("%s-day free trial", plan.trial_duration);
    }
    return _t("%(duration)s-%(unit)s free trial", {
      duration: plan.trial_duration,
      unit: plan.trial_unit,
    });
  }
  // 增量：一律 base.unit（年繳為年值；寫死 /月＝12 倍 bug）；不加購由 template 標「已含」
  /**
   * #773：這個席位選項裝不裝得下服務目前已列出的位址數？
   *
   * 只做**視覺標註、不 disable**：權威判定在後端 gate（四條換方案路徑共用同一把尺），
   * 前端若也決定「可不可以選」就等於第二個判定處，兩邊一旦漂移使用者會看到
   * 「畫面說可以但送出被擋」或反過來。標註的目的只是讓人**在按確認之前**就知道
   * 要先去刪，真正的攔截與可讀訊息仍由後端回。
   *
   * @param {number} seats 該選項提供的席位數（「No extra seats」傳 0）
   */
  isSeatOverCurrentUsage(seats) {
    return (this.props.usedSeats || 0) > seats;
  }

  formatSeatDelta(base, variant) {
    const delta = variant.price - base.price;
    return _t("+%(amount)s pts/%(unit)s", {
      amount: delta,
      unit: this._unitLabel(base.unit),
    });
  }

  onPlanClick(base) {
    this.props.onSelect(base.id); // 選 base＝回「不加購」基準；有變體時第二步隨即出現
  }
  onSeatSelect(base, variant) {
    this.props.onSelect(variant ? variant.plan_id : base.id);
  }
  onSeatCheckbox(base) {
    const v = base.variants[0];
    // 勾＝選變體、取消＝回 base；受控（依當前 selectedPlanId 決定 toggle 方向）
    this.props.onSelect(
      this.props.selectedPlanId === v.plan_id ? base.id : v.plan_id);
  }
}
