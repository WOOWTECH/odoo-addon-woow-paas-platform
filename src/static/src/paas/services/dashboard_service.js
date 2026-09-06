/** @odoo-module **/
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { jsonRpc } from "./rpc";

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
                    throw new Error(result.error || _t("Failed to load dashboard"));
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
