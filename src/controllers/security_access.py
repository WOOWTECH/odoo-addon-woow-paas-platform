"""Security Access API controller.

JSON-RPC endpoints for Security Access CRUD within a workspace context.
Mirrors controllers/smart_home.py gating: _get_workspace_or_error for
visibility, owner/admin role check for mutations, NO .sudo() on
list/get paths (orm-vs-endpoint-acl-test-coverage pattern).
"""
from __future__ import annotations

import logging
from typing import Any

from odoo.http import request, route, Controller

from ..models.workspace_access import ROLE_OWNER, ROLE_ADMIN

_logger = logging.getLogger(__name__)


class SecurityAccessController(Controller):

    # ==================== Collection ====================

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses",
        auth="user", methods=["POST"], type="json",
    )
    def api_security_accesses(
        self,
        workspace_id: int,
        action: str = "list",
        name: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}

        if action == "list":
            return self._list_security_accesses(workspace)
        elif action == "create":
            access = workspace.check_user_access(request.env.user)
            if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {"success": False, "error": "Insufficient permissions"}
            return self._create_security_access(
                workspace, name,
                kwargs.get("plan_id"), kwargs.get("template_id"),
            )
        return {"success": False, "error": f"Unknown action: {action}"}

    # ==================== Detail ====================

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>",
        auth="user", methods=["POST"], type="json",
    )
    def api_security_access_detail(
        self,
        workspace_id: int,
        sa_id: int,
        action: str = "get",
        **kwargs: Any,
    ) -> dict[str, Any]:
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": "Security access not found"}

        if action == "get":
            return {"success": True, "data": sa.to_dict()}
        elif action == "refresh_status":
            try:
                sa.action_refresh_status()
            except Exception as e:
                return {"success": False, "error": str(e)}
            return {"success": True, "data": sa.to_dict()}

        # mutations below require owner/admin
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}

        if action == "update":
            vals = {}
            if kwargs.get("name"):
                vals["name"] = kwargs["name"]
            if vals:
                sa.write(vals)
            return {"success": True, "data": sa.to_dict()}
        elif action == "provision":
            try:
                sa._check_operation_allowed(operation='write')
                sa.action_provision()
            except Exception as e:
                return {"success": False, "error": str(e)}
            return {"success": True, "data": sa.to_dict()}
        elif action == "delete":
            try:
                sa._check_operation_allowed(operation='write')
                sa.action_delete()
            except Exception as e:
                return {"success": False, "error": str(e)}
            return {"success": True,
                    "data": {"message": "Security access deleted"}}
        return {"success": False, "error": f"Unknown action: {action}"}

    # ==================== Routes ====================

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>/routes",
        auth="user", methods=["POST"], type="json",
    )
    def api_security_access_routes(
        self,
        workspace_id: int,
        sa_id: int,
        action: str = "list",
        **kwargs: Any,
    ) -> dict[str, Any]:
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": "Security access not found"}

        if action == "list":
            return {"success": True,
                    "data": [r.to_dict() for r in sa.route_ids]}

        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}
        try:
            sa._check_operation_allowed(operation='write')
        except Exception as e:
            return {"success": False, "error": str(e)}

        if action == "create":
            return self._create_route(
                sa,
                kwargs.get("subdomain_prefix"),
                kwargs.get("service_url"),
                kwargs.get("http_host_header", True),
            )
        elif action == "update":
            return self._update_route(sa, kwargs.get("route_id"), kwargs)
        elif action == "delete":
            return self._delete_route(sa, kwargs.get("route_id"))
        return {"success": False, "error": f"Unknown action: {action}"}

    # ==================== Availability (platform-level) ====================

    @route(
        "/api/security-access/routes/check",
        auth="user", methods=["POST"], type="json",
    )
    def api_route_check(self, prefix: str | None = None,
                        **kwargs: Any) -> dict[str, Any]:
        if not prefix:
            return {"success": False, "error": "prefix is required"}
        return self._check_route_prefix(prefix)

    # ==================== Templates / Plans (platform-level) ============

    @route(
        "/api/security-access/templates",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_templates(self, **kwargs: Any) -> dict[str, Any]:
        """Catalog data — sudo justified (mirrors smart_home templates)."""
        Template = request.env[
            "woow_paas_platform.security_access_template"].sudo()
        templates = Template.search([("is_active", "=", True)])
        return {"success": True, "data": [
            {"id": t.id, "name": t.name, "slug": t.slug,
             "description": t.description}
            for t in templates
        ]}

    @route(
        "/api/security-access/templates/<int:template_id>/plans",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_template_plans(self, template_id: int,
                              **kwargs: Any) -> dict[str, Any]:
        # sudo: catalog data, portal users lack plan ACL (mirrors smart_home)
        Template = request.env[
            "woow_paas_platform.security_access_template"].sudo()
        template = Template.browse(template_id)
        if not template.exists() or not template.is_active:
            return {"success": False,
                    "error": "Security Access template not found or inactive"}
        if not template.subscription_plan_ids:
            return {"success": False,
                    "error": "Template has no subscription plans configured"}
        return {"success": True, "data": [
            {
                "id": p.id,
                "name": p.name,
                "price": p.sh_plan_price,
                "duration": p.sh_duration,
                "unit": p.sh_unit,
                "has_trial": p.sh_trial,
                "trial_duration": p.sh_trial_duration if p.sh_trial else 0,
                "trial_unit": p.sh_trial_unit if p.sh_trial else None,
                "max_routes": p.sa_max_routes,
                "max_access_policies": p.sa_max_access_policies,
                "max_custom_domains": p.sa_max_custom_domains,
                "max_email_rules": p.sa_max_email_rules,
            }
            for p in template.subscription_plan_ids
        ]}

    # ==================== Subscription trio ====================

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>/skip-trial-estimate",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_skip_trial_estimate(self, workspace_id: int, sa_id: int,
                                   **kwargs: Any) -> dict[str, Any]:
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": "Security access not found"}
        try:
            return {"success": True, "data": sa._skip_trial_estimate()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>/skip-trial",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_skip_trial(self, workspace_id: int, sa_id: int,
                          **kwargs: Any) -> dict[str, Any]:
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": "Security access not found"}
        try:
            invoice_info = sa._skip_trial()
        except Exception as e:
            return {"success": False, "error": str(e)}
        return {"success": True, "data": {
            "invoice": invoice_info,
            "subscription": sa._get_subscription_info(),
        }}

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>/change-plan",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_change_plan(self, workspace_id: int, sa_id: int,
                           plan_id: int | None = None,
                           **kwargs: Any) -> dict[str, Any]:
        """完全鏡像 smart_home.api_smarthome_change_plan 的流程與 sudo 理由：
        refund 計算 → 退 wallet → close 舊訂閱 → 開新訂閱。"""
        if not plan_id:
            return {"success": False, "error": "plan_id is required"}
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": "Security access not found"}
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}

        # sudo: portal users lack ACL on sh.subscription.plan; catalog data,
        # and the workspace+role check above already gates who can change a
        # security access, so this widening is narrow and intentional
        # (mirrors smart_home.api_smarthome_change_plan).
        Plan = request.env["sh.subscription.plan"].sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {"success": False, "error": "Plan not found"}
        # Plan must belong to THIS SA's template — symmetric with the _create
        # path's `plan not in template.subscription_plan_ids` gate. The role
        # gate above only checks who may change the plan, not whether the plan
        # belongs here; without this an owner could switch to another SA
        # template's (or an orphan) plan, producing a subscription/invoice line
        # inconsistent with the instance's template. Skip only when the SA has
        # no template (admin/legacy records created outside the BFF).
        if sa.template_id and plan not in sa.template_id.subscription_plan_ids:
            return {"success": False,
                    "error": "Selected plan is not available for this template"}

        # sudo: workspace owners typically lack ACL on
        # sh.subscription.subscription; the workspace+role check above already
        # gates who drives change_plan, so this narrow widening mirrors
        # smart_home.api_smarthome_change_plan's cascade.
        old_sub = sa.sudo().subscription_id
        if not old_sub or old_sub.state != "in_progress":
            return {"success": False, "error": "No active subscription"}
        if not old_sub.sh_trial:
            if not old_sub.sh_end_date or not old_sub.sh_start_date:
                return {"success": False,
                        "error": "Subscription dates are incomplete"}

        refund = old_sub._compute_unused_refund()
        partner = old_sub.sh_partner_id
        if refund > 0:
            # sudo: crediting wallet points writes loyalty.card.points which
            # portal users can't write; same narrow widening as the role-gated
            # cascade above (mirrors smart_home.api_smarthome_change_plan).
            partner.sudo().add_wallet_points(
                amount=refund,
                description="Plan change refund: %s"
                            % old_sub.sh_subscription_plan_id.name,
            )
            partner.invalidate_recordset(['wallet_balance'])
        # sudo: closing the old subscription needs sh.subscription.subscription
        # write ACL workspace owners lack; continuation of the gated cascade.
        old_sub.sudo().write({
            "state": "close",
            "sh_reason": "Plan changed to: %s" % plan.name,
        })
        sa._create_and_confirm_subscription(partner, plan)
        return {"success": True, "data": {
            "refund_amount": refund,
            "new_plan": plan.name,
            "subscription": sa._get_subscription_info(),
        }}

    # ==================== Private Helpers ====================

    def _get_workspace_or_error(self, workspace_id: int):
        user = request.env.user
        WorkspaceAccess = request.env["woow_paas_platform.workspace_access"]
        access = WorkspaceAccess.search([
            ("user_id", "=", user.id),
            ("workspace_id", "=", workspace_id),
            ("workspace_id.state", "=", "active"),
        ], limit=1)
        if not access:
            return None
        return access.workspace_id

    def _get_sa_or_error(self, sa_id: int, workspace):
        SecurityAccess = request.env["woow_paas_platform.security_access"]
        sa = SecurityAccess.browse(sa_id).exists()
        if not sa or sa.workspace_id.id != workspace.id:
            return None
        return sa

    def _list_security_accesses(self, workspace) -> dict[str, Any]:
        SecurityAccess = request.env["woow_paas_platform.security_access"]
        records = SecurityAccess.search([
            ("workspace_id", "=", workspace.id),
        ])
        data = [r.to_dict() for r in records]
        return {"success": True, "data": data, "count": len(data)}

    def _create_security_access(
        self, workspace, name: str | None,
        plan_id: int | None = None,
        template_id: int | None = None,
    ) -> dict[str, Any]:
        """Mirrors smart_home._create_smarthome validation order."""
        if not name:
            return {"success": False, "error": "Name is required"}
        if not template_id:
            return {"success": False,
                    "error": "Security Access template is required"}
        if not plan_id:
            return {"success": False,
                    "error": "Subscription plan is required"}

        Template = request.env[
            "woow_paas_platform.security_access_template"].sudo()
        template = Template.browse(template_id)
        if not template.exists() or not template.is_active:
            return {"success": False,
                    "error": "Security Access template not found or inactive"}
        if not template.subscription_plan_ids:
            return {"success": False,
                    "error": "Template has no subscription plans configured"}

        # sudo: catalog data, portal users lack plan ACL (mirrors smart_home)
        Plan = request.env["sh.subscription.plan"].sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {"success": False, "error": "Subscription plan not found"}
        if plan not in template.subscription_plan_ids:
            return {"success": False,
                    "error": "Selected plan is not available for this template"}

        SecurityAccess = request.env["woow_paas_platform.security_access"]
        try:
            sa = SecurityAccess.create({
                "name": name,
                "workspace_id": workspace.id,
                "template_id": template.id,
            })
            partner = workspace.owner_id.partner_id
            _logger.info(
                "[SA] create sa=%s workspace=%s partner=%s plan=%s",
                sa.id, workspace.id, partner.id, plan.id,
            )
            sa._create_and_confirm_subscription(partner, plan)
            return {"success": True, "data": sa.to_dict()}
        except Exception as e:
            _logger.exception("[SA] Failed to create security access: %s", e)
            return {"success": False,
                    "error": "Failed to create security access. Please try again."}

    def _check_route_prefix(self, prefix: str) -> dict[str, Any]:
        """Two-layer availability: Odoo DB first, then Cloudflare DNS."""
        Route = request.env[
            "woow_paas_platform.security_access_route"].sudo()
        # sudo justified: existence check across ALL workspaces is the very
        # point of a flat-namespace availability check; returns only a
        # boolean, no record data crosses the tenant boundary.
        db_taken = bool(Route.search_count(
            [("subdomain_prefix", "=", prefix)]))
        if db_taken:
            return {"success": True,
                    "data": {"prefix": prefix, "available": False,
                             "reason": "already used by another route"}}
        try:
            from ..services.paas_operator import get_paas_operator_client
            client = get_paas_operator_client(request.env)
            if client:
                result = client.check_route_availability(prefix)
                # Operator returns status: free / taken / unknown. unknown
                # means the CF API errored (no longer silently "available").
                if result.get("status") == "unknown":
                    return {"success": True,
                            "data": {"prefix": prefix, "available": False,
                                     "reason": "Could not confirm availability "
                                               "(Cloudflare check unavailable); "
                                               "try again."}}
                if not result.get("available", True):
                    return {"success": True,
                            "data": {"prefix": prefix, "available": False,
                                     "reason": "DNS record already exists"}}
        except Exception as e:
            _logger.warning("[SA] CF availability check failed: %s", e)
            # fail-open to DB-only result: CF check is best-effort feedback;
            # the DB UNIQUE constraint + CF-side create error still protect
            # the actual create path.
        return {"success": True,
                "data": {"prefix": prefix, "available": True}}

    def _create_route(self, sa, subdomain_prefix, service_url,
                      http_host_header) -> dict[str, Any]:
        if not subdomain_prefix:
            return {"success": False, "error": "subdomain_prefix is required"}

        # two-layer quota: constraint re-checks at ORM layer
        sub = sa.sudo().subscription_id
        if sub and sub.sh_subscription_plan_id:
            quota = sub.sh_subscription_plan_id.sa_max_routes
            count = len(sa.route_ids.filtered(lambda r: not r.is_primary))
            if count >= quota:
                return {"success": False,
                        "error": "Route quota exceeded: plan allows %d "
                                 "custom route(s)" % quota}

        check = self._check_route_prefix(subdomain_prefix)
        if not check["data"]["available"]:
            return {"success": False,
                    "error": "Subdomain prefix is not available: %s"
                             % check["data"].get("reason", "taken")}

        Route = request.env["woow_paas_platform.security_access_route"]
        vals = {
            "security_access_id": sa.id,
            "subdomain_prefix": subdomain_prefix,
            "http_host_header": bool(http_host_header),
        }
        if service_url:
            vals["service_url"] = service_url
        try:
            route_rec = Route.create(vals)
            return {"success": True, "data": route_rec.to_dict()}
        except Exception as e:
            _logger.exception("[SA] Failed to create route: %s", e)
            return {"success": False, "error": str(e)}

    def _update_route(self, sa, route_id, kwargs) -> dict[str, Any]:
        route_rec = sa.route_ids.filtered(lambda r: r.id == route_id)
        if not route_rec:
            return {"success": False, "error": "Route not found"}
        vals = {}
        if "service_url" in kwargs and kwargs["service_url"]:
            vals["service_url"] = kwargs["service_url"]
        if "http_host_header" in kwargs:
            vals["http_host_header"] = bool(kwargs["http_host_header"])
        if "is_protected" in kwargs:
            vals["is_protected"] = bool(kwargs["is_protected"])
        try:
            if vals:
                route_rec.write(vals)
            return {"success": True, "data": route_rec.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _delete_route(self, sa, route_id) -> dict[str, Any]:
        route_rec = sa.route_ids.filtered(lambda r: r.id == route_id)
        if not route_rec:
            return {"success": False, "error": "Route not found"}
        try:
            route_rec.unlink()
            return {"success": True, "data": {"message": "Route deleted"}}
        except Exception as e:
            return {"success": False, "error": str(e)}
