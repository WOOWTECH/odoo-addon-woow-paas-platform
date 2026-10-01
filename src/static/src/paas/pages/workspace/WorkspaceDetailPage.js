/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount, onWillStart } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { ServiceCard } from "../../components/service-card/ServiceCard";
import { SmartHomeCard } from "../../components/smart-home/SmartHomeCard";
import { CreateSmartHomeModal } from "../../components/smart-home/CreateSmartHomeModal";
import { CreateSecurityAccessModal } from "../../components/modal/CreateSecurityAccessModal";
import { DeleteWorkspaceModal } from "../../components/modal/DeleteWorkspaceModal";
import { StatusBadge } from "../../components/common/StatusBadge";
import { workspaceService } from "../../services/workspace_service";
import { securityAccessService } from "../../services/security_access";
import { cloudService, getDomain } from "../../services/cloud_service";
import { breadcrumbService } from "../../services/breadcrumb_service";
import { router } from "../../core/router";
import { getRoleBadgeClass, formatDate } from "../../services/utils";

export class WorkspaceDetailPage extends Component {
    static template = "woow_paas_platform.WorkspaceDetailPage";
    static components = { WoowCard, WoowIcon, WoowButton, ServiceCard, SmartHomeCard, CreateSmartHomeModal, CreateSecurityAccessModal, DeleteWorkspaceModal, StatusBadge };
    static props = {
        workspaceId: { type: Number },
    };

    setup() {
        this.state = useState({
            workspace: null,
            loading: true,
            error: null,
            services: [],
            loadingServices: true,
            servicesError: null,
            domain: "woowtech.io",
            smartHomes: [],
            loadingSmartHomes: true,
            smartHomesError: null,
            showCreateSmartHomeModal: false,
            securityAccesses: [],
            loadingSecurityAccesses: true,
            securityAccessesError: null,
            showCreateSecurityAccessModal: false,
            showDeleteWorkspaceModal: false,
        });
        this.router = useState(router);
        this.servicesPollingInterval = null;
        this.smartHomesPollingInterval = null;
        this.securityAccessesPollingInterval = null;

        onWillStart(async () => {
            this.state.domain = await getDomain();
        });

        onMounted(() => {
            this.loadData();
        });

        onWillUnmount(() => {
            this.stopServicesPolling();
            this.stopSmartHomesPolling();
            this.stopSecurityAccessesPolling();
        });
    }

    async loadData() {
        this.state.loading = true;
        this.state.loadingServices = true;
        this.state.loadingSmartHomes = true;
        this.state.loadingSecurityAccesses = true;
        this.state.error = null;
        this.state.servicesError = null;
        this.state.smartHomesError = null;
        this.state.securityAccessesError = null;

        // Load workspace, services, smart homes, and security accesses in parallel
        const [workspaceResult, servicesResult, smartHomesResult, securityAccessesResult] = await Promise.all([
            workspaceService.getWorkspace(this.props.workspaceId),
            this.fetchServices(),
            this.fetchSmartHomes(),
            this.fetchSecurityAccesses(),
        ]);

        if (workspaceResult.success) {
            this.state.workspace = workspaceResult.data;
            breadcrumbService.set("workspace-detail", [
                { label: _t("Workspaces"), path: "workspaces" },
                { label: this.state.workspace.name, path: null },
            ]);
        } else {
            this.state.error = workspaceResult.error || _t("Failed to load workspace");
        }

        this.state.loading = false;
        this.state.loadingServices = false;
        this.state.loadingSmartHomes = false;
        this.state.loadingSecurityAccesses = false;

        // Start polling if there are pending services, smart homes, or security accesses
        this.startServicesPolling();
        this.startSmartHomesPolling();
        this.startSecurityAccessesPolling();
    }

    async fetchServices() {
        try {
            await cloudService.fetchServices(this.props.workspaceId);
            this.state.services = cloudService.services;
            this.state.servicesError = cloudService.error;
            return { success: !cloudService.error };
        } catch (err) {
            this.state.servicesError = err.message;
            return { success: false, error: err.message };
        }
    }

    startServicesPolling() {
        const hasPending = this.state.services.some(s =>
            ["deploying", "initializing", "upgrading", "deleting", "pending"].includes(s.state)
        );

        if (hasPending && !this.servicesPollingInterval) {
            this.servicesPollingInterval = setInterval(async () => {
                await this.fetchServices();
                this.state.services = cloudService.services;

                const stillPending = this.state.services.some(s =>
                    ["deploying", "initializing", "upgrading", "deleting", "pending"].includes(s.state)
                );

                if (!stillPending) {
                    this.stopServicesPolling();
                }
            }, 5000);
        }
    }

    stopServicesPolling() {
        if (this.servicesPollingInterval) {
            clearInterval(this.servicesPollingInterval);
            this.servicesPollingInterval = null;
        }
    }

    // Smart Home methods
    async fetchSmartHomes() {
        try {
            const result = await workspaceService.getSmartHomes(this.props.workspaceId);
            if (result.success) {
                this.state.smartHomes = result.data;
                this.state.smartHomesError = null;
                return { success: true };
            } else {
                this.state.smartHomesError = result.error;
                return { success: false, error: result.error };
            }
        } catch (err) {
            this.state.smartHomesError = err.message;
            return { success: false, error: err.message };
        }
    }

    startSmartHomesPolling() {
        const hasPending = this.state.smartHomes.some(h =>
            ["provisioning", "deleting"].includes(h.state)
        );

        if (hasPending && !this.smartHomesPollingInterval) {
            this.smartHomesPollingInterval = setInterval(async () => {
                await this.fetchSmartHomes();

                const stillPending = this.state.smartHomes.some(h =>
                    ["provisioning", "deleting"].includes(h.state)
                );

                if (!stillPending) {
                    this.stopSmartHomesPolling();
                }
            }, 5000);
        }
    }

    stopSmartHomesPolling() {
        if (this.smartHomesPollingInterval) {
            clearInterval(this.smartHomesPollingInterval);
            this.smartHomesPollingInterval = null;
        }
    }

    get hasSmartHomes() {
        return this.state.smartHomes && this.state.smartHomes.length > 0;
    }

    openCreateSmartHomeModal() {
        this.state.showCreateSmartHomeModal = true;
    }

    closeCreateSmartHomeModal() {
        this.state.showCreateSmartHomeModal = false;
    }

    async onSmartHomeCreated(homeData) {
        this.state.showCreateSmartHomeModal = false;
        // Refresh smart homes list
        await this.fetchSmartHomes();
        this.startSmartHomesPolling();
    }

    goToSmartHome(homeId) {
        this.router.navigate(`workspace/${this.props.workspaceId}/smarthome/${homeId}`);
    }

    // Security Access methods
    async fetchSecurityAccesses() {
        try {
            const result = await securityAccessService.fetchList(this.props.workspaceId);
            if (result.success) {
                this.state.securityAccesses = result.data;
                this.state.securityAccessesError = null;
                return { success: true };
            } else {
                this.state.securityAccessesError = result.error;
                return { success: false, error: result.error };
            }
        } catch (err) {
            this.state.securityAccessesError = err.message;
            return { success: false, error: err.message };
        }
    }

    startSecurityAccessesPolling() {
        const hasPending = this.state.securityAccesses.some(sa =>
            ["pending", "provisioning", "deleting"].includes(sa.state)
        );

        if (hasPending && !this.securityAccessesPollingInterval) {
            this.securityAccessesPollingInterval = setInterval(async () => {
                await this.fetchSecurityAccesses();

                const stillPending = this.state.securityAccesses.some(sa =>
                    ["pending", "provisioning", "deleting"].includes(sa.state)
                );

                if (!stillPending) {
                    this.stopSecurityAccessesPolling();
                }
            }, 5000);
        }
    }

    stopSecurityAccessesPolling() {
        if (this.securityAccessesPollingInterval) {
            clearInterval(this.securityAccessesPollingInterval);
            this.securityAccessesPollingInterval = null;
        }
    }

    get hasSecurityAccesses() {
        return this.state.securityAccesses && this.state.securityAccesses.length > 0;
    }

    openCreateSecurityAccessModal() {
        this.state.showCreateSecurityAccessModal = true;
    }

    closeCreateSecurityAccessModal() {
        this.state.showCreateSecurityAccessModal = false;
    }

    async onSecurityAccessCreated(sa) {
        this.state.showCreateSecurityAccessModal = false;
        await this.fetchSecurityAccesses();
        this.startSecurityAccessesPolling();
        // 建立後直接進 detail 看 provisioning 進度
        this.openSecurityAccess(sa.id);
    }

    openSecurityAccess(saId) {
        this.router.navigate(`workspace/${this.props.workspaceId}/security-access/${saId}/overview`);
    }

    get workspace() {
        return this.state.workspace;
    }

    get hasServices() {
        return this.state.services && this.state.services.length > 0;
    }

    get computedStats() {
        const services = this.state.services || [];
        const runningCount = services.filter(s => s.state === "running").length;
        const errorCount = services.filter(s => s.state === "error").length;

        return [
            { label: _t("Estimated Cost"), value: _t("$0/month"), icon: "payments", color: "purple" },
            { label: _t("Total Apps"), value: String(services.length), icon: "apps", color: "blue" },
            { label: _t("Healthy"), value: String(runningCount), icon: "check_circle", color: "green" },
            { label: _t("Alerts"), value: String(errorCount), icon: "warning", color: "orange" },
        ];
    }

    goBack() {
        this.router.navigate("workspaces");
    }

    goToTeam() {
        this.router.navigate(`workspace/${this.props.workspaceId}/team`);
    }

    goToSettings() {
        // TODO: Implement workspace settings
    }

    // #1295：刪除（封存）工作區——只有 owner 看得到按鈕（後端 `_delete_workspace`
    // 也是 owner-only）；底下還有未刪除的服務時由 modal 列出清單，不會封存。
    openDeleteWorkspaceModal() {
        this.state.showDeleteWorkspaceModal = true;
    }

    closeDeleteWorkspaceModal() {
        this.state.showDeleteWorkspaceModal = false;
    }

    onWorkspaceDeleted() {
        this.state.showDeleteWorkspaceModal = false;
        this.router.navigate("workspaces");
    }

    goToMarketplace() {
        this.router.navigate(`workspace/${this.props.workspaceId}/services/marketplace`);
    }

    goToService(serviceId) {
        this.router.navigate(`workspace/${this.props.workspaceId}/services/${serviceId}`);
    }

    openService(serviceId) {
        // Open service web UI - ServiceCard handles this internally
        const service = this.state.services.find(s => s.id === serviceId);
        if (service && service.subdomain && service.state === "running") {
            window.open(`https://${service.subdomain}.${this.state.domain}`, "_blank");
        }
    }

    getRoleBadgeClass(role) {
        return getRoleBadgeClass(role);
    }

    formatDate(dateString) {
        return formatDate(dateString);
    }

    serviceTypes = [
        {
            name: _t("Cloud Services"),
            description: _t("Deploy Docker apps with one click."),
            icon: "cloud",
            color: "blue",
            action: "marketplace",
        },
        {
            name: _t("Security Access"),
            description: _t("Setup Zero Trust Tunnels."),
            icon: "shield",
            // 'emerald' is the existing green-toned card style
            // (o_woow_service_card_emerald); there is no 'green' variant.
            color: "emerald",
            action: "security-access",
        },
        {
            name: _t("Smart Home"),
            description: _t("Connect Home Assistant."),
            icon: "home_iot_device",
            color: "orange",
            action: "smarthome",
        },
    ];
}
