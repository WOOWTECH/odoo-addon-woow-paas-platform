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
 * @typedef {Object} ProjectData
 * @property {number} id - Project ID
 * @property {string} name - Project name
 * @property {string} description - Project description
 * @property {number} workspace_id - Associated workspace ID
 * @property {string} workspace_name - Associated workspace name
 * @property {number} task_count - Number of tasks
 * @property {number} open_task_count - Number of open tasks
 * @property {string} color - Project color index
 * @property {string} created_date - ISO date string
 */

/**
 * @typedef {Object} TaskData
 * @property {number} id - Task ID
 * @property {string} name - Task title
 * @property {string} description - Task description (HTML)
 * @property {number} project_id - Parent project ID
 * @property {string} project_name - Parent project name
 * @property {string} stage_name - Current stage name (e.g., "New", "In Progress", "Done")
 * @property {number} stage_id - Current stage ID
 * @property {number|null} user_id - Assigned user ID
 * @property {string|null} user_name - Assigned user name
 * @property {string} priority - Task priority ('0', '1', '2', '3')
 * @property {boolean} chat_enabled - Whether chat is enabled
 * @property {number|null} channel_id - Associated discuss.channel ID
 * @property {boolean} ai_auto_reply - Whether AI auto-reply is enabled
 * @property {string} created_date - ISO date string
 * @property {string|null} date_deadline - Deadline ISO date string
 */

/**
 * @typedef {Object} TaskStats
 * @property {number} total - Total task count
 * @property {number} active - Active (non-done) task count
 * @property {number} completion - Completion percentage (0-100)
 */

/**
 * Support Service
 * Handles all API calls related to support projects and tasks
 * @type {Object}
 */
export const supportService = reactive({
    /** @type {ProjectData[]} */
    projects: [],
    /** @type {TaskData[]} */
    tasks: [],
    /** @type {boolean} */
    loading: false,
    /** @type {Object.<string, boolean>} */
    operationLoading: {},
    /** @type {string|null} */
    error: null,
    /** @type {TaskStats} */
    stats: { total: 0, active: 0, completion: 0 },

    /**
     * Fetch projects for a workspace
     * @param {number} workspaceId - Workspace ID
     * @returns {Promise<void>}
     */
    async fetchProjects(workspaceId) {
        this.loading = true;
        this.error = null;
        try {
            const result = await jsonRpc(`/api/support/projects/${workspaceId}`, {
                action: "list",
            });
            if (result.success) {
                this.projects = result.data;
            } else {
                this.error = result.error || "Failed to fetch projects";
            }
        } catch (err) {
            this.error = err.message || "Network error";
        } finally {
            this.loading = false;
        }
    },

    /**
     * Fetch tasks for a workspace (across all projects)
     * @param {number} workspaceId - Workspace ID
     * @param {Object} [filters={}] - Optional filters
     * @param {number} [filters.project_id] - Filter by project
     * @param {string} [filters.stage] - Filter by stage name
     * @param {boolean} [filters.my_tasks] - Only show current user's tasks
     * @returns {Promise<void>}
     */
    async fetchTasks(workspaceId, filters = {}) {
        this.loading = true;
        this.error = null;
        try {
            const params = {
                action: "list",
                ...filters,
            };
            const result = await jsonRpc(`/api/support/tasks/${workspaceId}`, params);
            if (result.success) {
                this.tasks = result.data.tasks || result.data;
                // Update stats if provided
                if (result.data.stats) {
                    this.stats = result.data.stats;
                } else {
                    this._computeStats();
                }
            } else {
                this.error = result.error || "Failed to fetch tasks";
            }
        } catch (err) {
            this.error = err.message || "Network error";
        } finally {
            this.loading = false;
        }
    },

    /**
     * Fetch a single task by ID
     * @param {number} taskId - Task ID
     * @returns {Promise<{success: boolean, data?: TaskData, error?: string}>}
     */
    async fetchTask(taskId) {
        this.operationLoading.fetchTask = true;
        try {
            const result = await jsonRpc(`/api/support/tasks/detail/${taskId}`, {});
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.fetchTask = false;
        }
    },

    /**
     * Create a new task in a workspace project
     * @param {number} workspaceId - Workspace ID
     * @param {Object} data - Task creation data
     * @param {string} data.name - Task title
     * @param {string} [data.description] - Task description
     * @param {number} data.project_id - Target project ID
     * @param {string} [data.priority='0'] - Task priority
     * @param {number|null} [data.user_id] - Assigned user ID
     * @param {string|null} [data.date_deadline] - Deadline ISO date
     * @returns {Promise<{success: boolean, data?: TaskData, error?: string}>}
     */
    async createTask(workspaceId, data) {
        this.operationLoading.createTask = true;
        try {
            const result = await jsonRpc(`/api/support/tasks/${workspaceId}`, {
                action: "create",
                ...data,
            });
            if (result.success) {
                this.tasks = [result.data, ...this.tasks];
                this._computeStats();
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.createTask = false;
        }
    },

    /**
     * Update an existing task
     * @param {number} taskId - Task ID
     * @param {Object} data - Fields to update
     * @param {string} [data.name] - New task title
     * @param {string} [data.description] - New description
     * @param {number} [data.stage_id] - New stage ID
     * @param {string} [data.priority] - New priority
     * @param {number|null} [data.user_id] - New assigned user
     * @param {boolean} [data.chat_enabled] - Enable/disable chat
     * @param {boolean} [data.ai_auto_reply] - Enable/disable AI auto-reply
     * @returns {Promise<{success: boolean, data?: TaskData, error?: string}>}
     */
    async updateTask(taskId, data) {
        this.operationLoading.updateTask = true;
        try {
            const result = await jsonRpc(`/api/support/tasks/detail/${taskId}`, {
                action: "update",
                ...data,
            });
            if (result.success) {
                // Update local state
                const index = this.tasks.findIndex(t => t.id === taskId);
                if (index !== -1) {
                    this.tasks[index] = { ...this.tasks[index], ...result.data };
                }
                this._computeStats();
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.updateTask = false;
        }
    },

    /**
     * Fetch task stats for the hub page
     * @param {number|null} [workspaceId=null] - Optional workspace filter
     * @returns {Promise<{success: boolean, data?: TaskStats, error?: string}>}
     */
    async fetchStats(workspaceId = null) {
        this.operationLoading.fetchStats = true;
        try {
            const params = {};
            if (workspaceId) {
                params.workspace_id = workspaceId;
            }
            const result = await jsonRpc("/api/support/stats", params);
            if (result.success) {
                this.stats = result.data;
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.fetchStats = false;
        }
    },

    /**
     * Compute stats from local tasks data
     * @private
     */
    _computeStats() {
        const total = this.tasks.length;
        const done = this.tasks.filter(t =>
            t.stage_name === "Done" || t.stage_name === "Cancelled"
        ).length;
        const active = total - done;
        const completion = total > 0 ? Math.round((done / total) * 100) : 0;
        this.stats = { total, active, completion };
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
