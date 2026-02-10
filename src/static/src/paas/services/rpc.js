/** @odoo-module **/

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
            throw new Error("Request timed out. Please try again.");
        }
        throw new Error("Network error. Please check your connection and try again.");
    } finally {
        clearTimeout(timeoutId);
    }

    if (!response.ok) {
        if (response.status === 401) {
            throw new Error("Session expired. Please refresh the page.");
        } else if (response.status === 403) {
            throw new Error("Access denied.");
        } else if (response.status >= 500) {
            throw new Error("Server error. Please try again later.");
        }
        throw new Error(`Request failed with status ${response.status}`);
    }

    let data;
    try {
        data = await response.json();
    } catch (parseError) {
        throw new Error("Invalid response from server.");
    }

    if (data.error) {
        const errorMsg = data.error.data?.message || data.error.message || "Unknown error";
        throw new Error(errorMsg);
    }

    return data.result;
}
