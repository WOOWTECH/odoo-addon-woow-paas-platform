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
            return self._create_smarthome(workspace, name, ha_port)
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
            return self._delete_smarthome(smarthome)
        elif action == "provision":
            return self._provision_smarthome(smarthome)
        elif action == "refresh_status":
            return self._refresh_status(smarthome)
        else:
            return {"success": False, "error": f"Unknown action: {action}"}

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
    ) -> dict[str, Any]:
        """Create a new smart home in the workspace."""
        if not name:
            return {"success": False, "error": "Name is required"}

        SmartHome = request.env["woow_paas_platform.smart_home"]
        vals = {
            "name": name,
            "workspace_id": workspace.id,
        }
        if ha_port:
            vals["ha_port"] = ha_port

        try:
            smarthome = SmartHome.create(vals)
            return {"success": True, "data": smarthome.to_dict()}
        except Exception as e:
            _logger.error("Failed to create smart home: %s", e)
            return {"success": False, "error": str(e)}

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
