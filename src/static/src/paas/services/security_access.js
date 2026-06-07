/** @odoo-module **/

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
        : { success: false, error: result.error || "Failed to fetch" };
    } catch (err) {
      return { success: false, error: err.message || "Network error" };
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

  async updateAccessConfig(workspaceId, saId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/access-config`,
        vals,
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async createPolicy(workspaceId, saId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/access-policies`,
        { action: "create", ...vals },
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
