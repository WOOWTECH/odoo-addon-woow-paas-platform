/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { StatusBadge } from "../../components/common/StatusBadge";
import { SkipTrialModal } from "../../components/modal/SkipTrialModal";
import { ChangePlanModal } from "../../components/modal/ChangePlanModal";
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
 * - Tunnel info panel (Tunnel ID, Connector Type/ID, Route URL, Status, Uptime)
 * - Skip Trial button (visible during trial)
 * - Delete button with confirmation
 * - Connection guide section
 *
 * Props:
 *   - workspaceId (number): Parent workspace ID
 *   - homeId (number): Smart home ID to display
 */
export class SmartHomeDetailPage extends Component {
    static template = "woow_paas_platform.SmartHomeDetailPage";
    static components = {
        WoowCard, WoowIcon, WoowButton, StatusBadge, SkipTrialModal,
        ChangePlanModal,
    };
    static props = {
        workspaceId: { type: Number },
        homeId: { type: Number },
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
            this.state.error = result.error || "Failed to load smart home";
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
            { label: "Workspaces", path: "workspaces" },
            { label: ws?.name || "Workspace", path: `workspace/${this.props.workspaceId}` },
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

    get tunnelInfo() {
        if (!this.home) return [];
        return [
            { label: "Tunnel ID", value: this.home.tunnel_id || "N/A", icon: "vpn_key" },
            { label: "Connector Type", value: this.home.connector_type || "N/A", icon: "cable" },
            { label: "Connector ID", value: this.home.connector_id || "N/A", icon: "tag" },
            { label: "Route URL", value: this.displayUrl || "Not configured", icon: "link", isUrl: !!this.routeUrl },
            { label: "Status", value: this.home.state || "Unknown", icon: "info" },
            { label: "Uptime", value: this.home.uptime || "N/A", icon: "schedule" },
        ];
    }

    // Navigation
    goBack() {
        this.router.navigate(`workspace/${this.props.workspaceId}`);
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
            this.state.home = result.data;
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
                result.error || "Failed to refresh smart home status";
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
            this.state.deleteError = result.error || "Failed to delete smart home";
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
                error: t.error || "No Smart Home template available",
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
