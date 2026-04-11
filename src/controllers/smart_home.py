"""Smart Home API controller.

Provides JSON-RPC endpoints for Smart Home CRUD operations
within a workspace context.
"""
from __future__ import annotations

import logging
from typing import Any

from odoo.http import request, route, Controller

_logger = logging.getLogger(__name__)


class SmartHomeController(Controller):

    # ==================== Smart Home API ====================

    @route(
        "/api/workspaces/<int:workspace_id>/smarthomes",
        auth="user", methods=["POST"], type="json",
    )
    def api_smarthomes(
        self,
        workspace_id: int,
        action: str = "list",
        name: str | None = None,
        ha_port: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Handle smart home collection operations.

        Args:
            workspace_id: Workspace ID from URL.
            action: 'list' or 'create'.
            name: Smart home name (required for create).
            ha_port: Home Assistant port (optional, default 8123).
        """
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False, "error": "Workspace not found or access denied"}

        if action == "list":
            return self._list_smarthomes(workspace)
        elif action == "create":
            plan_id = kwargs.get("plan_id")
            return self._create_smarthome(workspace, name, ha_port, plan_id)
        else:
            return {"success": False, "error": f"Unknown action: {action}"}

    @route(
        "/api/workspaces/<int:workspace_id>/smarthomes/<int:smarthome_id>",
        auth="user", methods=["POST"], type="json",
    )
    def api_smarthome_detail(
        self,
        workspace_id: int,
        smarthome_id: int,
        action: str = "get",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Handle single smart home operations.

        Args:
            workspace_id: Workspace ID from URL.
            smarthome_id: Smart home ID from URL.
            action: 'get', 'delete', 'provision', or 'refresh_status'.
        """
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False, "error": "Workspace not found or access denied"}

        smarthome = self._get_smarthome_or_error(smarthome_id, workspace)
        if not smarthome:
            return {"success": False, "error": "Smart home not found"}

        if action == "get":
            return {"success": True, "data": smarthome.to_dict()}
        elif action == "delete":
            try:
                smarthome._check_operation_allowed(operation='write')
            except Exception as e:
                return {"success": False, "error": str(e)}
            return self._delete_smarthome(smarthome)
        elif action == "provision":
            try:
                smarthome._check_operation_allowed(operation='write')
            except Exception as e:
                return {"success": False, "error": str(e)}
            return self._provision_smarthome(smarthome)
        elif action == "refresh_status":
            return self._refresh_status(smarthome)
        else:
            return {"success": False, "error": f"Unknown action: {action}"}

    @route(
        "/api/workspaces/<int:workspace_id>/smarthomes/<int:smarthome_id>/change-plan",
        auth="user", methods=["POST"], type="json",
    )
    def api_smarthome_change_plan(
        self,
        workspace_id: int,
        smarthome_id: int,
        plan_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Change subscription plan for a smart home."""
        if not plan_id:
            return {"success": False, "error": "plan_id is required"}

        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False, "error": "Workspace not found or access denied"}

        smarthome = self._get_smarthome_or_error(smarthome_id, workspace)
        if not smarthome:
            return {"success": False, "error": "Smart home not found"}

        Plan = request.env["sh.subscription.plan"]
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {"success": False, "error": "Plan not found"}

        old_sub = smarthome.subscription_id
        if not old_sub or old_sub.state != "in_progress":
            return {"success": False, "error": "No active subscription"}

        if not old_sub.sh_end_date or not old_sub.sh_start_date:
            return {"success": False, "error": "Subscription dates are incomplete"}

        from odoo import fields as odoo_fields

        total_days = (old_sub.sh_end_date - old_sub.sh_start_date).days or 1
        remaining_days = max(0, (old_sub.sh_end_date - odoo_fields.Date.today()).days)
        refund = round(old_sub.sh_plan_price * remaining_days / total_days, 2)

        partner = old_sub.sh_partner_id

        if refund > 0:
            partner.sudo().add_wallet_points(
                amount=refund,
                description="Plan change refund: %s" % old_sub.sh_subscription_plan_id.name,
            )

        old_sub.sudo().write({
            "state": "close",
            "sh_reason": "Plan changed to: %s" % plan.name,
        })

        smarthome._create_and_confirm_subscription(partner, plan)

        return {
            "success": True,
            "data": {
                "refund_amount": refund,
                "new_plan": plan.name,
                "subscription": smarthome._get_subscription_info(),
            },
        }

    @route(
        "/api/smart-home/plans",
        auth="user", methods=["POST"], type="json",
    )
    def api_smarthome_plans(self, **kwargs: Any) -> dict[str, Any]:
        """Get available subscription plans for Smart Home."""
        Plan = request.env["sh.subscription.plan"]
        plans = Plan.search([
            ("name", "ilike", "Smart Home%"),
            ("active", "=", True),
        ])
        data = []
        for plan in plans:
            data.append({
                "id": plan.id,
                "name": plan.name,
                "price": plan.sh_plan_price,
                "duration": plan.sh_duration,
                "unit": plan.sh_unit,
                "has_trial": plan.sh_trial,
                "trial_duration": plan.sh_trial_duration if plan.sh_trial else 0,
                "trial_unit": plan.sh_trial_unit if plan.sh_trial else None,
            })
        return {"success": True, "data": data}

    # ==================== Private Helpers ====================

    def _get_workspace_or_error(self, workspace_id: int):
        """Verify user has access to workspace and return it."""
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

    def _get_smarthome_or_error(self, smarthome_id: int, workspace):
        """Get smart home belonging to workspace."""
        SmartHome = request.env["woow_paas_platform.smart_home"]
        smarthome = SmartHome.browse(smarthome_id).exists()
        if not smarthome or smarthome.workspace_id.id != workspace.id:
            return None
        return smarthome

    def _list_smarthomes(self, workspace) -> dict[str, Any]:
        """List all smart homes in a workspace."""
        SmartHome = request.env["woow_paas_platform.smart_home"]
        homes = SmartHome.search([
            ("workspace_id", "=", workspace.id),
        ])
        data = [h.to_dict() for h in homes]
        return {"success": True, "data": data, "count": len(data)}

    def _create_smarthome(
        self, workspace, name: str | None, ha_port: int | None,
        plan_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a new smart home in the workspace."""
        if not name:
            return {"success": False, "error": "Name is required"}
        if not plan_id:
            return {"success": False, "error": "Subscription plan is required"}

        # Validate plan
        Plan = request.env["sh.subscription.plan"]
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {"success": False, "error": "Subscription plan not found"}

        SmartHome = request.env["woow_paas_platform.smart_home"]
        vals = {
            "name": name,
            "workspace_id": workspace.id,
        }
        if ha_port:
            vals["ha_port"] = ha_port

        try:
            smarthome = SmartHome.create(vals)
            # Create subscription
            partner = workspace.owner_id.partner_id
            smarthome._create_and_confirm_subscription(partner, plan)
            return {"success": True, "data": smarthome.to_dict()}
        except Exception as e:
            _logger.exception("Failed to create smart home: %s", e)
            return {"success": False, "error": "Failed to create smart home. Please try again."}

    def _delete_smarthome(self, smarthome) -> dict[str, Any]:
        """Delete a smart home and its tunnel."""
        try:
            smarthome.action_delete()
            return {"success": True, "data": {"message": "Smart home deleted"}}
        except Exception as e:
            _logger.error("Failed to delete smart home: %s", e)
            return {"success": False, "error": str(e)}

    def _provision_smarthome(self, smarthome) -> dict[str, Any]:
        """Provision a smart home tunnel."""
        try:
            smarthome.action_provision()
            return {"success": True, "data": smarthome.to_dict()}
        except Exception as e:
            _logger.error("Failed to provision smart home: %s", e)
            return {"success": False, "error": str(e)}

    def _refresh_status(self, smarthome) -> dict[str, Any]:
        """Refresh tunnel status."""
        try:
            smarthome.action_refresh_status()
            return {"success": True, "data": smarthome.to_dict()}
        except Exception as e:
            _logger.error("Failed to refresh smart home status: %s", e)
            return {"success": False, "error": str(e)}
