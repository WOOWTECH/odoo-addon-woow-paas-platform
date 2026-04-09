/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { WoowButton } from "../../../components/button/WoowButton";
import { OdooAddonSelector } from "../../../components/odoo-addon-selector/OdooAddonSelector";
import { AddonRemovalDialog } from "../../../components/addon-removal-dialog/AddonRemovalDialog";
import { cloudService } from "../../../services/cloud_service";

export class AddonsTab extends Component {
  static template = "woow_paas_platform.AddonsTab";
  static components = { WoowCard, WoowIcon, WoowButton, OdooAddonSelector, AddonRemovalDialog };
  static props = {
    service: { type: Object },
    workspaceId: { type: Number },
    onSave: { type: Function, optional: true },
  };

  setup() {
    this.state = useState({
      editing: false,
      saving: false,
      checking: false,
      error: null,
      selectedIds: [],
      showDialog: false,
      dialogModules: {},
    });
  }

  get currentAddons() {
    return this.props.service.odoo_addons || [];
  }

  get currentAddonIds() {
    return this.currentAddons.map(a => a.id);
  }

  get hasChanges() {
    const current = new Set(this.currentAddonIds);
    const selected = new Set(this.state.selectedIds);
    if (current.size !== selected.size) return true;
    for (const id of current) {
      if (!selected.has(id)) return true;
    }
    return false;
  }

  get canEdit() {
    const disabledStates = ["deploying", "initializing", "upgrading", "deleting", "pending"];
    return !disabledStates.includes(this.props.service.state);
  }

  get removedAddonIds() {
    const selected = new Set(this.state.selectedIds);
    return this.currentAddonIds.filter(id => !selected.has(id));
  }

  get odooAppsUrl() {
    const svc = this.props.service;
    if (svc.subdomain) {
      return `https://${svc.subdomain}.woowtech.io/odoo/apps/modules`;
    }
    return null;
  }

  startEditing() {
    this.state.selectedIds = [...this.currentAddonIds];
    this.state.editing = true;
    this.state.error = null;
  }

  cancelEditing() {
    this.state.editing = false;
    this.state.selectedIds = [];
    this.state.error = null;
    this.state.showDialog = false;
  }

  onSelectionChange(ids) {
    this.state.selectedIds = ids;
  }

  async saveAddons() {
    if (!this.hasChanges) return;

    const removed = this.removedAddonIds;

    if (removed.length === 0) {
      return this._doUpdateAddons();
    }

    this.state.checking = true;
    this.state.error = null;

    try {
      const result = await cloudService.checkAddonRemoval(
        this.props.service.id,
        removed,
      );

      if (!result.success) {
        this.state.error = result.error || "Failed to check addon states";
        return;
      }

      const modules = result.modules || {};
      const hasInstalled = Object.values(modules).some(m => m.state === 'installed');

      if (!hasInstalled) {
        return this._doUpdateAddons();
      }

      // Installed addons found — block deselection, require user to
      // deactivate in Odoo first
      this.state.dialogModules = modules;
      this.state.showDialog = true;
    } catch (err) {
      this.state.error = err.message || "Failed to check addon states";
    } finally {
      this.state.checking = false;
    }
  }

  closeDialog() {
    this.state.showDialog = false;
    this.state.dialogModules = {};
  }

  async _doUpdateAddons() {
    this.state.saving = true;
    this.state.error = null;

    try {
      const result = await cloudService.updateServiceAddons(
        this.props.workspaceId,
        this.props.service.id,
        this.state.selectedIds,
      );

      if (result.success) {
        this.state.editing = false;
        this.state.selectedIds = [];
        if (this.props.onSave) {
          this.props.onSave(result.data);
        }
      } else {
        this.state.error = result.error || "Failed to update addons";
      }
    } catch (err) {
      this.state.error = err.message || "Failed to update addons";
    } finally {
      this.state.saving = false;
    }
  }
}
