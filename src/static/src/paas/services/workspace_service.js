/** @odoo-module **/
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { jsonRpc } from "./rpc";

/**
 * #1052：provision 走 operator 的 helm 安裝，平台側 HELM_OPERATION_TIMEOUT 是 120 秒
 * （src/services/paas_operator.py）。本檔改用共用 rpc.js 之前沒有 client timeout；共用版
 * 預設 30 秒會在伺服器還在等 helm 時先 abort，租戶看到 timeout、後端卻照常完成。
 * 所以 provision 給比伺服器預算大的預算；其餘 19 個呼叫都是快速 CRUD，吃預設 30 秒。
 */
const PROVISION_REQUEST_TIMEOUT_MS = 150000;

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
            const result = await jsonRpc("/api/workspaces", { action: "list" });
            if (result.success) {
                this.workspaces = result.data;
            } else {
                this.error = result.error || _t("Failed to fetch workspaces");
            }
        } catch (err) {
            this.error = err.message || _t("Network error");
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
                action: "create",
                name: payload.name,
                description: payload.description || "",
            });
            if (result.success) {
                this.workspaces = [result.data, ...this.workspaces];
                return { success: true, data: result.data };
            } else {
                let errMsg = result.error || _t("Failed to create workspace");
                if (typeof errMsg === "object") {
                    errMsg = errMsg.message || errMsg.name || JSON.stringify(errMsg);
                }
                this.error = errMsg;
                return { success: false, error: errMsg };
            }
        } catch (err) {
            this.error = err.message || _t("Network error");
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
            const result = await jsonRpc(`/api/workspaces/${workspaceId}`, {
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
            const result = await jsonRpc(`/api/workspaces/${workspaceId}`, {
                action: "update",
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
            const result = await jsonRpc(`/api/workspaces/${workspaceId}`, {
                action: "delete",
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
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/members`, {
                action: "list",
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
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/members`, {
                action: "invite",
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
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/members/${accessId}`, {
                action: "update_role",
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
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/members/${accessId}`, {
                action: "remove",
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

    // -------------------------------------------------------------------------
    // Smart Home Methods
    // -------------------------------------------------------------------------

    /**
     * Fetch available Smart Home templates (catalog entries that group plans).
     *
     * Mirrors the cloud-service template flow: a template is the unit an
     * admin manages plan-bindings on, and the frontend has the user pick
     * one before listing plans. With a single active template the picker is
     * a passive default; once multiple active templates exist the modal
     * surfaces them as a choice.
     *
     * Returns ApiResponse<Array> so callers can surface backend errors —
     * the design rejects silent fallback to an empty list because that
     * hides admin misconfiguration ("No plans available" with no clue
     * what's actually wrong).
     *
     * @returns {Promise<ApiResponse<Array>>}
     */
    async fetchSmartHomeTemplates() {
        try {
            const result = await jsonRpc("/api/smart-home/templates", {});
            if (result.success) {
                return { success: true, data: result.data };
            }
            return {
                success: false,
                error: result.error || _t("Failed to fetch templates"),
            };
        } catch (err) {
            return { success: false, error: err.message || _t("Network error") };
        }
    },

    /**
     * Fetch subscription plans bound to a Smart Home template.
     *
     * `templateId` is required — there is no global "all SH plans" listing
     * any more (hard fail when no template). Pass an id obtained from
     * `fetchSmartHomeTemplates`.
     *
     * @param {number} templateId
     * @returns {Promise<ApiResponse<Array>>}
     */
    async fetchSmartHomePlans(templateId) {
        if (!templateId) {
            return { success: false, error: _t("templateId is required") };
        }
        try {
            const result = await jsonRpc(
                `/api/smart-home/templates/${templateId}/plans`, {},
            );
            if (result.success) {
                return { success: true, data: result.data };
            }
            return {
                success: false,
                error: result.error || _t("Failed to fetch plans"),
            };
        } catch (err) {
            return { success: false, error: err.message || _t("Network error") };
        }
    },

    /**
     * Get the upcoming-invoice breakdown for a Smart Home skip-trial.
     *
     * Side-effect-free counterpart to `skipTrialSmartHome`. Used by the
     * confirm modal to render the actual amount + wallet sufficiency
     * before the user commits.
     *
     * @param {number} workspaceId
     * @param {number} homeId
     * @returns {Promise<ApiResponse<Object>>}
     */
    async getSkipTrialEstimateSmartHome(workspaceId, homeId) {
        this.operationLoading.skipTrialEstimateSmartHome = true;
        try {
            const result = await jsonRpc(
                `/api/workspaces/${workspaceId}/smarthomes/${homeId}/skip-trial-estimate`,
                {},
            );
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.skipTrialEstimateSmartHome = false;
        }
    },

    /**
     * End a Smart Home subscription's trial and start paid billing.
     *
     * @param {number} workspaceId
     * @param {number} homeId
     * @returns {Promise<ApiResponse<Object>>}
     */
    async skipTrialSmartHome(workspaceId, homeId) {
        this.operationLoading.skipTrialSmartHome = true;
        try {
            const result = await jsonRpc(
                `/api/workspaces/${workspaceId}/smarthomes/${homeId}/skip-trial`,
                {},
            );
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.skipTrialSmartHome = false;
        }
    },

    /**
     * Change a Smart Home subscription's plan（含 AC 席數變體切換）.
     *
     * 後端 `api_smarthome_change_plan`（controllers/smart_home.py）已完成
     * refund→wallet→close-old→open-new cascade；此 client 讓 SH detail 面的
     * ChangePlanModal 能觸發它——補齊 SH 端唯一缺的前端綁定（對齊
     * cloud_service.changePlan / securityAccessService.changePlan）。席數變化
     * 靠選到不同的席數變體 plan 表達，故 payload 與 CS/SA 一致只送 plan_id。
     *
     * @param {number} workspaceId
     * @param {number} homeId
     * @param {number} planId - 目標方案 id（base 或席數變體）
     * @returns {Promise<ApiResponse<Object>>}
     */
    async changeSmartHomePlan(workspaceId, homeId, planId) {
        this.operationLoading.changeSmartHomePlan = true;
        try {
            const result = await jsonRpc(
                `/api/workspaces/${workspaceId}/smarthomes/${homeId}/change-plan`,
                { plan_id: planId },
            );
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.changeSmartHomePlan = false;
        }
    },

    /**
     * Fetch all smart homes for a workspace
     * @param {number} workspaceId - Target workspace ID
     * @returns {Promise<ApiResponse<Object[]>>}
     */
    async getSmartHomes(workspaceId) {
        this.operationLoading.getSmartHomes = true;
        try {
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/smarthomes`, {
                action: "list",
            });
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.getSmartHomes = false;
        }
    },

    /**
     * Create a new smart home
     * @param {number} workspaceId - Target workspace ID
     * @param {Object} data - Smart home creation data
     * @param {string} data.name - Smart home name
     * @param {number} data.template_id - Smart Home template ID (required)
     * @param {number} data.plan_id - Subscription plan ID (required, must
     *   belong to the chosen template)
     * @returns {Promise<ApiResponse<Object>>}
     */
    async createSmartHome(workspaceId, data) {
        this.operationLoading.createSmartHome = true;
        try {
            // ha_port 不帶：後端 model default=8123（issue #269，port 壓死不可編輯）
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/smarthomes`, {
                action: "create",
                name: data.name,
                plan_id: data.plan_id,
                template_id: data.template_id,
            });
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.createSmartHome = false;
        }
    },

    /**
     * Get a single smart home by ID
     * @param {number} workspaceId - Target workspace ID
     * @param {number} homeId - Smart home ID
     * @returns {Promise<ApiResponse<Object>>}
     */
    async getSmartHome(workspaceId, homeId) {
        this.operationLoading.getSmartHome = true;
        try {
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/smarthomes/${homeId}`, {
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
            this.operationLoading.getSmartHome = false;
        }
    },

    /**
     * Delete a smart home
     * @param {number} workspaceId - Target workspace ID
     * @param {number} homeId - Smart home ID
     * @returns {Promise<ApiResponse<void>>}
     */
    async deleteSmartHome(workspaceId, homeId) {
        this.operationLoading.deleteSmartHome = true;
        try {
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/smarthomes/${homeId}`, {
                action: "delete",
            });
            if (result.success) {
                return { success: true };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.deleteSmartHome = false;
        }
    },

    /**
     * Provision a smart home (set up tunnel)
     * @param {number} workspaceId - Target workspace ID
     * @param {number} homeId - Smart home ID
     * @returns {Promise<ApiResponse<Object>>}
     */
    async provisionSmartHome(workspaceId, homeId) {
        this.operationLoading.provisionSmartHome = true;
        try {
            const result = await jsonRpc(
                `/api/workspaces/${workspaceId}/smarthomes/${homeId}`,
                { action: "provision" },
                PROVISION_REQUEST_TIMEOUT_MS
            );
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.provisionSmartHome = false;
        }
    },

    /**
     * Refresh smart home status
     * @param {number} workspaceId - Target workspace ID
     * @param {number} homeId - Smart home ID
     * @returns {Promise<ApiResponse<Object>>}
     */
    async refreshSmartHomeStatus(workspaceId, homeId) {
        this.operationLoading.refreshSmartHomeStatus = true;
        try {
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/smarthomes/${homeId}`, {
                action: "refresh_status",
            });
            if (result.success) {
                return { success: true, data: result.data };
            } else {
                return { success: false, error: result.error };
            }
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.refreshSmartHomeStatus = false;
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
