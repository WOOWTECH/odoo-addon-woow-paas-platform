/** @odoo-module **/
import { reactive } from "@odoo/owl";

/**
 * Workspace Service
 * Handles all API calls related to workspaces
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
            const response = await fetch("/api/workspaces", {
                method: "GET",
                headers: {
                    "Content-Type": "application/json",
                },
            });
            const data = await response.json();
            if (data.success) {
                this.workspaces = data.data;
            } else {
                this.error = data.error || "Failed to fetch workspaces";
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
            const response = await fetch("/api/workspaces", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify(payload),
            });
            const data = await response.json();
            if (data.success) {
                this.workspaces = [data.data, ...this.workspaces];
                return { success: true, data: data.data };
            } else {
                let errMsg = data.error || "Failed to create workspace";
                // Ensure error is a string, not an object
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
            const response = await fetch(`/api/workspaces/${workspaceId}`, {
                method: "GET",
                headers: {
                    "Content-Type": "application/json",
                },
            });
            const data = await response.json();
            if (data.success) {
                return { success: true, data: data.data };
            } else {
                return { success: false, error: data.error };
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
            const response = await fetch(`/api/workspaces/${workspaceId}`, {
                method: "PUT",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify(payload),
            });
            const data = await response.json();
            if (data.success) {
                // Update local state
                const index = this.workspaces.findIndex(w => w.id === workspaceId);
                if (index !== -1) {
                    this.workspaces[index] = { ...this.workspaces[index], ...data.data };
                }
                return { success: true, data: data.data };
            } else {
                return { success: false, error: data.error };
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
            const response = await fetch(`/api/workspaces/${workspaceId}`, {
                method: "DELETE",
                headers: {
                    "Content-Type": "application/json",
                },
            });
            const data = await response.json();
            if (data.success) {
                // Remove from local state
                this.workspaces = this.workspaces.filter(w => w.id !== workspaceId);
                return { success: true };
            } else {
                return { success: false, error: data.error };
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
            const response = await fetch(`/api/workspaces/${workspaceId}/members`, {
                method: "GET",
                headers: {
                    "Content-Type": "application/json",
                },
            });
            const data = await response.json();
            if (data.success) {
                return { success: true, data: data.data };
            } else {
                return { success: false, error: data.error };
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
            const response = await fetch(`/api/workspaces/${workspaceId}/members`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify(payload),
            });
            const data = await response.json();
            if (data.success) {
                return { success: true, data: data.data };
            } else {
                return { success: false, error: data.error };
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
            const response = await fetch(`/api/workspaces/${workspaceId}/members/${accessId}`, {
                method: "PUT",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({ role }),
            });
            const data = await response.json();
            if (data.success) {
                return { success: true, data: data.data };
            } else {
                return { success: false, error: data.error };
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
            const response = await fetch(`/api/workspaces/${workspaceId}/members/${accessId}`, {
                method: "DELETE",
                headers: {
                    "Content-Type": "application/json",
                },
            });
            const data = await response.json();
            if (data.success) {
                return { success: true };
            } else {
                return { success: false, error: data.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        }
    },
});
