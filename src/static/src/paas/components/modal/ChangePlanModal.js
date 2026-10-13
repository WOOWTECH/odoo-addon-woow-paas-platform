/** @odoo-module **/
import { Component, useState, onMounted, useRef, useEffect } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
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
 * 試用中的二階段確認（#36，Q9 改判為 P2）：判準是**目前是否在試用**
 * （`service.subscription.is_trial`）**單獨**成立，刻意不看目標方案有沒有 trial。
 * 原 Q9 裁決寫的是「trial→no-trial only」，但那個判準與後端實際行為不符——
 * 見下方 `willEndTrial` 的完整論證與守線測試連結。
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
 *   - usedRoutes (Number, optional): #1261，服務目前的 route 數（SA 才傳）。只用於
 *     方案卡的警告標註；降級被 route gate 擋時，後端錯誤訊息照常顯示在 `state.error`。
 *   - resizesResources (Boolean, optional): true 時多顯示「規格不同會重啟服務套用、
 *     儲存空間不會縮小」（#1158；僅 CS 父頁傳）。
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
    // #1261：服務目前的 route 數，原樣轉給 PlanSelector 標註「裝不下」的方案卡。
    // 由父頁注入（只有 SA 詳情頁傳），modal 自身仍不 branch service type。
    usedRoutes: { type: Number, optional: true },
    // #1261／#342：只有 Cloud Service 父頁傳 true——CS 的 vCPU／RAM／儲存空間只在
    // 建立時套用、change-plan 不會 redeploy，modal 要多講這一句（見 xml 同名那格）。
    resizesResources: { type: Boolean, optional: true },
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
      // #36 P2：試用中變更方案的二階段確認。false＝選擇階段，true＝警告階段。
      // 只是一個 view flag——切換它**不動** selectedPlanId，所以使用者從警告返回
      // 選擇階段時原本的選擇（含席數變體）原封不動（E2E 情境 2 驗這格）。
      confirmingTrialEnd: false,
    });
    // onMounted（非 onWillStart）：讓 modal 先掛載再載方案，使用者按下「變更方案」
    // 當下就看得到 modal 與 loading 態。onWillStart 會 block 首次 render，RPC 期間
    // 畫面毫無回饋（SA/SH 的 loadPlans 是 fetchTemplates→fetchPlans 兩支序列 RPC，
    // 空窗更明顯），且會讓 loadingPlans 的 spinner 分支成為永不可達的死碼。
    onMounted(() => this.loadPlans());

    // #1647：確認失敗時把錯誤訊息捲進可視範圍。訊息排在可捲動的 body 最後，而 body 的
    // 內容常比視窗高（選了有席數變體的方案會多出幾排席數選項）：1366×768、1440×900 時
    // 整塊落在可視範圍之外，按下確認像是沒有反應——換方案被擋時（配額放不下、席數／route
    // 超過新方案）租戶只靠這一則訊息知道沒換成、沒有扣款、該找誰。
    // 依賴用 `failureCount` 而不只看 ref：同一則訊息再出現一次（使用者捲走之後又按一次
    // 確認）時 DOM 可能完全沒變，仍然要捲。它不是 reactive state——只是給 effect 比對的值。
    this.errorRef = useRef("errorMessage");
    this.failureCount = 0;
    useEffect(
      (el) => {
        if (el) {
          el.scrollIntoView({ block: "nearest" });
        }
      },
      () => [this.errorRef.el, this.failureCount]
    );
  }

  async loadPlans() {
    this.state.loadingPlans = true;
    this.state.plansError = null;
    const res = await this.props.loadPlans();
    if (res.success) {
      this.state.plans = res.data;
    } else {
      this.state.plansError = res.error || _t("Failed to load plans");
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

  /**
   * 這次變更會不會「當場結束試用並開始計費」。
   *
   * 判準只看**目前訂閱是否在試用中**，不看目標方案的 trial 欄位——這是刻意的，
   * 而且與 issue #34 Q9 的字面裁決（"Trial→no-trial only"）不同。理由是後端行為：
   *
   *   change-plan 的三個 endpoint 都先 gate「entity 的 subscription_id 為 truthy
   *   且 state==='in_progress'」才往下走，而該欄位正是
   *   `subscription_lifecycle_mixin._create_and_confirm_subscription` 用來決定給不給
   *   試用的 `entity_has_prior_sub`。gate 與 guard 讀同一個欄位、中間沒有任何一段
   *   會清它 ⇒ 在 change-plan 路徑上 `entity_has_prior_sub` **結構性恆為 True**
   *   ⇒ `plan.sh_trial and not entity_has_prior_sub` 恆 False ⇒ 一律走
   *   `_force_active_dates_from_plan`（寫 `sh_trial=False`、`sh_start_date=today`）。
   *
   * 也就是說：**試用中換到「標榜有試用」的方案，一樣會當場結束試用並開首期帳單**。
   * 照 Q9 字面把 trial→trial 判成靜默，恰好會讓真正扣到錢的那條路徑不出聲。
   * 這個改判經 Eugene 裁定（#36），紀錄回寫於 #34 / #36。
   *
   * ⚠ 本元件的文案對使用者保證「一定會結束試用並產生首期帳單」，而那個「一定」
   * 是**後端**保證的、不是 UI 保證的。守住這個前提的測試：
   *   - CS：`src/tests/test_trial_reset_guard_367.py`
   *   - SA / SH：`src/tests/test_change_plan_ends_trial_36.py`
   * 若哪天有人改後端讓 trial→trial 保留試用，請一併改這裡的文案——否則它會變成謊言。
   */
  get willEndTrial() {
    return !!this.props.service.subscription?.is_trial;
  }

  /**
   * #773：這台服務目前已列出的席位數（去重後的 email 位址數）。
   *
   * 🔴 值由後端算好放在 `_get_subscription_info()` 的 `ac_used_access_seats`，
   * ⛔ 前端不自己數：payload 裡雖然另有 access_policies 原始陣列，但要從它算出席數
   * 得把「只計 email_list」「strip+lower 折疊」「跨 policy 去重」整套規則在瀏覽器
   * 重寫一次＝長出第三把尺，與後端 gate／寫入守衛必然漂移。
   *
   * 舊 payload（後端尚未升版）沒有這個 key ⇒ 退成 0，效果是「不標任何警告」，
   * 換方案照樣由後端 gate 把關，不會誤擋。
   */
  get usedSeats() {
    return this.props.service.subscription?.ac_used_access_seats || 0;
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

  /** 從警告階段返回選擇階段：不送出、選擇保留。 */
  onBackFromTrialWarning() {
    this.state.confirmingTrialEnd = false;
  }

  async onConfirm() {
    if (!this.canConfirm) return;

    // #36 P2 第一階段：試用中還沒看過警告 ⇒ 只切到警告階段，**不呼叫
    // confirmChange**。使用者按「返回」回選擇階段、按「確認」才會再次進到這裡，
    // 屆時 confirmingTrialEnd 已為 true、直接落到下方送出。
    if (this.willEndTrial && !this.state.confirmingTrialEnd) {
      this.state.confirmingTrialEnd = true;
      return;
    }

    this.state.changing = true;
    this.state.error = null;

    const res = await this.props.confirmChange(this.state.selectedPlanId);

    if (res.success) {
      // 帶 data（refund_amount / new_plan）供父頁運用；目前父頁只關 modal + reload。
      this.props.onChanged(res.data);
    } else {
      // 先記一次失敗再設訊息：setup 的 effect 據此把訊息捲進可視範圍（#1647）。
      this.failureCount += 1;
      this.state.error = res.error || _t("Failed to change plan");
      this.state.changing = false;
    }
  }
}
