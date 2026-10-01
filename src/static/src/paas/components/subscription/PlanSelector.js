/** @odoo-module **/
import { Component, useState, onWillUpdateProps } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { groupPlansByCycle } from "./plan_selector_utils";
import { formatPointsPerUnit } from "../../utils/billing_helpers";

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
    // #1261：這台服務目前的 route 數（只有 Security Access 會傳）。optional ⇒
    // 建立流程與 CS／SH 不傳，卡片不標任何 route 警告，行為與本 PR 之前完全相同。
    usedRoutes: { type: Number, optional: true },
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
  /**
   * #1310：年繳折扣原本寫死「Save 33%」，但各方案不同（Odoo 1,500／12,000＝33%，
   * pi Agent 4,500／45,000＝17%）。依同 group_key 的月／年價實算；
   * 各組折扣不同時顯示「最多省 N%」，算不出來就只顯示「Yearly」。
   */
  get yearlySavingsLabel() {
    const savings = [];
    for (const yearly of this.props.plans.filter((p) => p.unit === "year" && p.group_key)) {
      const monthly = this.props.plans.find(
        (p) => p.group_key === yearly.group_key && p.unit === "month");
      if (monthly && monthly.price > 0 && yearly.price > 0) {
        const pct = Math.round((1 - yearly.price / (monthly.price * 12)) * 100);
        if (pct > 0) savings.push(pct);
      }
    }
    if (!savings.length) return _t("Yearly");
    const max = Math.max(...savings);
    return Math.min(...savings) === max
      ? _t("Yearly · Save %(pct)s", { pct: `${max}%` })
      : _t("Yearly · Save up to %(pct)s", { pct: `${max}%` });
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

  // #1310：價格／單位字樣統一走 billing_helpers.formatPointsPerUnit（千分位跟隨語系、
  // 單位縮寫與 PlanTotalBar／marketplace 卡片同一份）。
  formatPrice(plan) {
    return formatPointsPerUnit(plan.price, plan.unit);
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

  /**
   * #773：裝不下時要顯示的那一句。
   *
   * 🔴 整句在 JS 組好再讓模板 `t-esc`，⛔ 不在 XML 裡把數字用 `<t t-esc/>` 夾在
   * 文字中間——那會讓抽取器把一句話切成兩個殘缺 msgid（「Delete all」「listed
   * email address(es) first」），翻譯時無法組回通順的句子。
   * 參數化形式照本 repo 既有慣例 `_t("%(x)s …", { x })`（見 utils/billing_helpers.js）。
   */
  overUsageHint(seats) {
    const used = this.props.usedSeats || 0;
    return seats
      ? _t("Currently using %(used)s — delete some first", { used })
      : _t("Delete all %(used)s listed email address(es) first", { used });
  }

  /**
   * #1261：這張方案卡的 route 上限裝不裝得下服務目前的 route 數？
   *
   * 與 `isSeatOverCurrentUsage`（#773）同一個決定：只做**視覺標註、不 disable**，
   * 權威判定在後端 `_route_gate_denial`（換方案端點在退點之前擋）。前端若也決定
   * 「可不可以選」就是第二個判定處，漂移時會出現「畫面說可以但送出被擋」或反過來。
   *
   * `max_routes` 只有 SA 的 plans API 會帶（`sa_max_routes`，**0 ＝一條都不准**，
   * 不是 unlimited——與後端 gate 同一個 `>` 比較子）；CS／SH 的 plan 沒有這個 key、
   * 或呼叫端沒傳 `usedRoutes` ⇒ 一律不標。
   * 席數變體沿用 base 卡的判定：變體只加 Access 席位，route 上限跟著 base tier 走。
   */
  isPlanOverRouteUsage(plan) {
    const used = this.props.usedRoutes;
    const max = plan.max_routes;
    return typeof used === "number" && typeof max === "number" && used > max;
  }

  /**
   * #1261：route 裝不下時的那一句。整句在 JS 組好再 `t-esc`（理由同 `overUsageHint`：
   * 數字夾在 XML 文字中間會把一句話切成殘缺 msgid）。
   */
  routeOverUsageHint(plan) {
    const used = this.props.usedRoutes || 0;
    const max = plan.max_routes;
    return max
      ? _t("Allows %(max)s route(s); you have %(used)s — delete some first", { max, used })
      : _t("Delete all %(used)s route(s) first", { used });
  }

  formatSeatDelta(base, variant) {
    const delta = variant.price - base.price;
    return formatPointsPerUnit(delta, base.unit, { signed: true });
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
