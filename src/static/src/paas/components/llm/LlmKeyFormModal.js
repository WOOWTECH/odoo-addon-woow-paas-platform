/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowIcon } from "../icon/WoowIcon";
import { WoowButton } from "../button/WoowButton";
import { llmService } from "../../services/llm_service";
import { inertOutside, trapTab } from "../../utils/modal_focus";

export function normalizePaidDraft(draft, allowedModels) {
  const allowed = new Set(allowedModels);
  const raw = draft.limitPoints;
  const parsed = raw === "" || raw === null || raw === undefined ? null
    : (/^\d+$/.test(String(raw)) && Number.isSafeInteger(Number(raw)) ? Number(raw) : null);
  return {
    name: typeof draft.name === "string" ? draft.name.trim() : "",
    limitPoints: parsed,
    expires: draft.expires === "never" ? "never" : "90d",
    models: Array.isArray(draft.models) ? [...new Set(draft.models.filter((model) => allowed.has(model)))] : [],
  };
}

export function validatePaidDraft(draft, { paidModels, usedPoints = null }) {
  const normalized = normalizePaidDraft(draft, paidModels);
  if (!normalized.name || normalized.models.length === 0) return { valid: false, error: "invalid", remaining: null };
  if (draft.limitPoints !== "" && draft.limitPoints !== null && draft.limitPoints !== undefined
      && normalized.limitPoints === null) return { valid: false, error: "invalid", remaining: null };
  if (normalized.limitPoints !== null && Number.isInteger(usedPoints) && normalized.limitPoints < usedPoints) {
    return { valid: false, error: "below_used", remaining: null };
  }
  return { valid: true, error: null,
    remaining: normalized.limitPoints !== null && Number.isInteger(usedPoints) ? normalized.limitPoints - usedPoints : null };
}

export class LlmKeyFormModal extends Component {
  static template = "woow_paas_platform.LlmKeyFormModal";
  static components = { WoowIcon, WoowButton };
  static props = {
    onClose: { type: Function },
    onDraft: { type: Function },
    keyItem: { type: Object, optional: true },
  };

  setup() {
    this.state = useState({ name: "", limitPoints: "", expires: "90d", models: [], confirmingClose: false, error: null });
    this.backdropRef = useRef("backdrop");
    this.dialogRef = useRef("dialog");
    this.passwordRef = useRef("password");
    this.returnFocus = null;
    this.restoreInert = null;
    this.destroyed = false;
    this.onKeydown = (ev) => {
      if (ev.key === "Escape") this.requestClose();
      else if (ev.key === "Tab") trapTab(ev, this.dialogRef.el);
    };
    onMounted(() => {
      this.returnFocus = document.activeElement;
      document.addEventListener("keydown", this.onKeydown);
      this.restoreInert = inertOutside(this.backdropRef.el);
      this.dialogRef.el?.focus();
      this.loadModels();
    });
    onWillUnmount(() => this.teardown());
  }

  get paidModels() {
    const items = llmService.models?.items;
    return Array.isArray(items) ? items.filter((item) => item && item.group === "default"
      && typeof item.model === "string" && item.model) : [];
  }
  get validation() { return validatePaidDraft(this.state, { paidModels: this.paidModels.map((item) => item.model), usedPoints: this.props.keyItem?.used_points }); }
  get remainingAfterChange() { return this.validation.remaining; }
  get usedText() {
    return Number.isInteger(this.props.keyItem?.used_points)
      ? _t("Used %(used)s points; %(remaining)s points remain after this change.", {
        used: this.props.keyItem.used_points, remaining: this.remainingAfterChange,
      }) : "";
  }
  get hasLimit() { return this.state.limitPoints !== ""; }
  get needsReauth() { return this.state.expires === "never" && this.state.limitPoints === ""; }
  get submitDisabled() { return !this.validation.valid; }

  async loadModels() {
    if (!llmService.modelsLoaded) await llmService.fetchModels();
    if (this.destroyed) return;
    if (!this.state.models.length) this.state.models = this.paidModels.map((item) => item.model);
  }
  toggleModel(model) {
    const allowed = new Set(this.paidModels.map((item) => item.model));
    if (!allowed.has(model)) return;
    this.state.models = this.state.models.includes(model)
      ? this.state.models.filter((item) => item !== model) : [...this.state.models, model];
  }
  safeDraft() {
    return normalizePaidDraft(this.state, this.paidModels.map((item) => item.model));
  }
  submitDraft() {
    if (!this.validation.valid) return;
    // Task 9 connects this safe callback directly to the reveal component. Do not create and discard plaintext here.
    this.props.onDraft(this.safeDraft());
  }
  async verifyIdentity() {
    // Task 9 will own the final create/reveal flow; do not retain a grant or password here.
    const password = this.passwordRef.el?.value || "";
    if (this.passwordRef.el) this.passwordRef.el.value = "";
    if (!password) return;
    const request = llmService.reauth(password);
    await request;
    // No client-side reauth state is written: the grant remains bound to the server session.
  }
  requestClose() {
    if (this.state.confirmingClose) { this.state.confirmingClose = false; return; }
    const hasDraft = !!this.state.name.trim() || !!this.state.limitPoints || this.state.models.length > 0;
    if (hasDraft) this.state.confirmingClose = true;
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
