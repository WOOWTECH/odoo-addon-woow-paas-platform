/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";

/**
 * AddonRemovalDialog Component
 *
 * Shows warnings when deselecting addons that are installed in Odoo.
 *
 * Props:
 *   - mode ('blocked' | 'confirm'): Dialog type
 *   - modules (Object): { technical_name: { state, dependents } }
 *   - odooAppsUrl (string): URL to open Odoo Apps page
 *   - onClose (Function): Called when dialog is dismissed
 *   - onForceUninstall (Function): Called when user confirms force uninstall
 */
export class AddonRemovalDialog extends Component {
  static template = "woow_paas_platform.AddonRemovalDialog";
  static components = { WoowButton, WoowIcon };
  static props = {
    mode: { type: String },
    modules: { type: Object },
    odooAppsUrl: { type: String, optional: true },
    onClose: { type: Function },
    onForceUninstall: { type: Function, optional: true },
  };

  setup() {
    this.state = useState({ uninstalling: false, error: null });
  }

  get installedModules() {
    return Object.entries(this.props.modules)
      .filter(([_, m]) => m.state === "installed")
      .map(([name, m]) => ({ name, ...m }));
  }

  get blockedModules() {
    return Object.entries(this.props.modules)
      .filter(([_, m]) => m.dependents && m.dependents.length > 0)
      .map(([name, m]) => ({ name, dependents: m.dependents }));
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.uninstalling) {
      this.props.onClose();
    }
  }

  openOdooApps() {
    if (this.props.odooAppsUrl) {
      window.open(this.props.odooAppsUrl, "_blank");
    }
    this.props.onClose();
  }

  async onForceUninstall() {
    if (this.state.uninstalling) return;
    this.state.uninstalling = true;
    this.state.error = null;
    try {
      await this.props.onForceUninstall();
    } catch (err) {
      this.state.error = err.message || "Uninstall failed";
      this.state.uninstalling = false;
    }
  }
}
