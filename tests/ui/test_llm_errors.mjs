// LLM-01c（DESIGN §3.1、§3.4、§3.5、§5.4）：錯誤碼 → 繁中句子，以及 llm_service 的契約。
//
// - 畫面上的句子只由 code＋數字參數組成；後端 `error` 文字、rpc.js 的例外訊息（可能是伺服器
//   例外原文）、new-api 原文一律不出現。
// - 後端 llm_common.py 的每一個 ERR_* 都要有對應句子（碼表兩邊對帳，同 repo-lint 第 16 條的家族）。
// - 建金鑰用 40 秒逾時（> 後端 20 秒預算）；請求沒拿到回應時回「可能已建立」的專用碼。
// - 完整金鑰不進 llm_service 的 store。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  loadPo, readPaas, stubImports, toDataUrl, translateFromGlobal, translator,
} from "./lib/llm_test_support.mjs";

const po = await loadPo();
globalThis.__llmT = translator(po, "zh_TW");

const errorsUrl = toDataUrl(stubImports(await readPaas("services/llm_errors.js"), {
  "@web/core/l10n/translation": translateFromGlobal,
}));
const errors = await import(errorsUrl);

globalThis.__llmRpc = async () => ({ success: true, data: null });
const serviceSource = stubImports(await readPaas("services/llm_service.js"), {
  "@odoo/owl": "const reactive = (value) => value;",
  "./rpc": "const jsonRpc = (...args) => globalThis.__llmRpc(...args);",
  "./llm_errors": `const { LLM_KEY_CREATE_INTERRUPTED, LLM_NETWORK_ERROR, LLM_SESSION_EXPIRED } = await import("${errorsUrl}");`,
});
const { llmService, KEY_CREATE_TIMEOUT_MS, newClientRequestId, isSessionExpired } = await import(toDataUrl(serviceSource));

/** rpc.js 丟出的例外（訊息＋不含文字的分類欄位）。 */
const rpcError = (message, fields) => Object.assign(new Error(message), fields);

const backendCommon = await readFile(
  new URL("../../src/models/llm_common.py", import.meta.url), "utf8");

// ----------------------------------------------------------------------------- llm_errors

test("每個後端 ERR_* 碼都有句子（缺一個就會掉到「系統發生錯誤」）", () => {
  const backendCodes = [...backendCommon.matchAll(/^ERR_\w+ = '([a-z_]+)'$/gm)].map((m) => m[1]);
  assert.ok(backendCodes.length >= 15, "後端碼表抓不到（掃描壞了 ≠ 全部涵蓋）");
  const missing = backendCodes.filter((code) => !errors.LLM_ERROR_CODES.includes(code));
  assert.deepEqual(missing, []);
});

test("DESIGN §3.4 的繁中句子（與後端 fallback 同一條譯文）", () => {
  const m = (code, extra = {}) => errors.llmErrorMessage({ success: false, code, ...extra });
  assert.equal(m("llm_disabled"), "此功能尚未開放");
  assert.equal(m("llm_not_activated"), "請先開通 AI 模型服務");
  assert.equal(m("llm_tos_required"), "請先同意《AI 模型服務條款》");
  assert.equal(m("llm_provisioning"), "正在建立 AI 模型帳戶，請稍候");
  assert.equal(m("llm_unavailable"), "AI 模型服務暫時無法連線，請稍後再試");
  assert.equal(m("llm_free_key_exists"), "每個帳號只能有一把免費試用金鑰");
  assert.equal(m("llm_key_limit"), "每個帳號最多 20 把金鑰，請先刪除不用的金鑰");
  assert.equal(m("llm_invalid_request"), "找不到此筆點數轉換申請，或申請資料無效。");
  assert.equal(m("llm_order_not_found"), "找不到此筆點數轉換申請，或申請資料無效。");
  assert.equal(m("llm_account_error"), "AI 模型帳戶建立失敗，請聯絡客服");
  assert.equal(m("llm_internal_error"), "系統發生錯誤，請稍後再試");
  assert.equal(m("llm_auth_required"), "需要登入");
  assert.equal(m("llm_guard"), "系統維護中，暫時無法建立金鑰或轉換點數；既有金鑰不受影響");
  assert.equal(m("llm_free_capped_today"), "今日免費試用已用完，台北時間 00:00 後才能再建立免費試用金鑰");
});

test("帶參數的碼：retry_after 換成分鐘（無條件進位）、used、reason", () => {
  const m = (code, extra = {}) => errors.llmErrorMessage({ success: false, code, ...extra });
  assert.equal(m("llm_rate_limited", { retry_after: 61 }), "操作太頻繁，請在 2 分鐘後再試");
  assert.equal(m("llm_rate_limited", { retry_after: 5 }), "操作太頻繁，請在 1 分鐘後再試");
  assert.equal(m("llm_rate_limited"), "操作太頻繁，請稍後再試");
  assert.equal(m("llm_key_limit_below_used", { used: 30.7 }), "這把金鑰已用 30 點，上限不能低於已用量");
  assert.equal(m("llm_key_cannot_enable", { reason: "free_capped" }), "今日免費試用已用完，台北時間 00:00 自動恢復");
  assert.equal(m("llm_key_cannot_enable", { reason: "expired" }), "這把金鑰已過期，請先延長到期日");
  assert.equal(m("llm_key_cannot_enable", { reason: "???" }), "這把金鑰目前無法啟用");
  // 原型鏈上的名字：一般屬性查找會回 Object（顯示成 [object Object]）或直接丟 TypeError。
  for (const reason of ["constructor", "__proto__", "toString", "hasOwnProperty", 7, null]) {
    assert.equal(m("llm_key_cannot_enable", { reason }), "這把金鑰目前無法啟用", String(reason));
  }
});

test("llm_suspended 的句子不指向「橫幅」（在沒有橫幅的地方也講得通）", () => {
  assert.equal(errors.llmErrorMessage({ code: "llm_suspended" }), "AI 模型服務已暫停");
});

test("「可能已建立」給兩種清單狀態都走得通的下一步；登入逾時另有句子", () => {
  const interrupted = errors.llmErrorMessage({ code: "llm_key_create_interrupted" });
  assert.match(interrupted, /^金鑰可能已建立但沒有顯示。/);
  assert.match(interrupted, /「建立中斷」的金鑰無法使用，可以重新建立一把/);
  assert.match(interrupted, /「啟用中」，表示金鑰已建立但無法再次顯示，請聯絡客服協助重設/);
  assert.equal(errors.llmErrorMessage({ code: "llm_session_expired" }), "登入已逾時，請重新整理頁面並重新登入");
});

test("不認得的 code、缺 code、非物件：一律固定句子，不轉述任何原文", () => {
  const raw = "upstream https://openrouter.ai/x failed: Traceback (most recent call last)";
  for (const input of [
    { success: false, code: "something_new", error: raw },
    { success: false, error: raw },
    null,
    raw,
  ]) {
    const message = errors.llmErrorMessage(input);
    assert.equal(message, "系統發生錯誤，請稍後再試");
    assert.doesNotMatch(message, /openrouter|Traceback/);
  }
  // 原型鏈上的名字不能被當成 code。
  assert.equal(errors.llmErrorMessage({ code: "toString" }), "系統發生錯誤，請稍後再試");
});

test("暫停原因（§6.2）：認得的有句子、不認得的回 null", () => {
  assert.equal(errors.llmSuspendReason("ops"), "維運處理中，請聯絡客服");
  assert.equal(errors.llmSuspendReason("not_me"), "您回報了非本人操作，我們已暫停服務並會與您聯絡");
  assert.equal(errors.llmSuspendReason("weird"), null);
  assert.equal(errors.llmSuspendReason(undefined), null);
});

// ----------------------------------------------------------------------------- llm_service

test("成功回應只取 data；失敗只留 code 與白名單參數，丟掉後端 error 文字", async () => {
  globalThis.__llmRpc = async () => ({
    success: false, code: "llm_rate_limited", error: "internal detail", retry_after: 120,
    secret: "sk-should-not-pass",
  });
  const result = await llmService.fetchState();
  assert.deepEqual(result, { success: false, code: "llm_rate_limited", retry_after: 120 });
  assert.equal(llmService.overviewError, "llm_rate_limited");

  globalThis.__llmRpc = async () => ({ success: true, data: { account: null } });
  const ok = await llmService.fetchState();
  assert.deepEqual(ok, { success: true, data: { account: null } });
  assert.deepEqual(llmService.overview, { account: null });
  assert.equal(llmService.overviewError, null);
});

test("傳輸錯誤（逾時、網路、JSON-RPC 例外）→ llm_network_error，例外訊息不外流", async () => {
  globalThis.__llmRpc = async () => {
    throw new Error("psycopg2.errors.SerializationFailure: could not serialize access");
  };
  const result = await llmService.fetchKeys();
  assert.deepEqual(result, { success: false, code: "llm_network_error" });
  assert.equal(errors.llmErrorMessage(result), "AI 模型服務暫時無法連線，請稍後再試");
  // 沒有 code 的失敗也不轉述文字。
  globalThis.__llmRpc = async () => ({ success: false, error: "raw" });
  assert.deepEqual(await llmService.fetchUsageRecent(), { success: false, code: "llm_internal_error" });
});

test("usage API 失敗時保留舊快取並標錯，成功重取後清除錯誤", async () => {
  const previous = { free_trial: { day_tpe: "2026-10-18", today_count: 200, capped: true } };
  globalThis.__llmRpc = async () => ({ success: true, data: previous });
  await llmService.fetchUsageRecent();
  assert.equal(llmService.usage, previous);

  globalThis.__llmRpc = async () => { throw new Error("request failed"); };
  assert.deepEqual(await llmService.fetchUsageRecent(), { success: false, code: "llm_network_error" });
  assert.equal(llmService.usage, previous);
  assert.equal(llmService.usageError, "llm_network_error");

  const fresh = { free_trial: { day_tpe: "2026-10-19", today_count: 0, capped: false } };
  globalThis.__llmRpc = async () => ({ success: true, data: fresh });
  await llmService.fetchUsageRecent();
  assert.equal(llmService.usage, fresh);
  assert.equal(llmService.usageError, null);
});

test("登入逾時（JSON-RPC code 100／SessionExpiredException／HTTP 401）→ llm_session_expired，不是「暫時無法連線」", async () => {
  const expired = [
    rpcError("Session expired", { rpcCode: 100, rpcName: "odoo.http.SessionExpiredException" }),
    rpcError("x", { rpcName: "odoo.http.SessionExpiredException" }),
    rpcError("Session expired. Please refresh the page.", { httpStatus: 401 }),
  ];
  for (const error of expired) {
    assert.equal(isSessionExpired(error), true);
    globalThis.__llmRpc = async () => {
      throw error;
    };
    assert.deepEqual(await llmService.fetchState(), { success: false, code: "llm_session_expired" });
    assert.equal(llmService.overviewError, "llm_session_expired");
    // 建金鑰：請求在進 handler 之前就被擋下，**確定沒建**——不能回「可能已建立」。
    assert.deepEqual(await llmService.createFreeTrialKey(newClientRequestId()),
      { success: false, code: "llm_session_expired" });
  }
  // 其他 JSON-RPC 例外、HTTP 500、逾時：照舊是傳輸錯誤（例外訊息不外流）。
  for (const error of [
    rpcError("Odoo Server Error", { rpcCode: 200, rpcName: "builtins.KeyError" }),
    rpcError("Server error. Please try again later.", { httpStatus: 502 }),
    rpcError("Request timed out. Please try again.", { timedOut: true }),
    new Error("plain"),
  ]) {
    assert.equal(isSessionExpired(error), false);
    globalThis.__llmRpc = async () => {
      throw error;
    };
    assert.deepEqual(await llmService.fetchKeys(), { success: false, code: "llm_network_error" });
    assert.deepEqual(await llmService.createFreeTrialKey(newClientRequestId()),
      { success: false, code: "llm_key_create_interrupted" });
  }
  assert.equal(isSessionExpired(null), false);
});

test("建金鑰：kind=free_trial、40 秒逾時（> 後端 20 秒預算）；沒拿到回應回「可能已建立」", async () => {
  assert.equal(KEY_CREATE_TIMEOUT_MS, 40000);
  const calls = [];
  globalThis.__llmRpc = async (...args) => {
    calls.push(args);
    throw new Error("Request timed out. Please try again.");
  };
  const id = newClientRequestId();
  const result = await llmService.createFreeTrialKey(id);
  assert.deepEqual(calls, [["/api/llm/keys/create", { client_request_id: id, kind: "free_trial" }, 40000]]);
  assert.deepEqual(result, { success: false, code: "llm_key_create_interrupted" });
  assert.match(errors.llmErrorMessage(result), /金鑰可能已建立但沒有顯示/);
});

test("建金鑰成功：明文只回給呼叫端，store 裡任何地方都沒有", async () => {
  const key = "sk-" + "A1b2C3d4".repeat(6);
  globalThis.__llmRpc = async () => ({
    success: true, data: { key_id: 7, key, masked_key: "sk-A1b2…C3d4", status: "active" },
  });
  const before = JSON.stringify(llmService);
  const result = await llmService.createFreeTrialKey(newClientRequestId());
  assert.equal(result.data.key, key);
  const after = JSON.stringify(llmService);
  assert.equal(after, before);
  assert.doesNotMatch(after, /sk-A1b2C3d4/);
});

test("其他端點用預設逾時（rpc.js 30 秒），只有建金鑰明確傳 40 秒", async () => {
  const calls = [];
  globalThis.__llmRpc = async (...args) => {
    calls.push(args);
    return { success: true, data: {} };
  };
  await llmService.fetchState();
  await llmService.activate("draft-0");
  await llmService.fetchKeys();
  await llmService.fetchUsageRecent();
  assert.deepEqual(calls.map((args) => [args[0], args.length]), [
    ["/api/llm/state", 2],
    ["/api/llm/account/activate", 2],
    ["/api/llm/keys/list", 2],
    ["/api/llm/usage/recent", 2],
  ]);
  assert.deepEqual(calls[1][1], { tos_version: "draft-0" });
});

test("paid key mutations validate, preserve omitted/null fields, refresh both resources, and sanitize results", async () => {
  const calls = [];
  globalThis.__llmRpc = async (...args) => {
    calls.push(args);
    if (args[0] === "/api/llm/keys/update") return { success: true, data: { key: "sk-" + "SyntheticSecret" } };
    return { success: true, data: args[0] === "/api/llm/keys/list" ? { items: [] } : {} };
  };
  assert.deepEqual(await llmService.updateKey(7, { name: null, limitPoints: null, ignored: "no" }), {
    success: true, data: { key: "sk-SyntheticSecret" },
  });
  assert.deepEqual(calls.slice(0, 3).map(([url, params]) => [url, params]), [
    ["/api/llm/keys/update", { key_id: 7, name: null, limit_points: null }],
    ["/api/llm/keys/list", {}], ["/api/llm/state", {}],
  ]);
  assert.equal(JSON.stringify(llmService).includes("sk-SyntheticSecret"), false);

  calls.length = 0;
  assert.deepEqual(await llmService.updateKey(7, { expires: "90d" }), { success: true, data: { key: "sk-SyntheticSecret" } });
  assert.deepEqual(calls[0][1], { key_id: 7, expires: "90d" });
  calls.length = 0;
  for (const args of [[0, { name: "x" }], [1.5, { name: "x" }], [1, {}], [1, { name: undefined }], [1, null], [1, { name: "x", other: true }].slice(0, 1)]) {
    const result = await llmService.updateKey(...args);
    assert.deepEqual(result, { success: false, code: "llm_key_invalid" });
  }
  await llmService.extendKey(8);
  await llmService.setKeyStatus(8, false);
  await llmService.deleteKey(8, "Confirm");
  assert.deepEqual(calls.filter(([url]) => ["/api/llm/keys/extend", "/api/llm/keys/set_status", "/api/llm/keys/delete"].includes(url)).map(([url, params]) => [url, params]), [
    ["/api/llm/keys/extend", { key_id: 8 }],
    ["/api/llm/keys/set_status", { key_id: 8, enabled: false }],
    ["/api/llm/keys/delete", { key_id: 8, confirm_name: "Confirm" }],
  ]);
});

test("action failures do not refresh and session expiry stays classified", async () => {
  const calls = [];
  globalThis.__llmRpc = async (...args) => {
    calls.push(args);
    if (args[0] === "/api/llm/keys/extend") throw rpcError("raw transport sk-" + "synthetic", { httpStatus: 401 });
    return { success: true, data: {} };
  };
  assert.deepEqual(await llmService.extendKey(81), { success: false, code: "llm_session_expired" });
  assert.deepEqual(calls.map(([url]) => url), ["/api/llm/keys/extend"]);
  assert.equal(JSON.stringify(llmService).includes("synthetic"), false);
});

test("concurrent identical action deduplicates mutation and stale resource responses cannot overwrite newer state", async () => {
  const calls = [];
  let releaseAction;
  globalThis.__llmRpc = (...args) => {
    calls.push(args);
    if (args[0] === "/api/llm/keys/extend") return new Promise((resolve) => { releaseAction = () => resolve({ success: true, data: {} }); });
    if (args[0] === "/api/llm/keys/list") return Promise.resolve({ success: true, data: { items: [{ id: calls.length }] } });
    return Promise.resolve({ success: true, data: { marker: calls.length } });
  };
  const first = llmService.extendKey(90);
  const second = llmService.extendKey(90);
  await Promise.resolve();
  releaseAction();
  assert.deepEqual(await Promise.all([first, second]), [{ success: true, data: {} }, { success: true, data: {} }]);
  assert.equal(calls.filter(([url]) => url === "/api/llm/keys/extend").length, 1);
  assert.equal(calls.filter(([url]) => url === "/api/llm/keys/list").length, 1);
  assert.equal(calls.filter(([url]) => url === "/api/llm/state").length, 1);

  let resolveOldKeys;
  let resolveOldState;
  globalThis.__llmRpc = (url) => {
    if (url === "/api/llm/keys/list") return new Promise((resolve) => { resolveOldKeys = resolve; });
    if (url === "/api/llm/state") return new Promise((resolve) => { resolveOldState = resolve; });
    throw new Error("unexpected");
  };
  const oldKeys = llmService.fetchKeys();
  const oldState = llmService.fetchState();
  globalThis.__llmRpc = async (url) => url === "/api/llm/keys/list"
    ? { success: true, data: { items: [{ id: "new" }] } }
    : { success: true, data: { marker: "new" } };
  await llmService.fetchKeys();
  await llmService.fetchState();
  resolveOldKeys({ success: true, data: { items: [{ id: "old" }] } });
  resolveOldState({ success: true, data: { marker: "old" } });
  await Promise.all([oldKeys, oldState]);
  assert.deepEqual(llmService.keys, [{ id: "new" }]);
  assert.deepEqual(llmService.overview, { marker: "new" });
});

test("client_request_id 是後端收的小寫 UUID v4 形狀，每次不同", () => {
  const backend = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const a = newClientRequestId();
  const b = newClientRequestId();
  assert.match(a, backend);
  assert.match(a, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.notEqual(a, b);
});

// ----------------------------------------------------------------------------- rpc.js

const rpc = await import(toDataUrl(stubImports(await readPaas("services/rpc.js"), {
  "@web/core/l10n/translation": translateFromGlobal,
})));

async function rpcFailure(fetchImpl) {
  const original = globalThis.fetch;
  globalThis.fetch = fetchImpl;
  try {
    await rpc.jsonRpc("/api/llm/state", {}, 1000);
  } catch (error) {
    return error;
  } finally {
    globalThis.fetch = original;
  }
  assert.fail("jsonRpc 應該丟例外");
}

test("rpc.js 的例外帶不含文字的分類欄位（session 過期 code 100、HTTP 狀態、逾時），訊息照舊", async () => {
  const expired = await rpcFailure(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ error: { code: 100, message: "Odoo Session Expired",
      data: { name: "odoo.http.SessionExpiredException", message: "Session expired" } } }),
  }));
  assert.equal(expired.message, "Session expired");
  assert.equal(expired.rpcCode, 100);
  assert.equal(expired.rpcName, "odoo.http.SessionExpiredException");
  assert.equal(isSessionExpired(expired), true);

  const unauthorized = await rpcFailure(async () => ({ ok: false, status: 401 }));
  assert.equal(unauthorized.httpStatus, 401);
  assert.equal(isSessionExpired(unauthorized), true);

  const server = await rpcFailure(async () => ({ ok: false, status: 502 }));
  assert.equal(server.httpStatus, 502);
  assert.equal(isSessionExpired(server), false);

  const aborted = await rpcFailure(async () => {
    const error = new Error("aborted");
    error.name = "AbortError";
    throw error;
  });
  assert.equal(aborted.timedOut, true);
  assert.equal(isSessionExpired(aborted), false);
});
