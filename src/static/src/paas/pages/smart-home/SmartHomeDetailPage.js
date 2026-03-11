/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { StatusBadge } from "../../components/common/StatusBadge";
import { workspaceService } from "../../services/workspace_service";
import { getDomain } from "../../services/cloud_service";
import { router } from "../../core/router";

/**
 * SmartHomeDetailPage Component
 *
 * Displays detailed information about a smart home instance including:
 * - Header with name and status badge
 * - Tunnel info panel (Tunnel ID, Connector Type/ID, Route URL, Status, Uptime)
 * - Delete button with confirmation
 * - Connection guide section
 *
 * Props:
 *   - workspaceId (number): Parent workspace ID
 *   - homeId (number): Smart home ID to display
 */
export class SmartHomeDetailPage extends Component {
    static template = "woow_paas_platform.SmartHomeDetailPage";
    static components = { WoowCard, WoowIcon, WoowButton, StatusBadge };
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
            this.startPollingIfNeeded();
        } else {
            this.state.error = result.error || "Failed to load smart home";
        }

        this.state.loading = false;
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
        const result = await workspaceService.refreshSmartHomeStatus(
            this.props.workspaceId,
            this.props.homeId
        );
        if (result.success) {
            this.state.home = result.data;
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
}
