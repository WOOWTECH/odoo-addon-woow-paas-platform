/** @odoo-module **/
import { Component, useState, onMounted, useExternalListener } from "@odoo/owl";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { CreateTaskModal } from "../../components/modal/CreateTaskModal";
import { router } from "../../core/router";
import { supportService } from "../../services/support_service";
import { aiService } from "../../services/ai_service";
import { breadcrumbService } from "../../services/breadcrumb_service";
import { formatDate } from "../../services/utils";
import { podStateLabel } from "../../services/agent_pod_state";

export class ProjectKanbanPage extends Component {
    static template = "woow_paas_platform.ProjectKanbanPage";
    static components = { WoowIcon, CreateTaskModal };
    static props = {
        projectId: { type: Number },
    };

    setup() {
        this.router = router;
        this.supportService = useState(supportService);
        this.aiService = useState(aiService);
        this.state = useState({
            showCreateModal: false,
            defaultStageId: null,
            // Status pills (header, right of Create Task)
            connectionLoading: true,
            agentLoading: true,
            agentPodLoading: true,
            openStatusPopover: null, // 'connection' | 'agent' | null
        });

        // Close any open status popover on an outside click. Pill / popover
        // clicks stopPropagation, so only true outside clicks reach this.
        useExternalListener(window, "click", () => {
            this.state.openStatusPopover = null;
        });

        onMounted(async () => {
            await this._loadData();
            // Status pills load independently of the kanban board so a slow
            // operator never blocks the board render.
            this._loadStatuses();
        });
    }

    async _loadStatuses() {
        // Stage 1 — config readiness (fast DB/config reads; gates the pills).
        this.state.connectionLoading = true;
        this.state.agentLoading = true;
        await Promise.all([
            this.aiService.fetchConnectionStatus(),
            this.aiService.fetchAgentStatus(this.props.projectId),
        ]);
        this.state.connectionLoading = false;
        this.state.agentLoading = false;
        // Stage 2 — live pod lifecycle (augments the Agent pill; non-blocking).
        this.state.agentPodLoading = true;
        await this.aiService.fetchAgentPodStatus(this.props.projectId);
        this.state.agentPodLoading = false;
    }

    toggleStatusPopover(which, ev) {
        if (ev) {
            ev.stopPropagation();
        }
        this.state.openStatusPopover =
            this.state.openStatusPopover === which ? null : which;
    }

    get connection() {
        return this.aiService.connectionStatus || {
            connected: false, provider_name: "", model_name: "",
        };
    }

    get agent() {
        return this.aiService.agentStatus || {
            ready: false, enabled: false, operator_configured: false,
            template_name: "", model_name: "", reason: "",
        };
    }

    get agentPod() {
        return this.aiService.agentPodStatus || { state: "" };
    }

    get isConnectionLoading() {
        return this.state.connectionLoading;
    }

    get isAgentLoading() {
        return this.state.agentLoading;
    }

    /** Short zh-tw label for the live pod state — only shown once the Agent is
     *  config-ready. Empty fallback for unknown states (XML guards display with
     *  ``t-if``). Label字面與 portal 共用 services/agent_pod_state.js，避免漂移
     *  （舊版手寫 map 缺 spawning/unknown，已由共用模組補齊）。 */
    get agentLiveLabel() {
        return podStateLabel(this.agentPod.state, "");
    }

    openCreateModal(stageId = null) {
        this.state.showCreateModal = true;
        this.state.defaultStageId = stageId;
    }

    closeCreateModal() {
        this.state.showCreateModal = false;
        this.state.defaultStageId = null;
    }

    async onTaskCreated(task) {
        this.state.showCreateModal = false;
        this.state.defaultStageId = null;
        await this._loadData();
    }

    async _loadData() {
        await Promise.all([
            supportService.fetchProjectStages(this.props.projectId),
            supportService.fetchAllTasks({ project_id: this.props.projectId }),
        ]);
        // 麵包屑軌跡：Projects → {專案名}（看板是專案的主工作視圖，末段即專案本身）
        breadcrumbService.set("ai-project-kanban", [
            { label: "Projects", path: "projects" },
            { label: this.projectName, path: null },
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

        // Stages are already sorted by sequence from the backend
        return stages.map((stage) => ({
                stage,
                tasks: tasksByStage[stage.id] || [],
            }));
    }

    _getPriorityLevel(task) {
        return parseInt(task.priority, 10) || 0;
    }

    hasPriority(task) {
        return this._getPriorityLevel(task) > 0;
    }

    getPriorityStars(task) {
        return "\u2605".repeat(this._getPriorityLevel(task));
    }

    formatDeadline(dateStr) {
        return formatDate(dateStr);
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
        // 統一回 canonical 的 Projects 列表入口（#/projects），與卡片進入點一致，
        // 不再導去舊的 ai-assistant/projects URL。
        this.router.navigate("projects");
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
        } catch (parseErr) {
            console.error("Failed to parse drag data:", parseErr);
            this.supportService.error = "拖曳操作失敗，請重新整理頁面後再試。";
            return;
        }

        if (data.fromStageId === targetStageId) {
            return;
        }

        // Optimistic UI update
        this._moveTaskLocally(data.taskId, targetStageId);

        try {
            const result = await supportService.updateTask(data.taskId, { stage_id: targetStageId });
            if (!result.success) {
                this._moveTaskLocally(data.taskId, data.fromStageId);
                this.supportService.error = result.error || "更新任務階段失敗，請重試。";
            }
        } catch (err) {
            this._moveTaskLocally(data.taskId, data.fromStageId);
            this.supportService.error = "更新任務階段時發生錯誤，請重試。";
        }
    }

    _moveTaskLocally(taskId, newStageId) {
        const task = this.supportService.tasks.find(t => t.id === taskId);
        if (task) {
            task.stage_id = newStageId;
        }
    }
}
