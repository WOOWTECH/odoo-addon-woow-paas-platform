/** @odoo-module **/

import { Component, onMounted, onPatched, useRef } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { router } from "../../core/router";
import { LLM_TABS, isLlmTabLive, llmTabPath, normalizeLlmTab } from "../../core/llm_routes";
import { LlmModelsTab } from "./tabs/LlmModelsTab";
import { LlmOverviewTab } from "./tabs/LlmOverviewTab";

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
 * 總覽與模型價格已上線，其他子頁依各自的票開放。
 *
 * 子頁列是「改網址的導覽」，不是 WAI-ARIA tabs（那要 roving tabindex、方向鍵、aria-controls）：
 * 照 ServiceDetailPage 的 .o_woow_tabs 用一般按鈕，外層 `<nav>` 帶名稱，目前的子頁標 aria-current。
 * 手機上子頁列可橫捲：載入（例如重新整理 `#/llm/docs`）與切換後把目前的子頁捲進可見範圍。
 */
export class LlmPage extends Component {
  static template = "woow_paas_platform.LlmPage";
  static components = { WoowIcon, LlmOverviewTab, LlmModelsTab };
  static props = {
    tab: { type: String, optional: true },
  };

  setup() {
    this.tabsRef = useRef("tabs");
    this.scrolledTab = null;
    onMounted(() => this.revealActiveTab());
    onPatched(() => this.revealActiveTab());
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

  get subtitle() {
    return _t("Call AI models with your credit through an OpenAI-compatible API.");
  }

  get comingSoonText() {
    return _t("This feature is coming soon. Stay tuned.");
  }

  setTab(tab) {
    router.navigate(llmTabPath(tab));
  }
}
