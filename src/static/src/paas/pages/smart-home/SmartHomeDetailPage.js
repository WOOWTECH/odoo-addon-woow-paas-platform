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

/**
 * SmartHomeDetailPage Component
 *
 * Displays detailed information about a smart home instance including:
 * - Header with name and status badge
 * - Subscription info panel (trial badge, next bill date, plan name)
 * - Tunnel info panel (Tunnel ID, Connector Type/ID, Route URL, tunnel Status, Connector Opened)
 * - Skip Trial button (visible during trial)
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

    async loadSmartHome() {
        this.state.loading = true;
        this.state.error = null;

        const result = await workspaceService.getSmartHome(
            this.props.workspaceId,
            this.props.homeId
        );

        if (result.success) {
            this.state.home = result.data;
            this._publishBreadcrumb();
            this.startPollingIfNeeded();
        } else {
            this.state.error = result.error || _t("Failed to load smart home");
        }

        this.state.loading = false;
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
            this.pollingInterval = setInterval(() => this.loadSmartHome(), 5000);
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
    get hasAccessSeats() {
        return (this.home?.subscription?.ac_max_access_seats ?? 0) > 0;
    }

    get routeUrl() {
        if (!this.home?.subdomain) return null;
        return `https://${this.home.subdomain}.${this.state.domain}`;
    }

    get displayUrl() {
        if (!this.home?.subdomain) return null;
        return `${this.home.subdomain}.${this.state.domain}`;
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
     * operator 拿不到時是 DB 裡上一次 Refresh 寫入的值。
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
