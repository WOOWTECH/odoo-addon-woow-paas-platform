/** @odoo-module **/
import { reactive } from "@odoo/owl";

/** Default timeout for API requests (30 seconds). */
const REQUEST_TIMEOUT_MS = 30000;

/**
 * JSON-RPC helper. Mirrors the shape used by profile_service / cloud_service /
 * billing_service so the dashboard error handling matches the rest of the app.
 */
async function jsonRpc(url, params, timeoutMs = REQUEST_TIMEOUT_MS) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    let response;
    try {
        response = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                jsonrpc: "2.0",
                method: "call",
                params,
                id: Math.floor(Math.random() * 1000000),
            }),
            signal: controller.signal,
        });
    } catch (error) {
        if (error.name === "AbortError") throw new Error("Request timed out.");
        throw new Error("Network error.");
    } finally {
        clearTimeout(timeoutId);
    }
    if (!response.ok) {
        if (response.status === 401) throw new Error("Session expired. Please refresh.");
        if (response.status === 403) throw new Error("Access denied.");
        if (response.status >= 500) throw new Error("Server error.");
        throw new Error(`Request failed (${response.status})`);
    }
    const json = await response.json();
    if (json.error) {
        const msg = json.error.data?.message || json.error.message || "Request failed";
        throw new Error(msg);
    }
    return json.result;
}

/**
 * dashboardService — reactive store for the post-login dashboard summary.
 *
 * Single endpoint (`POST /api/dashboard/summary`) returns the full payload:
 * user / billing / workspaces / members / recent_activity. The page component
 * subscribes to this store, calls `fetchSummary()` on mount, and renders the
 * three reactive states: loading / error / data.
 *
 * The `_fetchPromise` field deduplicates concurrent fetches the same way
 * profile_service does — multiple components mounting at once share one HTTP
 * round-trip.
 */
export const dashboardService = reactive({
    /** @type {Object|null} The full data block returned by the endpoint. */
    data: null,
    loading: false,
    error: null,
    /** @type {Promise|null} */
    _fetchPromise: null,

    /**
     * Fetch the dashboard summary. Idempotent: concurrent calls share one
     * promise; successive calls re-issue the request (so a manual refresh UI
     * could call this without rebuilding the service).
     */
    async fetchSummary() {
        if (this._fetchPromise) return this._fetchPromise;
        this._fetchPromise = (async () => {
            this.loading = true;
            this.error = null;
            try {
                const result = await jsonRpc("/api/dashboard/summary", {});
                if (!result.success) {
                    throw new Error(result.error || "Failed to load dashboard");
                }
                this.data = result.data;
            } catch (err) {
                this.error = err.message || String(err);
            } finally {
                this.loading = false;
                this._fetchPromise = null;
            }
        })();
        return this._fetchPromise;
    },
});
