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
 * Workspace Service
 * Handles all API calls related to workspaces using Odoo JSON-RPC
 */
export const workspaceService = reactive({
    workspaces: [],
    loading: false,
    error: null,

    /**
     * Fetch all workspaces for current user
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
     * @param {Object} payload - { name, description }
     */
    async createWorkspace(payload) {
        this.loading = true;
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
        }
    },

    /**
     * Get a single workspace by ID
     * @param {number} workspaceId
     */
    async getWorkspace(workspaceId) {
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
        }
    },

    /**
     * Update a workspace
     * @param {number} workspaceId
     * @param {Object} payload - { name, description }
     */
    async updateWorkspace(workspaceId, payload) {
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
        }
    },

    /**
     * Delete (archive) a workspace
     * @param {number} workspaceId
     */
    async deleteWorkspace(workspaceId) {
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
        }
    },

    /**
     * Get workspace members
     * @param {number} workspaceId
     */
    async getMembers(workspaceId) {
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
        }
    },

    /**
     * Invite a member to workspace
     * @param {number} workspaceId
     * @param {Object} payload - { email, role }
     */
    async inviteMember(workspaceId, payload) {
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
        }
    },

    /**
     * Update member role
     * @param {number} workspaceId
     * @param {number} accessId
     * @param {string} role
     */
    async updateMemberRole(workspaceId, accessId, role) {
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
        }
    },

    /**
     * Remove a member from workspace
     * @param {number} workspaceId
     * @param {number} accessId
     */
    async removeMember(workspaceId, accessId) {
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
        }
    },
});
