/** @odoo-module **/

import { Component, onMounted, onPatched, onWillUnmount, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowIcon } from "../icon/WoowIcon";
import { WoowButton } from "../button/WoowButton";
import { llmService } from "../../services/llm_service";
import { llmErrorMessage } from "../../services/llm_errors";
import { inertOutside, trapTab } from "../../utils/modal_focus";

export function normalizePaidDraft(draft, allowedModels) {
  const allowed = new Set(allowedModels);
  const raw = draft.limitPoints;
  const parsed = raw === "" || raw === null || raw === undefined ? null
    : (/^\d+$/.test(String(raw)) && Number.isSafeInteger(Number(raw)) ? Number(raw) : null);
  return {
    name: typeof draft.name === "string" ? draft.name.trim() : "",
    limitPoints: parsed,
    expires: ["30d", "90d", "180d", "never"].includes(draft.expires) ? draft.expires : "90d",
    models: Array.isArray(draft.models)
      ? (() => {
        const selected = [...new Set(draft.models.filter((model) => allowed.has(model)))];
        return selected.length === allowed.size && selected.every((model) => allowed.has(model)) ? null : selected;
      })() : null,
  };
}

export function validatePaidDraft(draft, { paidModels, usedPoints = null }) {
  const normalized = normalizePaidDraft(draft, paidModels);
  if (!normalized.name || normalized.name.length > 64 || !Array.isArray(paidModels) || paidModels.length === 0
      || (normalized.models !== null && normalized.models.length === 0)) return { valid: false, error: "invalid", remaining: null };
  if (draft.expires !== "keep" && !["30d", "90d", "180d", "never"].includes(draft.expires)) {
    return { valid: false, error: "invalid", remaining: null };
  }
  if (draft.limitPoints !== "" && draft.limitPoints !== null && draft.limitPoints !== undefined
      && normalized.limitPoints === null) return { valid: false, error: "invalid", remaining: null };
  if (normalized.limitPoints === 0) return { valid: false, error: "limit_must_be_positive", remaining: null };
  if (normalized.limitPoints !== null && Number.isInteger(usedPoints) && normalized.limitPoints < usedPoints) {
    return { valid: false, error: "below_used", remaining: null };
  }
  return { valid: true, error: null,
    remaining: normalized.limitPoints !== null && Number.isInteger(usedPoints) ? normalized.limitPoints - usedPoints : null };
}

/** Construct only intentional edits. An omitted expiry means preserve its current backend value. */
export function paidKeyEditDiff(item, draft, paidModels) {
  const normalized = normalizePaidDraft(draft, paidModels);
  const changes = {};
  const currentName = typeof item.display_name === "string" ? item.display_name : "";
  const draftName = typeof draft.name === "string" ? draft.name : "";
  // Edit mode starts from the backend's raw value, including legacy whitespace.
  // Only a raw user-input change should submit the normalized name.
  if (draftName !== currentName) changes.name = normalized.name;
  const currentLimit = Number.isInteger(item.limit_points) ? item.limit_points : null;
  if (normalized.limitPoints !== currentLimit) changes.limitPoints = normalized.limitPoints;
  if (draft.expires !== "keep") changes.expires = draft.expires;
  const currentModels = Array.isArray(item.models) ? item.models : null;
  const draftModels = Array.isArray(draft.models) ? draft.models : null;
  const untouchedModels = currentModels === null ? draftModels === null
    : draftModels !== null && currentModels.length === draftModels.length
      && currentModels.every((model) => draftModels.includes(model));
  const sameModels = currentModels === null ? normalized.models === null
    : normalized.models !== null && currentModels.length === normalized.models.length
      && currentModels.every((model) => normalized.models.includes(model));
  if (!untouchedModels && !sameModels) changes.models = normalized.models;
  return changes;
}

export class LlmKeyFormModal extends Component {
  static template = "woow_paas_platform.LlmKeyFormModal";
  static components = { WoowIcon, WoowButton };
  static props = {
    onClose: { type: Function },
    onDraft: { type: Function },
    onUpdate: { type: Function, optional: true },
    keyItem: { type: Object, optional: true },
  };

  setup() {
    const item = this.props.keyItem;
    this.state = useState({ name: item?.display_name || "", limitPoints: item && item.limit_points != null ? String(item.limit_points) : "", expires: item ? "keep" : "90d", models: Array.isArray(item?.models) ? [...item.models] : null, confirmingClose: false, error: null, saving: false });
    this.backdropRef = useRef("backdrop");
    this.dialogRef = useRef("dialog");
    this.confirmRef = useRef("confirm");
    this.keepEditingRef = useRef("keepEditing");
    this.passwordRef = useRef("password");
    this.returnFocus = null;
    this.confirmReturnFocus = null;
    this.restoreInert = null;
    this.destroyed = false;
    this.onKeydown = (ev) => {
      if (ev.key === "Escape") this.requestClose();
      else if (ev.key === "Tab") trapTab(ev, this.state.confirmingClose ? this.confirmRef.el : this.dialogRef.el);
    };
    onMounted(() => {
      this.returnFocus = document.activeElement;
      document.addEventListener("keydown", this.onKeydown);
      this.restoreInert = inertOutside(this.backdropRef.el);
      this.dialogRef.el?.focus();
      this.syncConfirmFocus();
      this.loadModels();
    });
    onPatched(() => this.syncConfirmFocus());
    onWillUnmount(() => this.teardown());
  }

  syncConfirmFocus() {
    if (this.state.confirmingClose) {
      if (this.confirmReturnFocus && !this.confirmReturnFocus.isConnected) this.confirmReturnFocus = null;
      if (document.activeElement !== this.keepEditingRef.el) this.keepEditingRef.el?.focus();
    } else if (this.confirmReturnFocus) {
      const target = this.confirmReturnFocus;
      this.confirmReturnFocus = null;
      if (target.isConnected && this.dialogRef.el?.contains(target)) target.focus();
      else this.dialogRef.el?.focus();
    }
  }

  get paidModels() {
    const items = llmService.models?.items;
    return Array.isArray(items) ? items.filter((item) => item && item.group === "default"
      && typeof item.model === "string" && item.model) : [];
  }
  get validation() { return validatePaidDraft(this.state, { paidModels: this.paidModels.map((item) => item.model), usedPoints: this.props.keyItem?.used_points }); }
  get remainingAfterChange() { return this.validation.remaining; }
  get hasUsedPoints() { return Number.isInteger(this.props.keyItem?.used_points); }
  get usedText() {
    return this.hasUsedPoints
      ? _t("Used %(used)s points; %(remaining)s points remain after this change.", {
        used: this.props.keyItem.used_points, remaining: this.remainingAfterChange,
      }) : "";
  }
  get hasLimit() { return this.state.limitPoints !== ""; }
  get needsReauth() { return this.state.expires === "never" && this.state.limitPoints === ""; }
  get submitDisabled() { return !this.validation.valid; }
  get editMode() { return !!this.props.keyItem; }
  get title() { return this.editMode ? _t("Edit paid API key") : _t("Create paid API key"); }
  get submitLabel() { return this.editMode ? _t("Save changes") : _t("Continue"); }

  async loadModels() {
    if (!llmService.modelsLoaded) await llmService.fetchModels();
    if (this.destroyed) return;
    if (this.state.models === null) return;
  }
  toggleModel(model) {
    if (this.state.saving) return;
    const allowed = new Set(this.paidModels.map((item) => item.model));
    if (!allowed.has(model)) return;
    const selected = this.state.models === null ? [...allowed] : [...this.state.models];
    this.state.models = selected.includes(model)
      ? selected.filter((item) => item !== model) : [...selected, model];
  }
  safeDraft() {
    return normalizePaidDraft(this.state, this.paidModels.map((item) => item.model));
  }
  submitDraft() {
    if (this.state.saving) return;
    if (!this.validation.valid) return;
    if (this.editMode) {
      const changes = paidKeyEditDiff(this.props.keyItem, this.state, this.paidModels.map((item) => item.model));
      if (!Object.keys(changes).length) { this.confirmClose(); return; }
      this.submitEdit(changes);
      return;
    }
    // Pass only safe metadata to the one-time reveal flow; plaintext is never created or retained here.
    this.props.onDraft(this.safeDraft());
  }
  async submitEdit(changes) {
    if (!this.props.onUpdate || this.state.saving) return;
    this.state.error = null;
    this.state.saving = true;
    let result;
    try { result = await this.props.onUpdate(this.props.keyItem, changes); }
    catch { result = { success: false, code: "llm_internal_error" }; }
    if (this.destroyed) return result;
    this.state.saving = false;
    if (!result?.success) this.state.error = llmErrorMessage(result);
    return result;
  }
  async verifyIdentity() {
    // Do not retain a grant or password in this form.
    const password = this.passwordRef.el?.value || "";
    if (this.passwordRef.el) this.passwordRef.el.value = "";
    if (!password) return;
    const request = llmService.reauth(password);
    await request;
    // No client-side reauth state is written: the grant remains bound to the server session.
  }
  requestClose() {
    if (this.state.saving) return;
    if (this.state.confirmingClose) { this.state.confirmingClose = false; return; }
    const hasDraft = this.editMode ? Object.keys(paidKeyEditDiff(this.props.keyItem, this.state, this.paidModels.map((item) => item.model))).length > 0
      : (!!this.state.name.trim() || !!this.state.limitPoints || this.state.models !== null);
    if (hasDraft) {
      this.confirmReturnFocus = document.activeElement;
      this.state.confirmingClose = true;
    }
    else this.confirmClose();
  }
  onBackdropClick(ev) { if (ev.target === ev.currentTarget) this.requestClose(); }
  cancelClose() { this.state.confirmingClose = false; }
  confirmClose() { this.teardown(); this.props.onClose(); }
  teardown() {
    if (this.destroyed) return;
    this.destroyed = true;
    if (this.passwordRef.el) this.passwordRef.el.value = "";
    if (typeof document !== "undefined") document.removeEventListener("keydown", this.onKeydown);
    this.restoreInert?.(); this.restoreInert = null;
    this.returnFocus?.focus(); this.returnFocus = null;
  }
}
