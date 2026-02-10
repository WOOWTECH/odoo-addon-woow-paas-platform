/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { supportService } from "../../services/support_service";

export class CreateTaskModal extends Component {
    static template = "woow_paas_platform.CreateTaskModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        onClose: { type: Function },
        onCreated: { type: Function },
        defaultProjectId: { type: Number, optional: true },
    };

    setup() {
        this.supportService = useState(supportService);
        this.state = useState({
            name: "",
            description: "",
            projectId: this.props.defaultProjectId || null,
            priority: "0",
            deadline: "",
            loading: false,
            error: null,
        });

        onMounted(() => this._loadProjects());
    }

    async _loadProjects() {
        try {
            await supportService.fetchAllProjects();
        } catch (err) {
            console.error("CreateTaskModal: failed to load projects:", err);
            this.state.error = "Failed to load projects";
        }
    }

    onNameInput(ev) {
        this.state.name = ev.target.value;
        this.state.error = null;
    }

    onDescriptionInput(ev) {
        this.state.description = ev.target.value;
    }

    onProjectChange(ev) {
        this.state.projectId = ev.target.value ? parseInt(ev.target.value, 10) : null;
        this.state.error = null;
    }

    onPriorityChange(ev) {
        this.state.priority = ev.target.value;
    }

    onDeadlineInput(ev) {
        this.state.deadline = ev.target.value;
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget) {
            this.props.onClose();
        }
    }

    async onSubmit() {
        const name = this.state.name.trim();
        if (!name) {
            this.state.error = "Task name is required";
            return;
        }

        if (!this.state.projectId) {
            this.state.error = "Please select a project";
            return;
        }

        const project = supportService.projects.find(
            (p) => p.id === this.state.projectId
        );
        const workspaceId = project?.workspace_id;

        if (!workspaceId) {
            this.state.error = "Selected project has no associated workspace";
            return;
        }

        this.state.loading = true;
        this.state.error = null;

        try {
            const data = {
                name,
                description: this.state.description.trim(),
                project_id: this.state.projectId,
                priority: this.state.priority,
            };

            if (this.state.deadline) {
                data.date_deadline = this.state.deadline;
            }

            const result = await supportService.createTask(workspaceId, data);

            if (result.success) {
                this.props.onCreated(result.data);
            } else {
                this.state.error = result.error || "Failed to create task";
            }
        } catch (err) {
            console.error("CreateTaskModal: submit failed:", err);
            this.state.error = "An unexpected error occurred. Please try again.";
        } finally {
            this.state.loading = false;
        }
    }
}
