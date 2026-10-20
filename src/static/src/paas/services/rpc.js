/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";

/** Default timeout for API requests (30 seconds) */
const REQUEST_TIMEOUT_MS = 30000;

/**
 * Error thrown by jsonRpc. The message is for display (legacy callers show it),
 * the extra fields carry no text so callers can classify without parsing the
 * message (LLM-01c: a session expiry must not read as "network error"):
 * - `httpStatus`: HTTP status of a non-2xx response;
 * - `rpcCode` / `rpcName`: JSON-RPC `error.code` and `error.data.name`
 *   (Odoo answers an expired session on an auth='user' json route with
 *   HTTP 200, code 100, name "odoo.http.SessionExpiredException");
 * - `timedOut`: the request was aborted by the timeout.
 */
function rpcError(message, fields) {
    const error = new Error(message);
    Object.assign(error, fields);
    return error;
}

/**
 * JSON-RPC helper for Odoo API calls
 * @param {string} url - API endpoint
 * @param {Object} params - Request parameters
 * @param {number} [timeoutMs=30000] - Request timeout in milliseconds
 * @returns {Promise<Object>} API response result
 */
export async function jsonRpc(url, params, timeoutMs = REQUEST_TIMEOUT_MS) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    let response;
    try {
        response = await fetch(url, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                jsonrpc: "2.0",
                method: "call",
                params: params,
                id: Math.floor(Math.random() * 1000000),
            }),
            signal: controller.signal,
        });
    } catch (error) {
        if (error.name === "AbortError") {
            throw rpcError(_t("Request timed out. Please try again."), { timedOut: true });
        }
        throw rpcError(_t("Network error. Please check your connection and try again."), {});
    } finally {
        clearTimeout(timeoutId);
    }

    if (!response.ok) {
        const fields = { httpStatus: response.status };
        if (response.status === 401) {
            throw rpcError(_t("Session expired. Please refresh the page."), fields);
        } else if (response.status === 403) {
            throw rpcError(_t("Access denied."), fields);
        } else if (response.status >= 500) {
            throw rpcError(_t("Server error. Please try again later."), fields);
        }
        throw rpcError(_t("Request failed with status %s", response.status), fields);
    }

    let data;
    try {
        data = await response.json();
    } catch {
        // SyntaxError messages may echo response text, including credentials.
        // Keep diagnostics to the endpoint; never log the parser exception.
        console.error(`JSON parse failed for ${url}`);
        throw new Error(
            _t("Invalid response from server (%s). The server may be restarting.", url)
        );
    }

    if (data.error) {
        const errorMsg = data.error.data?.message || data.error.message || _t("Unknown error");
        throw rpcError(errorMsg, {
            rpcCode: data.error.code,
            rpcName: typeof data.error.data?.name === "string" ? data.error.data.name : undefined,
        });
    }

    return data.result;
}
