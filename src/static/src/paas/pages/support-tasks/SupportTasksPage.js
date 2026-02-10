/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { router } from "../../core/router";
import { supportService } from "../../services/support_service";
import { formatDate, getInitials } from "../../services/utils";

export class SupportTasksPage extends Component {
    static template = "woow_paas_platform.SupportTasksPage";
    static components = { WoowCard, WoowIcon, WoowButton };
    static props = {};

    setup() {
        this.router = router;
        this.state = useState({
            loading: true,
            searchQuery: "",
            filterMode: "my",
        });
        this.supportService = useState(supportService);

        onMounted(async () => {
            await this._loadTasks();
        });
    }

    async _loadTasks() {
        this.state.loading = true;
        try {
            const filters = {};
            if (this.state.filterMode === "my") {
                filters.my_tasks = true;
            }
            await supportService.fetchAllTasks(filters);
        } catch (err) {
            console.warn("Failed to load tasks:", err);
        } finally {
            this.state.loading = false;
        }
    }

    get filteredTasks() {
        let tasks = this.supportService.tasks || [];
        const query = this.state.searchQuery.trim().toLowerCase();
        if (query) {
            tasks = tasks.filter(
                (t) =>
                    (t.name && t.name.toLowerCase().includes(query)) ||
                    (t.project_name && t.project_name.toLowerCase().includes(query))
            );
        }
        return tasks;
    }

    get groupedTasks() {
        const groups = {};
        for (const task of this.filteredTasks) {
            const pid = task.project_id || 0;
            const pname = task.project_name || "Unassigned";
            if (!groups[pid]) {
                groups[pid] = { projectId: pid, projectName: pname, tasks: [] };
            }
            groups[pid].tasks.push(task);
        }
        return Object.values(groups);
    }

    get isLoading() {
        return this.state.loading;
    }

    get hasTasks() {
        return this.filteredTasks.length > 0;
    }

    get stats() {
        return this.supportService.stats;
    }

    onSearch(ev) {
        this.state.searchQuery = ev.target.value;
    }

    async onFilterToggle(mode) {
        if (this.state.filterMode === mode) return;
        this.state.filterMode = mode;
        await this._loadTasks();
    }

    navigateToTask(taskId) {
        this.router.navigate("ai-assistant/tasks/" + taskId);
    }

    navigateBack() {
        this.router.navigate("ai-assistant");
    }

    getPriorityStars(priority) {
        const level = parseInt(priority, 10) || 0;
        return [level >= 1, level >= 2, level >= 3];
    }

    formatDate(dateString) {
        return formatDate(dateString);
    }

    getInitials(name) {
        return getInitials(name);
    }

    isOverdue(task) {
        if (!task.date_deadline) return false;
        const deadline = new Date(task.date_deadline);
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        return deadline < today && task.stage_name !== "Done" && task.stage_name !== "Cancelled";
    }

    getCompletion(task) {
        const stageMap = {
            "New": 0,
            "In Progress": 40,
            "Review": 70,
            "Done": 100,
            "Cancelled": 100,
        };
        return stageMap[task.stage_name] || 0;
    }

    getPriorityClass(priority) {
        const level = parseInt(priority, 10) || 0;
        if (level >= 3) return "o_woow_priority_urgent";
        if (level >= 2) return "o_woow_priority_high";
        if (level >= 1) return "o_woow_priority_normal";
        return "o_woow_priority_low";
    }
}
