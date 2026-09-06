/** @odoo-module **/
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { jsonRpc } from "./rpc";

/**
 * @typedef {Object} WalletData
 * @property {number} balance - Current wallet balance
 * @property {boolean} cardBound - Whether a payment card is bound
 * @property {string} cardLast4 - Last 4 digits of bound card
 * @property {string} cardExpiry - Card expiry date
 * @property {string} cardBank - Issuing bank of bound card
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
        cardBank: "",
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
    subscriptions: [],
    /** @type {number} */
    subscriptionsTotal: 0,
    /** @type {boolean} */
    subscriptionsLoading: false,
    /** @type {Object|null} */
    subscriptionDetail: null,

    /** @type {Array<Object>} */
    transactions: [],
    /** @type {number} */
    transactionsTotal: 0,
    /** @type {boolean} */
    transactionsLoading: false,

    /** @type {Array<Object>} */
    topupPlans: [],
    /** @type {boolean} */
    topupPlansLoading: false,

    /** @type {{ has_low_balance: boolean, upcoming: Array, shortfall: number }} */
    upcomingRenewals: {
        has_low_balance: false,
        upcoming: [],
        shortfall: 0,
    },

    /**
     * Fetch paginated subscription list
     * @param {string} [filter='active'] - Filter: 'active', 'past', or 'all'
     * @param {number} [page=1] - Page number
     * @param {number} [limit=10] - Items per page
     * @returns {Promise<void>}
     */
    async fetchSubscriptions(filter = "active", page = 1, limit = 10) {
        this.subscriptionsLoading = true;
        try {
            const result = await jsonRpc("/api/billing/subscriptions", { filter, page, limit });
            if (result.success) {
                this.subscriptions = result.data ?? [];
                this.subscriptionsTotal = result.total ?? 0;
            } else {
                console.error("Failed to fetch subscriptions:", result.error);
            }
        } catch (err) {
            console.error("Failed to fetch subscriptions:", err);
        } finally {
            this.subscriptionsLoading = false;
        }
    },

    /**
     * Fetch paginated transaction list
     * @param {number} [page=1] - Page number
     * @param {number} [limit=20] - Items per page
     * @returns {Promise<void>}
     */
    async fetchTransactions(page = 1, limit = 20) {
        this.transactionsLoading = true;
        try {
            const result = await jsonRpc("/api/billing/transactions", { page, limit });
            if (result.success) {
                this.transactions = result.data ?? [];
                this.transactionsTotal = result.total ?? 0;
            } else {
                console.error("Failed to fetch transactions:", result.error);
            }
        } catch (err) {
            console.error("Failed to fetch transactions:", err);
        } finally {
            this.transactionsLoading = false;
        }
    },

    /**
     * Fetch single subscription detail (with services)
     * @param {number} id - Subscription ID
     * @returns {Promise<void>}
     */
    async fetchSubscription(id) {
        try {
            const result = await jsonRpc(`/api/billing/subscriptions/${id}`, {});
            if (result.success) {
                this.subscriptionDetail = result.data ?? null;
            } else {
                console.error("Failed to fetch subscription:", result.error);
            }
        } catch (err) {
            console.error("Failed to fetch subscription:", err);
        }
    },

    /**
     * Cancel a subscription
     * @param {number} id - Subscription ID
     * @param {string} [reason=''] - Cancellation reason
     * @returns {Promise<{success: boolean, error?: string}>}
     */
    async cancelSubscription(id, reason = "") {
        try {
            const params = {};
            if (reason) {
                params.reason = reason;
            }
            const result = await jsonRpc(`/api/billing/subscriptions/${id}/cancel`, params);
            if (result.success) {
                return { success: true };
            }
            return { success: false, error: result.error || _t("Cancellation failed") };
        } catch (err) {
            return { success: false, error: err.message || _t("Cancellation failed") };
        }
    },

    /** @type {Promise<void>|null} */
    _fetchPaymentMethodsPromise: null,

    /**
     * Fetch bound payment methods. In-flight calls are deduplicated.
     * @returns {Promise<void>}
     */
    async fetchPaymentMethods() {
        if (this._fetchPaymentMethodsPromise) return this._fetchPaymentMethodsPromise;
        this._fetchPaymentMethodsPromise = (async () => {
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
                this._fetchPaymentMethodsPromise = null;
            }
        })();
        return this._fetchPaymentMethodsPromise;
    },

    /**
     * Step 1 of ECPG binding flow. Returns ECPay Token + member_id + trade_no.
     * @returns {Promise<{success: boolean, data?: Object, error?: string, code?: string}>}
     */
    async preparePaymentMethodBind() {
        try {
            const result = await jsonRpc("/api/billing/payment-methods/prepare-bind", {});
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error, code: result.code };
        } catch (err) {
            return { success: false, error: err.message || _t("Prepare bind failed") };
        }
    },

    /**
     * Step 2 of ECPG binding flow. Consumes BindCardPayToken from the JS SDK.
     * Returns { three_d_url, bind_card_id }. Frontend must redirect if three_d_url is non-empty.
     * @param {Object} payload - { member_id, trade_no, bind_card_pay_token }
     * @returns {Promise<{success: boolean, data?: Object, error?: string, code?: string}>}
     */
    async createPaymentMethodBind(payload) {
        try {
            const result = await jsonRpc("/api/billing/payment-methods/create-bind", payload);
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error, code: result.code };
        } catch (err) {
            return { success: false, error: err.message || _t("Create bind failed") };
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
            return { success: false, error: result.error || _t("Failed to delete card") };
        } catch (err) {
            return { success: false, error: err.message || _t("Failed to delete card") };
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
                await this.fetchPaymentMethods();
                return { success: true };
            }
            return { success: false, error: result.error || _t("Failed to set default") };
        } catch (err) {
            return { success: false, error: err.message || _t("Failed to set default") };
        }
    },

    /**
     * Fetch upcoming renewals in the next 3 days.
     * @returns {Promise<void>}
     */
    async fetchUpcomingRenewals() {
        try {
            const result = await jsonRpc("/api/billing/upcoming-renewals", {});
            if (result.success) {
                this.upcomingRenewals = {
                    has_low_balance: result.data.has_low_balance,
                    upcoming: result.data.upcoming ?? [],
                    shortfall: result.data.shortfall ?? 0,
                };
            } else {
                console.error("Failed to fetch upcoming renewals:", result.error);
            }
        } catch (err) {
            console.error("Failed to fetch upcoming renewals:", err);
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
                    cardBank: result.data.card_bank ?? "",
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
                this.invoices = result.data ?? [];
                this.invoicesTotal = result.total ?? 0;
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
            return { success: false, error: result.error || _t("Payment failed") };
        } catch (err) {
            return { success: false, error: err.message || _t("Payment failed") };
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

    async topup(planId, invoiceIntent = null) {
        try {
            const result = await jsonRpc("/api/billing/wallet/topup", {
                plan_id: planId,
                invoice_intent: invoiceIntent,
            });
            return result;
        } catch (err) {
            console.error("topup call failed", err);
            return { success: false, error: String(err), code: 'sdk_error' };
        }
    },

    async getTopupStatus(topupId) {
        try {
            return await jsonRpc(`/api/billing/topup/${topupId}/status`, {});
        } catch (err) {
            console.error("getTopupStatus failed", err);
            return { success: false, error: String(err), code: 'sdk_error' };
        }
    },

    /**
     * Redeem a gift card code into wallet points.
     * Backend grants points to the caller's own wallet (partner is bound
     * server-side to request.env.user — the code param is the only input).
     * @param {string} code - Gift card code
     * @returns {Promise<{success: boolean, data?: {new_balance: number, points_granted: number}, error?: string, code?: string}>}
     */
    async redeemCode(code) {
        try {
            const result = await jsonRpc("/api/billing/redeem-code", { code });
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error, code: result.code };
        } catch (err) {
            return { success: false, error: err.message || _t("Redeem failed") };
        }
    },
});
