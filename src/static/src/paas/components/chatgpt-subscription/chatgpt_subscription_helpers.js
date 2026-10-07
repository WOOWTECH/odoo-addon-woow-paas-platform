/** @odoo-module **/
import { _t } from "@web/core/l10n/translation";
import { formatUserDate, formatUserDateTime } from "../../utils/datetime_helpers";

/**
 * LiteLLM「ChatGPT 訂閱」卡片與連接視窗共用的顯示 helper。
 *
 * 狀態資料一律是後端白名單過的視圖（`_chatgpt_subscription_view`），不含 token；
 * `pending.user_code` 是一次性登入代碼——只顯示，不 console.log、不放進網址。
 */

/** 方案代碼（ChatGPT 的 chatgpt_plan_type）→ 顯示字。未知代碼原樣顯示。 */
export function chatgptPlanLabel(planType) {
  const labels = {
    plus: "ChatGPT Plus",
    pro: "ChatGPT Pro",
    team: "ChatGPT Team",
    business: "ChatGPT Business",
    enterprise: "ChatGPT Enterprise",
    edu: "ChatGPT Edu",
    free: "ChatGPT Free",
  };
  if (!planType) {
    return _t("Unknown");
  }
  return labels[String(planType).toLowerCase()] || String(planType);
}

/** epoch 秒 → 使用者語系的日期時間；沒有值回空字串。 */
export function formatEpochSeconds(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) {
    return "";
  }
  return formatUserDateTime(new Date(seconds * 1000));
}

/** ChatGPT 的 subscription_active_until：可能是 epoch 秒或 ISO 字串；讀不懂就原樣顯示。 */
export function formatSubscriptionUntil(value) {
  if (typeof value === "number") {
    return formatEpochSeconds(value);
  }
  if (typeof value === "string" && value) {
    return formatUserDate(value, value);
  }
  return "";
}

/** 剩餘毫秒 → m:ss。 */
export function formatCountdown(ms) {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

/** add_models 的結果 → 一句話（新加入／原本就有）。 */
export function describeAddedModels(data) {
  const added = (data && data.added) || [];
  const kept = (data && data.kept) || [];
  const parts = [];
  if (added.length) {
    parts.push(_t("Added: %s.", added.join(", ")));
  }
  if (kept.length) {
    parts.push(_t("Already there: %s.", kept.join(", ")));
  }
  if (!parts.length) {
    parts.push(_t("No models were added."));
  }
  return parts.join(" ");
}
