/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { jsonRpc } from "./rpc";

/**
 * Security Access API client.
 *
 * Thin wrapper over the BFF endpoints in
 * `src/controllers/security_access.py`. Every method returns a normalized
 * `{ success, data } | { success, error }` envelope so callers never touch
 * the raw JSON-RPC result. Mirrors the conventions used by
 * `services/support_service.js` / `services/ai_service.js` (jsonRpc helper
 * imported from `./rpc`).
 */
export const securityAccessService = {
  async fetchList(workspaceId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses`,
        { action: "list" },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error || _t("Failed to fetch") };
    } catch (err) {
      return { success: false, error: err.message || _t("Network error") };
    }
  },

  async create(workspaceId, name, planId, templateId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses`,
        { action: "create", name, plan_id: planId, template_id: templateId },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async fetchDetail(workspaceId, saId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}`,
        { action: "get" },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async update(workspaceId, saId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}`,
        { action: "update", ...vals },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async provision(workspaceId, saId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}`,
        { action: "provision" },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async remove(workspaceId, saId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}`,
        { action: "delete" },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async createRoute(workspaceId, saId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/routes`,
        { action: "create", ...vals },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  // #1196 第一層：後端的 _update_route 一直都接受 service_url /
  // http_host_header，只是前端沒有對應方法，逼得使用者要改後端位址時只能
  // 刪掉重建——那會放掉 subdomain 的保留、斷線、而且 route id 會換掉。
  async updateRoute(workspaceId, saId, routeId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/routes`,
        { action: "update", route_id: routeId, ...vals },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  // #1196 第二層：取得 tunnel token 讓使用者自行接線（Cloudflared Web GUI
  // addon / docker run / 換一台機器接手）。後端已把權限收斂到 owner/admin，
  // 見 controllers/security_access.py::api_security_access_tunnel_token。
  async getTunnelToken(workspaceId, saId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/tunnel-token`,
        {},
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  // #1234：每條 route 的對外健康狀態。後端從公開網路探測、行程內快取 30 秒；
  // force=true 略過快取（使用者按「Check now」、或剛改完 route）。
  async fetchRouteHealth(workspaceId, saId, force = false) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/routes`,
        { action: "health", force },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async deleteRoute(workspaceId, saId, routeId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/routes`,
        { action: "delete", route_id: routeId },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  // NOTE: the legacy SA-level updateAccessConfig was removed in P2.5 — Access
  // config is per-route now (updateRouteAccess below). The SA-level bridge
  // endpoint still exists server-side for in-flight clients but no UI calls it.

  // P2.5 per-route Access config. Each protected Application Route is its own
  // Cloudflare Access app, so config (is_protected / login methods / session
  // duration) is route-scoped. Hits the route-pinned BFF endpoint; the backend
  // whitelists is_protected/login_google/login_email_otp/session_duration and
  // returns the updated route.to_dict() (server-owned access_app_id /
  // access_unresolved_idp_types are never client-settable).
  async updateRouteAccess(workspaceId, saId, routeId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/routes/${routeId}/access-config`,
        vals,
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  // P2.5: a policy binds to a route. routeId pins it to one Application Route
  // (the backend re-validates the route belongs to the gated SA — a cross-SA
  // route_id is rejected, never written to another SA's route).
  async createPolicy(workspaceId, saId, routeId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/access-policies`,
        { action: "create", route_id: routeId, ...vals },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async updatePolicy(workspaceId, saId, policyId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/access-policies`,
        { action: "update", policy_id: policyId, ...vals },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async deletePolicy(workspaceId, saId, policyId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/access-policies`,
        { action: "delete", policy_id: policyId },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  // P-rule: rewrite policy precedence to the given id order. The backend pins
  // each id to the gated SA (a cross-SA id is silently skipped, never written).
  async reorderPolicies(workspaceId, saId, policyIds) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/access-policies`,
        { action: "reorder", policy_ids: policyIds },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async checkRoutePrefix(prefix) {
    try {
      const result = await jsonRpc(
        "/api/security-access/routes/check", { prefix },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async fetchTemplates() {
    try {
      const result = await jsonRpc("/api/security-access/templates", {});
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async fetchPlans(templateId) {
    try {
      const result = await jsonRpc(
        `/api/security-access/templates/${templateId}/plans`, {},
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async getSkipTrialEstimate(workspaceId, saId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/skip-trial-estimate`,
        {},
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async skipTrial(workspaceId, saId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/skip-trial`,
        {},
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  // Client binding for the change-plan BFF endpoint
  // (`api_sa_change_plan` in controllers/security_access.py). The backend
  // refund→wallet→close-old→open-new cascade is complete and covered by
  // backend tests; this method exists so the endpoint is not orphaned at the
  // service boundary (mirrors cloud_service.changePlan + this service's own
  // skipTrial). NOTE: no SA detail-page UI triggers this yet — wiring a plan
  // picker / refund-preview modal is a deliberate follow-up (the tenant-facing
  // self-serve plan-change UX is not in P1 scope; until then change-plan is
  // driven from backend/admin only).
  async changePlan(workspaceId, saId, planId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/change-plan`,
        { plan_id: planId },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },
};
