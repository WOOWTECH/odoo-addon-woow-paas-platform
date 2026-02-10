/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { router } from "../../core/router";
import { supportService } from "../../services/support_service";
import { formatDate } from "../../services/utils";

export class ProjectKanbanPage extends Component {
    static template = "woow_paas_platform.ProjectKanbanPage";
    static components = { WoowIcon };
    static props = {
        projectId: { type: Number },
    };

    setup() {
        this.router = router;
        this.supportService = useState(supportService);

        onMounted(async () => {
            await this._loadData();
        });
    }

    async _loadData() {
        await Promise.all([
            supportService.fetchProjectStages(this.props.projectId),
            supportService.fetchAllTasks({ project_id: this.props.projectId }),
        ]);
    }

    get loading() {
        return this.supportService.loading;
    }

    get error() {
        return this.supportService.error;
    }

    get projectName() {
        const project = this.supportService.projects.find(
            (p) => p.id === this.props.projectId
        );
        return project ? project.name : "Project";
    }

    get taskCount() {
        return this.supportService.tasks.length;
    }

    get columns() {
        const stages = this.supportService.stages;
        const tasks = this.supportService.tasks;

        if (!stages || stages.length === 0) {
            return [];
        }

        const tasksByStage = {};
        for (const stage of stages) {
            tasksByStage[stage.id] = [];
        }

        for (const task of tasks) {
            if (tasksByStage[task.stage_id] !== undefined) {
                tasksByStage[task.stage_id].push(task);
            }
        }

        return stages
            .slice()
            .sort((a, b) => (a.sequence || 0) - (b.sequence || 0))
            .map((stage) => ({
                stage,
                tasks: tasksByStage[stage.id] || [],
            }));
    }

    hasPriority(task) {
        const level = parseInt(task.priority, 10) || 0;
        return level > 0;
    }

    getPriorityStars(task) {
        const level = parseInt(task.priority, 10) || 0;
        return "\u2605".repeat(level);
    }

    formatDeadline(dateStr) {
        return formatDate(dateStr) || "";
    }

    getDeadlineClass(dateStr) {
        if (!dateStr) {
            return "";
        }
        const deadline = new Date(dateStr);
        const now = new Date();
        now.setHours(0, 0, 0, 0);
        deadline.setHours(0, 0, 0, 0);

        const diffMs = deadline.getTime() - now.getTime();
        const diffDays = diffMs / (1000 * 60 * 60 * 24);

        if (diffDays < 0) {
            return "o_woow_kanban__card_deadline--overdue";
        }
        if (diffDays <= 3) {
            return "o_woow_kanban__card_deadline--soon";
        }
        return "";
    }

    navigateToTask(taskId) {
        this.router.navigate("ai-assistant/tasks/" + taskId);
    }

    navigateBack() {
        this.router.navigate("ai-assistant/projects");
    }
}
