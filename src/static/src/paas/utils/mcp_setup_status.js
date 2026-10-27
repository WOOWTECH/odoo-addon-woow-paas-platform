/** @odoo-module **/
import { _t } from "@web/core/l10n/translation";

/**
 * 2026-10-27：MCP 服務「還沒設定連線」提示的呈現邏輯（純函式）。
 *
 * 輸入是 `/mcp-status` 端點回的 `state` 與 `backend`（後端 `services/mcp_setup_status.py` 從管理頁
 * `/healthz` 的 `mcp`／`backend` 欄位讀出，已收斂成下面的封閉集合）。這裡只做「代碼 → 文案」：提示畫不畫
 * 看 payload 的 `has_mcp_setup_status`（capability-gate-single-source-field），前端不用 slug 判斷。
 *
 * 三種要租戶動手的情況（`mcpSetupCondition`）：
 * - `not_configured`：還沒在管理頁 → Connection 存好連線（新開的 Nextcloud MCP 在這之前一律回 503）；
 * - `not_running`：MCP 服務程式沒有在運作（例如憑證錯誤後停止重試），原因在管理頁的 Logs；
 * - `backend_missing`（第五輪）：MCP 在運作（`state=running`），但 `/healthz` 說後端連線還沒設定
 *   （`backend=missing`；odoo、litellm、hermes、n8n 才有這個欄位）——工具列得出來，用到後端的會失敗，
 *   所以是較輕的提示。
 * 其他（`running` 且 `backend` 是 configured／沒有欄位、`unknown`）不顯示：`unknown`＝讀不到、舊映像
 * 沒有這個欄位，不能當成錯誤。
 *
 * 文案分兩種說法（第五輪 D2-B）：能管理這台服務的人（owner／admin，payload 的 `viewer_can_manage`）
 * 拿得到管理密碼，叫他去管理頁設定；其他成員（guest、Portal 的 user）拿不到密碼，請他找工作區擁有者
 * 或管理員。文案不用「子行程」這類工程術語。
 *
 * 服務狀態本身不改：服務是正常的，只是要設定，徽章照樣是 Running；同一頁 Connection Info 的狀態改用
 * `mcpConnectionStatusFor` 的值（#1647 的規則：同一頁的提示說「不通／要設定」時，那一格不能寫 ONLINE）。
 *
 * 文案放 JS（`_t` 單行字串）而不是模板的多行文字節點：模板文字的 msgid 會連同換行與縮排一起抽出，
 * 排版一動翻譯就對不上。
 */

/** 端點回的四個 `state`（後端 `mcp_setup_status.STATES` 與此一一對應）；其他值一律當 `unknown`。 */
export const MCP_SETUP_STATES = ["running", "not_configured", "not_running", "unknown"];

/** 端點回的三個 `backend`（後端 `mcp_setup_status.BACKENDS` 與此一一對應）；其他值一律當 `unknown`。 */
export const MCP_BACKEND_STATES = ["configured", "missing", "unknown"];

/** `mcpSetupCondition` 的三個值；Connection Info 的狀態與提示都只認這三個。 */
export const MCP_SETUP_CONDITIONS = ["not_configured", "not_running", "backend_missing"];

export function normalizeMcpSetupState(value) {
  return MCP_SETUP_STATES.includes(value) ? value : "unknown";
}

export function normalizeMcpBackendState(value) {
  return MCP_BACKEND_STATES.includes(value) ? value : "unknown";
}

/**
 * 要租戶動手的情況；不需要時回 ""（falsy，模板直接拿來當 t-if）。
 * 與後端 `mcp_setup_status.is_actionable` 是同一個判斷（後端用它決定快取期限）。
 *
 * @param {string|null|undefined} state
 * @param {string|null|undefined} backend
 * @returns {""|"not_configured"|"not_running"|"backend_missing"}
 */
export function mcpSetupCondition(state, backend) {
  if (state === "not_configured" || state === "not_running") {
    return state;
  }
  if (state === "running" && backend === "missing") {
    return "backend_missing";
  }
  return "";
}

/**
 * 要顯示的提示；不是三種情況之一回 null（不畫）。
 *
 * @param {string|null|undefined} condition `mcpSetupCondition` 的結果
 * @param {boolean} canManage payload 的 `viewer_can_manage === true`（owner／admin）
 * @returns {{condition: string, tone: string, icon: string, text: string}|null}
 */
export function mcpSetupNotice(condition, canManage) {
  const manager = canManage === true;
  if (condition === "not_configured") {
    return {
      condition,
      tone: "warning",
      icon: "settings",
      text: manager
        ? _t("MCP is not connected yet: open the admin page → Connection, fill it in and save. MCP starts providing tools only after that.")
        : _t("MCP is not connected yet: ask a workspace owner or admin to set up the connection on the admin page. MCP starts providing tools only after that."),
    };
  }
  if (condition === "not_running") {
    return {
      condition,
      tone: "warning",
      icon: "warning",
      text: manager
        ? _t("The MCP server is not running. Check Logs on the admin page.")
        : _t("The MCP server is not running. Ask a workspace owner or admin to check Logs on the admin page."),
    };
  }
  if (condition === "backend_missing") {
    return {
      condition,
      tone: "info",
      icon: "info",
      text: manager
        ? _t("The backend connection is not set up yet, so tools that need the backend will not work: set it up under Connection on the admin page.")
        : _t("The backend connection is not set up yet, so tools that need the backend will not work: ask a workspace owner or admin to set it up on the admin page."),
    };
  }
  return null;
}

/**
 * Connection Info「狀態」那一格在三種情況下的值（OverviewTab 的 class 名稱）；其他回 ""（沿用原本的
 * online／updating／offline）。只在服務 running、沒有更嚴重的狀況（#1647 workload_fault、VPN 節點）時套用。
 *
 * @param {string|null|undefined} condition
 * @returns {""|"needs_setup"|"mcp_not_running"|"backend_missing"}
 */
export function mcpConnectionStatusFor(condition) {
  if (condition === "not_configured") return "needs_setup";
  if (condition === "not_running") return "mcp_not_running";
  if (condition === "backend_missing") return "backend_missing";
  return "";
}

/** `mcpConnectionStatusFor` 的值 → 那一格的文字（與提示同一種說法）；其他回 ""。 */
export function mcpConnectionStatusLabel(status) {
  if (status === "needs_setup") return _t("NEEDS SETUP");
  if (status === "mcp_not_running") return _t("MCP NOT RUNNING");
  if (status === "backend_missing") return _t("BACKEND NOT SET");
  return "";
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
