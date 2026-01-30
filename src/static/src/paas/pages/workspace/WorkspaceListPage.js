/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { CreateWorkspaceModal } from "../../components/modal/CreateWorkspaceModal";
import { workspaceService } from "../../services/workspace_service";
import { router } from "../../core/router";

export class WorkspaceListPage extends Component {
    static template = "woow_paas_platform.WorkspaceListPage";
    static components = { WoowCard, WoowIcon, WoowButton, CreateWorkspaceModal };

    setup() {
        this.state = useState({
            showCreateModal: false,
        });
        this.workspaceService = useState(workspaceService);
        this.router = useState(router);

        onMounted(() => {
            this.loadWorkspaces();
        });
    }

    async loadWorkspaces() {
        await workspaceService.fetchWorkspaces();
    }

    get workspaces() {
        return this.workspaceService.workspaces;
    }

    get loading() {
        return this.workspaceService.loading;
    }

    get hasWorkspaces() {
        return this.workspaces.length > 0;
    }

    openCreateModal() {
        this.state.showCreateModal = true;
    }

    closeCreateModal() {
        this.state.showCreateModal = false;
    }

    async onWorkspaceCreated(workspace) {
        this.state.showCreateModal = false;
        // Navigate to workspace detail
        this.router.navigate(`workspace/${workspace.id}`);
    }

    navigateToWorkspace(workspaceId) {
        this.router.navigate(`workspace/${workspaceId}`);
    }

    getRoleBadgeClass(role) {
        const classes = {
            owner: "o_woow_badge_purple",
            admin: "o_woow_badge_blue",
            user: "o_woow_badge_green",
            guest: "o_woow_badge_gray",
        };
        return classes[role] || "o_woow_badge_gray";
    }

    formatDate(dateString) {
        if (!dateString) return "";
        const date = new Date(dateString);
        return date.toLocaleDateString("zh-TW", {
            year: "numeric",
            month: "short",
            day: "numeric",
        });
    }

    serviceTypes = [
        {
            name: "Cloud Services",
            description: "Deploy Docker apps like AnythingLLM, n8n, and more with one click.",
            icon: "cloud",
            color: "blue",
        },
        {
            name: "Security Access",
            description: "Setup Zero Trust Tunnels via Podman or HAOS for secure remote connections.",
            icon: "security",
            color: "emerald",
        },
        {
            name: "Smart Home Connect",
            description: "Remote access configuration for Home Assistant & Woow App ecosystem.",
            icon: "home_iot_device",
            color: "orange",
        },
    ];
}
