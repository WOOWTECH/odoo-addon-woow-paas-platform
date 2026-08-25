/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
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
            this.state.error = _t("Failed to load workspaces");
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
            // D5=A: list every service; the server-side `unique_cloud_service`
            // constraint is the source of truth for the 1:1 limit, and the
            // "— No service —" first option lets users create an unbound project.
            this.state.cloudServices = cloudService.services || [];
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
            this.state.error = _t("Project name is required");
            return;
        }

        // D1=A: workspace is optional. Three paths:
        //   - cloud_service picked  → bound create (service path)
        //   - workspace picked only → workspace-scoped create (workspace path)
        //   - neither picked        → fully unbound create (personal project)
        this.state.loading = true;
        this.state.error = null;

        try {
            const payload = {
                name,
                description: this.state.description.trim(),
            };

            let result;
            if (this.state.cloudServiceId) {
                result = await supportService.createProjectForService(
                    this.state.cloudServiceId,
                    payload,
                );
            } else if (this.state.workspaceId) {
                result = await supportService.createProject(this.state.workspaceId, payload);
            } else {
                result = await supportService.createUnboundProject(payload);
            }

            if (result.success) {
                this.props.onCreated(result.data);
            } else {
                this.state.error = result.error || _t("Failed to create project");
            }
        } catch (err) {
            console.error("CreateProjectModal: submit failed:", err);
            this.state.error = _t("An unexpected error occurred. Please try again.");
        } finally {
            this.state.loading = false;
        }
    }
}
