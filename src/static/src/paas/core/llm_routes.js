/** @odoo-module **/

/**
 * 「AI 模型」分頁的路由表、子頁白名單與 gate 判斷（DESIGN §6.1；票 LLM-01c）。
 *
 * 純模組：**不 import** owl、`@web/session`、`_t`——`tests/ui/test_llm_nav.mjs` 直接用 node
 * 載入它驗證路由與 gate。router.js、platform_config.js、Root、LlmPage 的判準都從這裡取，
 * 不各寫一份（子頁白名單漏寫一處 ⇒ 換頁後重新整理就掉進 NotFoundPage）。
 */

/** 只有一個 routeName（〔REV〕57）：帶子頁與不帶子頁兩條 route 共用它，Header 才不會把子頁當成下一層。 */
export const LLM_ROUTE_NAME = "llm";

/** 子頁白名單，順序＝分頁列的顯示順序（DESIGN §6.3）。 */
export const LLM_TABS = Object.freeze(["overview", "keys", "usage", "models", "credit", "docs"]);

export const LLM_DEFAULT_TAB = "overview";

/** 本票只上線「總覽」；其他子頁在各自的票上線時加進來，在那之前顯示「即將推出」。 */
export const LLM_LIVE_TABS = Object.freeze(["overview"]);

/** `llm/<子頁>`：帶子頁的那條 route（pattern 在前、exact `llm` 在後，照 router.js 的雙路由慣例）。 */
export const LLM_TAB_PATTERN = new RegExp("^llm\\/(" + LLM_TABS.join("|") + ")$");

/**
 * router.js 的兩條 route。`label` 由呼叫端用 `_t()` 傳進來（本模組不碰翻譯）。
 * 兩條的 `name` 相同：Header 的麵包屑取「第一個 routeName 相符」的 route 名稱。
 */
export function buildLlmRoutes(label) {
  return [
    { path: "llm/:tab", name: label, routeName: LLM_ROUTE_NAME, pattern: LLM_TAB_PATTERN },
    { path: "llm", name: label, routeName: LLM_ROUTE_NAME },
  ];
}

/** 不在白名單的值（含 `undefined`：exact `llm` 那條沒有子頁）一律退回總覽。 */
export function normalizeLlmTab(tab) {
  return LLM_TABS.includes(tab) ? tab : LLM_DEFAULT_TAB;
}

export function isLlmTabLive(tab) {
  return LLM_LIVE_TABS.includes(tab);
}

/** 切換子頁時寫回 hash 的路徑（重新整理後停在同一個子頁）。 */
export function llmTabPath(tab) {
  return LLM_ROUTE_NAME + "/" + normalizeLlmTab(tab);
}

/**
 * 外觀開關：讀 `/woow` bootstrap 注入的 `session.woow_show_llm`（後端 `llm_visible`，與
 * `/api/llm/*` 的 gate 同一個判準）。fail closed：缺 key、非布林 true 一律隱藏。
 */
export function llmVisibleFromSession(session) {
  return !!session && session.woow_show_llm === true;
}

/**
 * 開關關時把 `llm` 收斂到總覽（照 router.js 的 `_applyProjectGate`）。
 * 回傳要導去的 routeName；不需要導向時回 `null`。
 */
export function llmGateRedirect(routeName, visible) {
  return routeName === LLM_ROUTE_NAME && visible !== true ? "dashboard" : null;
}
