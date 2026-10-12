/** @odoo-module **/
import { Component, useState, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowButton } from "../../components/button/WoowButton";
import { workspaceService } from "../../services/workspace_service";
import { router } from "../../core/router";
import { getInvitationToken, clearInvitationToken, getInvitationRevision } from "../../core/invitation_context";

export class WorkspaceInvitationPage extends Component {
  static template = "woow_paas_platform.WorkspaceInvitationPage";
  static components = { WoowCard, WoowButton };
  static props = {};

  setup() {
    this._active = true;
    // A private component field, never a prop, input, reactive state or log.
    this._token = getInvitationToken();
    this._revision = getInvitationRevision();
    clearInvitationToken(this._revision);
    this.state = useState({
      available: !!this._token, loading: false, accepted: false,
      workspaceId: null, error: null,
    });
    onWillUnmount(() => {
      this._active = false;
      this._token = null;
      clearInvitationToken(this._revision);
    });
  }

  async acceptInvitation() {
    if (!this._active || !this._token || this.state.loading || this.state.accepted) return;
    this.state.loading = true;
    this.state.error = null;
    const result = await workspaceService.acceptInvitation(this._token);
    if (!this._active) return;
    this.state.loading = false;
    if (result.success) {
      this._token = null;
      clearInvitationToken(this._revision);
      this.state.accepted = true;
      this.state.workspaceId = result.data.workspace_id;
    } else {
      this.state.error = result.error || _t("The invitation could not be accepted. Sign in to the invited account and reopen the email link, or ask an administrator for a new invitation.");
    }
  }

  openWorkspace() {
    if (this.state.accepted && this.state.workspaceId) {
      router.navigate(`workspace/${this.state.workspaceId}`);
    }
  }

  openWorkspaces() {
    router.navigate("workspaces");
  }
}
