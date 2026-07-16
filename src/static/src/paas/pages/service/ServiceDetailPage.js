/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { StatusBadge } from "../../components/common/StatusBadge";
import { OverviewTab } from "./tabs/OverviewTab";
import { ConfigurationTab } from "./tabs/ConfigurationTab";
import { AddonsTab } from "./tabs/AddonsTab";
import { McpServersTab } from "./tabs/McpServersTab";
import { DeleteServiceModal } from "../../components/modal/DeleteServiceModal";
import { RollbackModal } from "../../components/modal/RollbackModal";
import { SkipTrialModal } from "../../components/modal/SkipTrialModal";
import { EditDomainModal } from "../../components/modal/EditDomainModal";
import { ChangePlanModal } from "../../components/modal/ChangePlanModal";
import { CreateProjectModal } from "../../components/modal/CreateProjectModal";
import { AdminPasswordModal } from "../../components/modal/AdminPasswordModal";
import { cloudService } from "../../services/cloud_service";
import { supportService } from "../../services/support_service";
import { breadcrumbService } from "../../services/breadcrumb_service";
import { router } from "../../core/router";
import { showProjects } from "../../core/platform_config";

/**
 * ServiceDetailPage Component
 *
 * Displays detailed information about a cloud service with tabbed navigation.
 * Features:
 * - Service header with icon, name, and status
 * - Tab navigation (Overview, Configuration)
 * - Action buttons (Open Web UI, Rollback, Delete)
 * - Status polling for deploying/upgrading states
 *
 * Props:
 *   - workspaceId (number): Parent workspace ID
 *   - serviceId (number): Service ID to display
 *   - initialTab (string, optional): Initial tab to show ('overview' | 'configuration')
 */
export class ServiceDetailPage extends Component {
    static template = "woow_paas_platform.ServiceDetailPage";
    static components = {
        WoowCard,
        WoowIcon,
        WoowButton,
        StatusBadge,
        OverviewTab,
        ConfigurationTab,
        AddonsTab,
        McpServersTab,
        DeleteServiceModal,
        RollbackModal,
        SkipTrialModal,
        EditDomainModal,
        ChangePlanModal,
        CreateProjectModal,
        AdminPasswordModal,
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
            showSkipTrialModal: false,
            showDomainModal: false,
            showChangePlanModal: false,
            showCreateProjectModal: false,
            supportProject: null,
            supportProjectLoading: false,
            adminPassword: null,
            resetPasswordError: null,
        });
        this.router = useState(router);
        this.pollingInterval = null;

        onMounted(() => {
            this.loadService();
            this.loadSupportProject();
        });

        onWillUnmount(() => {
            this.stopPolling();
        });
    }

    async loadService() {
        this.state.loading = true;
        this.state.error = null;

        const result = await cloudService.fetchService(
            this.props.workspaceId,
            this.props.serviceId
        );

        if (result.success) {
            this.state.service = result.data;
            this._publishBreadcrumb();
            this.startPollingIfNeeded();
        } else {
            this.state.error = result.error || "Failed to load service";
        }

        this.state.loading = false;
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
        breadcrumbService.set("service-detail", [
            { label: "Workspaces", path: "workspaces" },
            { label: service.workspace_name || "Workspace", path: `workspace/${this.props.workspaceId}` },
            { label: service.name, path: null },
        ]);
    }

    startPollingIfNeeded() {
        const pendingStates = ["deploying", "initializing", "upgrading", "deleting"];
        if (this.state.service && pendingStates.includes(this.state.service.state)) {
            this.stopPolling(); // Clear any existing interval
            this.pollingInterval = setInterval(() => this.loadService(), 5000);
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

    get canPerformActions() {
        // Disable actions when service is in transitional state
        const disabledStates = ["deploying", "initializing", "upgrading", "deleting", "pending"];
        return this.service && !disabledStates.includes(this.service.state);
    }

    get isOdooService() {
        return this.service?.template?.slug === 'odoo';
    }

    get isMcpAdminService() {
        return ["mcp-odoo", "mcp-odoo-manage", "mcp-n8n"].includes(
            this.service?.template?.slug
        );
    }

    get isResetAdminPasswordLoading() {
        return !!cloudService.operationLoading.resetAdminPassword;
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
        if (this.state.supportProject) {
            this.router.navigate(`ai-assistant/projects/${this.state.supportProject.id}`);
        }
    }

    showCreateProject() {
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

    // Domain Modal
    showDomainModal() {
        this.state.showDomainModal = true;
    }

    hideDomainModal() {
        this.state.showDomainModal = false;
    }

    onDomainSaved() {
        this.hideDomainModal();
        this.loadService();
    }

    async resetAdminPassword() {
        this.state.resetPasswordError = null;
        const result = await cloudService.resetAdminPassword(
            this.props.workspaceId,
            this.props.serviceId
        );
        if (result.success) {
            this.state.adminPassword = result.data.admin_password;
            await this.loadService();
        } else {
            this.state.resetPasswordError = result.error || "Failed to reset admin password";
        }
    }

    closeAdminPassword() {
        this.state.adminPassword = null;
    }

    async handleConfigSave(values) {
        // Reload service after config update
        await this.loadService();
    }

    // Template icon helper
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
