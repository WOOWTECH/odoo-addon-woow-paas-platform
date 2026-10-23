/** @odoo-module **/

import { Component, onMounted, onPatched, onWillUnmount, onWillUpdateProps, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { router } from "../../core/router";
import { LLM_TABS, isLlmTabLive, llmTabPath, normalizeLlmTab } from "../../core/llm_routes";
import { LlmCreditTab } from "./tabs/LlmCreditTab";
import { LlmModelsTab } from "./tabs/LlmModelsTab";
import { LlmOverviewTab } from "./tabs/LlmOverviewTab";
import { LlmKeysTab } from "./tabs/LlmKeysTab";
import { LlmDocsTab } from "./tabs/LlmDocsTab";
import { llmService } from "../../services/llm_service";
import { llmErrorMessage } from "../../services/llm_errors";
import { formatTaipeiDateTime } from "../../utils/llm_format";
import { createLlmDocsKeyHandoff, validatedLlmPublicBaseUrl } from "./tabs/llm_docs_helpers";
import { LlmUsageTab } from "./tabs/LlmUsageTab";

/** 子頁的名稱與圖示（順序取 llm_routes.LLM_TABS）。名稱在呼叫時才 `_t()`。 */
const TAB_META = {
  overview: { icon: "dashboard", label: () => _t("Overview") },
  keys: { icon: "key", label: () => _t("API keys") },
  usage: { icon: "monitoring", label: () => _t("Usage & Logs") },
  models: { icon: "sell", label: () => _t("Models & Pricing") },
  credit: { icon: "account_balance_wallet", label: () => _t("AI Credit") },
  docs: { icon: "menu_book", label: () => _t("Usage Guide") },
};

/**
 * 「AI 模型」分頁的容器（DESIGN §6.1、〔REV〕58；票 LLM-01c）。
 *
 * 子頁用平台既有的水平 `.o_woow_tabs`（桌面與手機同一套、手機可橫捲）。**子頁的唯一來源是
 * 網址**（`props.tab`，由 Root 從 router.params 帶進來）：切換時只改 hash，重新整理、上一頁／
 * 下一頁都停在同一個子頁；本元件不另存一份 currentTab（那會與網址分岔）。
 * 總覽、模型價格、額度與使用說明已上線，其他子頁依各自的票開放。
 *
 * 子頁列是「改網址的導覽」，不是 WAI-ARIA tabs（那要 roving tabindex、方向鍵、aria-controls）：
 * 照 ServiceDetailPage 的 .o_woow_tabs 用一般按鈕，外層 `<nav>` 帶名稱，目前的子頁標 aria-current。
 * 手機上子頁列可橫捲：載入（例如重新整理 `#/llm/docs`）與切換後把目前的子頁捲進可見範圍。
 */
export class LlmPage extends Component {
  static template = "woow_paas_platform.LlmPage";
  static components = { WoowIcon, LlmOverviewTab, LlmModelsTab, LlmCreditTab, LlmKeysTab, LlmDocsTab, LlmUsageTab };
  static props = {
    tab: { type: String, optional: true },
  };

  setup() {
    this.priceNotices = useState({ items: [] });
    this.priceNoticeGeneration = 0;
    this.onNoticeFocus = () => this.refreshPriceNotices();
    this.tabsRef = useRef("tabs");
    this.llm = useState(llmService);
    this.docsKeyHandoff = createLlmDocsKeyHandoff();
    this.notMeToken = null;
    this.notMeSubmitting = false;
    this.notMeGeneration = 0;
    this.notMeState = useState({ confirmation: false, busy: false, error: null, done: false });
    this.onHashChange = () => this.consumeNotMeQuery();
    this.scrolledTab = null;
    onMounted(() => {
      this.refreshPriceNotices();
      window.addEventListener("focus", this.onNoticeFocus);
      this.revealActiveTab();
      window.addEventListener("hashchange", this.onHashChange);
      this.consumeNotMeQuery();
    });
    onPatched(() => this.revealActiveTab());
    onWillUnmount(() => {
      this.priceNoticeGeneration++;
      this.priceNotices.items = [];
      window.removeEventListener("focus", this.onNoticeFocus);
      window.removeEventListener("hashchange", this.onHashChange);
      this.docsKeyHandoff.clear();
      this.clearNotMe();
    });
    onWillUpdateProps((nextProps) => {
      if (nextProps.tab !== this.props.tab) this.refreshPriceNotices();
      if (normalizeLlmTab(nextProps.tab) !== "docs") this.docsKeyHandoff.clear();
      if (normalizeLlmTab(nextProps.tab) !== "overview") this.clearNotMe();
    });
  }

  async refreshPriceNotices() {
    const generation = ++this.priceNoticeGeneration;
    // Withdraw immediately while revalidating; failures never keep an old notice.
    this.priceNotices.items = [];
    try {
      const result = await llmService.fetchPriceNotices();
      if (generation !== this.priceNoticeGeneration) return;
      this.priceNotices.items = result?.success ? result.data.notices : [];
    } catch {
      // Transport failures are silent and do not block any of the six tabs.
    }
  }

  get priceNoticeModelsLabel() { return _t("Models & Pricing"); }

  get priceNoticeChangesLabel() { return _t("Price changes"); }
  get priceNoticeBeforeLabel() { return _t("Before"); }
  get priceNoticeAfterLabel() { return _t("After"); }
  get priceNoticeEffectiveLabel() { return _t("Effective from"); }
  get priceNoticeTimezoneLabel() { return _t("Taipei time"); }

  priceNoticeRows(notice) {
    return notice.changes.map((change, index) => {
      const fields = change.mode === "fixed"
        ? [{ id: "per_call", label: _t("Per call"), unit: _t("points per call") }]
        : [
          { id: "input", label: _t("Input"), unit: _t("points per million tokens") },
          { id: "output", label: _t("Output"), unit: _t("points per million tokens") },
          { id: "cache_read", label: _t("Cache read"), unit: _t("points per million tokens") },
          { id: "cache_write", label: _t("Cache write"), unit: _t("points per million tokens") },
        ];
      return {
        key: `${notice.id}-${index}`,
        model: change.model,
        rates: fields.map((field) => ({
          id: field.id,
          label: field.label,
          unit: field.unit,
          before: change.before[field.id],
          after: change.after[field.id],
        })),
      };
    });
  }

  priceNoticeEffectiveTime(notice) {
    return formatTaipeiDateTime(this.priceNoticeEffectiveIso(notice));
  }

  priceNoticeEffectiveIso(notice) {
    return new Date(notice.not_before * 1000).toISOString();
  }

  get currentTab() {
    return normalizeLlmTab(this.props.tab);
  }

  /** 目前的子頁在橫捲範圍外時捲進來（只捲子頁列本身：block 'nearest' 不動整頁的直向位置）。 */
  revealActiveTab() {
    if (this.scrolledTab === this.currentTab) return;
    const bar = this.tabsRef.el;
    const active = bar ? bar.querySelector(".o_woow_tab.active") : null;
    if (active && typeof active.scrollIntoView === "function") {
      active.scrollIntoView({ inline: "nearest", block: "nearest" });
      this.scrolledTab = this.currentTab;
    }
  }

  get tabs() {
    return LLM_TABS.map((id) => ({ id, icon: TAB_META[id].icon, label: TAB_META[id].label() }));
  }

  get currentTabLabel() {
    return TAB_META[this.currentTab].label();
  }

  get tabsLabel() {
    return _t("AI Models sections");
  }

  get currentTabLive() {
    return isLlmTabLive(this.currentTab);
  }

  get docsBaseUrl() {
    return validatedLlmPublicBaseUrl(this.llm.overview);
  }

  openDocsWithKey(key, kind, allowedModels) {
    this.docsKeyHandoff.put(key, kind === "free_trial" ? "free" : "paid", allowedModels);
  }

  consumeDocsKey() {
    this.docsKeyHandoff.clear();
  }

  get subtitle() {
    return _t("Call AI models with your credit through an OpenAI-compatible API.");
  }

  get comingSoonText() {
    return _t("This feature is coming soon. Stay tuned.");
  }

  setTab(tab) {
    if (normalizeLlmTab(tab) !== "docs") this.docsKeyHandoff.clear();
    if (normalizeLlmTab(tab) !== "overview") this.clearNotMe();
    router.navigate(llmTabPath(tab));
  }

  clearNotMe() {
    this.notMeGeneration++;
    this.notMeToken = null;
    this.notMeSubmitting = false;
    if (this.notMeState) Object.assign(this.notMeState, { confirmation: false, busy: false, error: null, done: false });
  }

  consumeNotMeQuery() {
    const hash = globalThis.location?.hash || "";
    const queryAt = hash.indexOf("?");
    const route = queryAt < 0 ? hash : hash.slice(0, queryAt);
    if (route !== "#/llm/overview") {
      this.clearNotMe();
      return;
    }
    if (queryAt < 0) return;
    const query = hash.slice(queryAt + 1);
    let values = [];
    try { values = new URLSearchParams(query).getAll("not_me"); } catch { /* malformed query is ignored */ }
    if (!values.length) return;
    let token = values.length === 1 ? values[0] : null;
    const tokenMatch = typeof token === "string" ? token.match(/^[A-Za-z0-9_.-]{1,512}$/) : null;
    if (!tokenMatch || tokenMatch[0] !== token) token = null;
    const params = new URLSearchParams(query);
    params.delete("not_me");
    const nextHash = route + (params.toString() ? "?" + params.toString() : "");
    const base = globalThis.location.href ? globalThis.location.href.replace(/#.*$/, "") : "";
    globalThis.history?.replaceState(globalThis.history.state, "", base + nextHash);
    this.clearNotMe();
    if (token) {
      this.notMeToken = token;
      this.notMeState.confirmation = true;
    } else {
      this.notMeState.error = "llm_not_me_invalid";
      this.notMeState.confirmation = true;
    }
  }

  dismissNotMe() { this.clearNotMe(); }

  async confirmNotMe() {
    if (!this.notMeToken || this.notMeSubmitting || this.notMeState.busy) return;
    const generation = this.notMeGeneration;
    const token = this.notMeToken;
    this.notMeSubmitting = true;
    this.notMeState.busy = true;
    this.notMeState.error = null;
    let result;
    try { result = await llmService.reportNotMe(token); }
    catch { result = { success: false, code: "llm_internal_error" }; }
    if (generation !== this.notMeGeneration || this.notMeToken !== token) return;
    this.notMeSubmitting = false;
    this.notMeState.busy = false;
    if (result?.success) {
      this.clearNotMe();
      this.notMeState.done = true;
      await llmService.fetchState();
    } else {
      this.notMeState.error = result?.code || "llm_internal_error";
    }
  }

  get notMeErrorText() { return this.notMeState.error ? llmErrorMessage({ code: this.notMeState.error }) : ""; }
}
