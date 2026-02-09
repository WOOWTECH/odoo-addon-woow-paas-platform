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
 * @typedef {Object} AgentData
 * @property {number} id - Agent ID
 * @property {string} name - Agent internal name
 * @property {string} display_name - Agent display name
 * @property {string} system_prompt - Agent system prompt
 * @property {number} provider_id - Associated provider ID
 * @property {string} avatar_color - Avatar color hex
 * @property {boolean} is_default - Whether this is the default agent
 */

/**
 * @typedef {Object} ChatMessage
 * @property {number} id - Message ID
 * @property {string} body - Message body (HTML)
 * @property {string} author - Author display name
 * @property {number} author_id - Author user ID
 * @property {string} date - ISO datetime string
 * @property {boolean} is_ai - Whether message is from AI
 * @property {Array} attachments - Message attachments
 */

/**
 * @typedef {Object} ConnectionStatus
 * @property {boolean} connected - Whether AI provider is connected
 * @property {string} provider_name - Active provider name
 * @property {string} model_name - Active model name
 */

/**
 * AI Service
 * Handles all API calls related to AI agents, chat, and streaming
 * @type {Object}
 */
export const aiService = reactive({
    /** @type {AgentData[]} */
    agents: [],
    /** @type {boolean} */
    loading: false,
    /** @type {Object.<string, boolean>} */
    operationLoading: {},
    /** @type {string|null} */
    error: null,
    /** @type {ConnectionStatus|null} */
    connectionStatus: null,

    /**
     * Fetch all available AI agents
     * @returns {Promise<void>}
     */
    async fetchAgents() {
        this.loading = true;
        this.error = null;
        try {
            const result = await jsonRpc("/api/ai/agents", {});
            if (result.success) {
                this.agents = result.data;
            } else {
                this.error = result.error || "Failed to fetch agents";
            }
        } catch (err) {
            this.error = err.message || "Network error";
        } finally {
            this.loading = false;
        }
    },

    /**
     * Fetch chat history for a given channel
     * @param {number} channelId - discuss.channel ID
     * @param {number} [limit=50] - Number of messages to fetch
     * @param {number} [beforeId=null] - Fetch messages before this ID (pagination)
     * @returns {Promise<{success: boolean, data?: ChatMessage[], error?: string}>}
     */
    async fetchChatHistory(channelId, limit = 50, beforeId = null) {
        this.operationLoading.fetchHistory = true;
        try {
            const params = {
                channel_id: channelId,
                limit: limit,
            };
            if (beforeId) {
                params.before_id = beforeId;
            }
            const result = await jsonRpc("/api/ai/chat/history", params);
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.fetchHistory = false;
        }
    },

    /**
     * Post a message to a chat channel
     * @param {number} channelId - discuss.channel ID
     * @param {string} body - Message body text
     * @param {number|null} [agentId=null] - AI agent to mention (triggers AI reply)
     * @returns {Promise<{success: boolean, data?: ChatMessage, error?: string}>}
     */
    async postMessage(channelId, body, agentId = null) {
        this.operationLoading.postMessage = true;
        try {
            const params = {
                channel_id: channelId,
                body: body,
            };
            if (agentId) {
                params.agent_id = agentId;
            }
            const result = await jsonRpc("/api/ai/chat/post", params);
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.postMessage = false;
        }
    },

    /**
     * Get the SSE stream URL for a channel
     * The frontend should create an EventSource with this URL
     * @param {number} channelId - discuss.channel ID
     * @returns {string} SSE endpoint URL
     */
    getStreamUrl(channelId) {
        return `/api/ai/stream/${channelId}`;
    },

    /**
     * Fetch AI connection status (active provider info)
     * @returns {Promise<{success: boolean, data?: ConnectionStatus, error?: string}>}
     */
    async fetchConnectionStatus() {
        this.operationLoading.connectionStatus = true;
        try {
            const result = await jsonRpc("/api/ai/providers", {});
            if (result.success) {
                const providers = result.data || [];
                const active = providers.find(p => p.is_active);
                this.connectionStatus = {
                    connected: !!active,
                    provider_name: active ? active.name : "",
                    model_name: active ? active.model_name : "",
                };
                return { success: true, data: this.connectionStatus };
            } else {
                this.connectionStatus = { connected: false, provider_name: "", model_name: "" };
                return { success: false, error: result.error };
            }
        } catch (err) {
            this.connectionStatus = { connected: false, provider_name: "", model_name: "" };
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.connectionStatus = false;
        }
    },

    /**
     * Check if a specific operation is loading
     * @param {string} operation - Operation name
     * @returns {boolean}
     */
    isLoading(operation) {
        return this.operationLoading[operation] || false;
    },
});
