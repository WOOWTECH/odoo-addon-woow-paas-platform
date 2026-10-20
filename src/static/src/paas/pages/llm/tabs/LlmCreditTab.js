/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { billingService } from "../../../services/billing_service";
import { llmService } from "../../../services/llm_service";
import { formatAiQuota, formatTaipeiDateTime } from "../../../utils/llm_format";
import { llmErrorMessage } from "../../../services/llm_errors";
import { LlmConvertModal, creditOrderText, CREDIT_PENDING_STATES, creditPollDelay } from "../../../components/llm/LlmConvertModal";

export class LlmCreditTab extends Component {
  static template = "woow_paas_platform.LlmCreditTab";
  static components = { LlmConvertModal };
  static props = {};

  setup() {
    this.service = useState(llmService);
    this.state = useState({ items: [], total: 0, page: 1, loading: false, error: null, showConvert: false,
      threshold: "50", thresholdError: null, thresholdNotice: null, savingThreshold: false });
    this.destroyed = false;
    this.pollTimer = null;
    this.pollCount = 0;
    this.loadSeq = 0;
    this.thresholdInitialized = false;
    this.walletStates = new Map();
    onMounted(() => this.refresh());
    onWillUnmount(() => { this.destroyed = true; this.clearPoll(); });
  }
  get balanceText() {
    const quota = this.service.overview?.account?.balance_quota;
    return Number.isInteger(quota)
      ? _t("AI credit: %(balance)s", { balance: formatAiQuota(quota) })
      : _t("AI credit is not available right now.");
  }
  get hintText() { return _t("AI credit is only for AI models. Move it from your points wallet; once converted, it cannot be moved back."); }
  get blockedText() {
    const overview = this.service.overview;
    if (!overview) return llmErrorMessage({ code: this.service.overviewError || "llm_unavailable" });
    if (overview.pending_review || this.state.items.some(order => order.state === "needs_review")) return llmErrorMessage({ code: "llm_pending_review" });
    if (overview.guard?.tripped) return llmErrorMessage({ code: "llm_guard" });
    if (!overview.account) return llmErrorMessage({ code: "llm_not_activated" });
    if (overview.account.state !== "active") return llmErrorMessage({ code: overview.account.state === "error" ? "llm_account_error" : "llm_" + overview.account.state });
    return "";
  }
  get pageText() { return _t("Page %(page)s", { page: this.state.page }); }
  get thresholdVisible() { return ["active", "suspended"].includes(this.service.overview?.account?.state); }
  get rows() { return this.state.items.map(order => ({ ...order, time: formatTaipeiDateTime(order.created_at), status: creditOrderText(order.state) })); }
  async load(page = this.state.page, { refreshState = false } = {}) {
    const seq = ++this.loadSeq;
    this.clearPoll();
    this.state.loading = true;
    const [result, stateResult] = await Promise.all([
      llmService.fetchCreditOrders(page, 20),
      refreshState ? llmService.fetchState() : Promise.resolve(null),
    ]);
    if (this.destroyed || seq !== this.loadSeq) return;
    if (refreshState && stateResult?.success && !this.thresholdInitialized) {
      const value = stateResult.data?.account?.low_balance_threshold;
      this.state.threshold = Number.isInteger(value) && value >= 0 && value <= 100000 ? String(value) : "50";
      this.thresholdInitialized = true;
    }
    if (result.success && Array.isArray(result.data?.items)) {
      // Keep the Header wallet current even if a conversion finishes after its modal closes.
      const walletChanged = result.data.items.some(order => this.walletStates.get(order.id) !== order.state);
      for (const order of result.data.items) this.walletStates.set(order.id, order.state);
      if (walletChanged) {
        billingService.fetchWallet();
        if (!refreshState) await llmService.fetchState();
        if (this.destroyed || seq !== this.loadSeq) return;
      }
      this.state.loading = false;
      this.state.items = result.data.items;
      this.state.total = result.data.total;
      this.state.page = page;
      this.state.error = null;
      if (this.state.items.some(order => CREDIT_PENDING_STATES.includes(order.state)) && this.pollCount < 90) {
        this.pollCount++;
        this.pollTimer = setTimeout(() => this.load(), creditPollDelay(result.data.poll_after_ms));
      }
    } else {
      this.state.loading = false;
      this.state.error = llmErrorMessage(result);
    }
  }
  refresh() { return this.load(this.state.page, { refreshState: true }); }
  get thresholdInputError() {
    const raw = this.state.threshold;
    return typeof raw === "string" && /^(0|[1-9]\d*)$/.test(raw) && Number(raw) <= 100000 ? null : "llm_settings_invalid";
  }
  get thresholdInputErrorText() {
    return this.thresholdInputError ? llmErrorMessage({ code: this.thresholdInputError }) : "";
  }
  get thresholdSaveErrorText() {
    return this.state.thresholdError ? llmErrorMessage({ code: this.state.thresholdError }) : "";
  }
  setThreshold(value) {
    this.state.threshold = value;
    this.state.thresholdError = null;
    this.state.thresholdNotice = null;
  }
  async saveThreshold() {
    if (this.state.savingThreshold || this.thresholdInputError) return;
    this.state.savingThreshold = true;
    this.state.thresholdError = null;
    this.state.thresholdNotice = null;
    let result;
    try { result = await llmService.updateSettings(Number(this.state.threshold)); }
    catch { result = { success: false, code: "llm_internal_error" }; }
    if (this.destroyed) return;
    this.state.savingThreshold = false;
    if (result?.success) {
      if (this.service.overview?.account) this.service.overview.account.low_balance_threshold = Number(this.state.threshold);
      this.state.thresholdNotice = _t("Low balance threshold saved.");
    } else {
      this.state.thresholdError = result?.code || "llm_internal_error";
    }
  }
  openConvert() { if (!this.blockedText) this.state.showConvert = true; }
  onConvertClose() {
    this.state.showConvert = false;
    this.pollCount = 0;
    this.load(1, { refreshState: true });
  }
  clearPoll() { if (this.pollTimer !== null) clearTimeout(this.pollTimer); this.pollTimer = null; }
}
