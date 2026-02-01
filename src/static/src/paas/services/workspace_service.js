/** @odoo-module **/
import { reactive } from "@odoo/owl";

/**
 * JSON-RPC helper for Odoo API calls
 * Odoo type="json" routes wrap response in { jsonrpc: "2.0", result: {...} }
 * @param {string} url - API endpoint
 * @param {Object} params - Request parameters
 * @returns {Promise<Object>} API response result
 * @throws {Error} Network or API errors
 */
async function jsonRpc(url, params) {
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
        });
    } catch (networkError) {
        throw new Error("Network error. Please check your connection and try again.");
    }

    // Check HTTP status before parsing
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

    // Parse JSON response
    let data;
    try {
        data = await response.json();
    } catch (parseError) {
        throw new Error("Invalid response from server.");
    }

    // Handle JSON-RPC error
    if (data.error) {
        const errorMsg = data.error.data?.message || data.error.message || "Unknown error";
        throw new Error(errorMsg);
    }

    return data.result;
}

/**
 * @typedef {Object} WorkspaceData
 * @property {number} id - Workspace ID
 * @property {string} name - Workspace name
 * @property {string} description - Workspace description
 * @property {string} slug - URL-friendly identifier
 * @property {string} state - Workspace state ('active' | 'archived')
 * @property {string} role - Current user's role ('owner' | 'admin' | 'user' | 'guest')
 * @property {number} member_count - Number of members
 * @property {boolean} is_owner - Whether current user is owner
 * @property {string} created_date - ISO date string
 */

/**
 * @typedef {Object} MemberData
 * @property {number} id - Access record ID
 * @property {number} user_id - User ID
 * @property {string} name - User display name
 * @property {string} email - User email
 * @property {string} role - Member role
 * @property {string|null} invited_by - Name of inviter
 * @property {string|null} invited_date - ISO date string
 */

/**
 * @typedef {Object} ApiResponse
 * @property {boolean} success - Whether operation succeeded
 * @property {*} [data] - Response data (on success)
 * @property {string} [error] - Error message (on failure)
 */

/**
 * Workspace Service
 * Handles all API calls related to workspaces using Odoo JSON-RPC
 * @type {Object}
 */
export const workspaceService = reactive({
    /** @type {WorkspaceData[]} */
    workspaces: [],
    /** @type {boolean} Global loading state */
    loading: false,
    /** @type {Object.<string, boolean>} Operation-specific loading states */
    operationLoading: {},
    /** @type {string|null} */
    error: null,

    /**
     * Fetch all workspaces for current user
     * @returns {Promise<void>}
     */
    async fetchWorkspaces() {
        this.loading = true;
        this.error = null;
        try {
            const result = await jsonRpc("/api/workspaces", { method: "list" });
            if (result.success) {
                this.workspaces = result.data;
            } else {
                this.error = result.error || "Failed to fetch workspaces";
            }
        } catch (err) {
            this.error = err.message || "Network error";
        } finally {
            this.loading = false;
        }
    },

    /**
     * Create a new workspace
     * @param {Object} payload - Workspace creation data
     * @param {string} payload.name - Workspace name
     * @param {string} [payload.description] - Workspace description
     * @returns {Promise<ApiResponse<WorkspaceData>>}
     */
    async createWorkspace(payload) {
        this.loading = true;
        this.operationLoading.create = true;
        this.error = null;
        try {
            const result = await jsonRpc("/api/workspaces", {
                method: "create",
                name: payload.name,
                description: payload.description || "",
            });
            if (result.success) {
                this.workspaces = [result.data, ...this.workspaces];
                return { success: true, data: result.data };
            } else {
                let errMsg = result.error || "Failed to create workspace";
                if (typeof errMsg === "object") {
                    errMsg = errMsg.message || errMsg.name || JSON.stringify(errMsg);
                }
                this.error = errMsg;
                return { success: false, error: errMsg };
            }
        } catch (err) {
            this.error = err.message || "Network error";
            return { success: false, error: this.error };
        } finally {
            this.loading = false;
            this.operationLoading.create = false;
        }
    },

    /**
     * Get a single workspace by ID
     * @param {number} workspaceId - Target workspace ID
     * @returns {Promise<ApiResponse<WorkspaceData>>}
     */
    async getWorkspace(workspaceId) {
        this.operationLoading.get = true;
        try {
            const result = await jsonRpc("/api/workspaces", {
                method: "get",
                workspace_id: workspaceId,
            });
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.get = false;
        }
    },

    /**
     * Update a workspace
     * @param {number} workspaceId - Target workspace ID
     * @param {Object} payload - Update data
     * @param {string} [payload.name] - New workspace name
     * @param {string} [payload.description] - New workspace description
     * @returns {Promise<ApiResponse<WorkspaceData>>}
     */
    async updateWorkspace(workspaceId, payload) {
        this.operationLoading.update = true;
        try {
            const result = await jsonRpc("/api/workspaces", {
                method: "update",
                workspace_id: workspaceId,
                ...payload,
            });
            if (result.success) {
                // Update local state
                const index = this.workspaces.findIndex(w => w.id === workspaceId);
                if (index !== -1) {
                    this.workspaces[index] = { ...this.workspaces[index], ...result.data };
                }
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.update = false;
        }
    },

    /**
     * Delete (archive) a workspace
     * @param {number} workspaceId - Target workspace ID
     * @returns {Promise<ApiResponse<void>>}
     */
    async deleteWorkspace(workspaceId) {
        this.operationLoading.delete = true;
        try {
            const result = await jsonRpc("/api/workspaces", {
                method: "delete",
                workspace_id: workspaceId,
            });
            if (result.success) {
                // Remove from local state
                this.workspaces = this.workspaces.filter(w => w.id !== workspaceId);
                return { success: true };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.delete = false;
        }
    },

    /**
     * Get workspace members
     * @param {number} workspaceId - Target workspace ID
     * @returns {Promise<ApiResponse<MemberData[]>>}
     */
    async getMembers(workspaceId) {
        this.operationLoading.getMembers = true;
        try {
            const result = await jsonRpc("/api/workspaces/members", {
                method: "list",
                workspace_id: workspaceId,
            });
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.getMembers = false;
        }
    },

    /**
     * Invite a member to workspace
     * @param {number} workspaceId - Target workspace ID
     * @param {Object} payload - Invitation data
     * @param {string} payload.email - User email to invite
     * @param {string} [payload.role='user'] - Role to assign
     * @returns {Promise<ApiResponse<MemberData>>}
     */
    async inviteMember(workspaceId, payload) {
        this.operationLoading.invite = true;
        try {
            const result = await jsonRpc("/api/workspaces/members", {
                method: "invite",
                workspace_id: workspaceId,
                email: payload.email,
                role: payload.role || "user",
            });
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.invite = false;
        }
    },

    /**
     * Update member role
     * @param {number} workspaceId - Target workspace ID
     * @param {number} accessId - Access record ID
     * @param {string} role - New role ('admin' | 'user' | 'guest')
     * @returns {Promise<ApiResponse<{id: number, role: string}>>}
     */
    async updateMemberRole(workspaceId, accessId, role) {
        this.operationLoading.updateRole = true;
        try {
            const result = await jsonRpc("/api/workspaces/members", {
                method: "update_role",
                workspace_id: workspaceId,
                access_id: accessId,
                role: role,
            });
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.updateRole = false;
        }
    },

    /**
     * Remove a member from workspace
     * @param {number} workspaceId - Target workspace ID
     * @param {number} accessId - Access record ID to remove
     * @returns {Promise<ApiResponse<void>>}
     */
    async removeMember(workspaceId, accessId) {
        this.operationLoading.remove = true;
        try {
            const result = await jsonRpc("/api/workspaces/members", {
                method: "remove",
                workspace_id: workspaceId,
                access_id: accessId,
            });
            if (result.success) {
                return { success: true };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.remove = false;
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
