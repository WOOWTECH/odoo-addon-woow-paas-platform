/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { PlanSelector } from "../subscription/PlanSelector";
import { PlanTotalBar } from "../subscription/PlanTotalBar";

/**
 * ChangePlanModal Component
 *
 * 讓已部署服務改方案／改席數的確認 modal，內嵌復用 PlanSelector（C2）——
 * 帶 AC 席數變體的方案會自動展開第二步席數選項，無變體則退化為單純選卡片。
 * 與 SkipTrialModal 同為 service-type-agnostic：由父頁注入 `loadPlans` /
 * `confirmChange` callback 指向對應後端（Cloud Service / Security Access /
 * Smart Home），modal 自身完全不 branch，故三 surface 共用同一元件。
 *
 * 退點說明：後端 change-plan 會在同一次呼叫內「關閉舊訂閱→按未使用比例退點
 * 至錢包→依新方案重新計費」。目前平台沒有 side-effect-free 的退點預覽 API
 * （退點金額 `refund_amount` 只在確認後的回應才回），故 modal 於確認前只顯示
 * 通用退點說明。成功後 `onChanged(data)` 會帶回後端的 refund_amount / new_plan
 * 供父頁運用，但本 PR 的三個父頁僅以它關 modal 並重載服務——**實際退點金額尚未
 * 對使用者揭露**（UI 揭露列為 follow-up，見 PR 描述）。
 *
 * Props:
 *   - service (Object): 顯示用，{ name, subscription: { plan_name, plan_price } }。
 *     modal 不從 service 推導請求對象——callback 已知道要打哪個後端。
 *   - currentPlanId (Number, optional): 目前方案 plan_id（來自 _get_subscription_info
 *     的新增欄位）。開啟即以它定位 PlanSelector 的高亮（cycle×group×seats 反推）並
 *     擋「churn 成同方案」（selectedPlanId===currentPlanId 時 canConfirm=false）。
 *     缺（無訂閱／無 plan）時退回未選態。
 *   - loadPlans (Function): () => Promise<ApiResponse<Plan[]>>。onMounted 時呼叫，
 *     回傳的每個 plan 可帶 variants=[{seats,plan_id,price}]。應為 side-effect-free。
 *   - confirmChange (Function): (planId) => Promise<ApiResponse<{refund_amount,...}>>。
 *     使用者確認時呼叫；會關舊訂閱／退點／開新訂閱。
 *   - onClose (Function): 取消／關閉時呼叫。
 *   - onChanged (Function): confirmChange 成功後呼叫，帶後端回傳 data
 *     （含 refund_amount / new_plan，供父頁選用）；父頁負責關 modal + reload 服務。
 */
export class ChangePlanModal extends Component {
  static template = "woow_paas_platform.ChangePlanModal";
  static components = { WoowButton, WoowIcon, PlanSelector, PlanTotalBar };
  static props = {
    service: { type: Object },
    currentPlanId: { type: Number, optional: true },
    loadPlans: { type: Function },
    confirmChange: { type: Function },
    onClose: { type: Function },
    onChanged: { type: Function },
  };

  setup() {
    this.state = useState({
      plans: [],
      // 開啟即定位目前方案（#368 PR-3）：selectedPlanId 初值＝currentPlanId。
      // PlanSelector 受 xml 的 t-if="!loadingPlans" 保護、於 plans 載入「後」才首次
      // 掛載，故其 setup 反推 cycle×group×seats 時 plans 已就緒、目前方案正確高亮。
      // currentPlanId 缺（無訂閱／無 plan）時退回 undefined＝未選態。用 undefined 而非
      // null——PlanSelector 的 selectedPlanId 是 optional Number，OWL props 驗證只對
      // undefined 放行（null 會被當非法 Number 型別而在 dev/assets 模式拋錯）；內部
      // `sel == null` 寬鬆比較同涵蓋 undefined，未選語意（無高亮、不展開席數）一致。
      selectedPlanId: this.props.currentPlanId ?? undefined,
      loadingPlans: true,
      plansError: null,
      changing: false,
      error: null,
    });
    // onMounted（非 onWillStart）：讓 modal 先掛載再載方案，使用者按下「變更方案」
    // 當下就看得到 modal 與 loading 態。onWillStart 會 block 首次 render，RPC 期間
    // 畫面毫無回饋（SA/SH 的 loadPlans 是 fetchTemplates→fetchPlans 兩支序列 RPC，
    // 空窗更明顯），且會讓 loadingPlans 的 spinner 分支成為永不可達的死碼。
    onMounted(() => this.loadPlans());
  }

  async loadPlans() {
    this.state.loadingPlans = true;
    this.state.plansError = null;
    const res = await this.props.loadPlans();
    if (res.success) {
      this.state.plans = res.data;
    } else {
      this.state.plansError = res.error || "Failed to load plans";
    }
    this.state.loadingPlans = false;
  }

  get currentPlanName() {
    // 變體 plan 名已內含席數（例「…+3access月繳」），故用 plan_name 即可顯示
    // 「目前方案（含席數）」，無需另取結構化席數欄位。
    return this.props.service.subscription?.plan_name || "—";
  }

  onSelectPlan(planId) {
    this.state.selectedPlanId = planId;
    this.state.error = null;
  }

  get canConfirm() {
    return (
      !this.state.loadingPlans &&
      !this.state.plansError &&
      !!this.state.selectedPlanId &&
      // 不可 churn 成同方案：定位後（selectedPlanId===currentPlanId）確認鈕維持
      // disabled，使用者需真的改選另一方案／席數才 arm。currentPlanId 缺時此條恆真
      // （undefined !== 任何 plan id），退化為單純「有選才可確認」。
      this.state.selectedPlanId !== this.props.currentPlanId &&
      !this.state.changing
    );
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.changing) {
      this.props.onClose();
    }
  }

  async onConfirm() {
    if (!this.canConfirm) return;

    this.state.changing = true;
    this.state.error = null;

    const res = await this.props.confirmChange(this.state.selectedPlanId);

    if (res.success) {
      // 帶 data（refund_amount / new_plan）供父頁運用；目前父頁只關 modal + reload。
      this.props.onChanged(res.data);
    } else {
      this.state.error = res.error || "Failed to change plan";
      this.state.changing = false;
    }
  }
}
