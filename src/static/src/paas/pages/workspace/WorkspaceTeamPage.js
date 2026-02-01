/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { InviteMemberModal } from "../../components/modal/InviteMemberModal";
import { workspaceService } from "../../services/workspace_service";
import { router } from "../../core/router";
import { MANAGEMENT_ROLES, getRoleBadgeClass, formatDate, getInitials } from "../../services/utils";

export class WorkspaceTeamPage extends Component {
    static template = "woow_paas_platform.WorkspaceTeamPage";
    static components = { WoowCard, WoowIcon, WoowButton, InviteMemberModal };
    static props = {
        workspaceId: { type: Number },
    };

    setup() {
        this.state = useState({
            workspace: null,
            members: [],
            loading: true,
            error: null,
            showInviteModal: false,
        });
        this.router = useState(router);

        onMounted(() => {
            this.loadData();
        });
    }

    async loadData() {
        this.state.loading = true;
        this.state.error = null;

        // Load workspace and members in parallel
        const [workspaceResult, membersResult] = await Promise.all([
            workspaceService.getWorkspace(this.props.workspaceId),
            workspaceService.getMembers(this.props.workspaceId),
        ]);

        if (workspaceResult.success) {
            this.state.workspace = workspaceResult.data;
        } else {
            this.state.error = workspaceResult.error;
        }

        if (membersResult.success) {
            this.state.members = membersResult.data;
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
        this.state.showInviteModal = true;
    }

    closeInviteModal() {
        this.state.showInviteModal = false;
    }

    async onMemberInvited(member) {
        this.state.showInviteModal = false;
        this.state.members = [...this.state.members, member];
    }

    async onRoleChange(member, newRole) {
        const result = await workspaceService.updateMemberRole(
            this.props.workspaceId,
            member.id,
            newRole
        );

        if (result.success) {
            // Update local state
            const index = this.state.members.findIndex(m => m.id === member.id);
            if (index !== -1) {
                this.state.members[index] = { ...this.state.members[index], role: newRole };
            }
        }
    }

    async removeMember(member) {
        if (!confirm(`Are you sure you want to remove ${member.name} from this workspace?`)) {
            return;
        }

        const result = await workspaceService.removeMember(
            this.props.workspaceId,
            member.id
        );

        if (result.success) {
            this.state.members = this.state.members.filter(m => m.id !== member.id);
        }
    }

    getRoleBadgeClass(role) {
        return getRoleBadgeClass(role);
    }

    formatDate(dateString) {
        return formatDate(dateString);
    }

    getInitials(name) {
        return getInitials(name);
    }
}
