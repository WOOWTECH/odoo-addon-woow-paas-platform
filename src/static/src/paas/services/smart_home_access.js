/** @odoo-module **/

import { jsonRpc } from "./rpc";

/**
 * Smart Home Access Control API client（#768，對應 `controllers/smart_home.py`
 * 的 `api_smarthome_route_access_config` / `api_smarthome_access_policies`）。
 *
 * 形狀對齊 **SA**（`services/security_access.js`）而不是 CS：
 *
 *   - SA / SH：錨點是 **route**（一台底下 N 條 route＝N 個 Access app），所以
 *     config 是 route-pinned（URL 帶 route_id），policy 那支是父層 pinned ＋
 *     `action` 分派，且**只有 create 帶 `route_id`**（其餘 action 由後端從
 *     policy_id 反解錨點，client 給不了也不該給）。
 *   - CS：一台服務就是唯一錨點，所以它的 config 路徑只到 service。
 *
 * 錯誤處理走 CS（`services/cloud_service_access.js`）那一版的形狀：每支自己
 * try/catch，一律回 `{ success, data } | { success, error }` 信封，網路層例外也
 * 折成 `success: false` ——呼叫端只有一條錯誤路徑要接。
 *
 * 刻意**不併進 `services/workspace_service.js`**：那支是 workspace／SH 主線
 * （建立、provision、換方案…），Access 是另一個子系統（policy／CF 同步），理由
 * 與 #746 把 CS 的 access 拆出來時逐字相同。
 */
export const smartHomeAccessService = {
  /**
   * 單一 route 的保護開關、登入方式與 session 長度。
   *
   * 後端白名單只收 is_protected / login_google / login_email_otp /
   * session_duration，回傳整包 `route.to_dict()`（含伺服器自己擁有的
   * `access_unresolved_idp_types`），所以呼叫端拿回應覆蓋該條 route 的本地狀態
   * 即可，不需要再拉一次 SH detail。
   */
  async updateRouteAccess(workspaceId, smartHomeId, routeId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/smarthomes/${smartHomeId}` +
          `/routes/${routeId}/access-config`,
        vals,
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message || "Network error" };
    }
  },

  /**
   * 這台 Smart Home 底下的**全部** policy（跨 route 的平面清單，每筆帶
   * `smart_home_route_id`）。呼叫端要自己依錨點分桶——後端刻意不分，因為
   * policy 端點是父層 pinned 的（見檔頭）。
   */
  async fetchPolicies(workspaceId, smartHomeId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/smarthomes/${smartHomeId}/access-policies`,
        { action: "list" },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message || "Network error" };
    }
  },

  // 建立是**唯一**帶錨點的 action：policy 要掛在哪條 route 只有 client 知道。
  // 後端會重驗這個 route 屬於已 gate 過的 SH（`_sh_route()` 用 search 帶
  // smart_home_id 條件），跨 SH 的 route_id 會被擋成 "Route not found"，不會被
  // 寫進別人的 route。
  async createPolicy(workspaceId, smartHomeId, routeId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/smarthomes/${smartHomeId}/access-policies`,
        { action: "create", route_id: routeId, ...vals },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message || "Network error" };
    }
  },

  async updatePolicy(workspaceId, smartHomeId, policyId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/smarthomes/${smartHomeId}/access-policies`,
        { action: "update", policy_id: policyId, ...vals },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message || "Network error" };
    }
  },

  async deletePolicy(workspaceId, smartHomeId, policyId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/smarthomes/${smartHomeId}/access-policies`,
        { action: "delete", policy_id: policyId },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message || "Network error" };
    }
  },

  // 重寫 precedence。後端只回 `{reordered: [...]}`（不是整份清單），而且會對
  // **每一條被動到的 route** 各推一次 Cloudflare——呼叫端要自己再 fetchPolicies
  // 一次確認伺服器順序。
  async reorderPolicies(workspaceId, smartHomeId, policyIds) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/smarthomes/${smartHomeId}/access-policies`,
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
