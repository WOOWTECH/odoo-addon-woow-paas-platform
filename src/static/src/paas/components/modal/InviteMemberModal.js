/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { workspaceService } from "../../services/workspace_service";

export class InviteMemberModal extends Component {
    static template = "woow_paas_platform.InviteMemberModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        workspaceId: { type: Number },
        onClose: { type: Function },
        onInvited: { type: Function },
    };

    setup() {
        this.state = useState({
            email: "",
            role: "user",
            loading: false,
            error: null,
        });
    }

    onEmailInput(ev) {
        this.state.email = ev.target.value;
        this.state.error = null;
    }

    onRoleChange(ev) {
        this.state.role = ev.target.value;
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget) {
            this.props.onClose();
        }
    }

    async onSubmit() {
        const email = this.state.email.trim();
        if (!email) {
            this.state.error = "Email is required";
            return;
        }

        // Basic email validation
        const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
        if (!emailRegex.test(email)) {
            this.state.error = "Please enter a valid email address";
            return;
        }

        this.state.loading = true;
        this.state.error = null;

        const result = await workspaceService.inviteMember(this.props.workspaceId, {
            email,
            role: this.state.role,
        });

        this.state.loading = false;

        if (result.success) {
            this.props.onInvited(result.data);
        } else {
            this.state.error = result.error || "Failed to invite member";
        }
    }

    roles = [
        { value: "admin", label: "Admin", description: "Can manage members and workspace settings" },
        { value: "user", label: "User", description: "Can view and edit workspace content" },
        { value: "guest", label: "Guest", description: "Can only view workspace content" },
    ];
}
