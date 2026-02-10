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

    // ==================== Drag and Drop ====================

    onDragStart(ev, task) {
        ev.dataTransfer.setData("text/plain", JSON.stringify({
            taskId: task.id,
            fromStageId: task.stage_id,
        }));
        ev.dataTransfer.effectAllowed = "move";
        ev.currentTarget.classList.add("o_woow_kanban__card--dragging");
    }

    onDragEnd(ev) {
        ev.currentTarget.classList.remove("o_woow_kanban__card--dragging");
    }

    onDragOver(ev) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        const column = ev.currentTarget;
        if (!column.classList.contains("o_woow_kanban__column--dragover")) {
            column.classList.add("o_woow_kanban__column--dragover");
        }
    }

    onDragLeave(ev) {
        const column = ev.currentTarget;
        // Only remove if leaving the column element itself (not entering a child)
        if (!column.contains(ev.relatedTarget)) {
            column.classList.remove("o_woow_kanban__column--dragover");
        }
    }

    async onDrop(ev, targetStageId) {
        ev.preventDefault();
        ev.currentTarget.classList.remove("o_woow_kanban__column--dragover");

        let data;
        try {
            data = JSON.parse(ev.dataTransfer.getData("text/plain"));
        } catch {
            return;
        }

        if (data.fromStageId === targetStageId) {
            return;
        }

        // Optimistic UI update
        this._moveTaskLocally(data.taskId, targetStageId);

        // Call API
        const result = await supportService.updateTask(data.taskId, { stage_id: targetStageId });
        if (!result.success) {
            // Rollback on failure
            this._moveTaskLocally(data.taskId, data.fromStageId);
            this.supportService.error = result.error || "Failed to update task stage";
        }
    }

    _moveTaskLocally(taskId, newStageId) {
        const task = this.supportService.tasks.find(t => t.id === taskId);
        if (task) {
            task.stage_id = newStageId;
        }
    }
}
