/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { router } from "../../core/router";
import { supportService } from "../../services/support_service";

export class SupportProjectsPage extends Component {
    static template = "woow_paas_platform.SupportProjectsPage";
    static components = { WoowCard, WoowIcon, WoowButton };
    static props = {};

    setup() {
        this.router = router;
        this.supportService = useState(supportService);
        this.state = useState({
            searchQuery: "",
            statusFilter: "all",
            viewMode: "grid",
        });
        onMounted(async () => {
            await this._loadProjects();
        });
    }

    async _loadProjects() {
        try {
            await supportService.fetchProjects(0);
        } catch (err) {
            console.warn("Failed to load support projects:", err);
        }
    }

    get loading() {
        return this.supportService.loading;
    }

    get projects() {
        return this.supportService.projects;
    }

    get filteredProjects() {
        let result = this.projects;
        if (this.state.statusFilter !== "all") {
            result = result.filter((p) => {
                const status = this._getProjectStatus(p);
                return status.toLowerCase() === this.state.statusFilter;
            });
        }
        if (this.state.searchQuery.trim()) {
            const query = this.state.searchQuery.toLowerCase().trim();
            result = result.filter(
                (p) =>
                    (p.name || "").toLowerCase().includes(query) ||
                    (p.description || "").toLowerCase().includes(query)
            );
        }
        return result;
    }

    get hasProjects() {
        return this.filteredProjects.length > 0;
    }

    get isGridView() {
        return this.state.viewMode === "grid";
    }

    get isListView() {
        return this.state.viewMode === "list";
    }

    get statusFilters() {
        return [
            { key: "all", label: "All" },
            { key: "active", label: "Active" },
            { key: "completed", label: "Completed" },
        ];
    }

    _getProjectStatus(project) {
        if (project.status) return project.status;
        if (project.task_count > 0 && project.open_task_count === 0) return "Completed";
        return "Active";
    }

    getStatusBadgeClass(project) {
        const status = this._getProjectStatus(project).toLowerCase();
        if (status === "completed") return "o_woow_sp_badge_completed";
        if (status === "on hold") return "o_woow_sp_badge_onhold";
        return "o_woow_sp_badge_active";
    }

    getStatusLabel(project) {
        return this._getProjectStatus(project);
    }

    formatDate(dateStr) {
        if (!dateStr) return "--";
        try {
            return new Date(dateStr).toLocaleDateString("en-US", {
                month: "short",
                day: "numeric",
                year: "numeric",
            });
        } catch {
            return dateStr;
        }
    }

    getTeamAvatars(project) {
        const members = project.members || project.team || [];
        return {
            visible: members.slice(0, 5),
            extraCount: Math.max(0, members.length - 5),
        };
    }

    getMemberInitials(member) {
        const name = member.name || member.user_name || "?";
        return name
            .split(" ")
            .map((w) => w[0])
            .join("")
            .toUpperCase()
            .slice(0, 2);
    }

    getAvatarColor(member, index) {
        const colors = [
            "#5f81fc", "#22c55e", "#f59e0b", "#ef4444",
            "#8b5cf6", "#ec4899", "#14b8a6",
        ];
        return colors[(member.id || index) % colors.length];
    }

    onSearchInput(ev) {
        this.state.searchQuery = ev.target.value;
    }

    onFilterChange(filter) {
        this.state.statusFilter = filter;
    }

    onViewModeChange(mode) {
        this.state.viewMode = mode;
    }

    navigateToProject(projectId) {
        this.router.navigate("ai-assistant/tasks?project=" + projectId);
    }

    navigateBack() {
        this.router.navigate("ai-assistant");
    }
}
