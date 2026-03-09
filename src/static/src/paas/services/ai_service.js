/** @odoo-module **/
import { reactive } from "@odoo/owl";
import { jsonRpc } from "./rpc";

/**
 * @typedef {Object} AssistantData
 * @property {number} id - Assistant ID
 * @property {string} name - Assistant name
 * @property {string} description - Assistant description
 * @property {number|null} config_id - Associated AI config ID
 * @property {string|null} config_name - Associated AI config name
 * @property {string} avatar_color - Avatar color hex
 * @property {boolean} is_default - Whether this is the default assistant
 */

/**
 * @typedef {Object} ChatMessage
 * @property {number} id - Message ID
 * @property {string} body - Message body (HTML)
 * @property {string} author_name - Author display name
 * @property {number} author_id - Author user ID
 * @property {string} date - ISO datetime string
 * @property {boolean} is_ai - Whether message is from AI
 * @property {Array} attachments - Message attachments
 */

/**
 * @typedef {Object} ConnectionStatus
 * @property {boolean} connected - Whether AI is connected
 * @property {string} provider_name - Active config name
 * @property {string} model_name - Active model name
 */

/**
 * AI Service
 * Handles all API calls related to AI assistants, chat, and streaming
 * @type {Object}
 */
export const aiService = reactive({
    /** @type {AssistantData[]} */
    assistants: [],
    /** @type {boolean} */
    loading: false,
    /** @type {Object.<string, boolean>} */
    operationLoading: {},
    /** @type {string|null} */
    error: null,
    /** @type {ConnectionStatus|null} */
    connectionStatus: null,

    /**
     * Fetch all available AI assistants
     * @returns {Promise<void>}
     */
    async fetchAssistants() {
        this.loading = true;
        this.error = null;
        try {
            const result = await jsonRpc("/api/ai/agents", {});
            if (result.success) {
                this.assistants = result.data;
            } else {
                this.error = result.error || "Failed to fetch assistants";
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
     * @returns {Promise<{success: boolean, data?: ChatMessage, error?: string}>}
     */
    async postMessage(channelId, body) {
        this.operationLoading.postMessage = true;
        try {
            const params = {
                channel_id: channelId,
                body: body,
            };
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
        const csrfToken = encodeURIComponent(odoo.csrf_token || '');
        return `/api/ai/stream/${channelId}?csrf_token=${csrfToken}`;
    },

    /**
     * Fetch AI connection status
     * @returns {Promise<{success: boolean, data?: ConnectionStatus, error?: string}>}
     */
    async fetchConnectionStatus() {
        this.operationLoading.connectionStatus = true;
        try {
            const result = await jsonRpc("/api/ai/connection-status", {});
            if (result.success) {
                this.connectionStatus = result.data;
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
