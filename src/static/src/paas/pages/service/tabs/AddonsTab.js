/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { WoowButton } from "../../../components/button/WoowButton";
import { OdooAddonSelector } from "../../../components/odoo-addon-selector/OdooAddonSelector";
import { cloudService } from "../../../services/cloud_service";

/**
 * AddonsTab Component
 *
 * Displays and allows editing of Odoo addon selection for a cloud service.
 * Uses OdooAddonSelector for browsing and toggling addons.
 *
 * Props:
 *   - service (Object): Service data object with odoo_addons and template
 *   - workspaceId (number): Workspace ID for API calls
 *   - onSave (Function): Callback when addons are saved
 */
export class AddonsTab extends Component {
  static template = "woow_paas_platform.AddonsTab";
  static components = { WoowCard, WoowIcon, WoowButton, OdooAddonSelector };
  static props = {
    service: { type: Object },
    workspaceId: { type: Number },
    onSave: { type: Function, optional: true },
  };

  setup() {
    this.state = useState({
      editing: false,
      saving: false,
      error: null,
      selectedIds: [],
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

  startEditing() {
    this.state.selectedIds = [...this.currentAddonIds];
    this.state.editing = true;
    this.state.error = null;
  }

  cancelEditing() {
    this.state.editing = false;
    this.state.selectedIds = [];
    this.state.error = null;
  }

  onSelectionChange(ids) {
    this.state.selectedIds = ids;
  }

  async saveAddons() {
    if (!this.hasChanges) return;

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
