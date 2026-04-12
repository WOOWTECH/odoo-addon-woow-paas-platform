/** @odoo-module **/
import { reactive } from "@odoo/owl";

/** Default timeout for API requests (30 seconds) */
const REQUEST_TIMEOUT_MS = 30000;

/**
 * JSON-RPC helper for Odoo API calls
 * @param {string} url - API endpoint
 * @param {Object} params - Request parameters
 * @param {number} [timeoutMs=30000] - Request timeout in milliseconds
 * @returns {Promise<Object>} API response result
 */
async function jsonRpc(url, params, timeoutMs = REQUEST_TIMEOUT_MS) {
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

/**
 * @typedef {Object} WalletData
 * @property {number} balance - Current wallet balance
 * @property {boolean} cardBound - Whether a payment card is bound
 * @property {string} cardLast4 - Last 4 digits of bound card
 * @property {string} cardExpiry - Card expiry date
 */

/**
 * Billing Service
 * Handles all API calls related to wallet and billing
 */
export const billingService = reactive({
    /** @type {WalletData} */
    wallet: {
        balance: 0,
        cardBound: false,
        cardLast4: "",
        cardExpiry: "",
    },
    /** @type {boolean} */
    walletLoading: false,

    /** @type {Array<Object>} Payment methods list */
    paymentMethods: [],
    /** @type {boolean} */
    paymentMethodsLoading: false,

    /** @type {Array<Object>} */
    invoices: [],
    /** @type {Object|null} */
    invoiceDetail: null,
    /** @type {number} */
    invoicesTotal: 0,
    /** @type {number} */
    invoicesPage: 1,
    /** @type {boolean} */
    invoicesLoading: false,
    /** @type {boolean} */
    invoiceDetailLoading: false,

    /** @type {Array<Object>} */
    topupPlans: [],
    /** @type {boolean} */
    topupPlansLoading: false,

    /**
     * Fetch bound payment methods
     * @returns {Promise<void>}
     */
    async fetchPaymentMethods() {
        this.paymentMethodsLoading = true;
        try {
            const result = await jsonRpc("/api/billing/payment-methods", {});
            if (result.success) {
                this.paymentMethods = result.data ?? [];
            } else {
                console.error("Failed to fetch payment methods:", result.error);
            }
        } catch (err) {
            console.error("Failed to fetch payment methods:", err);
        } finally {
            this.paymentMethodsLoading = false;
        }
    },

    /**
     * Add/bind a new payment method (credit card)
     * @param {Object} cardData - { card_number, expiry, cvc, cardholder_name }
     * @returns {Promise<{success: boolean, error?: string, code?: string}>}
     */
    async addPaymentMethod(cardData) {
        try {
            const result = await jsonRpc("/api/billing/payment-methods/add", cardData);
            if (result.success) {
                await this.fetchPaymentMethods();
                return { success: true };
            }
            return { success: false, error: result.error || "Failed to add card", code: result.code };
        } catch (err) {
            return { success: false, error: err.message || "Failed to add card" };
        }
    },

    /**
     * Delete/unbind a payment method
     * @param {string} id - Payment method ID
     * @returns {Promise<{success: boolean, error?: string}>}
     */
    async deletePaymentMethod(id) {
        try {
            const result = await jsonRpc(`/api/billing/payment-methods/${id}/delete`, {});
            if (result.success) {
                await this.fetchPaymentMethods();
                return { success: true };
            }
            return { success: false, error: result.error || "Failed to delete card" };
        } catch (err) {
            return { success: false, error: err.message || "Failed to delete card" };
        }
    },

    /**
     * Set a payment method as default
     * @param {string} id - Payment method ID
     * @returns {Promise<{success: boolean, error?: string}>}
     */
    async setDefaultPaymentMethod(id) {
        try {
            const result = await jsonRpc(`/api/billing/payment-methods/${id}/set-default`, {});
            if (result.success) {
                return { success: true };
            }
            return { success: false, error: result.error || "Failed to set default" };
        } catch (err) {
            return { success: false, error: err.message || "Failed to set default" };
        }
    },

    /**
     * Fetch wallet information
     * @returns {Promise<void>}
     */
    async fetchWallet() {
        this.walletLoading = true;
        try {
            const result = await jsonRpc("/api/billing/wallet", {});
            if (result.success) {
                this.wallet = {
                    balance: result.data.balance ?? 0,
                    cardBound: result.data.card_bound ?? false,
                    cardLast4: result.data.card_last4 ?? "",
                    cardExpiry: result.data.card_expiry ?? "",
                };
            } else {
                console.error("Failed to fetch wallet:", result.error);
            }
        } catch (err) {
            console.error("Failed to fetch wallet:", err);
        } finally {
            this.walletLoading = false;
        }
    },

    /**
     * Fetch paginated invoice list
     * @param {number} [page=1] - Page number
     * @param {number} [limit=10] - Items per page
     * @param {string} [sortBy='invoice_date'] - Sort field
     * @param {string} [sortOrder='desc'] - Sort direction
     * @param {string|null} [status=null] - Filter by status
     * @returns {Promise<void>}
     */
    async fetchInvoices(page = 1, limit = 10, sortBy = "invoice_date", sortOrder = "desc", status = null) {
        this.invoicesLoading = true;
        try {
            const params = { page, limit, sort_by: sortBy, sort_order: sortOrder };
            if (status) {
                params.status = status;
            }
            const result = await jsonRpc("/api/billing/invoices", params);
            if (result.success) {
                this.invoices = result.data?.invoices ?? [];
                this.invoicesTotal = result.data?.total ?? 0;
                this.invoicesPage = page;
            } else {
                console.error("Failed to fetch invoices:", result.error);
            }
        } catch (err) {
            console.error("Failed to fetch invoices:", err);
        } finally {
            this.invoicesLoading = false;
        }
    },

    /**
     * Fetch single invoice detail
     * @param {number} id - Invoice ID
     * @returns {Promise<void>}
     */
    async fetchInvoice(id) {
        this.invoiceDetailLoading = true;
        try {
            const result = await jsonRpc(`/api/billing/invoices/${id}`, {});
            if (result.success) {
                this.invoiceDetail = result.data ?? null;
            } else {
                console.error("Failed to fetch invoice:", result.error);
            }
        } catch (err) {
            console.error("Failed to fetch invoice:", err);
        } finally {
            this.invoiceDetailLoading = false;
        }
    },

    /**
     * Pay an invoice using wallet balance
     * @param {number} id - Invoice ID
     * @returns {Promise<{success: boolean, data?: Object, error?: string}>}
     */
    async payInvoice(id) {
        try {
            const result = await jsonRpc(`/api/billing/invoices/${id}/pay`, {});
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error || "Payment failed" };
        } catch (err) {
            return { success: false, error: err.message || "Payment failed" };
        }
    },

    /**
     * Fetch available top-up plans
     * @param {string|null} [category=null] - Optional category filter
     * @returns {Promise<void>}
     */
    async fetchTopupPlans(category = null) {
        this.topupPlansLoading = true;
        try {
            const params = {};
            if (category) {
                params.category = category;
            }
            const result = await jsonRpc("/api/billing/wallet/topup-plans", params);
            if (result.success) {
                this.topupPlans = result.data ?? [];
            } else {
                console.error("Failed to fetch topup plans:", result.error);
            }
        } catch (err) {
            console.error("Failed to fetch topup plans:", err);
        } finally {
            this.topupPlansLoading = false;
        }
    },

    /**
     * Purchase a top-up plan
     * @param {number} planId - Top-up plan ID
     * @returns {Promise<{success: boolean, data?: Object, error?: string, code?: string}>}
     */
    async topup(planId) {
        try {
            const result = await jsonRpc("/api/billing/wallet/topup", { plan_id: planId });
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error || "Top-up failed", code: result.code };
        } catch (err) {
            return { success: false, error: err.message || "Top-up failed" };
        }
    },
});
