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
        cloudServiceId: { type: Number, optional: true },
        cloudServiceName: { type: String, optional: true },
    };

    setup() {
        this.state = useState({
            name: "",
            description: "",
            workspaceId: null,
            loading: false,
            error: null,
            workspaces: [],
            workspacesLoading: !this.props.cloudServiceId,
        });

        if (!this.props.cloudServiceId) {
            onMounted(() => this._loadWorkspaces());
        }
    }

    async _loadWorkspaces() {
        this.state.workspacesLoading = true;
        try {
            await workspaceService.fetchWorkspaces();
            this.state.workspaces = workspaceService.workspaces;
            if (this.state.workspaces.length === 1) {
                this.state.workspaceId = this.state.workspaces[0].id;
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
        this.state.workspaceId = ev.target.value ? parseInt(ev.target.value, 10) : null;
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

        this.state.loading = true;
        this.state.error = null;

        try {
            let result;
            if (this.props.cloudServiceId) {
                result = await supportService.createProjectForService(this.props.cloudServiceId, {
                    name,
                    description: this.state.description.trim(),
                });
            } else {
                if (!this.state.workspaceId) {
                    this.state.error = "Please select a workspace";
                    this.state.loading = false;
                    return;
                }
                result = await supportService.createProject(this.state.workspaceId, {
                    name,
                    description: this.state.description.trim(),
                });
            }

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
