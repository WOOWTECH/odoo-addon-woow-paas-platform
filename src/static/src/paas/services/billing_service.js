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
});
