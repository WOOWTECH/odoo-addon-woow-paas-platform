/** @odoo-module **/

import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { router } from "../../core/router";
import { LLM_TABS, isLlmTabLive, llmTabPath, normalizeLlmTab } from "../../core/llm_routes";
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
 * 本票只上線「總覽」，其他子頁顯示「即將推出」。
 */
export class LlmPage extends Component {
  static template = "woow_paas_platform.LlmPage";
  static components = { WoowIcon, LlmOverviewTab };
  static props = {
    tab: { type: String, optional: true },
  };

  get currentTab() {
    return normalizeLlmTab(this.props.tab);
  }

  get tabs() {
    return LLM_TABS.map((id) => ({ id, icon: TAB_META[id].icon, label: TAB_META[id].label() }));
  }

  get currentTabLive() {
    return isLlmTabLive(this.currentTab);
  }

  get subtitle() {
    return _t("Call AI models with your credit through an OpenAI-compatible API.");
  }

  get comingSoonText() {
    return _t("This section is coming soon.");
  }

  setTab(tab) {
    router.navigate(llmTabPath(tab));
  }
}
