/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { supportService } from "../../services/support_service";
import { workspaceService } from "../../services/workspace_service";
import { cloudService } from "../../services/cloud_service";

export class CreateProjectModal extends Component {
    static template = "woow_paas_platform.CreateProjectModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        onClose: { type: Function },
        onCreated: { type: Function },
        cloudServiceId: { type: Number, optional: true },
        cloudServiceName: { type: String, optional: true },
        workspaceId: { type: Number, optional: true },
    };

    setup() {
        this.state = useState({
            name: "",
            description: "",
            workspaceId: this.props.workspaceId || null,
            cloudServiceId: this.props.cloudServiceId || null,
            loading: false,
            error: null,
            workspaces: [],
            workspacesLoading: true,
            cloudServices: [],
            cloudServicesLoading: false,
        });

        onMounted(async () => {
            await this._loadWorkspaces();
            // If workspace is pre-selected, load its cloud services
            if (this.state.workspaceId) {
                await this._loadCloudServices(this.state.workspaceId);
            }
        });
    }

    async _loadWorkspaces() {
        this.state.workspacesLoading = true;
        try {
            await workspaceService.fetchWorkspaces();
            this.state.workspaces = workspaceService.workspaces;
            if (!this.state.workspaceId && this.state.workspaces.length === 1) {
                this.state.workspaceId = this.state.workspaces[0].id;
                this._loadCloudServices(this.state.workspaceId);
            }
        } catch (err) {
            console.error("CreateProjectModal: failed to load workspaces:", err);
            this.state.error = "Failed to load workspaces";
        } finally {
            this.state.workspacesLoading = false;
        }
    }

    async _loadCloudServices(workspaceId) {
        if (!workspaceId) {
            this.state.cloudServices = [];
            return;
        }
        this.state.cloudServicesLoading = true;
        try {
            await cloudService.fetchServices(workspaceId);
            // Filter out services that already have a project (1:1 constraint)
            this.state.cloudServices = (cloudService.services || []).filter(
                (svc) => !svc.has_project
            );
            // If pre-selected cloud service is in the list, keep it; otherwise clear
            if (
                this.state.cloudServiceId &&
                !this.state.cloudServices.some((svc) => svc.id === this.state.cloudServiceId)
            ) {
                this.state.cloudServiceId = null;
            }
        } catch (err) {
            console.error("CreateProjectModal: failed to load services:", err);
        } finally {
            this.state.cloudServicesLoading = false;
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
        const newId = ev.target.value ? parseInt(ev.target.value, 10) : null;
        this.state.workspaceId = newId;
        this.state.cloudServiceId = null;
        this.state.cloudServices = [];
        this.state.error = null;
        if (newId) {
            this._loadCloudServices(newId);
        }
    }

    onCloudServiceChange(ev) {
        this.state.cloudServiceId = ev.target.value ? parseInt(ev.target.value, 10) : null;
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

        if (!this.state.cloudServiceId) {
            this.state.error = "Please select a cloud service";
            return;
        }

        this.state.loading = true;
        this.state.error = null;

        try {
            let result;
            const payload = {
                name,
                description: this.state.description.trim(),
            };

            result = await supportService.createProjectForService(
                this.state.cloudServiceId,
                payload
            );

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
