/** @odoo-module **/
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { jsonRpc } from "./rpc";

/**
 * profileService — reactive store for the current user's partner profile.
 * Shared by Header (for name/avatar, wired in Task 3) and ProfilePage (edit).
 */
export const profileService = reactive({
    partner: null,        // {id, name, email, phone, avatar_url} or null
    invoicePreference: null, // {invoice_carrier_type, invoice_carrier, ...} or null
    loading: false,
    saving: false,
    invoiceLoading: false,
    invoiceSaving: false,
    _fetchPromise: null,
    _invoiceFetchPromise: null,
    error: null,

    async fetch() {
        if (this._fetchPromise) return this._fetchPromise;
        this._fetchPromise = (async () => {
            this.loading = true;
            this.error = null;
            try {
                const result = await jsonRpc("/api/me", {});
                if (!result.success) throw new Error(result.error || _t("Failed to load profile"));
                this.partner = result.data;
            } catch (err) {
                this.error = err.message || String(err);
            } finally {
                this.loading = false;
                this._fetchPromise = null;
            }
        })();
        return this._fetchPromise;
    },

    async update(patch) {
        if (this.saving) {
            return { success: false, error: _t("Update already in progress") };
        }
        this.saving = true;
        this.error = null;
        try {
            const result = await jsonRpc("/api/me/update", patch);
            if (result.success) {
                this.partner = result.data;
            } else {
                this.error = result.error || _t("Update failed");
            }
            return result;
        } catch (err) {
            const msg = err.message || String(err);
            this.error = msg;
            return { success: false, error: msg };
        } finally {
            this.saving = false;
        }
    },

    async fetchInvoicePreference() {
        if (this._invoiceFetchPromise) return this._invoiceFetchPromise;
        this._invoiceFetchPromise = (async () => {
            this.invoiceLoading = true;
            try {
                const result = await jsonRpc("/api/me/invoice-preference", {});
                if (!result.success) throw new Error(result.error || _t("Failed to load invoice preference"));
                this.invoicePreference = result.data;
            } catch (err) {
                this.error = err.message || String(err);
            } finally {
                this.invoiceLoading = false;
                this._invoiceFetchPromise = null;
            }
        })();
        return this._invoiceFetchPromise;
    },

    async updateInvoicePreference(patch) {
        if (this.invoiceSaving) {
            return { success: false, error: _t("Update already in progress") };
        }
        this.invoiceSaving = true;
        try {
            const result = await jsonRpc("/api/me/invoice-preference/update", patch);
            if (result.success) {
                this.invoicePreference = result.data;
            }
            return result;
        } catch (err) {
            const msg = err.message || String(err);
            return { success: false, error: msg };
        } finally {
            this.invoiceSaving = false;
        }
    },
});
