/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { inertOutside, trapTab } from "../../utils/modal_focus";

export function restoreModalFocus(returnFocus, fallbackFocus) {
  if (returnFocus && returnFocus.isConnected !== false && typeof returnFocus.focus === "function") {
    returnFocus.focus();
    return true;
  }
  if (typeof fallbackFocus === "function") fallbackFocus();
  return false;
}

export class LlmKeyActionConfirmModal extends Component {
  static template = "woow_paas_platform.LlmKeyActionConfirmModal";
  static components = { WoowButton };
  static props = {
    item: { type: Object }, action: { type: String, validate: (v) => ["extend", "disable", "delete"].includes(v) },
    busy: { type: Boolean }, errorText: { type: String }, onClose: { type: Function }, onConfirm: { type: Function },
    onReturnFocus: { type: Function, optional: true },
  };

  setup() {
    this.state = useState({ confirmName: "" });
    this.backdropRef = useRef("backdrop");
    this.dialogRef = useRef("dialog");
    this.returnFocus = null;
    this.restoreInert = null;
    this.onKeydown = (ev) => {
      if (ev.key === "Escape") this.requestClose();
      else if (ev.key === "Tab") trapTab(ev, this.dialogRef.el);
    };
    onMounted(() => {
      this.returnFocus = document.activeElement;
      document.addEventListener("keydown", this.onKeydown);
      this.restoreInert = inertOutside(this.backdropRef.el);
      this.dialogRef.el?.focus();
    });
    onWillUnmount(() => this.teardown());
  }

  get isDelete() { return this.props.action === "delete"; }
  get isPaidDelete() { return this.isDelete && this.props.item.kind === "paid"; }
  get displayName() { return this.props.item.display_name || _t("API key"); }
  get typePrompt() {
    return _t("Type %(keyName)s to confirm", { keyName: this.props.item.display_name });
  }
  get title() {
    return this.props.action === "extend" ? _t("Extend API key")
      : this.props.action === "disable" ? _t("Disable API key") : _t("Delete API key");
  }
  get description() {
    return this.props.action === "extend" ? _t("Add 90 days to this key’s expiry date?")
      : this.props.action === "disable" ? _t("Disable this API key? It will stop accepting requests.")
        : this.isPaidDelete ? _t("Deleting this key cannot be undone. Type the key name exactly to confirm.")
          : _t("Deleting the free trial key cannot be undone. Confirm deletion to create another free trial key later.");
  }
  get nameMatches() { return !this.isPaidDelete || this.state.confirmName === this.props.item.display_name; }
  get submitLabel() {
    return this.props.action === "extend" ? _t("Extend 90 days")
      : this.props.action === "disable" ? _t("Disable key") : _t("Delete key");
  }
  requestClose() { if (!this.props.busy) this.props.onClose(); }
  onBackdropClick(ev) { if (ev.target === ev.currentTarget) this.requestClose(); }
  confirm() {
    if (this.props.busy || !this.nameMatches) return;
    this.props.onConfirm(this.isPaidDelete ? this.state.confirmName : "");
  }
  teardown() {
    document.removeEventListener("keydown", this.onKeydown);
    this.restoreInert?.();
    this.restoreInert = null;
    restoreModalFocus(this.returnFocus, this.props.onReturnFocus);
    this.returnFocus = null;
  }
}
