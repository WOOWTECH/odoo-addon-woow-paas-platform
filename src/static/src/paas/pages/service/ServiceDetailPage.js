/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { StatusBadge } from "../../components/common/StatusBadge";
import { OverviewTab } from "./tabs/OverviewTab";
import { ConfigurationTab } from "./tabs/ConfigurationTab";
import { DeleteServiceModal } from "../../components/modal/DeleteServiceModal";
import { RollbackModal } from "../../components/modal/RollbackModal";
import { EditDomainModal } from "../../components/modal/EditDomainModal";
import { cloudService } from "../../services/cloud_service";
import { router } from "../../core/router";

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
        DeleteServiceModal,
        RollbackModal,
        EditDomainModal,
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
            showDomainModal: false,
        });
        this.router = useState(router);
        this.pollingInterval = null;

        onMounted(() => {
            this.loadService();
        });

        onWillUnmount(() => {
            this.stopPolling();
        });
    }

    async loadService() {
        this.state.loading = true;
        this.state.error = null;

        const result = await cloudService.getService(
            this.props.workspaceId,
            this.props.serviceId
        );

        if (result.success) {
            this.state.service = result.data;
            this.startPollingIfNeeded();
        } else {
            this.state.error = result.error || "Failed to load service";
        }

        this.state.loading = false;
    }

    startPollingIfNeeded() {
        const pendingStates = ["deploying", "upgrading", "deleting"];
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
        return `https://${this.service.subdomain}.woowtech.com`;
    }

    get canPerformActions() {
        // Disable actions when service is in transitional state
        const disabledStates = ["deploying", "upgrading", "deleting", "pending"];
        return this.service && !disabledStates.includes(this.service.state);
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
