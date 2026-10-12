/** @odoo-module **/
import { Component, useState, onMounted, onWillUpdateProps, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { InviteMemberModal } from "../../components/modal/InviteMemberModal";
import { workspaceService } from "../../services/workspace_service";
import { breadcrumbService } from "../../services/breadcrumb_service";
import { router } from "../../core/router";
import { MANAGEMENT_ROLES, getRoleBadgeClass, getRoleLabel, formatDate, getInitials } from "../../services/utils";
import { formatUserDateTime } from "../../utils/datetime_helpers";

export class WorkspaceTeamPage extends Component {
    static template = "woow_paas_platform.WorkspaceTeamPage";
    static components = { WoowCard, WoowIcon, WoowButton, InviteMemberModal };
    static props = {
        workspaceId: { type: Number },
    };

    setup() {
        this._active = true;
        this._loadVersion = 0;
        this.state = useState({
            workspace: null,
            members: [],
            loading: true,
            error: null,
            showInviteModal: false,
            invitations: [],
            invitationError: null,
            actionError: null,
            notice: null,
            pendingBusy: null,
        });
        this.router = useState(router);

        onMounted(() => {
            this.loadData();
        });
        onWillUpdateProps((props) => this.loadData(props.workspaceId));
        onWillUnmount(() => {
            this._active = false;
            this._loadVersion++;
        });
    }

    async loadData(workspaceId = this.props.workspaceId) {
        const version = ++this._loadVersion;
        this.state.loading = true;
        this.state.error = null;
        this.state.workspace = null;
        this.state.members = [];
        this.state.invitations = [];
        this.state.invitationError = null;
        this.state.actionError = null;
        this.state.showInviteModal = false;
        this.state.notice = null;
        this.state.pendingBusy = null;

        // Load workspace and members in parallel
        const [workspaceResult, membersResult] = await Promise.all([
            workspaceService.getWorkspace(workspaceId),
            workspaceService.getMembers(workspaceId),
        ]);
        if (!this._active || version !== this._loadVersion) return;

        if (workspaceResult.success) {
            this.state.workspace = workspaceResult.data;
            // 麵包屑軌跡：Workspaces → {工作區名}(連回工作區) → Team
            breadcrumbService.set("workspace-team", [
                { label: _t("Workspaces"), path: "workspaces" },
                { label: this.state.workspace.name, path: `workspace/${workspaceId}` },
                { label: _t("Team"), path: null },
            ]);
        } else {
            this.state.error = workspaceResult.error;
        }

        if (membersResult.success) {
            this.state.members = membersResult.data;
        } else {
            this.state.error = membersResult.error || _t("Failed to load members");
        }

        if (workspaceResult.success && this.canManageMembers) {
            const invitationsResult = await workspaceService.getInvitations(workspaceId);
            if (!this._active || version !== this._loadVersion) return;
            if (invitationsResult.success) {
                this.state.invitations = invitationsResult.data;
            } else {
                this.state.invitationError = invitationsResult.error || _t("Failed to load invitations");
            }
        }

        this.state.loading = false;
    }

    get workspace() {
        return this.state.workspace;
    }

    get members() {
        return this.state.members;
    }

    get canManageMembers() {
        if (!this.workspace) return false;
        return MANAGEMENT_ROLES.includes(this.workspace.role);
    }

    goBack() {
        this.router.navigate(`workspace/${this.props.workspaceId}`);
    }

    openInviteModal() {
        if (!this.canManageMembers) return;
        this.state.showInviteModal = true;
    }

    closeInviteModal() {
        this.state.showInviteModal = false;
    }

    async onMemberInvited(invitation) {
        const workspaceId = this.props.workspaceId;
        if (!this._active || !invitation || invitation.workspace_id !== workspaceId) return;
        this.state.showInviteModal = false;
        // Pending DTO IDs are ledger IDs, never workspace membership IDs.
        await this.loadData(workspaceId);
        if (this._active && this.props.workspaceId === workspaceId) {
            this.state.notice = _t("Invitation queued. The recipient joins only after accepting the email link.");
        }
    }

    async revokeInvitation(invitation) {
        const workspaceId = this.props.workspaceId;
        if (!this.canManageMembers || this.state.pendingBusy || invitation.workspace_id !== workspaceId) return;
        this.state.pendingBusy = invitation.id;
        this.state.actionError = null;
        const result = await workspaceService.revokeInvitation(workspaceId, invitation.id);
        if (!this._active || this.props.workspaceId !== workspaceId) return;
        this.state.pendingBusy = null;
        if (!result.success) {
            this.state.actionError = result.error || _t("Failed to revoke invitation");
            return;
        }
        await this.loadData(workspaceId);
        if (this._active && this.props.workspaceId === workspaceId) {
            this.state.notice = _t("Invitation revoked. Its email link can no longer grant access.");
        }
    }

    async resendInvitation(invitation) {
        const workspaceId = this.props.workspaceId;
        if (!this.canManageMembers || this.state.pendingBusy || invitation.workspace_id !== workspaceId) return;
        this.state.pendingBusy = invitation.id;
        this.state.actionError = null;
        const result = await workspaceService.inviteMember(workspaceId, {
            email: invitation.email, role: invitation.role,
        });
        if (!this._active || this.props.workspaceId !== workspaceId) return;
        this.state.pendingBusy = null;
        if (!result.success) {
            this.state.actionError = result.error || _t("Failed to resend invitation");
            return;
        }
        await this.loadData(workspaceId);
        if (this._active && this.props.workspaceId === workspaceId) {
            this.state.notice = _t("New invitation queued. Previous invitation links are no longer valid.");
        }
    }

    async onRoleChange(member, newRole) {
        const workspaceId = this.props.workspaceId;
        const result = await workspaceService.updateMemberRole(
            workspaceId,
            member.id,
            newRole
        );

        if (!this._active || this.props.workspaceId !== workspaceId) return;
        if (result.success) await this.loadData(workspaceId);
        else this.state.actionError = result.error || _t("Failed to update member role");
    }

    async removeMember(member) {
        const workspaceId = this.props.workspaceId;
        const question = _t(
            "Are you sure you want to remove %s from this workspace?", member.name);
        if (!confirm(question)) {
            return;
        }

        const result = await workspaceService.removeMember(
            workspaceId,
            member.id
        );

        if (!this._active || this.props.workspaceId !== workspaceId) return;
        if (result.success) await this.loadData(workspaceId);
        else this.state.actionError = result.error || _t("Failed to remove member");
    }

    getRoleBadgeClass(role) {
        return getRoleBadgeClass(role);
    }

    getRoleLabel(role) {
        return getRoleLabel(role);
    }

    formatDate(dateString) {
        return formatDate(dateString);
    }

    formatInvitationExpiry(dateString) {
        // This new API field is an Odoo UTC datetime. Add its UTC offset
        // explicitly, without changing older date fields throughout the app.
        return formatUserDateTime(`${dateString}Z`);
    }

    getInitials(name) {
        return getInitials(name);
    }
}
