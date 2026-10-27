/** @odoo-module **/
import { _t } from "@web/core/l10n/translation";

/**
 * 2026-10-27：MCP 服務「還沒設定連線」提示的呈現邏輯（純函式）。
 *
 * 輸入是 `/mcp-status` 端點回的 `state`（後端 `services/mcp_setup_status.py` 從管理頁 `/healthz` 的
 * `mcp` 欄位讀出，已收斂成下面四個值）。這裡只做「代碼 → 文案」：提示畫不畫看 payload 的
 * `has_mcp_setup_status`（capability-gate-single-source-field），前端不用 slug 判斷。
 *
 * - `not_configured`：還沒在管理頁 → Connection 存好連線（新開的 Nextcloud MCP 在這之前一律回 503）；
 * - `not_running`：子行程沒有在跑（例如憑證錯誤後停止重試），原因在管理頁的 Logs；
 * - `running`／`unknown`：不顯示。`unknown`＝讀不到、舊映像沒有這個欄位，不能當成錯誤。
 *
 * 服務狀態本身不改：服務是正常的，只是要設定，徽章照樣是 Running。
 *
 * 文案放 JS（`_t` 單行字串）而不是模板的多行文字節點：模板文字的 msgid 會連同換行與縮排一起抽出，
 * 排版一動翻譯就對不上。
 */

/** 端點回的四個值（後端 `mcp_setup_status.STATES` 與此一一對應）；其他值一律當 `unknown`。 */
export const MCP_SETUP_STATES = ["running", "not_configured", "not_running", "unknown"];

export function normalizeMcpSetupState(value) {
  return MCP_SETUP_STATES.includes(value) ? value : "unknown";
}

/**
 * 要顯示的提示；`running`／`unknown`／其他值回 null（不畫）。
 *
 * @param {string|null|undefined} state
 * @returns {{state: string, tone: string, icon: string, text: string}|null}
 */
export function mcpSetupNotice(state) {
  if (state === "not_configured") {
    return {
      state,
      tone: "warning",
      icon: "settings",
      text: _t("MCP is not connected yet: open the admin page → Connection, fill it in and save. MCP starts providing tools only after that."),
    };
  }
  if (state === "not_running") {
    return {
      state,
      tone: "warning",
      icon: "warning",
      text: _t("The MCP child process is not running. Check Logs on the admin page."),
    };
  }
  return null;
}

/** 提示裡管理頁連結前的標籤（冒號一起翻：中文用全形）。 */
export function adminPageLabel() {
  return _t("Admin page:");
}

const HOST_CHARS = new Set("abcdefghijklmnopqrstuvwxyz0123456789.-");

/**
 * 管理頁網址只收 `https://<hostname>`（後端 `_admin_page_public_url` 的形狀）；其他一律 ""（不畫連結）。
 * 網址一律由後端給、前端不拼字串（#1542）；這一關只是防「哪天後端給了別的東西」被當成 href。
 */
export function safeAdminUrl(value) {
  if (typeof value !== "string" || !value) {
    return "";
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return "";
  }
  const host = parsed.hostname;
  const plainHost = !!host && host.includes(".") && [...host].every((char) => HOST_CHARS.has(char));
  const bare = parsed.pathname === "/" && !parsed.search && !parsed.hash;
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.port || !plainHost || !bare) {
    return "";
  }
  return value;
}
