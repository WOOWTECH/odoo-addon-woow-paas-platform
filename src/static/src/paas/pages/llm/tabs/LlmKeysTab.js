/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { WoowButton } from "../../../components/button/WoowButton";
import { LlmKeyFormModal } from "../../../components/llm/LlmKeyFormModal";
import { LlmKeyRevealModal } from "../../../components/llm/LlmKeyRevealModal";
import { LlmKeyActionConfirmModal } from "../../../components/llm/LlmKeyActionConfirmModal";
import { llmService } from "../../../services/llm_service";
import { llmErrorMessage } from "../../../services/llm_errors";
import { formatTaipeiDateTime, isExpiringSoon } from "../../../utils/llm_format";
import { router } from "../../../core/router";

/** Only the cached catalog's paid/default group may be offered to a paid key. */
export function paidKeyModels(items) {
  return Array.isArray(items) ? items.filter((item) => item && item.group === "default"
    && typeof item.model === "string" && item.model) : [];
}

export function parseExtendId(value) {
  if (typeof value !== "string" || !/^\d+$/.test(value)) return null;
  const id = Number(value);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

/** A consumed token is never retried by later patches; malformed/unmatched values result in no I/O. */
export function extendQueryFromHash(hash) {
  if (typeof hash !== "string") return "";
  const queryAt = hash.indexOf("?");
  return queryAt < 0 ? "" : hash.slice(queryAt + 1);
}

export function stripExtendFromHash(hash) {
  if (typeof hash !== "string") return hash;
  const queryAt = hash.indexOf("?");
  if (queryAt < 0) return hash;
  const route = hash.slice(0, queryAt);
  const params = new URLSearchParams(hash.slice(queryAt + 1));
  params.delete("extend");
  const query = params.toString();
  return route + (query ? "?" + query : "");
}

export function applyExtendQueryOnce(search, items, consumed) {
  let value = null;
  try {
    const params = new URLSearchParams(search || "");
    const values = params.getAll("extend");
    value = values.length === 1 ? values[0] : null;
  } catch { /* ignore invalid query */ }
  const id = parseExtendId(value);
  const key = id === null ? null : String(id);
  if (key === null || consumed.has(key)) return { id: null, consumed: true };
  consumed.add(key);
  return { id: items.some((item) => item && item.id === id) ? id : null, consumed: true };
}

export function keyRow(item) {
  const kind = item.kind === "paid" ? _t("Paid") : _t("Free trial");
  const total = Number.isInteger(item.limit_points) ? String(item.limit_points) : _t("Unlimited");
  const limited = Number.isInteger(item.limit_points) && item.limit_points > 0;
  const usedPoints = Number.isInteger(item.used_points) ? item.used_points : null;
  const totalPoints = limited ? item.limit_points : null;
  return {
    id: item.id,
    name: item.display_name || _t("API key"),
    masked: typeof item.masked_key === "string" ? item.masked_key : "—",
    kind,
    status: item.status || "unknown",
    statusLabel: statusLabel(item.status),
    used: usedPoints === null ? "—" : String(usedPoints),
    total,
    limited,
    progressMax: limited ? totalPoints : null,
    progressValue: limited && usedPoints !== null ? Math.min(Math.max(usedPoints, 0), totalPoints) : 0,
    progressLabel: limited ? _t("Used %(used)s of %(total)s points", { used: usedPoints ?? 0, total: totalPoints }) : "",
    lastUse: formatTaipeiDateTime(item.last_used_at),
    expires: item.expires_at ? formatTaipeiDateTime(item.expires_at) : _t("Never expires"),
    created: formatTaipeiDateTime(item.created_at),
    expiring: isExpiringSoon(item.expires_at),
  };
}

const MANAGED_KEY_KINDS = new Set(["paid", "free_trial"]);
const MANAGED_KEY_STATUSES = new Set([
  "active", "disabled", "expired", "exhausted", "free_capped", "suspended", "overdraft", "failed_reveal",
]);
const PAID_MUTABLE_KEY_STATUSES = new Set([
  "active", "disabled", "expired", "exhausted", "free_capped", "suspended", "overdraft",
]);
const CONFIRM_ACTIONS = new Set(["extend", "disable", "delete"]);
const TOS_ERROR_CODES = new Set(["llm_tos_outdated", "llm_tos_required"]);

export function keyActionEligibility(item, tosBlocking = false) {
  const status = item && typeof item.status === "string" ? item.status : "unknown";
  const managed = MANAGED_KEY_KINDS.has(item?.kind) && MANAGED_KEY_STATUSES.has(status);
  if (!managed) return { edit: false, extend: false, disable: false, enable: false, delete: false };
  if (status === "failed_reveal") return { edit: false, extend: false, disable: false, enable: false, delete: true };
  const paidMutable = item.kind === "paid" && PAID_MUTABLE_KEY_STATUSES.has(status);
  const hasExpiry = typeof item.expires_at === "string" && !!item.expires_at;
  return {
    edit: paidMutable,
    extend: paidMutable && hasExpiry,
    disable: status === "active",
    enable: status === "disabled" && !tosBlocking,
    delete: true,
  };
}

function statusLabel(status) {
  const labels = { active: _t("Enabled"), disabled: _t("Disabled"), expired: _t("Expired"),
    exhausted: _t("Limit used"), free_capped: _t("Daily limit used"), suspended: _t("Suspended"),
    overdraft: _t("Credit overdrawn"), failed_reveal: _t("Creation interrupted"), unknown: _t("Unknown") };
  return Object.hasOwn(labels, status) ? labels[status] : labels.unknown;
}

export class LlmKeysTab extends Component {
  static template = "woow_paas_platform.LlmKeysTab";
  static components = { WoowIcon, WoowButton, LlmKeyFormModal, LlmKeyRevealModal, LlmKeyActionConfirmModal };
  static props = { onOpenDocs: { type: Function, optional: true } };

  setup() {
    this.service = useState(llmService);
    this.state = useState({
      loading: false, overviewReady: false, showForm: false, editKey: null, showReveal: false,
      pendingDraft: null, createError: null, confirmation: null, busyKey: null, notice: null,
      actionError: null, redirectToOverviewAfterReveal: false,
    });
    this.keysHeadingRef = useRef("keysHeading");
    this.destroyed = false;
    this.extendConsumed = new Set();
    onMounted(() => this.refresh());
    onWillUnmount(() => { this.destroyed = true; });
  }

  get tosBlocking() {
    if (this.state.overviewReady !== true) return true;
    if (this.service.overviewLoaded !== true || this.service.overviewError) return true;
    const tos = this.service.overview?.account?.tos;
    if (!tos || typeof tos !== "object" || Array.isArray(tos)) return true;
    if (typeof tos.blocking === "boolean") return tos.blocking;
    return tos.needs_reaccept === true;
  }
  get termsBlockedMessage() {
    return _t("Review and accept the AI Models Terms of Service in Overview before creating a paid key or enabling a key.");
  }
  get termsBlockedActionLabel() { return _t("View Overview and Terms"); }
  get rows() { return this.service.keys.map((item) => ({ ...keyRow(item), ...keyActionEligibility(item, this.tosBlocking) })); }
  get errorText() { return this.service.keysError ? llmErrorMessage({ code: this.service.keysError }) : ""; }
  get emptyText() { return _t("No API keys yet."); }
  get title() { return _t("API keys"); }
  get paidBaseUrl() {
    const value = this.service.overview && this.service.overview.public_base_url;
    return typeof value === "string" && value ? value : undefined;
  }
  get paidModelAllowlist() { return paidKeyModels(this.service.models?.items).map((item) => item.model); }

  async refresh() {
    if (this.state.loading) return;
    this.state.loading = true;
    this.state.overviewReady = false;
    let stateResult;
    try {
      [, stateResult] = await Promise.all([llmService.fetchKeys(), llmService.fetchState()]);
    } catch { /* a failed refresh must leave terms fail-closed */ }
    if (this.destroyed) return;
    this.state.loading = false;
    this.state.overviewReady = stateResult?.success === true;
    this.consumeExtendQuery();
  }

  consumeExtendQuery() {
    const location = globalThis.location;
    const hash = location?.hash || "";
    const query = extendQueryFromHash(hash);
    const result = applyExtendQueryOnce(query, this.service.keys, this.extendConsumed);
    if (query && result.consumed && globalThis.history?.replaceState) {
      const nextHash = stripExtendFromHash(hash);
      const base = location.href ? location.href.replace(/#.*$/, "") : "";
      globalThis.history.replaceState(globalThis.history.state, "", base + nextHash);
    }
    if (result.id !== null) {
      const item = this.service.keys.find((key) => key.id === result.id);
      if (item && keyActionEligibility(item, this.tosBlocking).extend) this.openConfirmation(item, "extend");
    }
  }

  goToOverview() { if (!this.destroyed) router.navigate("llm"); }
  openCreate() {
    if (this.destroyed || this.state.busyKey != null) return;
    if (this.tosBlocking) {
      this.state.createError = this.termsBlockedMessage;
      return;
    }
    this.state.createError = null;
    this.state.redirectToOverviewAfterReveal = false;
    this.state.showForm = true;
  }
  closeForm() { this.state.showForm = false; this.state.editKey = null; }
  edit(item) {
    if (!this.destroyed && this.state.busyKey == null && keyActionEligibility(item).edit) {
      this.state.editKey = item;
      this.state.showForm = true;
    }
  }
  openConfirmation(item, action) {
    if (this.destroyed || this.state.busyKey != null || !CONFIRM_ACTIONS.has(action)) return;
    const eligible = keyActionEligibility(item, this.tosBlocking);
    if (!eligible[action]) return;
    this.state.confirmation = { item, action };
    this.state.actionError = null;
  }
  closeConfirmation() { if (!this.destroyed && this.state.busyKey == null) this.state.confirmation = null; }
  async saveEdit(item, changes) {
    if (this.destroyed || this.state.busyKey != null || !keyActionEligibility(item).edit
        || !changes || typeof changes !== "object" || Array.isArray(changes) || !Object.keys(changes).length) {
      return { success: false, code: "llm_key_invalid" };
    }
    this.state.busyKey = item.id;
    this.state.actionError = null;
    let result;
    try { result = await llmService.updateKey(item.id, changes); }
    catch { result = { success: false, code: "llm_internal_error" }; }
    if (this.destroyed) return result;
    this.state.busyKey = null;
    if (result?.success) { this.state.showForm = false; this.state.editKey = null; this.state.notice = _t("API key settings updated."); }
    return result;
  }
  async enable(item) {
    if (this.destroyed || this.state.busyKey != null || !keyActionEligibility(item).enable) return;
    if (this.tosBlocking) {
      this.state.actionError = this.termsBlockedMessage;
      return { success: false, code: "llm_tos_required" };
    }
    this.state.busyKey = item.id;
    this.state.actionError = null;
    let result;
    try { result = await llmService.setKeyStatus(item.id, true); }
    catch { result = { success: false, code: "llm_internal_error" }; }
    if (this.destroyed) return result;
    const termsOutdated = TOS_ERROR_CODES.has(result?.code);
    if (termsOutdated) {
      this.state.overviewReady = false;
      let stateResult;
      try { stateResult = await llmService.fetchState(); } catch { /* safe service failures leave enable blocked */ }
      if (this.destroyed) return result;
      this.state.overviewReady = stateResult?.success === true;
    }
    this.state.busyKey = null;
    if (result?.success) this.state.notice = _t("API key enabled.");
    else {
      this.state.actionError = llmErrorMessage(result);
      if (termsOutdated) {
        this.state.notice = llmErrorMessage(result);
        router.navigate("llm");
      }
    }
  }
  async confirmAction(confirmName) {
    const pending = this.state.confirmation;
    if (!pending || this.destroyed || this.state.busyKey != null || !CONFIRM_ACTIONS.has(pending.action)) return;
    const { item, action } = pending;
    if (!keyActionEligibility(item, this.tosBlocking)[action]) return;
    if (action === "delete" && item.kind === "paid"
        && (typeof item.display_name !== "string" || confirmName !== item.display_name)) return;
    if (action === "delete" && item.kind === "free_trial") confirmName = "";
    this.state.busyKey = item.id;
    this.state.actionError = null;
    let result;
    try {
      if (action === "extend") result = await llmService.extendKey(item.id);
      else if (action === "disable") result = await llmService.setKeyStatus(item.id, false);
      else if (action === "delete") result = await llmService.deleteKey(item.id, confirmName);
    } catch { result = { success: false, code: "llm_internal_error" }; }
    if (this.destroyed) return result;
    this.state.busyKey = null;
    if (result?.success) {
      this.state.confirmation = null;
      this.state.notice = action === "extend" ? _t("API key expiry extended by 90 days.")
        : action === "disable" ? _t("API key disabled.") : _t("API key deleted.");
    } else this.state.actionError = llmErrorMessage(result);
    return result;
  }
  onDraft(draft) {
    if (this.destroyed) return;
    if (this.tosBlocking) {
      this.state.pendingDraft = null;
      this.state.showForm = false;
      this.state.showReveal = false;
      this.state.createError = this.termsBlockedMessage;
      return;
    }
    this.state.pendingDraft = { ...draft, models: draft.models === null ? null : [...draft.models] };
    this.state.showForm = false;
    this.state.createError = null;
    this.state.redirectToOverviewAfterReveal = false;
    this.state.showReveal = true;
  }
  async createPendingPaidKey(requestId) {
    this.state.createError = null;
    let result;
    try { result = await llmService.createPaidKey(requestId, this.state.pendingDraft); }
    catch { result = { success: false, code: "llm_key_create_interrupted" }; }
    if (this.destroyed) return result;
    if (!result || !result.success) this.setCreateError(result);
    if (TOS_ERROR_CODES.has(result?.code)) {
      this.state.overviewReady = false;
      let stateResult;
      try { stateResult = await llmService.fetchState(); } catch { /* terms remain fail-closed */ }
      if (this.destroyed) return result;
      this.state.overviewReady = stateResult?.success === true;
      this.state.redirectToOverviewAfterReveal = true;
    }
    return result;
  }
  setCreateError(result) {
    this.state.createError = result && typeof result.code === "string"
      ? llmErrorMessage({ code: result.code }) : _t("The key may have been created without being shown. Create a new key and disable or delete the old one if needed.");
  }
  focusKeysHeading() {
    const heading = this.keysHeadingRef.el;
    if (heading && heading.isConnected !== false && typeof heading.focus === "function") heading.focus();
  }
  onRevealClosed() {
    if (this.destroyed) return;
    const redirectToOverview = this.state.redirectToOverviewAfterReveal;
    this.state.showReveal = false;
    this.state.pendingDraft = null;
    this.state.redirectToOverviewAfterReveal = false;
    if (redirectToOverview) {
      router.navigate("llm");
      return;
    }
    this.focusKeysHeading();
    llmService.fetchKeys();
  }
}
