/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { workspaceService } from "../../services/workspace_service";
import { router } from "../../core/router";

export class WorkspaceDetailPage extends Component {
    static template = "woow_paas_platform.WorkspaceDetailPage";
    static components = { WoowCard, WoowIcon, WoowButton };
    static props = {
        workspaceId: { type: Number },
    };

    setup() {
        this.state = useState({
            workspace: null,
            loading: true,
            error: null,
        });
        this.router = useState(router);

        onMounted(() => {
            this.loadWorkspace();
        });
    }

    async loadWorkspace() {
        this.state.loading = true;
        this.state.error = null;

        const result = await workspaceService.getWorkspace(this.props.workspaceId);

        if (result.success) {
            this.state.workspace = result.data;
        } else {
            this.state.error = result.error || "Failed to load workspace";
        }

        this.state.loading = false;
    }

    get workspace() {
        return this.state.workspace;
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
            month: "long",
            day: "numeric",
        });
    }

    // Stats for demonstration - in real app, these would come from API
    stats = [
        { label: "Estimated Cost", value: "$0/month", icon: "payments", color: "purple" },
        { label: "Total Apps", value: "0", icon: "apps", color: "blue" },
        { label: "Healthy", value: "0", icon: "check_circle", color: "green" },
        { label: "Alerts", value: "0", icon: "warning", color: "orange" },
    ];

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
