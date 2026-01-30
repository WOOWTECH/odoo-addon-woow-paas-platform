/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { workspaceService } from "../../services/workspace_service";

export class CreateWorkspaceModal extends Component {
    static template = "woow_paas_platform.CreateWorkspaceModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        onClose: { type: Function },
        onCreated: { type: Function },
    };

    setup() {
        this.state = useState({
            name: "",
            description: "",
            loading: false,
            error: null,
        });
    }

    onNameInput(ev) {
        this.state.name = ev.target.value;
        this.state.error = null;
    }

    onDescriptionInput(ev) {
        this.state.description = ev.target.value;
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget) {
            this.props.onClose();
        }
    }

    async onSubmit() {
        const name = this.state.name.trim();
        if (!name) {
            this.state.error = "Workspace name is required";
            return;
        }

        this.state.loading = true;
        this.state.error = null;

        const result = await workspaceService.createWorkspace({
            name,
            description: this.state.description.trim(),
        });

        this.state.loading = false;

        if (result.success) {
            this.props.onCreated(result.data);
        } else {
            this.state.error = result.error || "Failed to create workspace";
        }
    }
}
