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
 * @typedef {Object} TemplateData
 * @property {number} id - Template ID
 * @property {string} name - Template name
 * @property {string} slug - URL-friendly identifier
 * @property {string} description - Short description
 * @property {string} category - Template category
 * @property {string[]} tags - Template tags
 * @property {number} monthly_price - Monthly price in USD
 * @property {string} documentation_url - Documentation URL
 * @property {number} default_port - Default service port
 * @property {boolean} ingress_enabled - Whether ingress is enabled
 * @property {number} min_vcpu - Minimum vCPU
 * @property {number} min_ram_gb - Minimum RAM in GB
 * @property {number} min_storage_gb - Minimum storage in GB
 */

/**
 * @typedef {Object} ServiceData
 * @property {number} id - Service ID
 * @property {string} name - Service name
 * @property {string} state - Service state
 * @property {string} subdomain - Service subdomain
 * @property {string} custom_domain - Custom domain
 * @property {Object} template - Template info
 * @property {number} helm_revision - Helm revision number
 * @property {string} created_date - ISO date string
 * @property {string} deployed_at - ISO date string
 */

/**
 * Platform configuration cache
 */
let _configCache = null;

/**
 * Get platform configuration (cached)
 * @returns {Promise<{domain: string}>}
 */
export async function getConfig() {
    if (_configCache) {
        return _configCache;
    }
    try {
        const result = await jsonRpc("/api/config", {});
        if (result.success) {
            _configCache = result.data;
            return _configCache;
        }
    } catch (err) {
        console.warn("Failed to fetch config, using defaults:", err);
    }
    // Return default config
    return { domain: "woowtech.io" };
}

/**
 * Get the configured domain
 * @returns {Promise<string>}
 */
export async function getDomain() {
    const config = await getConfig();
    return config.domain;
}

/**
 * Cloud Service
 * Handles all API calls related to cloud templates and services
 */
export const cloudService = reactive({
    /** @type {TemplateData[]} */
    templates: [],
    /** @type {ServiceData[]} */
    services: [],
    /** @type {boolean} */
    loading: false,
    /** @type {Object.<string, boolean>} */
    operationLoading: {},
    /** @type {string|null} */
    error: null,
    /** @type {string} */
    domain: "woowtech.io",

    /**
     * Initialize config (call this early in app lifecycle)
     */
    async initConfig() {
        const config = await getConfig();
        this.domain = config.domain;
    },

    /**
     * Fetch all available templates
     * @param {string|null} category - Filter by category
     * @param {string|null} search - Search in name/description
     * @returns {Promise<void>}
     */
    async fetchTemplates(category = null, search = null) {
        this.loading = true;
        this.error = null;
        try {
            const result = await jsonRpc("/api/cloud/templates", {
                category: category || null,
                search: search || null,
            });
            if (result.success) {
                this.templates = result.data;
            } else {
                this.error = result.error || "Failed to fetch templates";
            }
        } catch (err) {
            this.error = err.message || "Network error";
        } finally {
            this.loading = false;
        }
    },

    /**
     * Get a single template by ID
     * @param {number} templateId - Template ID
     * @returns {Promise<{success: boolean, data?: TemplateData, error?: string}>}
     */
    async fetchTemplate(templateId) {
        this.operationLoading.fetchTemplate = true;
        try {
            const result = await jsonRpc(`/api/cloud/templates/${templateId}`, {});
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.fetchTemplate = false;
        }
    },

    /**
     * Fetch services for a workspace
     * @param {number} workspaceId - Workspace ID
     * @returns {Promise<void>}
     */
    async fetchServices(workspaceId) {
        this.operationLoading.fetchServices = true;
        this.error = null;
        try {
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/services`, {
                action: "list",
            });
            if (result.success) {
                this.services = result.data;
            } else {
                this.error = result.error || "Failed to fetch services";
            }
        } catch (err) {
            this.error = err.message || "Network error";
        } finally {
            this.operationLoading.fetchServices = false;
        }
    },

    /**
     * Create a new service
     * @param {number} workspaceId - Workspace ID
     * @param {Object} payload - Service creation data
     * @param {number} payload.template_id - Template ID
     * @param {string} payload.name - Service name
     * @param {Object} [payload.values] - Helm values override
     * @returns {Promise<{success: boolean, data?: ServiceData, error?: string}>}
     */
    async createService(workspaceId, payload) {
        this.operationLoading.createService = true;
        try {
            const params = {
                action: "create",
                template_id: payload.template_id,
                name: payload.name,
                values: payload.values || {},
            };
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/services`, params);
            if (result.success) {
                this.services = [result.data, ...this.services];
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.createService = false;
        }
    },

    /**
     * Get a single service
     * @param {number} workspaceId - Workspace ID
     * @param {number} serviceId - Service ID
     * @returns {Promise<{success: boolean, data?: ServiceData, error?: string}>}
     */
    async fetchService(workspaceId, serviceId) {
        this.operationLoading.fetchService = true;
        try {
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/services/${serviceId}`, {
                action: "get",
            });
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.fetchService = false;
        }
    },

    /**
     * Update/upgrade a service
     * @param {number} workspaceId - Workspace ID
     * @param {number} serviceId - Service ID
     * @param {Object} values - New Helm values
     * @param {string} version - Optional chart version
     * @returns {Promise<{success: boolean, data?: ServiceData, error?: string}>}
     */
    async updateService(workspaceId, serviceId, values, version = null) {
        this.operationLoading.updateService = true;
        try {
            const params = {
                action: "update",
                values: values,
            };
            if (version) {
                params.version = version;
            }
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/services/${serviceId}`, params);
            if (result.success) {
                // Update local state
                const index = this.services.findIndex(s => s.id === serviceId);
                if (index !== -1) {
                    this.services[index] = { ...this.services[index], ...result.data };
                }
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.updateService = false;
        }
    },

    /**
     * Delete a service
     * @param {number} workspaceId - Workspace ID
     * @param {number} serviceId - Service ID
     * @returns {Promise<{success: boolean, error?: string}>}
     */
    async deleteService(workspaceId, serviceId) {
        this.operationLoading.deleteService = true;
        try {
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/services/${serviceId}`, {
                action: "delete",
            });
            if (result.success) {
                this.services = this.services.filter(s => s.id !== serviceId);
                return { success: true };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.deleteService = false;
        }
    },

    /**
     * Get revision history for a service
     * @param {number} workspaceId - Workspace ID
     * @param {number} serviceId - Service ID
     * @returns {Promise<{success: boolean, data?: Array, error?: string}>}
     */
    async fetchRevisions(workspaceId, serviceId) {
        this.operationLoading.fetchRevisions = true;
        try {
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/services/${serviceId}/revisions`, {});
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.fetchRevisions = false;
        }
    },

    /**
     * Rollback a service to a previous revision
     * @param {number} workspaceId - Workspace ID
     * @param {number} serviceId - Service ID
     * @param {number} revision - Target revision number
     * @returns {Promise<{success: boolean, data?: ServiceData, error?: string}>}
     */
    async rollbackService(workspaceId, serviceId, revision) {
        this.operationLoading.rollbackService = true;
        try {
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/services/${serviceId}/rollback`, {
                revision: revision,
            });
            if (result.success) {
                // Update local state if service data is returned
                if (result.data) {
                    const index = this.services.findIndex(s => s.id === serviceId);
                    if (index !== -1) {
                        this.services[index] = { ...this.services[index], ...result.data };
                    }
                }
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.rollbackService = false;
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

    // ==================== MCP Servers ====================

    /**
     * Fetch MCP servers for a service
     * @param {number} workspaceId
     * @param {number} serviceId
     * @returns {Promise<{success: boolean, data?: Array, error?: string}>}
     */
    async fetchMcpServers(workspaceId, serviceId) {
        this.operationLoading.fetchMcpServers = true;
        try {
            const result = await jsonRpc(
                `/api/workspaces/${workspaceId}/services/${serviceId}/mcp-servers`,
                { action: "list" },
            );
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.fetchMcpServers = false;
        }
    },

    /**
     * Create an MCP server for a service
     * @param {number} workspaceId
     * @param {number} serviceId
     * @param {Object} payload - { name, url, transport?, api_key?, description? }
     * @returns {Promise<{success: boolean, data?: Object, error?: string}>}
     */
    async createMcpServer(workspaceId, serviceId, payload) {
        this.operationLoading.createMcpServer = true;
        try {
            const result = await jsonRpc(
                `/api/workspaces/${workspaceId}/services/${serviceId}/mcp-servers`,
                { action: "create", ...payload },
            );
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.createMcpServer = false;
        }
    },

    /**
     * Update an MCP server
     * @param {number} workspaceId
     * @param {number} serviceId
     * @param {number} serverId
     * @param {Object} payload - fields to update
     * @returns {Promise<{success: boolean, data?: Object, error?: string}>}
     */
    async updateMcpServer(workspaceId, serviceId, serverId, payload) {
        this.operationLoading.updateMcpServer = true;
        try {
            const result = await jsonRpc(
                `/api/workspaces/${workspaceId}/services/${serviceId}/mcp-servers`,
                { action: "update", server_id: serverId, ...payload },
            );
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.updateMcpServer = false;
        }
    },

    /**
     * Delete an MCP server
     * @param {number} workspaceId
     * @param {number} serviceId
     * @param {number} serverId
     * @returns {Promise<{success: boolean, error?: string}>}
     */
    async deleteMcpServer(workspaceId, serviceId, serverId) {
        this.operationLoading.deleteMcpServer = true;
        try {
            const result = await jsonRpc(
                `/api/workspaces/${workspaceId}/services/${serviceId}/mcp-servers`,
                { action: "delete", server_id: serverId },
            );
            if (result.success) {
                return { success: true };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.deleteMcpServer = false;
        }
    },

    /**
     * Sync tools from an MCP server
     * @param {number} workspaceId
     * @param {number} serviceId
     * @param {number} serverId
     * @returns {Promise<{success: boolean, data?: Object, error?: string}>}
     */
    async syncMcpServer(workspaceId, serviceId, serverId) {
        this.operationLoading.syncMcpServer = true;
        try {
            const result = await jsonRpc(
                `/api/workspaces/${workspaceId}/services/${serviceId}/mcp-servers`,
                { action: "sync", server_id: serverId },
                60000,  // 60s timeout for tool sync
            );
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.syncMcpServer = false;
        }
    },

    /**
     * Test MCP server connection
     * @param {number} workspaceId
     * @param {number} serviceId
     * @param {number} serverId
     * @returns {Promise<{success: boolean, data?: Object, error?: string}>}
     */
    async testMcpServer(workspaceId, serviceId, serverId) {
        this.operationLoading.testMcpServer = true;
        try {
            const result = await jsonRpc(
                `/api/workspaces/${workspaceId}/services/${serviceId}/mcp-servers`,
                { action: "test", server_id: serverId },
                60000,
            );
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.testMcpServer = false;
        }
    },
});
