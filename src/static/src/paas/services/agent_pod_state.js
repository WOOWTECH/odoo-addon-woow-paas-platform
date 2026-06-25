/** @odoo-module **/

/**
 * 共用的 agent pod state → zh-tw 短標籤映射。
 *
 * 單一來源（single source of truth），供：
 *   - portal PortalAgentStatus（portal/portal_agent_status.js）— 一律顯示標籤，
 *     未知 state fallback 到「未知」。
 *   - /woow ProjectKanbanPage（pages/project-kanban/ProjectKanbanPage.js）—
 *     僅在 agent config-ready 時顯示；未知 state 不顯示（空字串），
 *     用 podStateLabel(state, "") 取得空 fallback。
 *
 * 抽共用前兩處各有一份手寫映射且漂移（kanban 缺 spawning/unknown），
 * 此模組合併兩者並補齊，避免重複維護。
 */
export const POD_STATE_LABEL = {
  absent: "未啟動",
  spawning: "啟動中",
  active: "運行中",
  reaped: "閒置",
  unknown: "未知",
};

/**
 * 把 pod state 轉成 zh-tw 標籤。
 *
 * @param {string} state - pod state（absent / spawning / active / reaped / unknown）
 * @param {string} [fallback] - 未知 state 的 fallback；
 *   省略時回傳「未知」（portal 語意），傳 "" 則回傳空字串（kanban 語意：不顯示）。
 * @returns {string}
 */
export function podStateLabel(state, fallback = POD_STATE_LABEL.unknown) {
  return POD_STATE_LABEL[state] ?? fallback;
}
