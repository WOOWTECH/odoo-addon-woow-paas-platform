/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { StatusBadge } from "../../components/common/StatusBadge";
import { SkipTrialModal } from "../../components/modal/SkipTrialModal";
import { ChangePlanModal } from "../../components/modal/ChangePlanModal";
import { SmartHomeAccessTab } from "./tabs/SmartHomeAccessTab";
import { workspaceService } from "../../services/workspace_service";
import { getDomain } from "../../services/cloud_service";
import { breadcrumbService } from "../../services/breadcrumb_service";
import { router } from "../../core/router";
import { formatPoints } from "../../utils/billing_helpers";

// `provisionError`（重試**請求本身**失敗的訊息）仍有意義的 state：pending／error
// 時重試鈕還在，provisioning 時結果還沒出來。離開這三格＝那句訊息已經過期。
const RETRY_CONTEXT_STATES = ["pending", "error", "provisioning"];

/**
 * SmartHomeDetailPage Component
 *
 * Displays detailed information about a smart home instance including:
 * - Header with name and status badge
 * - Subscription info panel (trial badge, next bill date, plan name)
 * - Tunnel info panel (Tunnel ID, Connector Type/ID, Route URL, tunnel Status, Connector Opened)
 * - Skip Trial button (visible during trial)
 * - Provision / Retry setup button (visible while state is pending / error)
 * - Delete button with confirmation
 * - Connection guide section
 *
 * #768：長出頁籤（Overview / Access）。骨架逐字抄 ServiceDetailPage——header 與
 * modal 留在頁籤外（切到 Access 時仍看得到自己在哪一台、仍按得到 Skip Trial／
 * 變更方案／Delete），頁籤只切分頁內容。原本的 Overview 內容（Subscription／
 * Tunnel Information／Connection Guide）整段搬進 overview 分頁，一項都沒少。
 *
 * Props:
 *   - workspaceId (number): Parent workspace ID
 *   - homeId (number): Smart home ID to display
 *   - initialTab (string, optional): Initial tab to show ('overview' | 'access')
 */
export class SmartHomeDetailPage extends Component {
    static template = "woow_paas_platform.SmartHomeDetailPage";
    static components = {
        WoowCard, WoowIcon, WoowButton, StatusBadge, SkipTrialModal,
        ChangePlanModal, SmartHomeAccessTab,
    };
    static props = {
        workspaceId: { type: Number },
        homeId: { type: Number },
        initialTab: { type: String, optional: true },
    };

    setup() {
        this.state = useState({
            home: null,
            loading: true,
            error: null,
            showDeleteConfirm: false,
            confirmName: "",
            deleting: false,
            deleteError: null,
            refreshing: false,
            // #524：refresh 失敗的**專用**通道。刻意不共用 `state.error`——
            // 那一格在 template 是整頁的 "Unable to load smart home" 錯誤狀態
            // （`t-elif="state.error"` 會把整個內容區換掉），拿它報一次 refresh
            // 失敗等於把使用者踢出頁面，比原本的靜默還糟。
            refreshError: null,
            // Smart Home 開通重試：按下 Provision／Retry setup 的請求進行中。
            // 與 `refreshing` 分開——兩顆按鈕各自的 spinner 不能互相牽動。
            provisioning: false,
            // 重試**請求本身**失敗（權限、訂閱閘、網路／timeout）的專用通道，理由同
            // `refreshError`：不得借用整頁的 `state.error`。後端**收斂**失敗不走這裡
            // ——那時請求成功、記錄落 `error` ＋ `error_message`，由 header 下方的
            // 常駐錯誤橫幅顯示（重新整理頁面後也還看得到）。
            provisionError: null,
            showSkipTrialModal: false,
            showChangePlanModal: false,
            domain: "woowtech.io",
            currentTab: this.props.initialTab || "overview",
        });
        this.router = useState(router);
        this.pollingInterval = null;

        onMounted(async () => {
            this.state.domain = await getDomain();
            this.loadSmartHome();
        });

        onWillUnmount(() => {
            this.stopPolling();
        });
    }

    /**
     * @param {Object} [opts]
     * @param {boolean} [opts.silent=false] 背景重載（polling／重試之後對帳）：不切到
     *   整頁 loading、失敗也不把頁面換成 "Unable to load"。原本 polling 每 5 秒都走
     *   整頁 loading，`provisioning` 期間整個內容區（含剛按下的按鈕與錯誤訊息）會
     *   反覆消失再出現；開通重試會讓這個狀態變得常見，所以一併改掉。
     */
    async loadSmartHome({ silent = false } = {}) {
        if (!silent) {
            this.state.loading = true;
            this.state.error = null;
        }

        const result = await workspaceService.getSmartHome(
            this.props.workspaceId,
            this.props.homeId
        );

        if (result.success) {
            this.state.home = result.data;
            // 重試請求本身失敗（最常見是 client timeout）之後，後端可能仍把開通做完
            // ——polling 會把 state 帶到 `active`，Retry 鈕隨 canRetrySetup 消失，但
            // `provisionError` 若不清，就會有一句「逾時／失敗」永遠掛在一台 Active
            // 的 Smart Home 旁邊、而且沒有任何方式關掉。判準是「這次載入看到的
            // state 已經離開重試相關的三格」：pending／error（重試鈕還在，那句話仍
            // 是使用者需要的上下文）與 provisioning（結果還沒出來）都保留。
            if (!RETRY_CONTEXT_STATES.includes(result.data?.state)) {
                this.state.provisionError = null;
            }
            this._publishBreadcrumb();
            this.startPollingIfNeeded();
        } else if (!silent) {
            this.state.error = result.error || _t("Failed to load smart home");
        }

        if (!silent) {
            this.state.loading = false;
        }
    }

    /**
     * 麵包屑軌跡：Workspaces → {所屬工作區}(連回工作區) → {Smart Home 名稱}。
     * 工作區名從 workspaceService 清單快取取得，deep-link 缺快取時退守「Workspace」。
     */
    _publishBreadcrumb() {
        const ws = workspaceService.workspaces.find(w => w.id === this.props.workspaceId);
        breadcrumbService.set("smarthome-detail", [
            { label: _t("Workspaces"), path: "workspaces" },
            { label: ws?.name || _t("Workspace"), path: `workspace/${this.props.workspaceId}` },
            { label: this.state.home.name, path: null },
        ]);
    }

    startPollingIfNeeded() {
        const pendingStates = ["provisioning", "deleting"];
        if (this.state.home && pendingStates.includes(this.state.home.state)) {
            this.stopPolling();
            this.pollingInterval = setInterval(
                () => this.loadSmartHome({ silent: true }), 5000);
        } else {
            this.stopPolling();
        }
    }

    stopPolling() {
        if (this.pollingInterval) {
            clearInterval(this.pollingInterval);
            this.pollingInterval = null;
        }
    }

    get home() {
        return this.state.home;
    }

    get currentTab() {
        return this.state.currentTab;
    }

    /**
     * Access 頁籤的顯示判準（#768）。
     *
     * 逐字移植自 `pages/service/ServiceDetailPage.js` 的同名 getter（#746 設計
     * 拍板 6）：讀後端 payload 的 `subscription.ac_max_access_seats`——那是
     * **方案上限**，由 `_get_subscription_info()`（subscription_lifecycle_mixin）
     * 直接鏡射 `sh_subscription_plan_id.ac_max_access_seats`，不做任何推導。
     * 無方案時後端回 0，所以「沒方案」與「方案沒買席位」在這個判準下同義。
     *
     * 不得改判 template／state／有沒有 route：那是
     * capability-gate-single-source-field 明文禁止的第二判準，方向永遠是
     * 「後端放行、租戶看不到頁籤」＝修復不可達。席位是**方案**屬性。
     *
     * fail-closed（`?? 0`）：欄位缺席（舊快取、漏帶 subscription 的 payload）
     * 一律視同 0，寧可不顯示，也不要長出一個後端沒有對應設定的頁籤。
     */
    /**
     * Access 頁籤的 remount key。
     *
     * `SmartHomeAccessTab` 只在 setup 時從 `home.routes` seed 一次（它自己的寫入
     * 回應才是權威，見該元件 docstring），而本頁有兩條路會在頁籤**仍掛著**時換掉
     * `state.home`：header 的 Provision／Retry setup（`retrySetup`，按鈕在頁籤
     * 外、切到 Access 時照樣按得到）與 provisioning／deleting 期間的背景
     * polling（`loadSmartHome({ silent: true })` 不再切整頁 loading，所以也不再
     * 順便卸載頁籤）。不 remount 的話，一台 pending 的 SH 開通成功後，Access
     * 分頁仍停在「還沒有對外網址」直到使用者切走再切回來。
     *
     * 刻意只看「route 集合」與生命週期 `state`，不看 route 內容：頁籤自己的
     * 寫入（開關保護、改 policy）只改內容、不改 id 集合與 state，所以不會因為
     * 自己的寫入被重建、丟掉展開狀態；而開通／收斂一定伴隨 state 轉換或多出
     * primary route。
     */
    get accessTabKey() {
        const ids = (this.home?.routes || []).map((r) => r.id).join(",");
        return `${this.home?.state || ""}|${ids}`;
    }

    get hasAccessSeats() {
        return (this.home?.subscription?.ac_max_access_seats ?? 0) > 0;
    }

    /**
     * #1261 復原模式：方案沒有 Access 席位，但仍有 route 開著保護。
     *
     * 這一格是 prod 2026-09-29 實測出來的卡死狀態：有席位時開了保護（只留一條
     * Block policy），換到 0 席方案之後 route 仍 `is_protected`、Cloudflare Access
     * app 原樣留著 ⇒ 對外 403；而 `hasAccessSeats` 為假 ⇒ 頁籤與 `/access` 內容
     * 全部不顯示 ⇒ 租戶**關不掉保護**。後端 #1261 已在換方案時擋住新的這種記錄，
     * 本 getter 負責**已經卡住的那些**。
     *
     * ⚠ 這**不是** hasAccessSeats docstring 禁止的「第二能力判準」：那條禁的是
     * 用 template／state／route 去**放寬能力**（讓沒買席位的人設定 Access）。
     * 復原模式只放一個**收回**動作——頁籤以 `seatless` 掛載，只能把保護關掉，
     * 加 policy／改登入方式／開保護一律不給（見 SmartHomeAccessTab）。方向是
     * 「後端早已存在的設定，租戶至少拆得掉」，正好是那條規則要避免的「修復不可達」
     * 的反面。能力判準本身仍只有 `hasAccessSeats` 一個。
     *
     * 讀的是 detail payload 的 `routes[].is_protected`（`route.to_dict()`）。
     * `access_protection_lost` 的 route 照算：is_protected 仍是意圖，#803 cron
     * 會把 Access app 推回去。
     */
    get accessRecoveryMode() {
        return (
            !this.hasAccessSeats &&
            (this.home?.routes || []).some((r) => r.is_protected)
        );
    }

    get routeUrl() {
        if (!this.home?.subdomain) return null;
        return `https://${this.home.subdomain}.${this.state.domain}`;
    }

    get displayUrl() {
        if (!this.home?.subdomain) return null;
        return `${this.home.subdomain}.${this.state.domain}`;
    }

    /**
     * Provision／Retry setup 鈕的顯示判準——與後端
     * `smart_home._RETRYABLE_WITH_TUNNEL_STATES`（有 tunnel 時）及「沒有 tunnel 一律
     * 可開通」對齊：`pending`（從未開通、或建立視窗串接的那一次沒送出去）與
     * `error`（開通失敗，含 #484 `dns_failed` 留下的「tunnel 活著但 DNS 沒建成」）。
     * `active`／`suspended` 後端一律拒絕，顯示按鈕只會換來一句錯誤。
     */
    get canRetrySetup() {
        return ["pending", "error"].includes(this.home?.state);
    }

    get canDelete() {
        return this.state.confirmName.trim() === (this.home?.name || "");
    }

    /**
     * Tunnel Information 面板。
     *
     * 兩個欄位原本是錯的（與 SA 詳情頁 #1256 同一次對齊）：
     *   - Status 讀的是 `home.state`（生命週期：active／suspended…），不是
     *     tunnel。生命週期已經在 header 的 StatusBadge 上，這裡重複一次只會讓
     *     「Active」蓋掉「Connector 其實斷了」。改讀 `tunnel_status`，用同一顆
     *     StatusBadge（connected／disconnected／deleted／error 都有定義，#833）。
     *     沒有 tunnel（尚未 provision）時顯示 N/A，不假裝是 Disconnected。
     *   - Uptime 讀的是 `home.uptime`，而 `SmartHome.to_dict()` 的 key 是
     *     `tunnel_uptime` ⇒ 永遠 N/A。那個值是 connector 的 `opened_at` 時間戳、
     *     不是時長，標籤改成與 SA 頁相同的「Connector Opened」。
     *
     * 這些值由 detail `get` 疊上 operator 即時值（`_live_tunnel_status`，只讀）；
     * operator 拿不到時是 DB 裡上一次 Refresh 寫入的值。`retrySetup` 直接收的
     * provision 回應也帶同一份疊加（`_provision_smarthome`），否則重試成功的
     * 那一刻面板會倒退回舊值。
     */
    get tunnelInfo() {
        if (!this.home) return [];
        const hasTunnel = !!this.home.tunnel_id;
        return [
            { label: _t("Tunnel ID"), value: this.home.tunnel_id || _t("N/A"), icon: "vpn_key" },
            { label: _t("Connector Type"), value: this.home.connector_type || _t("N/A"), icon: "cable" },
            { label: _t("Connector ID"), value: this.home.connector_id || _t("N/A"), icon: "tag" },
            { label: _t("Route URL"), value: this.displayUrl || _t("Not configured"), icon: "link", isUrl: !!this.routeUrl },
            {
                label: _t("Status"),
                value: hasTunnel ? (this.home.tunnel_status || "disconnected") : _t("N/A"),
                icon: "info",
                isBadge: hasTunnel,
            },
            { label: _t("Connector Opened"), value: this.home.tunnel_uptime || _t("N/A"), icon: "schedule" },
        ];
    }

    // Navigation
    goBack() {
        this.router.navigate(`workspace/${this.props.workspaceId}`);
    }

    // 直接改 hash（不走 router.navigate）＝抄 ServiceDetailPage：切分頁不算一次
    // 導航，但網址仍要跟著變，重新整理／分享連結才回得到同一顆頁籤。對應的
    // 白名單在 core/router.js 的 smarthome-detail 帶 tab 那條。
    setTab(tab) {
        this.state.currentTab = tab;
        const basePath = `workspace/${this.props.workspaceId}/smarthome/${this.props.homeId}`;
        window.location.hash = `#/${basePath}/${tab}`;
    }

    // Refresh status
    async refreshStatus() {
        this.state.refreshing = true;
        this.state.refreshError = null;
        const result = await workspaceService.refreshSmartHomeStatus(
            this.props.workspaceId,
            this.props.homeId
        );
        if (result.success) {
            // #768：refresh 端點回的是 `to_dict()`（**沒有** include_details），
            // 也就是說它的 payload 裡根本沒有 `routes` 這個 key。直接整包覆蓋的
            // 話，Access 分頁的資料會被一次 Refresh 悄悄清空，畫面落到「這台
            // Smart Home 還沒有對外網址」——與真的沒有 route 完全不可區分。
            //
            // 判準是 key **在不在**，不是它的值真不真：
            //   - 沒這個 key ⇒ 這支端點不談 routes，沿用上一次 detail 的值；
            //   - 有這個 key（即使是 []）⇒ 伺服器明講了，照收。
            // 用 `data.routes || []` 之類的寫法會把兩者壓成同一格，那正是這裡
            // 要避免的事。
            this.state.home = Object.hasOwn(result.data, "routes")
                ? result.data
                : { ...result.data, routes: this.state.home?.routes || [] };
        } else {
            // #524：refresh_status 的後端 gate 從「全體成員」收窄到 owner/admin
            //（`action_refresh_status()` 內部是 self.write，而 smart_home 的 write
            // ir.rule 現在只發給 owner/admin）⇒ role=user / guest 會拿到
            // "Insufficient permissions"。
            //
            // 原本這裡**沒有 else**：失敗被整個吞掉，按鈕看起來只是沒反應。
            // 那正是本 repo 反覆記錄的那種失敗——「擋住了」與「壞掉了」對使用者
            // 輸出完全相同。至少要說出來。
            //
            // ⚠ 更完整的作法是依 role 直接把按鈕藏起來，但這個頁面今天完全沒有
            // role 的資料流（grep 不到任何 role/isOwner/canEdit），要接是另一個
            // 範圍的改動 —— 已回報，不在本 PR。
            this.state.refreshError =
                result.error || _t("Failed to refresh smart home status");
        }
        this.state.refreshing = false;
    }

    // Provision / Retry setup
    //
    // 形狀抄 SecurityAccessDetailPage.provision：打既有的 provision action，拿回
    // detail 形狀（含 routes）直接收。後端對「已有 tunnel」的 pending／error 走收斂
    // （同一條 tunnel 補 ingress／DNS，不會再開一條），所以重按是安全的。
    async retrySetup() {
        if (this.state.provisioning) return;
        this.state.provisioning = true;
        this.state.provisionError = null;
        const result = await workspaceService.provisionSmartHome(
            this.props.workspaceId,
            this.props.homeId
        );
        if (result.success) {
            // 端點成功不代表開通成功：operator 失敗時後端把記錄落 `error` ＋
            // `error_message` 並照樣回 success（controllers/smart_home.py
            // `_provision_smarthome`）。那一格由常駐錯誤橫幅顯示，這裡只收資料。
            this.state.home = result.data;
            this.startPollingIfNeeded();
        } else {
            this.state.provisionError =
                result.error || _t("Failed to start Smart Home setup");
            // 請求失敗不等於伺服器沒做：client timeout（150 秒）時後端可能仍在跑，
            // 記錄停在 `provisioning`。背景重載一次，讓畫面與 polling 跟上真實 state。
            await this.loadSmartHome({ silent: true });
        }
        this.state.provisioning = false;
    }

    // Delete confirmation
    showDeleteConfirm() {
        this.state.showDeleteConfirm = true;
        this.state.confirmName = "";
        this.state.deleteError = null;
    }

    hideDeleteConfirm() {
        this.state.showDeleteConfirm = false;
        this.state.confirmName = "";
        this.state.deleteError = null;
    }

    onConfirmNameInput(ev) {
        this.state.confirmName = ev.target.value;
        this.state.deleteError = null;
    }

    onDeleteBackdropClick(ev) {
        if (ev.target === ev.currentTarget && !this.state.deleting) {
            this.hideDeleteConfirm();
        }
    }

    async onDeleteConfirm() {
        if (!this.canDelete || this.state.deleting) return;

        this.state.deleting = true;
        this.state.deleteError = null;

        const result = await workspaceService.deleteSmartHome(
            this.props.workspaceId,
            this.props.homeId
        );

        if (result.success) {
            this.hideDeleteConfirm();
            this.goBack();
        } else {
            this.state.deleteError = result.error || _t("Failed to delete smart home");
            this.state.deleting = false;
        }
    }

    // -- Subscription / Skip Trial -------------------------------------

    get subscription() {
        return this.home?.subscription || null;
    }

    get isTrialActive() {
        return !!(this.subscription && this.subscription.is_trial);
    }

    get formattedPlanPrice() {
        const sub = this.subscription;
        if (!sub) return "";
        return formatPoints(sub.plan_price || 0);
    }

    showSkipTrial() {
        this.state.showSkipTrialModal = true;
    }

    hideSkipTrial() {
        this.state.showSkipTrialModal = false;
    }

    onTrialSkipped() {
        this.hideSkipTrial();
        this.loadSmartHome();
    }

    // Skip-trial callbacks bound to workspaceService — reuses the same
    // SkipTrialModal as ServiceDetailPage. The modal stays service-type
    // agnostic by accepting these as props.
    loadSmartHomeEstimate = () => {
        return workspaceService.getSkipTrialEstimateSmartHome(
            this.props.workspaceId, this.props.homeId,
        );
    };

    confirmSmartHomeSkip = () => {
        return workspaceService.skipTrialSmartHome(
            this.props.workspaceId, this.props.homeId,
        );
    };

    // -- Change Plan（改方案／改席數，復用 ChangePlanModal + PlanSelector）----

    showChangePlan() {
        this.state.showChangePlanModal = true;
    }

    hideChangePlan() {
        this.state.showChangePlanModal = false;
    }

    onPlanChanged() {
        this.hideChangePlan();
        this.loadSmartHome();
    }

    // SH 記錄無 template FK，故循 CreateSmartHomeModal 路徑先取唯一 SH template
    // （本 surface 僅一個）再抓 plans；plans 已含 C1 席數變體。confirmChange 打
    // 本 PR 於 workspace_service 補上的 changeSmartHomePlan client。
    loadSmartHomePlans = async () => {
        const t = await workspaceService.fetchSmartHomeTemplates();
        if (!t.success || !t.data.length) {
            return {
                success: false,
                error: t.error || _t("No Smart Home template available"),
            };
        }
        return workspaceService.fetchSmartHomePlans(t.data[0].id);
    };

    confirmSmartHomeChangePlan = (planId) => {
        return workspaceService.changeSmartHomePlan(
            this.props.workspaceId, this.props.homeId, planId,
        );
    };
}
