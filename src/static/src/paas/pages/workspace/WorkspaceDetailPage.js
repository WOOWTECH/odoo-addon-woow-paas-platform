/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { ServiceCard } from "../../components/service-card/ServiceCard";
import { workspaceService } from "../../services/workspace_service";
import { cloudService } from "../../services/cloud_service";
import { router } from "../../core/router";
import { getRoleBadgeClass, formatDate } from "../../services/utils";

export class WorkspaceDetailPage extends Component {
    static template = "woow_paas_platform.WorkspaceDetailPage";
    static components = { WoowCard, WoowIcon, WoowButton, ServiceCard };
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
        });
        this.router = useState(router);
        this.servicesPollingInterval = null;

        onMounted(() => {
            this.loadData();
        });

        onWillUnmount(() => {
            this.stopServicesPolling();
        });
    }

    async loadData() {
        this.state.loading = true;
        this.state.loadingServices = true;
        this.state.error = null;
        this.state.servicesError = null;

        // Load workspace and services in parallel
        const [workspaceResult, servicesResult] = await Promise.all([
            workspaceService.getWorkspace(this.props.workspaceId),
            this.fetchServices(),
        ]);

        if (workspaceResult.success) {
            this.state.workspace = workspaceResult.data;
        } else {
            this.state.error = workspaceResult.error || "Failed to load workspace";
        }

        this.state.loading = false;
        this.state.loadingServices = false;

        // Start polling if there are pending services
        this.startServicesPolling();
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
            ["deploying", "upgrading", "deleting", "pending"].includes(s.state)
        );

        if (hasPending && !this.servicesPollingInterval) {
            this.servicesPollingInterval = setInterval(async () => {
                await this.fetchServices();
                this.state.services = cloudService.services;

                const stillPending = this.state.services.some(s =>
                    ["deploying", "upgrading", "deleting", "pending"].includes(s.state)
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
            { label: "Estimated Cost", value: "$0/month", icon: "payments", color: "purple" },
            { label: "Total Apps", value: String(services.length), icon: "apps", color: "blue" },
            { label: "Healthy", value: String(runningCount), icon: "check_circle", color: "green" },
            { label: "Alerts", value: String(errorCount), icon: "warning", color: "orange" },
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
            window.open(`https://${service.subdomain}.paas.woow.tw`, "_blank");
        }
    }

    getRoleBadgeClass(role) {
        return getRoleBadgeClass(role);
    }

    formatDate(dateString) {
        return formatDate(dateString, { long: true });
    }

    serviceTypes = [
        {
            name: "Cloud Services",
            description: "Deploy Docker apps with one click.",
            icon: "cloud",
            color: "blue",
        },
        {
            name: "Security Access",
            description: "Setup Zero Trust Tunnels.",
            icon: "security",
            color: "emerald",
        },
        {
            name: "Smart Home",
            description: "Connect Home Assistant.",
            icon: "home_iot_device",
            color: "orange",
        },
    ];
}
