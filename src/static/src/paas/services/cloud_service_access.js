/** @odoo-module **/

import { jsonRpc } from "./rpc";

/**
 * Cloud Service Access Control API client（#746，對應 #745 的 T3 端點）。
 *
 * 形狀刻意與 `services/security_access.js` 的 access 那組逐字對齊（同樣的
 * `{ success, data } | { success, error }` 信封、同樣的 action 分派），差別只在
 * **錨點**：SA 的 Access app 掛在 route 上（route-pinned URL），CS 自己就是錨點
 * （一台服務一個 subdomain 一個 app，拍板 5 的整站粒度），所以路徑只到 service。
 *
 * 刻意**不併進 `services/cloud_service.js`**：那支已經 767 行、涵蓋部署／升級／
 * 憑證等主線，Access 是另一個子系統（policy／CF 同步），混在一起兩邊都難讀。
 *
 * 後端：`src/controllers/paas.py` 的 `api_service_access_config` /
 * `api_service_access_policies`。
 */
export const cloudServiceAccessService = {
  /**
   * 保護開關與登入方式。後端白名單只收 is_protected / login_google /
   * login_email_otp / session_duration，回傳整包 `_access_to_dict()`
   * （含伺服器自己回填的 access_unresolved_idp_types），所以呼叫端拿回應覆蓋
   * 本地狀態即可，不需要再拉一次 detail。
   */
  async updateConfig(workspaceId, serviceId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/services/${serviceId}/access-config`,
        vals,
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message || "Network error" };
    }
  },

  async fetchPolicies(workspaceId, serviceId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/services/${serviceId}/access-policies`,
        { action: "list" },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message || "Network error" };
    }
  },

  // 錨點（cloud_service_id）由後端從已 gate 過的 service 決定，不從 client 收
  // ——所以這裡不像 SA 的 createPolicy 那樣要多帶一個 route_id。
  async createPolicy(workspaceId, serviceId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/services/${serviceId}/access-policies`,
        { action: "create", ...vals },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message || "Network error" };
    }
  },

  async updatePolicy(workspaceId, serviceId, policyId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/services/${serviceId}/access-policies`,
        { action: "update", policy_id: policyId, ...vals },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message || "Network error" };
    }
  },

  async deletePolicy(workspaceId, serviceId, policyId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/services/${serviceId}/access-policies`,
        { action: "delete", policy_id: policyId },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message || "Network error" };
    }
  },

  // 重寫 precedence。後端只回 `{reordered: [...]}`（不是整份清單），呼叫端要自己
  // 再 fetchPolicies 一次確認伺服器順序。
  async reorderPolicies(workspaceId, serviceId, policyIds) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/services/${serviceId}/access-policies`,
        { action: "reorder", policy_ids: policyIds },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message || "Network error" };
    }
  },
};
