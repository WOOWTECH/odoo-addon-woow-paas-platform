/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";

/**
 * Odoo module states meaning the module is not present in the tenant DB —
 * dropping the addon from the selection is safe. Everything else blocks:
 * `ir.module.module.state` also yields `to install` / `to upgrade` /
 * `to remove`, and those are just as unsafe to rip the code out from under.
 */
export const SAFE_TO_REMOVE_STATES = new Set([
  "uninstalled",
  "uninstallable",
  "not_found",
]);

/**
 * Single source of truth for "this module blocks removal".
 *
 * AddonsTab decides whether to show the dialog, the dialog decides what to
 * list — if the two predicates drift apart the dialog can pop up with an empty
 * body (e.g. a `to upgrade` module: blocking, but not literally "installed").
 * @param {{state: string}} module
 */
export function blocksRemoval(module) {
  return !SAFE_TO_REMOVE_STATES.has(module.state);
}

/**
 * AddonRemovalDialog Component
 *
 * Blocks deselection of addons that are still present in the tenant's Odoo.
 * Instructs the user to uninstall the module in Odoo first.
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

  get blockingModules() {
    return Object.entries(this.props.modules)
      .filter(([_, m]) => blocksRemoval(m))
      .map(([name, m]) => ({ name, ...m }));
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
