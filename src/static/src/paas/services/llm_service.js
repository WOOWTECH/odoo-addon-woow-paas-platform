/** @odoo-module **/

import { reactive } from "@odoo/owl";
import { jsonRpc } from "./rpc";
import { LLM_KEY_CREATE_INTERRUPTED, LLM_NETWORK_ERROR, LLM_SESSION_EXPIRED } from "./llm_errors";

/**
 * 「AI 模型」分頁的 API（DESIGN §3.2；照 billing_service.js 的 reactive store）。
 *
 * 契約：
 * - 每個方法都回 `{success: true, data}` 或 `{success: false, code, …數字參數}`，**不丟例外**。
 *   失敗只保留 code 與白名單參數——後端的 `error` 文字、rpc.js 例外訊息（可能是伺服器
 *   例外原文 `data.error.data.message`）一律丟掉，畫面上的句子只由 llm_errors.js 依 code 組。
 * - store 只放清單與狀態。**完整金鑰不進 store**（DESIGN §5.4）：`createFreeTrialKey` 的回應只
 *   回給呼叫端（LlmKeyRevealModal 放進自己的元件 state，關閉即清掉），這裡不留副本、不寫 log。
 */

/**
 * 建金鑰的前端逾時（DESIGN §3.5）：後端總預算 KEY_CREATE_BUDGET_SECONDS＝20 秒，前端要比它長，
 * 分類才會由後端給出。repo-lint 第 42 條 newapi-timeout-ladder 守這個大小關係。
 */
export const KEY_CREATE_TIMEOUT_MS = 40000;

/** 失敗回應裡可以帶給 llm_errors.js 組句的參數（與 controllers/llm.py `_SAFE_PARAMS` 對齊、外加 §3.4 的參數）。 */
const NUMERIC_PARAMS = ["retry_after", "max_keys", "used", "needed_7d", "short_by"];
const KEY_ENABLE_REASONS = new Set(["expired", "exhausted", "free_capped", "suspended", "overdraft"]);
const KEY_INVALID = "llm_key_invalid";
const keyActionTails = new Map();
const keyActionsInFlight = new Map();

function validKeyId(keyId) {
  return Number.isSafeInteger(keyId) && keyId > 0;
}

function validUpdateName(value) {
  return typeof value === "string" && value.trim().length > 0 && value.length <= 64;
}

function validUpdateModels(value) {
  if (value === null) return true;
  if (!Array.isArray(value) || value.length === 0) return false;
  const seen = new Set();
  for (const model of value) {
    if (typeof model !== "string" || !/^[A-Za-z0-9._:/-]{1,128}$/.test(model) || seen.has(model)) {
      return false;
    }
    seen.add(model);
  }
  return true;
}

function failure(code) {
  return { success: false, code };
}

function normalize(result) {
  if (result && result.success === true) {
    return { success: true, data: result.data === undefined ? null : result.data };
  }
  if (result && typeof result.code === "string" && result.code) {
    const out = failure(result.code);
    for (const name of NUMERIC_PARAMS) {
      const value = result[name];
      if (typeof value === "number" && Number.isFinite(value) && value >= 0) out[name] = value;
    }
    if (KEY_ENABLE_REASONS.has(result.reason)) out.reason = result.reason;
    if (typeof result.current_version === "string" && /^[A-Za-z0-9._-]{1,64}$/.test(result.current_version)) {
      out.current_version = result.current_version;
    }
    return out;
  }
  // 沒有 code 的失敗（不該發生：後端每個失敗都帶 code）：當成程式錯誤，不轉述任何文字。
  return failure("llm_internal_error");
}

/** Odoo 18 的 SessionExpiredException：JSON-RPC error code 100（`odoo/http.py` `serialize_exception`）。 */
const RPC_SESSION_EXPIRED_CODE = 100;

/**
 * rpc.js 丟出的例外是不是「登入已逾時」：只看不含文字的欄位（`httpStatus`、`rpcCode`、
 * `rpcName`），不解析訊息。這種失敗發生在進 handler 之前，**一定沒有**執行。
 */
export function isSessionExpired(error) {
  if (!error || typeof error !== "object") return false;
  if (error.httpStatus === 401) return true;
  if (error.rpcCode === RPC_SESSION_EXPIRED_CODE) return true;
  return typeof error.rpcName === "string" && error.rpcName.endsWith("SessionExpiredException");
}

async function call(url, params, { timeoutMs = null, transportCode = LLM_NETWORK_ERROR } = {}) {
  let result;
  try {
    result = timeoutMs ? await jsonRpc(url, params, timeoutMs) : await jsonRpc(url, params);
  } catch (error) {
    // 例外訊息不往畫面送（見檔頭）。登入逾時另給一個碼：重試沒用、要重新整理頁面登入；
    // 建金鑰遇到它時也是「確定沒建」，不能回 transportCode（「可能已建立」）。
    if (isSessionExpired(error)) {
      return failure(LLM_SESSION_EXPIRED);
    }
    // 逾時、網路、其他 HTTP 錯誤、JSON-RPC 例外。
    return failure(transportCode);
  }
  return normalize(result);
}

async function runKeyAction(service, action, keyId, url, params) {
  let signature;
  try {
    signature = JSON.stringify([action, url, params]);
  } catch {
    return failure(KEY_INVALID);
  }
  const dedupeKey = `${keyId}:${signature}`;
  if (keyActionsInFlight.has(dedupeKey)) return keyActionsInFlight.get(dedupeKey);

  const previous = keyActionTails.get(keyId) || Promise.resolve();
  const pending = previous.catch(() => {}).then(async () => {
    const result = await call(url, params);
    if (!result.success) return result;
    await Promise.allSettled([service.fetchKeys(), service.fetchState()]);
    return { success: true, data: null };
  });
  const tail = pending.then(() => {}, () => {});
  keyActionTails.set(keyId, tail);
  keyActionsInFlight.set(dedupeKey, pending);

  const release = () => {
    if (keyActionsInFlight.get(dedupeKey) === pending) keyActionsInFlight.delete(dedupeKey);
    if (keyActionTails.get(keyId) === tail) keyActionTails.delete(keyId);
  };
  pending.then(release, release);
  return pending;
}

/**
 * 建金鑰的冪等鍵（打開建立流程時產生一次，DESIGN §3.2）。後端只收小寫的 UUID 形狀；
 * `crypto.randomUUID()` 只在安全來源可用，所以用 `getRandomValues()` 自己排成 v4。
 */
export function newClientRequestId(cryptoImpl = globalThis.crypto) {
  const bytes = new Uint8Array(16);
  cryptoImpl.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
}

export const llmService = reactive({
  /** `/api/llm/state` 的 data；還沒讀過時是 null。 */
  overview: null,
  /** 最近一次 `/api/llm/state` 的失敗碼（成功時清成 null）。 */
  overviewError: null,
  overviewLoaded: false,

  /** `/api/llm/keys/list` 的 items（只有遮罩，沒有明文）。 */
  keys: [],
  keysError: null,
  keysLoaded: false,

  /** `/api/llm/usage/recent` 的 data（今天的免費試用呼叫，前 10 筆）。 */
  usage: null,
  usageError: null,

  models: null,
  modelsLoaded: false,
  modelsRequest: 0,
  keysRequest: 0,
  overviewRequest: 0,

  // Conversion responses and passwords stay with the caller, never in the global store.
  async previewCredit(points) {
    return call("/api/llm/credit/preview", { points });
  },

  async convertCredit(params) {
    return call("/api/llm/credit/convert", params);
  },

  async fetchCreditOrders(page = 1, limit = 20) {
    return call("/api/llm/credit/orders", { page, limit });
  },

  async findCreditOrder(clientRequestId) {
    return call("/api/llm/credit/orders", { client_request_id: clientRequestId, page: 1, limit: 1 });
  },

  async fetchCreditOrder(id) {
    return call("/api/llm/credit/orders/" + id, {});
  },

  async reauth(password) {
    return call("/api/llm/account/reauth", { password });
  },

  async updateSettings(lowBalanceThreshold) {
    if (!Number.isSafeInteger(lowBalanceThreshold) || lowBalanceThreshold < 0 || lowBalanceThreshold > 100000) {
      return failure("llm_settings_invalid");
    }
    return call("/api/llm/account/settings", { low_balance_threshold: lowBalanceThreshold });
  },

  async reportNotMe(token) {
    const match = typeof token === "string" ? token.match(/^[A-Za-z0-9_.-]{1,512}$/) : null;
    if (!match || match[0] !== token) {
      return failure("llm_not_me_invalid");
    }
    return call("/api/llm/account/report_not_me", { token });
  },

  async fetchModels() {
    const request = ++this.modelsRequest;
    const result = await call("/api/llm/models", {});
    if (request !== this.modelsRequest) return result;
    // Pricing failure clears old rates; history's SWR policy does not apply to tariffs.
    this.models = result.success && result.data && result.data.state === "ok"
      && Array.isArray(result.data.items) ? result.data : { items: [], state: "maintenance" };
    this.modelsLoaded = true;
    return result;
  },

  async fetchState() {
    const request = ++this.overviewRequest;
    const result = await call("/api/llm/state", {});
    if (request !== this.overviewRequest) return result;
    if (result.success) {
      this.overview = result.data;
      this.overviewError = null;
    } else {
      this.overviewError = result.code;
    }
    this.overviewLoaded = true;
    return result;
  },

  async activate(tosVersion) {
    return call("/api/llm/account/activate", { tos_version: tosVersion });
  },

  /**
   * 《AI 模型服務條款》的 published 版（LLM-04）：`{version, title, content_html, is_draft, …}`。
   * `content_html` 是 HTML：畫面一律經 html_sanitize.js 的 `safeHtml()` 才顯示（LlmTermsModal）。
   * 不進 store：每次打開對話框都重新讀（條款可能在頁面打開之後改版）。
   */
  async fetchTerms() {
    return call("/api/llm/terms", {});
  },

  /** 改版後重新同意（DESIGN §6.4-3）：帶對話框裡顯示的那一版；成功回 `{tos}`。 */
  async acceptTerms(tosVersion) {
    return call("/api/llm/account/accept_terms", { tos_version: tosVersion });
  },

  async fetchKeys() {
    const request = ++this.keysRequest;
    const result = await call("/api/llm/keys/list", {});
    if (request !== this.keysRequest) return result;
    if (result.success) {
      const items = result.data && Array.isArray(result.data.items) ? result.data.items : [];
      this.keys = items;
      this.keysError = null;
    } else {
      this.keysError = result.code;
    }
    this.keysLoaded = true;
    return result;
  },

  async updateKey(keyId, changes) {
    let params;
    try {
      if (!validKeyId(keyId) || !changes || typeof changes !== "object") return failure(KEY_INVALID);
      params = { key_id: keyId };
      for (const name of ["name", "limitPoints", "expires", "models"]) {
        if (!Object.hasOwn(changes, name)) continue;
        const value = changes[name];
        if (value === undefined) return failure(KEY_INVALID);
        if (name === "name" && !validUpdateName(value)) return failure(KEY_INVALID);
        if (name === "limitPoints" && value !== null
          && (!Number.isSafeInteger(value) || value <= 0)) return failure(KEY_INVALID);
        if (name === "expires" && value !== null
          && !["30d", "90d", "180d", "never"].includes(value)) return failure(KEY_INVALID);
        if (name === "models" && !validUpdateModels(value)) return failure(KEY_INVALID);
        params[name === "limitPoints" ? "limit_points" : name] = name === "models" && value !== null
          ? Array.from(value, (model) => model)
          : value;
      }
      if (Object.keys(params).length === 1) return failure(KEY_INVALID);
    } catch {
      return failure(KEY_INVALID);
    }
    return runKeyAction(this, "update", keyId, "/api/llm/keys/update", params);
  },

  async extendKey(keyId) {
    if (!validKeyId(keyId)) return failure(KEY_INVALID);
    return runKeyAction(this, "extend", keyId, "/api/llm/keys/extend", { key_id: keyId });
  },

  async setKeyStatus(keyId, enabled) {
    if (!validKeyId(keyId) || typeof enabled !== "boolean") return failure(KEY_INVALID);
    return runKeyAction(this, "status", keyId, "/api/llm/keys/set_status", { key_id: keyId, enabled });
  },

  async deleteKey(keyId, confirmName) {
    if (!validKeyId(keyId) || typeof confirmName !== "string") return failure(KEY_INVALID);
    return runKeyAction(this, "delete", keyId, "/api/llm/keys/delete", { key_id: keyId, confirm_name: confirmName });
  },

  async fetchUsageRecent() {
    const result = await call("/api/llm/usage/recent", {});
    if (result.success) {
      this.usage = result.data;
      this.usageError = null;
    } else {
      this.usageError = result.code;
    }
    return result;
  },

  /** Usage aggregates are returned to the caller and are never kept in the shared LLM store. */
  async fetchUsageSummary(params) {
    return call("/api/llm/usage/summary", params);
  },

  async fetchUsageDaily(params) {
    return call("/api/llm/usage/daily", params);
  },

  async fetchUsageLogs(params) {
    return call("/api/llm/usage/logs", params);
  },

  /**
   * 建一把免費試用金鑰（付費金鑰在 LLM-07）。回應的 `data.key` 是**唯一一次**看得到的明文：
   * 這裡不存、不記錄，直接交還呼叫端。請求沒拿到回應時回 `llm_key_create_interrupted`
   * （金鑰可能已建好，見 llm_errors.js）；登入逾時回 `llm_session_expired`（確定沒建）。
   */
  async createFreeTrialKey(clientRequestId) {
    return call("/api/llm/keys/create", {
      client_request_id: clientRequestId,
      kind: "free_trial",
    }, { timeoutMs: KEY_CREATE_TIMEOUT_MS, transportCode: LLM_KEY_CREATE_INTERRUPTED });
  },

  /** Paid creation handoff for LLM-07. Task 9 must route this response directly to reveal local state. */
  async createPaidKey(clientRequestId, values) {
    return call("/api/llm/keys/create", {
      client_request_id: clientRequestId,
      kind: "paid",
      name: values.name,
      limit_points: values.limitPoints,
      expires: values.expires,
      models: values.models,
    }, { timeoutMs: KEY_CREATE_TIMEOUT_MS, transportCode: LLM_KEY_CREATE_INTERRUPTED });
  },
});
