/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { BasicInfoSection } from "./sections/BasicInfoSection";
import { CloudServiceBindingSection } from "./sections/CloudServiceBindingSection";
import { McpServersSection } from "./sections/McpServersSection";
import { supportService } from "../../services/support_service";
import { router } from "../../core/router";

export class ProjectDetailPage extends Component {
    static template = "woow_paas_platform.ProjectDetailPage";
    static components = {
        WoowCard,
        WoowIcon,
        WoowButton,
        BasicInfoSection,
        CloudServiceBindingSection,
        McpServersSection,
    };
    static props = {
        projectId: { type: Number },
    };

    setup() {
        this.state = useState({
            project: null,
            loading: true,
            error: null,
        });

        onMounted(() => this.loadProject());
    }

    async loadProject() {
        this.state.loading = true;
        this.state.error = null;
        const result = await supportService.fetchProjectDetail(this.props.projectId);
        if (result.success) {
            this.state.project = result.data;
        } else {
            this.state.error = result.error || "Failed to load project";
        }
        this.state.loading = false;
    }

    onProjectUpdated(updated) {
        this.state.project = { ...this.state.project, ...updated };
    }

    onMcpListChanged(mcpServers) {
        this.state.project = {
            ...this.state.project,
            mcp_server_ids: mcpServers,
        };
    }

    goBack() {
        router.navigate("projects");
    }

    viewTasks() {
        router.navigate(`ai-assistant/projects/${this.props.projectId}`);
    }
}
