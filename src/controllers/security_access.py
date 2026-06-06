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
