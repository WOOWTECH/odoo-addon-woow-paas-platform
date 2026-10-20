/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { llmService, newClientRequestId } from "../../services/llm_service";
import { LLM_NETWORK_ERROR, llmErrorMessage } from "../../services/llm_errors";
import { billingService } from "../../services/billing_service";
import { inertOutside, trapTab } from "../../utils/modal_focus";
import { LlmTermsModal } from "./LlmTermsModal";

export function parseCreditPoints(value) {
  if (typeof value !== "string" && typeof value !== "number") return null;
  if (!/^\d+$/.test(String(value))) return null;
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 50 && number <= 10000 ? number : null;
}

export function creditPollDelay(value) {
  return Number.isFinite(value) ? Math.max(1000, Math.min(30000, value)) : 2000;
}

export function creditOrderText(state) {
  switch (state) {
    case "credited": return _t("Credited");
    case "unknown": return _t("Confirming (usually a few minutes, up to about 15 minutes; your points will not be lost).");
    case "needs_review": return _t("Our team is reviewing this conversion. Your points will not be lost; we will handle it and notify you as soon as possible.");
    case "voided": return _t("Conversion did not complete. Your points have been returned to your points wallet.");
    case "void_failed": return _t("Our team is checking the return of your points. Your points will not be lost.");
    case "credit_lost": return _t("Our team is restoring this AI credit. We will notify you as soon as possible.");
    case "cancelled": return _t("Conversion cancelled.");
    default: return _t("Processing…");
  }
}

export const CREDIT_PENDING_STATES = Object.freeze(["paid", "sending", "unknown"]);

/** One component lifetime is one idempotent flow. Never replace the request ID after a timeout. */
export class LlmConvertModal extends Component {
  static template = "woow_paas_platform.LlmConvertModal";
  static components = { LlmTermsModal };
  static props = { onClose: { type: Function } };

  setup() {
    this.state = useState({ points: "100", preview: null, previewLoading: false,
      pointsLocked: false, acceptNoRefund: false, confirmReserve: false, busy: false, verifying: false,
      verifiedUntil: 0, termsBlocked: false, showTerms: false, phase: "form", order: null, error: null });
    this.service = useState(llmService);
    this.clientRequestId = newClientRequestId();
    this.quickPoints = [100, 300, 500, 1000];
    this.backdropRef = useRef("backdrop");
    this.dialogRef = useRef("dialog");
    this.passwordRef = useRef("password");
    this.previewSeq = 0;
    this.destroyed = false;
    this.pollTimer = null;
    this.reauthTimer = null;
    this.pollCount = 0;
    this.pollStarted = null;
    this.walletStates = new Set();
    this.returnFocus = null;
    this.lookupInFlight = false;
    this.pollInFlight = false;
    this.restoreInert = null;
    this.onKeydown = ev => {
      if (this.state.showTerms) return;
      if (ev.key === "Escape") this.requestClose();
      else if (ev.key === "Tab") trapTab(ev, this.dialogRef.el);
    };
    onMounted(() => {
      this.returnFocus = document.activeElement;
      document.addEventListener("keydown", this.onKeydown);
      this.restoreInert = inertOutside(this.backdropRef.el);
      this.dialogRef.el?.focus();
      this.loadPreview();
    });
    onWillUnmount(() => this.teardown());
  }

  get points() { return parseCreditPoints(this.state.points); }
  get termsBlocked() {
    return this.state.termsBlocked || this.service.overview?.account?.tos?.blocking === true
      || this.state.preview?.tos?.blocking === true;
  }
  get needsReauth() { return this.points > 1000 && this.state.verifiedUntil <= Date.now(); }
  get insufficientBalance() { return this.state.preview?.balance_after < 0; }
  get insufficientText() { return llmErrorMessage({ code: "insufficient_balance" }); }
  get reserveShort() { return Number(this.state.preview?.reserve?.short_by) > 0; }
  get canSubmit() {
    return this.state.phase === "form" && !this.state.busy && !this.state.verifying
      && !this.state.previewLoading && !!this.state.preview && this.points !== null
      && this.state.acceptNoRefund && (!this.reserveShort || this.state.confirmReserve)
      && !this.insufficientBalance && !this.needsReauth && !this.termsBlocked && this.state.preview.pending_review !== true;
  }
  get heading() { return _t("Move points wallet balance to AI credit"); }
  get closeLabel() { return _t("Close"); }
  get noRefundText() { return _t("I understand: converted AI credit cannot be returned as points or cash, including when I close my AI models account."); }
  get rangeText() { return _t("Choose 50–10,000 whole points. 1 point = NT$1 (tax included); the conversion rate is fixed at NT$32 per US$1."); }
  get walletText() {
    const p = this.state.preview;
    return p ? _t("Points wallet: %(before)s points → %(after)s points", { before: Math.floor(p.wallet_balance), after: Math.floor(p.balance_after) }) : "";
  }
  get creditText() {
    const p = this.state.preview;
    return p ? _t("AI credit: %(before)s points → %(after)s points", { before: Math.floor(p.quota_after_points - this.points), after: Math.floor(p.quota_after_points) }) : "";
  }
  get reserveText() {
    const p = this.state.preview;
    return p ? _t("Your points wallet will have %(after)s points left, below the %(needed)s points needed for renewals due within 7 days. Those services may be suspended if payment fails. Convert anyway?", { after: Math.floor(p.balance_after), needed: Math.floor(p.reserve?.needed_7d) }) : "";
  }
  get statusText() { return creditOrderText(this.state.order?.state || "unknown"); }
  get pendingReviewText() { return llmErrorMessage({ code: "llm_pending_review" }); }
  get termsText() { return llmErrorMessage({ code: "llm_tos_outdated" }); }
  get rangeError() { return llmErrorMessage({ code: "llm_points_out_of_range" }); }

  async setPoints(value) {
    if (this.state.busy || this.state.pointsLocked || this.state.phase !== "form") return;
    this.state.points = String(value);
    this.state.confirmReserve = false;
    await this.loadPreview();
  }
  async loadPreview() {
    const seq = ++this.previewSeq;
    this.state.preview = null;
    this.state.error = null;
    this.state.previewLoading = false;
    const points = this.points;
    if (points === null) return;
    this.state.previewLoading = true;
    const result = await llmService.previewCredit(points);
    if (this.destroyed || seq !== this.previewSeq) return;
    this.state.previewLoading = false;
    if (result.success) this.state.preview = result.data;
    else this.state.error = llmErrorMessage(result);
  }

  async verifyIdentity() {
    if (this.state.verifying || this.state.busy || this.state.phase !== "form") return;
    let password = this.passwordRef.el?.value || "";
    if (this.passwordRef.el) this.passwordRef.el.value = "";
    if (!password) return;
    this.state.verifying = true;
    this.state.error = null;
    const pending = llmService.reauth(password);
    password = "";
    const result = await pending;
    if (this.destroyed) return;
    this.state.verifying = false;
    if (result.success) {
      const until = Date.parse(result.data?.reauth_until);
      this.state.verifiedUntil = Number.isFinite(until) ? until : Date.now() + 600000;
      this.scheduleReauthExpiry();
    } else this.state.error = llmErrorMessage(result);
  }

  async submit() {
    if (!this.canSubmit) return;
    if (this.pollStarted === null) this.pollStarted = Date.now();
    this.state.busy = true;
    this.state.error = null;
    const result = await llmService.convertCredit({ points: this.points, client_request_id: this.clientRequestId,
      confirm_reserve: this.state.confirmReserve, accept_no_refund: true });
    if (this.destroyed) return;
    if (result.success && result.data?.order_id) {
      await this.acceptOrder({ ...result.data, id: result.data.order_id });
    } else if (result.code === LLM_NETWORK_ERROR || result.success) {
      this.state.phase = "confirming";
      this.state.pointsLocked = true;
      await this.recoverOrder();
    } else {
      this.state.error = llmErrorMessage(result);
      if (result.code === "llm_tos_outdated") this.state.termsBlocked = true;
      if (result.code === "llm_reauth_required") {
        this.clearReauthExpiry();
        this.state.verifiedUntil = 0;
      }
      if (result.code === "llm_pending_review" && this.state.preview) this.state.preview.pending_review = true;
      if (result.code === "llm_reserve_confirm_required") {
        this.state.confirmReserve = false;
        await this.loadPreview();
        this.state.error = llmErrorMessage(result);
      }
    }
    if (!this.destroyed) this.state.busy = false;
  }

  async recoverOrder() {
    if (this.destroyed || this.lookupInFlight) return;
    this.lookupInFlight = true;
    try {
      const result = await llmService.findCreditOrder(this.clientRequestId);
      if (this.destroyed) return;
      if (result.success && Array.isArray(result.data?.items)) {
        const order = result.data.items[0];
        if (order) await this.acceptOrder(order);
        else { this.state.phase = "form"; this.clearPoll(); }
      } else {
        this.state.phase = "confirming";
        this.schedulePoll(() => this.recoverOrder(), 3000);
      }
    } finally {
      this.lookupInFlight = false;
    }
  }

  async acceptOrder(order) {
    if (this.destroyed) return;
    this.state.order = order;
    this.state.phase = "order";
    this.clearPoll();
    // Once per order status: update the shared Header badge on acceptance and on void.
    const walletKey = order.id + ":" + (order.state === "voided" ? "voided" : "accepted");
    if (!this.walletStates.has(walletKey)) {
      this.walletStates.add(walletKey);
      await billingService.fetchWallet();
    }
    if (this.destroyed) return;
    if (CREDIT_PENDING_STATES.includes(order.state)) this.schedulePoll(() => this.pollOrder(), order.poll_after_ms);
  }
  async pollOrder() {
    if (this.destroyed || this.pollInFlight || !this.state.order) return;
    this.pollInFlight = true;
    try {
      const result = await llmService.fetchCreditOrder(this.state.order.id);
      if (this.destroyed) return;
      if (result.success && result.data?.id) await this.acceptOrder(result.data);
      else this.schedulePoll(() => this.pollOrder(), 5000);
    } finally {
      this.pollInFlight = false;
    }
  }

  scheduleReauthExpiry() {
    this.clearReauthExpiry();
    // Date.now() in a getter cannot cause an OWL render while the form is idle.
    this.reauthTimer = setTimeout(() => {
      this.reauthTimer = null;
      if (!this.destroyed) this.state.verifiedUntil = 0;
    }, Math.max(0, this.state.verifiedUntil - Date.now()));
  }
  clearReauthExpiry() {
    if (this.reauthTimer !== null) clearTimeout(this.reauthTimer);
    this.reauthTimer = null;
  }

  schedulePoll(callback, delay) {
    this.clearPoll();
    if (this.destroyed || this.pollStarted === null || this.pollCount >= 90 || Date.now() - this.pollStarted >= 900000) return;
    this.pollCount++;
    this.pollTimer = setTimeout(() => { this.pollTimer = null; callback(); }, creditPollDelay(delay));
  }
  clearPoll() {
    if (this.pollTimer !== null) clearTimeout(this.pollTimer);
    this.pollTimer = null;
  }
  async onTermsClose(result) {
    this.state.showTerms = false;
    if (result.accepted) {
      this.state.termsBlocked = false;
      await llmService.fetchState();
      if (!this.destroyed) await this.loadPreview();
    }
    this.dialogRef.el?.focus();
  }
  requestClose() {
    if (this.state.busy || this.state.verifying || this.state.showTerms || this.destroyed) return;
    this.teardown();
    this.props.onClose();
  }
  onBackdropClick(ev) { if (ev.target === ev.currentTarget) this.requestClose(); }
  teardown() {
    this.destroyed = true;
    this.clearPoll();
    this.clearReauthExpiry();
    if (this.passwordRef.el) this.passwordRef.el.value = "";
    if (typeof document !== "undefined") document.removeEventListener("keydown", this.onKeydown);
    this.restoreInert?.();
    this.restoreInert = null;
    this.returnFocus?.focus();
    this.returnFocus = null;
  }
}
