/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { WoowButton } from "../../../components/button/WoowButton";
import { LlmKeyFormModal } from "../../../components/llm/LlmKeyFormModal";
import { LlmKeyRevealModal } from "../../../components/llm/LlmKeyRevealModal";
import { llmService } from "../../../services/llm_service";
import { llmErrorMessage } from "../../../services/llm_errors";
import { formatTaipeiDateTime, isExpiringSoon } from "../../../utils/llm_format";

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

function statusLabel(status) {
  const labels = { active: _t("Enabled"), disabled: _t("Disabled"), expired: _t("Expired"),
    exhausted: _t("Limit used"), failed_reveal: _t("Creation interrupted"), unknown: _t("Unknown") };
  return Object.hasOwn(labels, status) ? labels[status] : labels.unknown;
}

export class LlmKeysTab extends Component {
  static template = "woow_paas_platform.LlmKeysTab";
  static components = { WoowIcon, WoowButton, LlmKeyFormModal, LlmKeyRevealModal };
  static props = {};

  setup() {
    this.service = useState(llmService);
    this.state = useState({ loading: false, showForm: false, showReveal: false, pendingDraft: null, createError: null, extendConfirmation: null, notice: null });
    this.keysHeadingRef = useRef("keysHeading");
    this.destroyed = false;
    this.extendConsumed = new Set();
    onMounted(() => this.refresh());
    onWillUnmount(() => { this.destroyed = true; });
  }

  get rows() { return this.service.keys.map(keyRow); }
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
    await llmService.fetchKeys();
    if (this.destroyed) return;
    this.state.loading = false;
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
      this.state.extendConfirmation = result.id;
      this.state.notice = _t("Review the 90-day extension for this key. No change has been submitted.");
    }
  }

  openCreate() { this.state.showForm = true; }
  closeForm() { this.state.showForm = false; }
  onDraft(draft) {
    this.state.pendingDraft = { ...draft, models: draft.models === null ? null : [...draft.models] };
    this.state.showForm = false;
    this.state.createError = null;
    this.state.showReveal = true;
  }
  createPendingPaidKey(requestId) {
    this.state.createError = null;
    return Promise.resolve().then(() => llmService.createPaidKey(requestId, this.state.pendingDraft)).then((result) => {
      if (!result || !result.success) this.setCreateError(result);
      return result;
    }, () => {
      const result = { success: false, code: "llm_key_create_interrupted" };
      this.setCreateError(result);
      return result;
    });
  }
  setCreateError(result) {
    this.state.createError = result && typeof result.code === "string"
      ? llmErrorMessage({ code: result.code }) : _t("The key may have been created without being shown. Create a new key and disable or delete the old one if needed.");
  }
  onRevealClosed() {
    this.state.showReveal = false;
    this.state.pendingDraft = null;
    const heading = this.keysHeadingRef.el;
    if (heading && typeof heading.focus === "function") heading.focus();
    llmService.fetchKeys();
  }
  cancelExtend() { this.state.extendConfirmation = null; }
  confirmExtend() {
    this.state.extendConfirmation = null;
    this.state.notice = _t("Extension request is ready for the next implementation step.");
  }
}
