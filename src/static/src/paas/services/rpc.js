/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";

/** Default timeout for API requests (30 seconds) */
const REQUEST_TIMEOUT_MS = 30000;

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
            throw new Error(_t("Request timed out. Please try again."));
        }
        throw new Error(_t("Network error. Please check your connection and try again."));
    } finally {
        clearTimeout(timeoutId);
    }

    if (!response.ok) {
        if (response.status === 401) {
            throw new Error(_t("Session expired. Please refresh the page."));
        } else if (response.status === 403) {
            throw new Error(_t("Access denied."));
        } else if (response.status >= 500) {
            throw new Error(_t("Server error. Please try again later."));
        }
        throw new Error(_t("Request failed with status %s", response.status));
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
        throw new Error(errorMsg);
    }

    return data.result;
}
