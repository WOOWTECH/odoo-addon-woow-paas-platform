/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";

/**
 * AddonRemovalDialog Component
 *
 * Blocks deselection of addons that are currently installed (active) in Odoo.
 * Instructs the user to deactivate the addon in Odoo first.
 *
 * Props:
 *   - modules (Object): { technical_name: { state, dependents } }
 *   - odooAppsUrl (string): URL to open Odoo Apps page
 *   - onClose (Function): Called when dialog is dismissed
 */
export class AddonRemovalDialog extends Component {
  static template = "woow_paas_platform.AddonRemovalDialog";
  static components = { WoowButton, WoowIcon };
  static props = {
    modules: { type: Object },
    odooAppsUrl: { type: String, optional: true },
    onClose: { type: Function },
  };

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
    if (ev.target === ev.currentTarget) {
      this.props.onClose();
    }
  }

  openOdooApps() {
    if (this.props.odooAppsUrl) {
      window.open(this.props.odooAppsUrl, "_blank");
    }
    this.props.onClose();
  }
}
