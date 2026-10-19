/** @odoo-module **/
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { jsonRpc } from "./rpc";

/**
 * #461 Reset admin password 專屬預算（90 秒）。
 *
 * 為什麼不用共用的 30s：reset 是「等 operator 換完叢集憑證」而不是一次讀取，
 * stg 實測連**成功**的案例都要 49.9s／76.7s（#459）——共用預設會讓瀏覽器在後端
 * 還在跑的時候就 abort，等於後端修好了租戶也看不到成功。
 *
 * 90s 是預算階梯的最外層，必須大於平台 client 的 75s（RESET_CONFIG_TIMEOUT）、
 * 而後者必須大於 operator 自己的 65s 硬上限（RESET_HARD_CAP_SECONDS）。三層由
 * scripts/audit/repo-lint.py 的 check_reset_budget_ladder 驗算。
 * 上限訂在 90s 而非更大，是為了繞開 Cloudflare 對 origin 回應約 100s 就回 524 的
 * 行為（未實證，但不必為此賭）。移除 rollout 等待後典型只花 5–15s。
 */
const RESET_REQUEST_TIMEOUT_MS = 90000;

/**
 * @typedef {Object} TemplateData
 * @property {number} id - Template ID
 * @property {string} name - Template name
 * @property {string} slug - URL-friendly identifier
 * @property {string} description - Short description
 * @property {string} category - Template category
 * @property {string[]} tags - Template tags
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
        console.error("Failed to fetch config, using defaults:", err);
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
    /** @type {string[]|null} 有公開範本的分類代碼（null＝後端未提供，頁籤全列） */
    templateCategories: null,
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

    /** @type {Array} */
    odooAddons: [],

    /**
     * Fetch available subscription plans for a template.
     *
     * Returns ApiResponse<Array> so callers can surface backend errors
     * instead of silently rendering "no plans" — same rationale as the
     * Smart Home variant in workspace_service.fetchSmartHomePlans. The
     * backend hard-fails on missing/inactive/empty templates with a
     * specific message; we want admins to see that message, not a blank
     * list that looks like a bug.
     *
     * @param {number} templateId - Template ID
     * @returns {Promise<ApiResponse<Array>>}
     */
    async fetchPlans(templateId) {
        try {
            const result = await jsonRpc(`/api/cloud-services/templates/${templateId}/plans`, {});
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
     * Change subscription plan for a service
     * @param {number} serviceId - Service ID
     * @param {number} planId - New plan ID
     * @returns {Promise<{success: boolean, data?: Object, error?: string}>}
     */
    async changePlan(serviceId, planId, { dryRun = false } = {}) {
        // AUDITFIX（#1677 A1）：dryRun＝只問「換得起嗎」（首期應付／錢包／差額），後端什麼都不寫。
        if (!dryRun) {
            this.operationLoading.changePlan = true;
        }
        try {
            const result = await jsonRpc(`/api/cloud-services/services/${serviceId}/change-plan`, {
                plan_id: planId,
                ...(dryRun ? { dry_run: true } : {}),
            });
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error, data: result.data };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            if (!dryRun) {
                this.operationLoading.changePlan = false;
            }
        }
    },

    /**
     * Skip trial period and start paid billing immediately.
     * @param {number} serviceId
     * @returns {Promise<{success: boolean, data?: Object, error?: string}>}
     */
    async skipTrial(serviceId) {
        this.operationLoading.skipTrial = true;
        try {
            const result = await jsonRpc(`/api/cloud-services/services/${serviceId}/skip-trial`, {});
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.skipTrial = false;
        }
    },

    /**
     * Get the upcoming-invoice breakdown for a skip-trial confirmation.
     *
     * Side-effect-free counterpart to `skipTrial`: returns untaxed / tax /
     * total + wallet sufficiency so the confirm modal can render the real
     * amount before the user commits.
     *
     * @param {number} serviceId
     * @returns {Promise<{success: boolean, data?: Object, error?: string}>}
     *   data has keys: untaxed, tax_rate, tax_amount, total, currency,
     *   wallet_balance, sufficient, shortage, billing_cycle_start,
     *   billing_cycle_end, payable_via_woow, pay_blocked_reason.
     */
    async getSkipTrialEstimate(serviceId) {
        try {
            const result = await jsonRpc(
                `/api/cloud-services/services/${serviceId}/skip-trial-estimate`,
                {},
            );
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        }
    },

    /**
     * #1611：續訂前的確認內容（可選方案與各自的首期金額、錢包餘額、付款能力）。不寫入。
     * @param {number} serviceId
     * @returns {Promise<{success: boolean, data?: Object, error?: string, code?: string}>}
     */
    async getRenewEstimate(serviceId) {
        try {
            const result = await jsonRpc(
                `/api/cloud-services/services/${serviceId}/renew-estimate`,
                {},
            );
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error, code: result.code, data: result.data };
        } catch (err) {
            return { success: false, error: err.message };
        }
    },

    /**
     * #1611：自助續訂（錢包付款 → 新訂閱綁回服務；停權中的服務隨後自動恢復）。
     * @param {number} serviceId
     * @param {number} planId
     * @returns {Promise<{success: boolean, data?: Object, error?: string, code?: string}>}
     */
    async renew(serviceId, planId) {
        this.operationLoading.renew = true;
        try {
            const result = await jsonRpc(`/api/cloud-services/services/${serviceId}/renew`, {
                plan_id: planId,
            });
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error, code: result.code, data: result.data };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.renew = false;
        }
    },

    /**
     * #1611：目前使用者（owner／admin）可以續訂的服務，帳務頁「待續訂」清單用。
     * @returns {Promise<{success: boolean, data?: Object[], error?: string}>}
     */
    async fetchRenewals() {
        try {
            const result = await jsonRpc("/api/cloud-services/renewals", {});
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        }
    },

    async fetchOdooAddons(templateId = null, category = null) {
        this.operationLoading.fetchOdooAddons = true;
        try {
            const params = {};
            if (templateId) params.template_id = templateId;
            if (category) params.category = category;
            const result = await jsonRpc("/api/cloud/odoo-addons", params);
            if (result.success) {
                this.odooAddons = result.data;
            }
        } catch (err) {
            console.warn("Failed to fetch Odoo addons:", err);
        } finally {
            this.operationLoading.fetchOdooAddons = false;
        }
    },

    /**
     * Fetch all available templates
     * @param {string|null} category - Filter by category
     * @param {string|null} search - Search in name/description/tags
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
                // X-LC-001-5：有公開範本的分類（舊後端沒有這個欄位 ⇒ null ＝ 全部頁籤照舊顯示）。
                this.templateCategories = Array.isArray(result.categories) ? result.categories : null;
            } else {
                this.error = result.error || _t("Failed to fetch templates");
            }
        } catch (err) {
            this.error = err.message || _t("Network error");
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
                this.error = result.error || _t("Failed to fetch services");
            }
        } catch (err) {
            this.error = err.message || _t("Network error");
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
                plan_id: payload.plan_id,
            };
            if (payload.odoo_addon_ids) {
                params.odoo_addon_ids = payload.odoo_addon_ids;
            }
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

    async fetchDocumentation(workspaceId, serviceId) {
        return jsonRpc(`/api/workspaces/${workspaceId}/services/${serviceId}/documentation`, {});
    },

    async fetchLogs(workspaceId, serviceId, options = {}) {
        return jsonRpc(`/api/workspaces/${workspaceId}/services/${serviceId}/logs`, {
            tail_lines: 500,
            previous: false,
            ...options,
        });
    },

    async resetAdminPassword(workspaceId, serviceId) {
        this.operationLoading.resetAdminPassword = true;
        try {
            const result = await jsonRpc(
                `/api/workspaces/${workspaceId}/services/${serviceId}/reset-admin-password`,
                {},
                // #461：共用的 30s 會在後端還在換憑證時就 abort（#459 實測成功案例
                // 也要 49.9s／76.7s）。這一層必須是階梯裡最寬的。
                RESET_REQUEST_TIMEOUT_MS
            );
            return result.success
                ? { success: true, data: result.data }
                : { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.resetAdminPassword = false;
        }
    },

    /**
     * 租戶自設 admin 帳號／密碼（template.admin_username_editable 的服務）。
     * `password` 為 null ＝ 平台隨機產生（回應帶一次性 admin_password）；
     * 省略 `username` ＝ 不改帳號。後端走與 Reset 相同的 helm upgrade ＋ restart，
     * 所以沿用同一個 loading 旗標與最寬的逾時階梯。
     *
     * @param {number} workspaceId
     * @param {number} serviceId
     * @param {{username?: string, password: string|null}} credentials
     */
    async setAdminCredentials(workspaceId, serviceId, credentials) {
        this.operationLoading.resetAdminPassword = true;
        try {
            const result = await jsonRpc(
                `/api/workspaces/${workspaceId}/services/${serviceId}/admin-credentials`,
                credentials,
                RESET_REQUEST_TIMEOUT_MS
            );
            return result.success
                ? { success: true, data: result.data }
                : { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.resetAdminPassword = false;
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
    async updateService(workspaceId, serviceId, values, version = null, clearKeys = null) {
        this.operationLoading.updateService = true;
        try {
            const params = {
                action: "update",
                values: values,
            };
            if (version) {
                params.version = version;
            }
            // #1455：要清除的憑證欄位（只在有勾選時送，舊後端不認得也不會帶到）。
            if (clearKeys && clearKeys.length) {
                params.clear_keys = clearKeys;
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
     * Update Odoo addon selection for a service
     * @param {number} workspaceId - Workspace ID
     * @param {number} serviceId - Service ID
     * @param {number[]} odooAddonIds - Selected addon IDs
     * @returns {Promise<{success: boolean, data?: ServiceData, error?: string}>}
     */
    /**
     * #1432：重新啟動服務（rollout restart 所有 Deployment／StatefulSet）。
     * 後端只「下達」不等待，回來時服務已轉 `upgrading`，由輪詢轉回 running。
     */
    async restartService(workspaceId, serviceId) {
        this.operationLoading.restartService = true;
        try {
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/services/${serviceId}`, {
                action: "restart",
            });
            if (result.success) {
                const index = this.services.findIndex(s => s.id === serviceId);
                if (index !== -1) {
                    this.services[index] = { ...this.services[index], ...result.data };
                }
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error };
        } catch (err) {
            return { success: false, error: err.message };
        } finally {
            this.operationLoading.restartService = false;
        }
    },

    async updateServiceAddons(workspaceId, serviceId, odooAddonIds) {
        this.operationLoading.updateServiceAddons = true;
        try {
            const params = {
                action: "update",
                odoo_addon_ids: odooAddonIds,
            };
            const result = await jsonRpc(`/api/workspaces/${workspaceId}/services/${serviceId}`, params);
            if (result.success) {
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
            this.operationLoading.updateServiceAddons = false;
        }
    },

    /**
     * Check if removed addons can be safely deselected
     * @param {number} serviceId - Service ID
     * @param {number[]} removedAddonIds - Addon IDs being removed
     * @returns {Promise<{success: boolean, modules?: Object, error?: string}>}
     */
    async checkAddonRemoval(serviceId, removedAddonIds) {
        try {
            return await jsonRpc(`/api/cloud-services/services/${serviceId}/check-addon-removal`, {
                removed_addon_ids: removedAddonIds,
            }, 180000);
        } catch (err) {
            return { success: false, error: err.message };
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
                // INFRA-FIX-018-1：historyMax＝operator 每個 release 保留幾筆 revision（舊後端沒有 ⇒ null）。
                return { success: true, data: result.data, historyMax: result.history_max ?? null };
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
