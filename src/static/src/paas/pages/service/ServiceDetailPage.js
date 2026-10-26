/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { BrandIcon } from "../../components/brand-icon/BrandIcon";
import { templateBrand } from "../../utils/brand_icons";
import { WoowButton } from "../../components/button/WoowButton";
import { StatusBadge } from "../../components/common/StatusBadge";
import { OverviewTab } from "./tabs/OverviewTab";
import { ConfigurationTab } from "./tabs/ConfigurationTab";
import { InformationTab } from "./tabs/InformationTab";
import { LogsTab } from "./tabs/LogsTab";
import { AddonsTab } from "./tabs/AddonsTab";
import { McpServersTab } from "./tabs/McpServersTab";
import { AccessTab } from "./tabs/AccessTab";
import { DeleteServiceModal } from "../../components/modal/DeleteServiceModal";
import { RollbackModal } from "../../components/modal/RollbackModal";
import { RestartServiceModal } from "../../components/modal/RestartServiceModal";
import { SkipTrialModal } from "../../components/modal/SkipTrialModal";
import { EditDomainModal } from "../../components/modal/EditDomainModal";
import { ChangePlanModal } from "../../components/modal/ChangePlanModal";
import { RenewSubscriptionModal } from "../../components/modal/RenewSubscriptionModal";
import { CreateProjectModal } from "../../components/modal/CreateProjectModal";
import { OneTimeCredentialsModal, buildOneTimeCredentials } from "../../components/modal/OneTimeCredentialsModal";
import { AdminCredentialsModal } from "../../components/modal/AdminCredentialsModal";
import { ResetAdminPasswordModal } from "../../components/modal/ResetAdminPasswordModal";
import { DataRetrievalModal } from "../../components/modal/DataRetrievalModal";
import { serviceRetrievalEndpoint } from "../../services/data_retrieval";
import { cloudService } from "../../services/cloud_service";
import { useServiceResources } from "../../services/service_resources";
import { supportService } from "../../services/support_service";
import { breadcrumbService } from "../../services/breadcrumb_service";
import { workspaceService } from "../../services/workspace_service";
import { router } from "../../core/router";
import { formatUserDate } from "../../utils/datetime_helpers";
import { showProjects } from "../../core/platform_config";
import { formatUserDateTime } from "../../utils/datetime_helpers";
import {
    getInitFailureText,
    getStaleObservation,
    getStaleObservationLabel,
    getSupportProjectAction,
    getWorkloadFaultText,
} from "../../utils/service_presentation";

/**
 * ServiceDetailPage Component
 *
 * Displays detailed information about a cloud service with tabbed navigation.
 * Features:
 * - Service header with icon, name, and status
 * - Tab navigation (Overview, Configuration, Information, Logs, capability-specific tabs)
 * - Action buttons (Open Web UI, Rollback, Delete)
 * - Status polling for deploying/upgrading states
 *
 * Props:
 *   - workspaceId (number): Parent workspace ID
 *   - serviceId (number): Service ID to display
 *   - initialTab (string, optional): Initial tab from the service detail route
 */
export class ServiceDetailPage extends Component {
    static template = "woow_paas_platform.ServiceDetailPage";
    static components = {
        WoowCard,
        WoowIcon, BrandIcon,
        WoowButton,
        StatusBadge,
        OverviewTab,
        ConfigurationTab,
        InformationTab,
        LogsTab,
        AddonsTab,
        McpServersTab,
        AccessTab,
        DeleteServiceModal,
        RollbackModal,
        RestartServiceModal,
        SkipTrialModal,
        EditDomainModal,
        ChangePlanModal,
        RenewSubscriptionModal,
        CreateProjectModal,
        OneTimeCredentialsModal,
        AdminCredentialsModal,
        ResetAdminPasswordModal,
        DataRetrievalModal,
    };
    static props = {
        workspaceId: { type: Number },
        serviceId: { type: Number },
        initialTab: { type: String, optional: true },
    };

    setup() {
        this.state = useState({
            service: null,
            loading: true,
            error: null,
            currentTab: this.props.initialTab || "overview",
            showDeleteModal: false,
            showRollbackModal: false,
            showRestartModal: false,
            showSkipTrialModal: false,
            showDomainModal: false,
            showChangePlanModal: false,
            // #1611：自助續訂（訂閱取消／到期、服務還在保留期內）。
            showRenewModal: false,
            showCreateProjectModal: false,
            supportProject: null,
            supportProjectLoading: false,
            // #530：一次性憑證由單一字串改成具名清單（Admin Password ＋ Gateway
            // API Key ＋ 完整 gateway 位址）。null＝不開視窗。
            oneTimeCredentials: null,
            resetPasswordError: null,
            showAdminCredentialsModal: false,
            // #1560：重設管理員密碼的確認框。
            showResetConfirmModal: false,
            // #1540：停權後的限時唯讀取回入口。
            showDataRetrievalModal: false,
        });
        this.router = useState(router);
        this.resources = useServiceResources(this.props.workspaceId, () => [this.props.serviceId]);
        this.pollingInterval = null;

        onMounted(() => {
            this.loadService();
            // #275：Projects 關閉時沒有 project 入口，不讀取更不建立隱藏 project。
            if (this.projectsVisible) {
                this.loadSupportProject();
            }
        });

        onWillUnmount(() => {
            this.stopPolling();
        });
    }

    /**
     * @param {{silent?: boolean}} [options] `silent`：背景輪詢用——不切到整頁的
     *   「Loading service...」、失敗時保留目前畫面（#1456：原本每 5 秒整頁閃一次，
     *   配置頁編輯到一半也會被洗掉）。
     */
    async loadService({ silent = false } = {}) {
        if (!silent) {
            this.state.loading = true;
            this.state.error = null;
        }

        const result = await cloudService.fetchService(
            this.props.workspaceId,
            this.props.serviceId
        );

        if (result.success) {
            this.state.service = result.data;
            this.state.error = null;
            this._publishBreadcrumb();
            this.startPollingIfNeeded();
        } else if (!silent) {
            this.state.error = result.error || _t("Failed to load service");
        }

        if (!silent) {
            this.state.loading = false;
        }
    }

    /**
     * 麵包屑軌跡：Workspaces → {所屬工作區}(連回該工作區) → {服務名}。
     * 中間段連回服務真正所屬的工作區（服務的唯一入口來源），修正舊版父層
     * 標籤寫死「Services」、卻導去工作區詳情、文字與落點對不上的問題。
     * 服務序列化未必帶 workspace 名稱，缺時退守泛用標籤「Workspace」。
     */
    _publishBreadcrumb() {
        const service = this.state.service;
        if (!service) return;
        const wsId = this.props.workspaceId;
        const known = service.workspace_name || workspaceService.cachedWorkspaceName(wsId);
        breadcrumbService.set("service-detail", [
            { label: _t("Workspaces"), path: "workspaces" },
            { label: known || _t("Workspace"), path: `workspace/${wsId}` },
            { label: service.name, path: null },
        ]);
        // X-LC-001-4／FU2 r139：服務序列化不帶工作區名稱，deep-link 時補讀一次。
        if (!known) {
            workspaceService.resolveWorkspaceName(wsId).then(
                (name) => breadcrumbService.relabelWorkspace("service-detail", wsId, name));
        }
    }

    /**
     * 轉移中每 5 秒、其餘狀態每 60 秒背景重讀一次。
     *
     * #1456：原本只有轉移中才輪詢，頁面開著時服務狀態（含別人做的操作、停權）
     * 一直停在進頁那一刻，R8 實測 24–60 分鐘不變。重讀的是平台最近一次觀測，
     * 不會觸發任何探測（list/get 純讀，#1159）。
     */
    startPollingIfNeeded() {
        const pendingStates = ["deploying", "initializing", "upgrading", "deleting"];
        if (!this.state.service) {
            this.stopPolling();
            return;
        }
        // #1611：續訂完成、服務恢復中（後端 renewal.resuming）也用 5 秒，恢復後馬上看得到。
        const fast =
            pendingStates.includes(this.state.service.state) ||
            this.state.service.renewal?.resuming === true;
        const interval = fast ? 5000 : 60000;
        if (this.pollingInterval && this.pollingIntervalMs === interval) {
            return;
        }
        this.stopPolling(); // Clear any existing interval
        this.pollingIntervalMs = interval;
        this.pollingInterval = setInterval(() => {
            // 對話框開著時不換掉 service（確認內容不該在使用者眼前變動）；下一輪再讀。
            if (!this.isModalOpen) {
                this.loadService({ silent: true });
            }
        }, interval);
    }

    get isModalOpen() {
        const s = this.state;
        return Object.keys(s).some((k) => /^show\w*Modal$/.test(k) && s[k]) || !!s.oneTimeCredentials;
    }

    stopPolling() {
        if (this.pollingInterval) {
            clearInterval(this.pollingInterval);
            this.pollingInterval = null;
            this.pollingIntervalMs = null;
        }
    }

    get service() {
        return this.state.service;
    }

    get currentTab() {
        return this.state.currentTab;
    }

    get publicUrl() {
        if (!this.service?.subdomain) return null;
        return `https://${this.service.subdomain}.${cloudService.domain}`;
    }

    get domain() {
        return cloudService.domain;
    }

    /**
     * #1560：呼叫者能不能操作這台服務（owner／admin）。讀 detail payload 的
     * `viewer_can_manage`（＝後端 `_can_manage_service`，與各寫入端點的 role gate
     * 同一個判準），前端不自己拿 role 字串再算一份。
     *
     * fail-closed：欄位缺席（舊快取、list payload）一律當作不能管理——寧可少顯示
     * 按鈕，也不要讓 user／guest 看到按了必然被拒的操作（原本的 N5）。
     */
    get canManageService() {
        return this.service?.viewer_can_manage === true;
    }

    get canPerformActions() {
        // Disable actions when service is in transitional state
        const disabledStates = ["deploying", "initializing", "upgrading", "deleting", "pending"];
        return this.service && !disabledStates.includes(this.service.state);
    }

    /**
     * #1159 Q30：initializing 期間也可以刪除（後端本來就允許）；確認框會註明
     * 「已送出的初始化可能仍在叢集內完成」（DeleteServiceModal）。
     */
    get canDelete() {
        return this.canPerformActions || this.service?.state === "initializing";
    }

    /**
     * #1159 Q30：error 時停用 Reset／Admin Credentials——後端只接受 running
     * （`_reset_admin_password`），在 error 上按下只會換來一個失敗訊息。
     */
    get canResetAdminCredentials() {
        return this.canPerformActions && this.service.state !== "error";
    }

    /**
     * #1159：init 失敗由共用 presentation helper 固定呈現，不得把含 Pod 名稱或
     * operator 細節的 error_message 帶到租戶畫面。
     */
    get initFailureText() {
        return getInitFailureText(this.service);
    }

    get initFailed() {
        return !!this.initFailureText;
    }

    /**
     * #1647：服務顯示 running、但平台確認它沒有在執行時的固定文案（否則 null）。
     * 只讀 payload 的 workload_fault；叢集的原始訊息不出後台。
     */
    get workloadFaultText() {
        return getWorkloadFaultText(this.service);
    }

    /**
     * #1159 Q40：list/get 只回背景 poll 的最近觀測。轉移中且 age 超過 3 分鐘才顯示。
     * observation_pending 時的 age 是本次 episode 等待時間，不能稱為最後真觀測。
     */
    get staleObservation() {
        return getStaleObservation(this.service);
    }

    get staleObservationLabel() {
        return getStaleObservationLabel(this.staleObservation);
    }

    get supportProjectAction() {
        return getSupportProjectAction({
            projectsVisible: this.projectsVisible,
            supportProject: this.state.supportProject,
            supportProjectLoading: this.state.supportProjectLoading,
            createModalOpen: this.state.showCreateProjectModal,
        });
    }

    contactSupport() {
        if (this.supportProjectAction === "open") {
            this.openSupportProject();
        } else if (this.supportProjectAction === "create") {
            this.showCreateProject();
        }
    }

    get isOdooService() {
        return this.service?.template?.slug === 'odoo';
    }

    /**
     * Reset 密碼按鈕的顯示判準。
     *
     * 讀模板的 admin_reset_supported 欄位——與後端 reset endpoint 的能力 gate
     * （controllers/paas.py 的 `service.template_id.admin_reset_supported`）收斂到
     * 同一個欄位。原本這裡是硬編碼的三筆 slug 白名單
     * （mcp-odoo／mcp-odoo-manage／mcp-n8n），與後端不一致：8 個
     * has_admin_gui=True 的模板有 5 個後端放行、前端卻按不到按鈕，修復對租戶
     * 不可達（#460 / F-449-4）。**不得再退回 slug 判準**。
     * 不直接讀 has_admin_gui：app_managed 的模板（omnigent）開通時照樣供給初始
     * 密碼，但之後密碼由 app 自己保管，平台重設不了——按鈕不該出現。
     *
     * 缺欄位（舊快取、尚未更新的 payload）一律視同 False——fail-closed：寧可
     * 不顯示，也不要長出一顆按了必然失敗的按鈕。
     */
    get adminResetSupported() {
        return this.service?.template?.admin_reset_supported === true;
    }

    /**
     * #530：這台服務的 Reset 會不會**一併換發 Gateway API Key**。
     *
     * 判準是後端交付判準本體的鏡射（payload 的 `has_gateway`
     * ＝ `service._gateway_public_url()` 非空），**不是** `hasAdminGui`：
     * has_admin_gui=True 的模板裡只有 hermes-agent 有 gateway，用
     * hasAdminGui 當 gate 等於對另外 7 個模板的租戶宣告一件永遠不會發生的事
     * （見 capability-gate-single-source-field：能力判準收斂到後端單一欄位）。
     * fail-closed（`=== true`）：欄位缺席（舊快取／漏帶的 payload）一律不顯示。
     */
    get hasGateway() {
        return this.service?.has_gateway === true;
    }

    /**
     * 「租戶自設帳密」的顯示判準：鏡射 payload 的 template.admin_credentials_settable
     * （＝後端 template.admin_credentials_settable，與 admin-credentials endpoint 的
     * 能力 gate 同一個欄位）。fail-closed：欄位缺席一律 false，退回原本的 Reset 按鈕。
     */
    get canEditAdminCredentials() {
        return this.service?.template?.admin_credentials_settable === true;
    }

    /**
     * 對話框要不要出現帳號欄：鏡射 template.admin_username_editable（＝後端
     * admin_username_helm_path）。只有密碼的服務（code-server）為 false。
     */
    get canEditAdminUsername() {
        return this.service?.template?.admin_username_editable === true;
    }

    /**
     * #1675：直接鏡射 template 的自設憑證重啟範圍，不自建 slug 判準。
     * 缺欄位時保守沿用 service，不能對會重啟的服務承諾不中斷。
     */
    get adminCredentialsRestartScope() {
        return this.service?.template?.credential_change_restart_scope || "service";
    }

    /**
     * #1370：Reset 走 operator exec 直接改 app DB（Odoo／n8n）——不重啟、立刻生效、
     * 舊 session 失效、2FA 關閉。鏡射 payload 的 template.app_admin_reset_kind
     * （＝後端 template 欄位），不用 slug 判斷；缺欄位視同 none。
     */
    get isAppAdminReset() {
        const kind = this.service?.template?.app_admin_reset_kind;
        return !!kind && kind !== "none";
    }

    /**
     * Omnigent（omnigent_exec）的 Reset 與上游 Members → Reset 相同：不登出既有 session、
     * 沒有 2FA 可關——提示文案不能沿用 Odoo／n8n 的「舊裝置會被登出」。
     */
    get appResetKeepsSessions() {
        return this.service?.template?.app_admin_reset_kind === "omnigent_exec";
    }

    /**
     * #1774：EMQX（emqx_exec）的 Reset 會登出已登入的 Dashboard，但開源版沒有 2FA——
     * 不能沿用 Odoo／n8n 的「兩步驟驗證已關閉」。
     */
    get appResetHasNoSecondFactor() {
        return this.service?.template?.app_admin_reset_kind === "emqx_exec";
    }

    get credentialsModalMode() {
        return this.isAppAdminReset ? "in_place" : "reset";
    }

    get isResetAdminPasswordLoading() {
        return !!cloudService.operationLoading.resetAdminPassword;
    }

    /**
     * Access 頁籤的顯示判準（#746，設計拍板 6）。
     *
     * 讀後端 payload 的 `subscription.ac_max_access_seats`——那是**方案上限**，
     * 由 `_get_subscription_info()`（subscription_lifecycle_mixin）直接鏡射
     * `sh_subscription_plan_id.ac_max_access_seats`，不做任何推導。無方案時後端
     * 回 0，所以「沒方案」與「方案沒買席位」在這個判準下同義（見該處註解）。
     *
     * 不得改判 template slug／category：那是 capability-gate-single-source-field
     * 明文禁止的第二判準，方向永遠是「後端放行、租戶看不到頁籤」＝修復不可達。
     * 席位是**方案**屬性不是模板屬性，任何 AC 席位方案的服務都該看得到這個頁籤。
     *
     * fail-closed（`?? 0`）：欄位缺席（舊快取、漏帶 subscription 的 payload）一律
     * 視同 0，寧可不顯示，也不要長出一個後端沒有對應設定的頁籤。
     */
    get hasAccessSeats() {
        return (this.service?.subscription?.ac_max_access_seats ?? 0) > 0;
    }

    /**
     * #1261 復原模式：方案沒有 Access 席位，但服務仍開著 Access 保護。
     *
     * 與 SmartHomeDetailPage 的同名 getter 同一件事（prod 2026-09-29 在 SH 實測
     * 重現，CS 的頁籤 gate 同形）：有席位時開了保護，換到 0 席方案後保護原樣留在
     * Cloudflare ⇒ 對外 403，而 hasAccessSeats 為假 ⇒ 頁籤不見 ⇒ 關不掉。後端
     * #1261 已擋住新的這種換方案，這裡處理已經卡住的記錄。
     *
     * ⚠ 不是第二個能力判準：復原模式只放「關保護」這一個**收回**動作（AccessTab
     * 以 `seatless` 掛載），開保護／policy／登入方式一律不給。能力判準仍只有
     * hasAccessSeats。讀 detail payload 的 `access.is_protected`（`_access_to_dict()`）。
     */
    get accessRecoveryMode() {
        return !this.hasAccessSeats && !!this.service?.access?.is_protected;
    }

    // Support Project
    async loadSupportProject() {
        this.state.supportProjectLoading = true;
        try {
            const result = await supportService.fetchProjectForService(this.props.serviceId);
            if (result.success && result.data) {
                this.state.supportProject = result.data;
            }
        } catch {
            // No project yet, that's fine
        } finally {
            this.state.supportProjectLoading = false;
        }
    }

    get projectsVisible() {
        return showProjects();  // #275：flag OFF 時隱藏 project 交叉入口
    }

    openSupportProject() {
        if (this.supportProjectAction !== "open") {
            return;
        }
        this.router.navigate(`ai-assistant/projects/${this.state.supportProject.id}`);
    }

    showCreateProject() {
        if (this.supportProjectAction !== "create") {
            return;
        }
        this.state.showCreateProjectModal = true;
    }

    hideCreateProject() {
        this.state.showCreateProjectModal = false;
    }

    onProjectCreated(project) {
        this.state.showCreateProjectModal = false;
        this.state.supportProject = project;
        this.router.navigate(`ai-assistant/projects/${project.id}`);
    }

    // Navigation
    goBack() {
        this.router.navigate(`workspace/${this.props.workspaceId}`);
    }

    setTab(tab) {
        this.state.currentTab = tab;
        // Update URL without full navigation
        const basePath = `workspace/${this.props.workspaceId}/services/${this.props.serviceId}`;
        window.location.hash = `#/${basePath}/${tab}`;
    }

    // Actions
    openWebUI() {
        if (this.publicUrl) {
            window.open(this.publicUrl, "_blank");
        }
    }

    // Delete Modal
    showDelete() {
        this.state.showDeleteModal = true;
    }

    hideDelete() {
        this.state.showDeleteModal = false;
    }

    onServiceDeleted() {
        this.hideDelete();
        this.goBack();
    }

    // Rollback Modal
    // #1432 Restart
    /**
     * 「重新啟動」只對 running 的服務開放（後端 `_restart_workloads` 同一判準）；
     * VM 型服務不支援（鏡射 `template.restart_supported`，前端不自建判準）。
     */
    get canRestart() {
        return (
            !!this.service &&
            this.service.state === "running" &&
            this.service.template?.restart_supported !== false
        );
    }

    // #1540 停權後的限時唯讀取回入口
    /**
     * 「取回資料」按鈕：owner／admin，且後端說這台支援（範本有 profile）並且此刻可以開
     * （停權中）或已經有開著的入口。判準全讀 payload 的 `data_retrieval`（後端
     * `_data_retrieval_capability`），前端不自建 slug／狀態判準。
     */
    get canRetrieveData() {
        const dr = this.service?.data_retrieval;
        return this.canManageService && !!dr?.supported && (!!dr.available || !!dr.session);
    }

    get retrievalSession() {
        return this.service?.data_retrieval?.session || null;
    }

    get retrievalBannerText() {
        return _t("A temporary download link for this service's data is open until %s.",
            formatUserDateTime(this.retrievalSession?.expires_at, {}, "-"));
    }

    get retrievalSuspendedHint() {
        return _t("This service is suspended. You can open a temporary read-only link to download its files.");
    }

    get dataRetrievalEndpoint() {
        return serviceRetrievalEndpoint(this.props.workspaceId, this.props.serviceId);
    }

    showDataRetrieval() {
        this.state.showDataRetrievalModal = true;
    }

    hideDataRetrieval() {
        // 視窗開著時輪詢暫停（isModalOpen）；關閉時重讀一次，讓提示與按鈕跟上開／關的結果。
        this.state.showDataRetrievalModal = false;
        this.loadService({ silent: true });
    }

    get showRestartButton() {
        return !!this.service && this.service.template?.restart_supported !== false;
    }

    showRestart() {
        this.state.showRestartModal = true;
    }

    hideRestart() {
        this.state.showRestartModal = false;
    }

    onRestarted() {
        this.hideRestart();
        this.loadService();
    }

    showRollback() {
        this.state.showRollbackModal = true;
    }

    hideRollback() {
        this.state.showRollbackModal = false;
    }

    onRollbackComplete() {
        this.hideRollback();
        this.loadService();
    }

    // Skip Trial Modal
    showSkipTrial() {
        this.state.showSkipTrialModal = true;
    }

    hideSkipTrial() {
        this.state.showSkipTrialModal = false;
    }

    onTrialSkipped() {
        this.hideSkipTrial();
        this.loadService();
    }

    // Skip-trial callbacks bound to cloudService — passed to SkipTrialModal
    // so the same modal works for cloud services and smart homes (and any
    // future service type) without modal-side branching.
    loadCloudServiceEstimate = () => {
        return cloudService.getSkipTrialEstimate(this.props.serviceId);
    };

    confirmCloudServiceSkip = () => {
        return cloudService.skipTrial(this.props.serviceId);
    };

    // #1611 Renew — 訂閱被取消（欠費）或到期、服務還在保留期內時自助續訂。
    /**
     * 續訂橫幅的內容；不需要顯示時回 null。判準全部讀後端 `renewal`
     * （`cloud_service._renewal_status()`，與兩支續訂端點同一份），前端不自己看訂閱狀態推導。
     * 「續訂」按鈕另外要 canManageService（owner／admin，與端點的角色閘同一個判準）。
     */
    get renewalBanner() {
        const renewal = this.service?.renewal;
        if (!renewal) {
            return null;
        }
        if (renewal.available) {
            const details = [
                this.service.state === "suspended"
                    ? _t("The service is suspended.")
                    : _t("The service will be suspended soon."),
            ];
            if (renewal.retention_until) {
                details.push(_t("Renew by %(date)s to keep the service and its data.", {
                    date: formatUserDate(renewal.retention_until, ""),
                }));
            }
            if (!this.canManageService) {
                details.push(_t("Ask a workspace owner or admin to renew it."));
            }
            return {
                tone: "warning",
                icon: "autorenew",
                title: renewal.subscription_state === "cancel"
                    ? _t("The subscription of this service was cancelled")
                    : _t("The subscription of this service has ended"),
                detail: details.join(" "),
                canRenew: this.canManageService,
            };
        }
        if (renewal.resuming) {
            return {
                tone: "info",
                icon: "sync",
                title: _t("Subscription renewed"),
                detail: _t("The service is being restored. This usually takes a few minutes."),
                canRenew: false,
            };
        }
        if (renewal.reason === "retention_expired") {
            return {
                tone: "error",
                icon: "block",
                title: _t("The retention period of this service has ended"),
                detail: _t("It can no longer be renewed. Create a new service to continue."),
                canRenew: false,
            };
        }
        if (renewal.reason === "period_not_ended") {
            return {
                tone: "info",
                icon: "event",
                title: _t("This subscription will not renew automatically"),
                detail: _t("The service keeps running until %(date)s. You can renew it here after that date.", {
                    date: formatUserDate(renewal.period_end, ""),
                }),
                canRenew: false,
            };
        }
        return null;
    }

    showRenew() {
        this.state.showRenewModal = true;
    }

    hideRenew() {
        this.state.showRenewModal = false;
    }

    async onRenewed() {
        this.hideRenew();
        await this.loadService();
    }

    loadRenewEstimate = () => {
        return cloudService.getRenewEstimate(this.props.serviceId);
    };

    confirmRenew = (planId) => {
        return cloudService.renew(this.props.serviceId, planId);
    };

    // Change Plan Modal — 改方案／改席數（復用 ChangePlanModal + PlanSelector）
    showChangePlan() {
        this.state.showChangePlanModal = true;
    }

    hideChangePlan() {
        this.state.showChangePlanModal = false;
    }

    onPlanChanged() {
        this.hideChangePlan();
        this.loadService();
    }

    // Change-plan callbacks bound to cloudService — 傳給 ChangePlanModal，
    // 讓同一 modal 免 branch 服務於三 surface。CS 直接用已知的 template.id。
    loadCloudServicePlans = () => {
        return cloudService.fetchPlans(this.service.template.id);
    };

    confirmCloudServiceChangePlan = (planId) => {
        return cloudService.changePlan(this.props.serviceId, planId);
    };

    // AUDITFIX（#1677 A1）：選到方案時問後端換不換得起（不寫 DB）。
    estimateCloudServiceChangePlan = (planId) => {
        return cloudService.changePlan(this.props.serviceId, planId, { dryRun: true });
    };

    // Domain Modal
    get customDomainSupported() {
        return Boolean(this.state.service?.custom_domain_capability?.supported);
    }

    get customDomainManagementAvailable() {
        // The backend exposes management for enabled services or retained claims.
        // `supported` separately gates create/check inside the modal.
        return Boolean(this.state.service?.custom_domain_capability?.management_available);
    }

    showDomainModal() {
        if (!this.customDomainManagementAvailable) {
            return;
        }
        this.state.showDomainModal = true;
    }

    hideDomainModal() {
        this.state.showDomainModal = false;
    }

    onDomainSaved() {
        this.loadService();
    }

    // #1560：Reset 一按就讓目前的密碼失效（MCP 類還會重啟服務），先確認再送出。
    showResetConfirm() {
        this.state.resetPasswordError = null;
        this.state.showResetConfirmModal = true;
    }

    hideResetConfirm() {
        if (!this.isResetAdminPasswordLoading) {
            this.state.showResetConfirmModal = false;
        }
    }

    async confirmResetAdminPassword() {
        try {
            await this.resetAdminPassword();
        } finally {
            this.state.showResetConfirmModal = false;
        }
    }

    async resetAdminPassword() {
        this.state.resetPasswordError = null;
        const result = await cloudService.resetAdminPassword(
            this.props.workspaceId,
            this.props.serviceId
        );
        if (result.success) {
            this.state.oneTimeCredentials = buildOneTimeCredentials(result.data);
            await this.loadService();
        } else {
            this.state.resetPasswordError = result.error || _t("Failed to reset admin password");
        }
    }

    closeCredentials() {
        this.state.oneTimeCredentials = null;
    }

    showAdminCredentials() {
        this.state.resetPasswordError = null;
        this.state.showAdminCredentialsModal = true;
    }

    hideAdminCredentials() {
        this.state.showAdminCredentialsModal = false;
    }

    async onAdminCredentialsSaved(data) {
        this.state.showAdminCredentialsModal = false;
        // 平台產生的密碼（選「隨機產生」）才有明文要一次性交付；租戶自設的後端不回傳。
        // 但有 gateway 的服務（LiteLLM 的 master key）不論密碼怎麼設，後端都換發了新的
        // Gateway API Key、舊的已失效，而這次回應是唯一一次交付——只看 admin_password
        // 會把它丟掉。視窗內容由 buildOneTimeCredentials 依實際帶回的欄位決定。
        if (data && (data.admin_password || data.gateway_api_key)) {
            this.state.oneTimeCredentials = buildOneTimeCredentials(data);
        }
        await this.loadService();
    }

    async handleConfigSave(values) {
        // Reload service after config update
        await this.loadService();
    }

    // Template icon helper
    get brandKey() {
        return templateBrand(this.service?.template?.slug);
    }

    get templateIcon() {
        // Use template icon if available, otherwise default to cloud icon
        return this.service?.template?.icon || "cloud";
    }

    // Service data for modals (includes workspace_id)
    get serviceForModal() {
        if (!this.service) return null;
        return {
            ...this.service,
            workspace_id: this.props.workspaceId,
        };
    }
}
