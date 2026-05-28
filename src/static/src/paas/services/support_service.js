/** @odoo-module **/
import { reactive } from "@odoo/owl";
import { jsonRpc } from "./rpc";

/**
 * @typedef {Object} ProjectData
 * @property {number} id - Project ID
 * @property {string} name - Project name
 * @property {string} description - Project description
 * @property {number|null} cloud_service_id - Associated cloud service ID
 * @property {string} cloud_service_name - Associated cloud service name
 * @property {number|null} workspace_id - Associated workspace ID (computed)
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
    /** @type {number} */
    _loadingCount: 0,
    /** @type {Object.<string, boolean>} */
    operationLoading: {},
    /** @type {string|null} */
    error: null,
    /** @type {TaskStats} */
    stats: { total: 0, active: 0, completion: 0 },
    /** @type {Array<{id: number, name: string, sequence: number}>} */
    stages: [],

    /**
     * Fetch stages for a specific project
     * @param {number} projectId - Project ID
     * @returns {Promise<void>}
     */
    async fetchProjectStages(projectId) {
        this._loadingCount++;
        this.loading = true;
        this.error = null;
        try {
            const result = await jsonRpc(`/api/support/projects/${projectId}/stages`, {});
            if (result.success) {
                this.stages = result.data;
            } else {
                this.error = result.error || "Failed to fetch stages";
            }
        } catch (err) {
            this.error = err.message || "Network error";
        } finally {
            this._loadingCount = Math.max(0, this._loadingCount - 1);
            this.loading = this._loadingCount > 0;
        }
    },

    /**
     * Fetch all projects (no workspace filter)
     * @returns {Promise<void>}
     */
    async fetchAllProjects() {
        this._loadingCount++;
        this.loading = true;
        this.error = null;
        try {
            const result = await jsonRpc("/api/support/projects", {
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
            this._loadingCount = Math.max(0, this._loadingCount - 1);
            this.loading = this._loadingCount > 0;
        }
    },

    /**
     * Fetch projects for a workspace
     * @param {number} workspaceId - Workspace ID
     * @returns {Promise<void>}
     */
    async fetchProjects(workspaceId) {
        this._loadingCount++;
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
            this._loadingCount = Math.max(0, this._loadingCount - 1);
            this.loading = this._loadingCount > 0;
        }
    },

    /**
     * Fetch all tasks (no workspace filter)
     * @param {Object} [filters={}] - Optional filters
     * @param {number} [filters.project_id] - Filter by project
     * @param {string} [filters.stage] - Filter by stage name
     * @param {boolean} [filters.my_tasks] - Only show current user's tasks
     * @returns {Promise<void>}
     */
    async fetchAllTasks(filters = {}) {
        this._loadingCount++;
        this.loading = true;
        this.error = null;
        try {
            const params = {
                action: "list",
                ...filters,
            };
            const result = await jsonRpc("/api/support/tasks", params);
            if (result.success) {
                this.tasks = result.data.tasks || result.data;
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
            this._loadingCount = Math.max(0, this._loadingCount - 1);
            this.loading = this._loadingCount > 0;
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
        this._loadingCount++;
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
            this._loadingCount = Math.max(0, this._loadingCount - 1);
            this.loading = this._loadingCount > 0;
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
     * Create a new project in a workspace
     * @param {number} workspaceId - Workspace ID
     * @param {Object} data - Project creation data
     * @param {string} data.name - Project name
     * @param {string} [data.description] - Project description
     * @returns {Promise<{success: boolean, data?: ProjectData, error?: string}>}
     */
    async createProject(workspaceId, data) {
        this.operationLoading.createProject = true;
        try {
            const result = await jsonRpc(`/api/support/projects/${workspaceId}`, {
                action: "create",
                ...data,
            });
            if (result.success) {
                this.projects = [result.data, ...this.projects];
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.createProject = false;
        }
    },

    /**
     * Create a new unbound (personal) project — no workspace, no cloud service.
     * @param {Object} data - Project creation data
     * @param {string} data.name - Project name
     * @param {string} [data.description] - Project description
     * @returns {Promise<{success: boolean, data?: ProjectData, error?: string}>}
     */
    async createUnboundProject(data) {
        this.operationLoading.createProject = true;
        try {
            const result = await jsonRpc("/api/support/projects", {
                action: "create",
                ...data,
            });
            if (result.success) {
                this.projects = [result.data, ...this.projects];
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.createProject = false;
        }
    },

    /**
     * Create a new project bound to a cloud service
     * @param {number} cloudServiceId - Cloud Service ID
     * @param {Object} data - Project creation data
     * @param {string} data.name - Project name
     * @param {string} [data.description] - Project description
     * @returns {Promise<{success: boolean, data?: ProjectData, error?: string}>}
     */
    async createProjectForService(cloudServiceId, data) {
        this.operationLoading.createProject = true;
        try {
            const result = await jsonRpc(`/api/support/cloud-services/${cloudServiceId}/project`, {
                action: "create",
                ...data,
            });
            if (result.success) {
                this.projects = [result.data, ...this.projects];
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.createProject = false;
        }
    },

    /**
     * Fetch the project bound to a cloud service (if any)
     * @param {number} cloudServiceId - Cloud Service ID
     * @returns {Promise<{success: boolean, data?: ProjectData|null, error?: string}>}
     */
    async fetchProjectForService(cloudServiceId) {
        this.operationLoading.fetchProjectForService = true;
        try {
            const result = await jsonRpc(`/api/support/cloud-services/${cloudServiceId}/project`, {
                action: "list",
            });
            if (result.success) {
                const project = result.data.length > 0 ? result.data[0] : null;
                return { success: true, data: project };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.fetchProjectForService = false;
        }
    },

    /**
     * Create a new task in a project
     * @param {Object} data - Task creation data
     * @param {string} data.name - Task title
     * @param {string} [data.description] - Task description
     * @param {number} data.project_id - Target project ID
     * @param {string} [data.priority='0'] - Task priority
     * @param {number|null} [data.user_id] - Assigned user ID
     * @param {string|null} [data.date_deadline] - Deadline ISO date
     * @returns {Promise<{success: boolean, data?: TaskData, error?: string}>}
     */
    async createTask(data) {
        this.operationLoading.createTask = true;
        try {
            const result = await jsonRpc("/api/support/tasks", {
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
     * Fetch full detail for a single project (used by ProjectDetailPage).
     * @param {number} projectId
     * @returns {Promise<{success: boolean, data?: Object, error?: string}>}
     */
    async fetchProjectDetail(projectId) {
        this.operationLoading.fetchProjectDetail = true;
        try {
            const result = await jsonRpc(`/api/support-projects/${projectId}`, {
                action: "get",
            });
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.fetchProjectDetail = false;
        }
    },

    /**
     * Update a project (name / description / cloud_service_id).
     * Optimistically refreshes the matching row in this.projects so list-page
     * badges reflect the change without a reload (Plan AC #17 / #21).
     * @param {number} projectId
     * @param {Object} payload
     * @returns {Promise<{success: boolean, data?: Object, error?: string}>}
     */
    async updateProject(projectId, payload) {
        this.operationLoading.updateProject = true;
        try {
            const result = await jsonRpc(`/api/support-projects/${projectId}`, {
                action: "update",
                ...payload,
            });
            if (result.success) {
                const idx = this.projects.findIndex((p) => p.id === projectId);
                if (idx !== -1) {
                    this.projects[idx] = { ...this.projects[idx], ...result.data };
                }
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.updateProject = false;
        }
    },

    /**
     * List user-scope MCP servers for a project.
     * @param {number} projectId
     */
    async fetchProjectMcpServers(projectId) {
        this.operationLoading.fetchProjectMcpServers = true;
        try {
            const result = await jsonRpc(
                `/api/support-projects/${projectId}/mcp-servers`,
                { action: "list" },
            );
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.fetchProjectMcpServers = false;
        }
    },

    async createProjectMcpServer(projectId, payload) {
        this.operationLoading.createProjectMcpServer = true;
        try {
            const result = await jsonRpc(
                `/api/support-projects/${projectId}/mcp-servers`,
                { action: "create", ...payload },
            );
            return result.success
                ? { success: true, data: result.data }
                : { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.createProjectMcpServer = false;
        }
    },

    /**
     * Fetch the sidecar MCP prefill hint (URL / transport / api_key /
     * suggested_name) for a project. Drives the ``Add Sidecar MCP``
     * shortcut's modal prefill.
     * @param {number} projectId
     */
    async fetchSidecarHint(projectId) {
        this.operationLoading.fetchSidecarHint = true;
        try {
            const result = await jsonRpc(
                `/api/support-projects/${projectId}/mcp-servers`,
                { action: "sidecar-hint" },
            );
            return result.success
                ? { success: true, data: result.data }
                : { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.fetchSidecarHint = false;
        }
    },

    async updateProjectMcpServer(projectId, serverId, payload) {
        this.operationLoading.updateProjectMcpServer = true;
        try {
            const result = await jsonRpc(
                `/api/support-projects/${projectId}/mcp-servers`,
                { action: "update", server_id: serverId, ...payload },
            );
            return result.success
                ? { success: true, data: result.data }
                : { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.updateProjectMcpServer = false;
        }
    },

    async deleteProjectMcpServer(projectId, serverId) {
        this.operationLoading.deleteProjectMcpServer = true;
        try {
            const result = await jsonRpc(
                `/api/support-projects/${projectId}/mcp-servers`,
                { action: "delete", server_id: serverId },
            );
            return result.success
                ? { success: true }
                : { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.deleteProjectMcpServer = false;
        }
    },

    async syncProjectMcpServer(projectId, serverId) {
        this.operationLoading.syncProjectMcpServer = true;
        try {
            const result = await jsonRpc(
                `/api/support-projects/${projectId}/mcp-servers`,
                { action: "sync", server_id: serverId },
            );
            return result.success
                ? { success: true, data: result.data }
                : { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.syncProjectMcpServer = false;
        }
    },

    async testProjectMcpServer(projectId, serverId) {
        this.operationLoading.testProjectMcpServer = true;
        try {
            const result = await jsonRpc(
                `/api/support-projects/${projectId}/mcp-servers`,
                { action: "test", server_id: serverId },
            );
            return result.success
                ? { success: true, data: result.data }
                : { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.testProjectMcpServer = false;
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
