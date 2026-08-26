/** @odoo-module **/
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { jsonRpc } from "./rpc";

/**
 * reconcileReportService — L4 反向孤兒 dry-run 報表的 reactive store（admin-only）。
 *
 * 後端 endpoints（admin has_group 真把關，前端僅 cosmetic 隱藏）：
 *   /api/reconcile-report/list      → {scanned_at, partial, candidates:[...]}
 *   /api/reconcile-report/reverify  → {still_orphan, reconcilable, env_hint, reason}
 *
 * **no-silent-drop（§C.4#7）**：list 回的 candidates 含 reconcilable=false（cross-env /
 * protected / unknown），前端**全列呈現**、不得以 reconcilable 過濾。
 */
export const reconcileReportService = reactive({
    candidates: [],
    scannedAt: null,
    partial: false,
    loading: false,
    error: null,
    _fetchPromise: null,

    async fetch() {
        if (this._fetchPromise) return this._fetchPromise;
        this._fetchPromise = (async () => {
            this.loading = true;
            this.error = null;
            try {
                const result = await jsonRpc("/api/reconcile-report/list", {});
                if (!result.success) {
                    throw new Error(result.error || _t("Failed to load the orphan reconcile report"));
                }
                const data = result.data || {};
                this.candidates = data.candidates || [];
                this.scannedAt = data.scanned_at || null;
                this.partial = !!data.partial;
            } catch (err) {
                this.error = err.message || String(err);
            } finally {
                this.loading = false;
                this._fetchPromise = null;
            }
        })();
        return this._fetchPromise;
    },

    /**
     * 單筆唯讀 TOCTOU 重查（不刪、不改 stored row）：重跑後端對帳看該筆是否仍為孤兒。
     * 回後端原始 {success, data:{still_orphan, reconcilable, env_hint, reason}} 或 {success:false, error}。
     */
    async reverify(kind, resourceUid) {
        try {
            return await jsonRpc("/api/reconcile-report/reverify", {
                kind: kind,
                resource_uid: resourceUid,
            });
        } catch (err) {
            return { success: false, error: err.message || String(err) };
        }
    },
});
