/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { supportService } from "../../services/support_service";
import { workspaceService } from "../../services/workspace_service";

export class CreateProjectModal extends Component {
    static template = "woow_paas_platform.CreateProjectModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        onClose: { type: Function },
        onCreated: { type: Function },
    };

    setup() {
        this.state = useState({
            name: "",
            description: "",
            workspaceId: "",
            loading: false,
            error: null,
            workspaces: [],
            workspacesLoading: true,
        });

        onMounted(async () => {
            await this._loadWorkspaces();
        });
    }

    async _loadWorkspaces() {
        this.state.workspacesLoading = true;
        try {
            await workspaceService.fetchWorkspaces();
            this.state.workspaces = workspaceService.workspaces;
            // Auto-select first workspace if only one exists
            if (this.state.workspaces.length === 1) {
                this.state.workspaceId = String(this.state.workspaces[0].id);
            }
        } catch (err) {
            console.error("CreateProjectModal: failed to load workspaces:", err);
            this.state.error = "Failed to load workspaces";
        } finally {
            this.state.workspacesLoading = false;
        }
    }

    onNameInput(ev) {
        this.state.name = ev.target.value;
        this.state.error = null;
    }

    onDescriptionInput(ev) {
        this.state.description = ev.target.value;
    }

    onWorkspaceChange(ev) {
        this.state.workspaceId = ev.target.value;
        this.state.error = null;
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget) {
            this.props.onClose();
        }
    }

    async onSubmit() {
        const name = this.state.name.trim();
        if (!name) {
            this.state.error = "Project name is required";
            return;
        }

        if (!this.state.workspaceId) {
            this.state.error = "Please select a workspace";
            return;
        }

        this.state.loading = true;
        this.state.error = null;

        try {
            const workspaceId = parseInt(this.state.workspaceId, 10);
            const result = await supportService.createProject(workspaceId, {
                name,
                description: this.state.description.trim(),
            });

            if (result.success) {
                this.props.onCreated(result.data);
            } else {
                this.state.error = result.error || "Failed to create project";
            }
        } catch (err) {
            console.error("CreateProjectModal: submit failed:", err);
            this.state.error = "An unexpected error occurred. Please try again.";
        } finally {
            this.state.loading = false;
        }
    }
}
