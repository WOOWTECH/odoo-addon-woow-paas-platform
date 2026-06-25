/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { BasicInfoSection } from "./sections/BasicInfoSection";
import { CloudServiceBindingSection } from "./sections/CloudServiceBindingSection";
import { McpServersSection } from "./sections/McpServersSection";
import { supportService } from "../../services/support_service";
import { breadcrumbService } from "../../services/breadcrumb_service";
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
            this._publishBreadcrumb();
        } else {
            this.state.error = result.error || "Failed to load project";
        }
        this.state.loading = false;
    }

    /**
     * 麵包屑軌跡：Projects → {專案名}(連回該專案看板) → Settings。
     * 末段「Settings」明確標示這是專案的設定/詳情視圖，與看板頁
     * （Projects → {專案名}）區隔——兩者是同一張專案卡的兩個入口。
     */
    _publishBreadcrumb() {
        const project = this.state.project;
        if (!project) return;
        breadcrumbService.set("project-detail", [
            { label: "Projects", path: "projects" },
            { label: project.name, path: `ai-assistant/projects/${this.props.projectId}` },
            { label: "Settings", path: null },
        ]);
    }

    onProjectUpdated(updated) {
        this.state.project = { ...this.state.project, ...updated };
        this._publishBreadcrumb();
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
