/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { WoowButton } from "../../../components/button/WoowButton";
import { OdooAddonSelector } from "../../../components/odoo-addon-selector/OdooAddonSelector";
import { AddonRemovalDialog, blocksRemoval, serviceAllowsRemoval } from "../../../components/addon-removal-dialog/AddonRemovalDialog";
import { cloudService } from "../../../services/cloud_service";

export class AddonsTab extends Component {
  static template = "woow_paas_platform.AddonsTab";
  static components = { WoowCard, WoowIcon, WoowButton, OdooAddonSelector, AddonRemovalDialog };
  static props = {
    service: { type: Object },
    workspaceId: { type: Number },
    // #1560：呼叫者是不是 owner／admin（父層讀 payload 的 viewer_can_manage）。
    canManage: { type: Boolean, optional: true },
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
    // #1560：user／guest 不給編輯（後端 update 會回「權限不足」）。fail-closed。
    if (this.props.canManage !== true) {
      return false;
    }
    const disabledStates = ["deploying", "initializing", "upgrading", "deleting", "pending"];
    if (disabledStates.includes(this.props.service.state)) {
      return false;
    }
    // #1159 Q30：error 只有 init 額度出口可以改 addon（reconfigure，算新一代）；
    // #1552：背景 upgrade 沒有確認套用的 error 也可以重新送出。其他 error 後端照擋。
    // 讀後端同一判準的鏡射欄位，前端不自建。
    return (
      this.props.service.state !== "error" ||
      !!this.props.service.init_reconfigure_allowed ||
      !!this.props.service.upgrade_retry_allowed
    );
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

  openOdooApps() {
    if (this.odooAppsUrl) {
      window.open(this.odooAppsUrl, "_blank");
    }
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

    // Removal needs the module's state inside the tenant's Odoo, which is only
    // readable while the service runs — the backend refuses outright otherwise
    // (#477), and so does the preflight below. Saying it here just avoids a
    // round-trip to a guaranteed rejection. `canEdit` stays permissive on
    // purpose: adding addons in a non-running state is still allowed.
    if (!serviceAllowsRemoval(this.props.service.state)) {
      this.state.error = _t(
        "Cannot remove addons while the service is %s. Start the service and try again.",
        this.props.service.state,
      );
      return;
    }

    this.state.checking = true;
    this.state.error = null;

    try {
      const result = await cloudService.checkAddonRemoval(
        this.props.service.id,
        removed,
      );

      if (!result.success) {
        this.state.error = result.error || _t("Failed to check addon states");
        return;
      }

      // Same predicate the dialog lists by (blocksRemoval) — sharing it is what
      // keeps "dialog shown" and "dialog has rows" from drifting apart.
      const modules = result.modules || {};
      if (!Object.values(modules).some(blocksRemoval)) {
        return this._doUpdateAddons();
      }

      // Module still present in Odoo — block deselection, require the user to
      // uninstall it in Odoo first.
      this.state.dialogModules = modules;
      this.state.showDialog = true;
    } catch (err) {
      this.state.error = err.message || _t("Failed to check addon states");
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
        this.state.error = result.error || _t("Failed to update addons");
      }
    } catch (err) {
      this.state.error = err.message || _t("Failed to update addons");
    } finally {
      this.state.saving = false;
    }
  }
}
