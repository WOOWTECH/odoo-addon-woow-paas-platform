/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";

/**
 * `/api/llm/*` 的錯誤碼 → 使用者看得到的句子（DESIGN §3.1、§3.4；照 topup_errors.js）。
 *
 * - 只依 `code` 與數字參數（`retry_after`、`max_keys`、`used`、`needed_7d`、`short_by`…）組句；
 *   **不顯示**後端的 `error` 文字、更不顯示 new-api 或例外的原文（可能帶上游主機名或例外內容）。
 *   不認得的 code 一律回固定的「系統發生錯誤」（後端每一個失敗都帶 code，碼表見 llm_common.py；
 *   tests/ui/test_llm_errors.mjs 對帳兩邊）。
 * - msgid 盡量與後端 controllers/llm.py 的 fallback 相同（同一條譯文，畫面與 API 一致）。
 * - 每一格都是函式：`_t()` 在呼叫當下才求值（模組載入時翻譯可能還沒載入）。
 */

/** 前端自己產生的碼：請求沒有拿到後端回應（逾時、網路、HTTP 錯誤、JSON-RPC 例外）。 */
export const LLM_NETWORK_ERROR = "llm_network_error";
/**
 * 建金鑰的請求沒有拿到回應：金鑰**可能**已經建好、只是沒顯示（DESIGN §3.5）。不能說「失敗」，
 * 也不能讓使用者以為按重試就會拿回同一把——明文只在那一次回應裡。
 */
export const LLM_KEY_CREATE_INTERRUPTED = "llm_key_create_interrupted";

function minutesFrom(seconds) {
  const n = Number(seconds);
  return Number.isFinite(n) && n > 0 ? Math.max(1, Math.ceil(n / 60)) : null;
}

function points(value) {
  const n = Number(value);
  return Number.isFinite(n) ? String(Math.floor(n)) : null;
}

const KEY_CANNOT_ENABLE = {
  expired: () => _t("This key has expired. Please extend its expiry date first."),
  exhausted: () => _t("This key has used up its total limit. Please raise the limit first."),
  free_capped: () => _t("Today's free trial is used up. It restores automatically at 00:00 Taipei time."),
  suspended: () => _t("The AI models service is suspended, so keys cannot be enabled."),
  overdraft: () => _t("Your AI credit is overdrawn. Please move credit from your points wallet first."),
};

const CODE_MAP = {
  llm_disabled: () => _t("This feature is not available yet."),
  llm_not_activated: () => _t("Please activate the AI models service first."),
  llm_tos_required: () => _t("Please accept the AI Models Terms of Service first."),
  llm_tos_outdated: () => _t("The AI Models Terms of Service have been updated. Please accept them again."),
  llm_provisioning: () => _t("Your AI models account is being set up. Please wait a moment."),
  llm_suspended: () => _t("The AI models service is suspended."),
  llm_closed: () => _t("The AI models account is closed. Please contact support to reactivate it."),
  llm_unavailable: () => _t("The AI models service is temporarily unavailable. Please try again later."),
  llm_guard: () => _t("The system is under maintenance, so keys cannot be created and points cannot be converted right now. Existing keys keep working."),
  llm_rate_limited: (params) => {
    const minutes = minutesFrom(params.retry_after);
    return minutes
      ? _t("Too many requests. Please try again in %(minutes)s minute(s).", { minutes })
      : _t("Too many requests. Please try again later.");
  },
  llm_key_limit: () => _t("Each account can have at most 20 keys. Please delete unused keys first."),
  llm_free_key_exists: () => _t("Each account can have only one free trial key."),
  llm_free_capped_today: () => _t("Today's free trial is used up. You can create a free trial key again after 00:00 Taipei time."),
  llm_free_key_daily_limit: () => _t("You have reached today's limit for creating free trial keys. Please try again tomorrow."),
  llm_key_not_found: () => _t("This key could not be found."),
  llm_key_already_shown: () => _t("This key has already been shown and cannot be shown again. Please delete it and create a new one."),
  llm_key_invalid: () => _t("The key settings are invalid."),
  llm_key_limit_below_used: (params) => {
    const used = points(params.used);
    return used === null
      ? _t("The total limit cannot be lower than what this key has already used.")
      : _t("This key has already used %(used)s points; the total limit cannot be lower than that.", { used });
  },
  llm_key_cannot_enable: (params) => {
    const reason = KEY_CANNOT_ENABLE[params.reason];
    return reason ? reason() : _t("This key cannot be enabled right now.");
  },
  llm_reauth_required: () => _t("To protect your account, please verify your identity again before continuing."),
  llm_pending_review: () => _t("Your previous conversion is being reviewed by our team. You can convert again after it finishes; your points will not be lost."),
  llm_points_out_of_range: () => _t("Each conversion must be between 50 and 10,000 points."),
  insufficient_balance: () => _t("Your points wallet balance is too low. Please top up first."),
  llm_reserve_confirm_required: (params) => {
    const needed = points(params.needed_7d);
    const short = points(params.short_by);
    return needed !== null && short !== null
      ? _t("After this conversion your points wallet will be %(short)s points short of the %(needed)s points needed for renewals due within 7 days, and those services may be suspended if payment fails.", { needed, short })
      : _t("After this conversion your points wallet may not cover renewals due within 7 days.");
  },
  llm_no_refund_ack_required: () => _t("Please confirm that converted AI credit cannot be refunded."),
  llm_invalid_range: () => _t("You can query at most 31 days at a time."),
  llm_account_error: () => _t("Setting up your AI models account failed. Please contact support."),
  llm_internal_error: () => _t("Something went wrong. Please try again later."),
  llm_auth_required: () => _t("Authentication required"),
  [LLM_NETWORK_ERROR]: () => _t("The AI models service is temporarily unavailable. Please try again later."),
  [LLM_KEY_CREATE_INTERRUPTED]: () => _t("The key may have been created without being shown. Refresh the list and create a new one; a key marked Creation interrupted cannot be used."),
};

/** tests/ui/test_llm_errors.mjs 用：碼表涵蓋了哪些 code。 */
export const LLM_ERROR_CODES = Object.freeze(Object.keys(CODE_MAP));

/**
 * `result`＝`llm_service` 回的失敗物件（`{success:false, code, …參數}`）。
 * 永遠回一句固定的話：不認得的 code、缺 code、或傳進來的根本不是物件，都回「系統發生錯誤」。
 */
export function llmErrorMessage(result) {
  const code = result && typeof result.code === "string" ? result.code : null;
  const entry = code && Object.prototype.hasOwnProperty.call(CODE_MAP, code) ? CODE_MAP[code] : null;
  return entry ? entry(result) : CODE_MAP.llm_internal_error();
}

/** 暫停原因（DESIGN §6.2「已暫停」橫幅）。不認得的原因只顯示「已暫停」那半句。 */
const SUSPEND_REASONS = {
  ops: () => _t("Maintenance in progress. Please contact support."),
  account_inactive: () => _t("Your platform account has been deactivated."),
  policy: () => _t("Violation of the AI Models Terms of Service. Please contact support."),
  not_me: () => _t("You reported activity that was not yours. We have suspended the service and will contact you."),
};

export function llmSuspendReason(reason) {
  const entry = Object.prototype.hasOwnProperty.call(SUSPEND_REASONS, reason) ? SUSPEND_REASONS[reason] : null;
  return entry ? entry() : null;
}
