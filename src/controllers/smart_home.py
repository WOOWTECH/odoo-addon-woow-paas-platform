"""Smart Home API controller.

Provides JSON-RPC endpoints for Smart Home CRUD operations
within a workspace context.
"""
from __future__ import annotations

import logging
from typing import Any

from odoo.http import request, route, Controller

from ..models.workspace_access import ROLE_OWNER, ROLE_ADMIN

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

        kwargs:
            plan_id: Subscription plan ID (required for create).
            template_id: Smart Home template ID (required for create).
        """
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False, "error": "Workspace not found or access denied"}

        if action == "list":
            return self._list_smarthomes(workspace)
        elif action == "create":
            access = workspace.check_user_access(request.env.user)
            if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {"success": False, "error": "Insufficient permissions"}
            plan_id = kwargs.get("plan_id")
            template_id = kwargs.get("template_id")
            return self._create_smarthome(
                workspace, name, ha_port, plan_id, template_id,
            )
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
            access = workspace.check_user_access(request.env.user)
            if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {"success": False, "error": "Insufficient permissions"}
            try:
                smarthome._check_operation_allowed(operation='write')
            except Exception as e:
                return {"success": False, "error": str(e)}
            return self._delete_smarthome(smarthome)
        elif action == "provision":
            access = workspace.check_user_access(request.env.user)
            if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {"success": False, "error": "Insufficient permissions"}
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

        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}

        # sudo: portal users lack ACL on sh.subscription.plan; the records are
        # catalog data and the workspace-access check above already gates who
        # can change a smart home, so widening here is narrow and intentional.
        Plan = request.env["sh.subscription.plan"].sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {"success": False, "error": "Plan not found"}

        # sudo: portal/workspace-owner users typically lack ACL on
        # sh.subscription.subscription (it gates on group_sh_subscription_manager,
        # which workspace owners don't auto-receive). The workspace + role
        # check above already gates who can drive change_plan, so the sudo
        # here is a narrow widening that mirrors paas.py:874's cascade.
        old_sub = smarthome.sudo().subscription_id
        _logger.info(
            "[SH] change_plan request workspace=%s sh=%s new_plan=%s old_sub=%s",
            workspace_id, smarthome_id, plan_id,
            old_sub.id if old_sub else None,
        )
        if not old_sub or old_sub.state != "in_progress":
            return {"success": False, "error": "No active subscription"}

        # During trial, sh_end_date is legitimately NULL (billing period
        # hasn't started yet — mixin sets sh_start_date = trial_end + 1 but
        # leaves sh_end_date empty until the first invoice cycle). Strict
        # date completeness is only meaningful for active subs, where NULL
        # dates would indicate a data-integrity bug worth surfacing.
        # `_compute_unused_refund` is already trial-aware (returns 0), and
        # `_create_and_confirm_subscription` re-derives all dates from the
        # new plan, so the rest of the flow does not need NULL-date guards.
        if not old_sub.sh_trial:
            if not old_sub.sh_end_date or not old_sub.sh_start_date:
                return {"success": False, "error": "Subscription dates are incomplete"}

        refund = old_sub._compute_unused_refund()

        partner = old_sub.sh_partner_id
        _logger.info(
            "[SH] change_plan refund calc sh=%s start=%s end=%s price=%s refund=%s partner=%s",
            smarthome_id, old_sub.sh_start_date, old_sub.sh_end_date,
            old_sub.sh_plan_price, refund, partner.id,
        )

        if refund > 0:
            # #318: refund-typed + subscription link for reconciliation
            partner.sudo().refund_wallet_points(
                amount=refund,
                description="Plan change refund: %s" % old_sub.sh_subscription_plan_id.name,
                subscription=old_sub,
            )
            partner.invalidate_recordset(['wallet_balance'])
            _logger.info(
                "[SH] change_plan refund credited sh=%s partner=%s amount=%s",
                smarthome_id, partner.id, refund,
            )

        old_sub.sudo().write({
            "state": "close",
            "sh_reason": "Plan changed to: %s" % plan.name,
        })

        smarthome._create_and_confirm_subscription(partner, plan)
        # sudo: same rationale as the old_sub read above — Odoo's prefetch
        # cache *usually* lets the post-create traversal succeed, but the
        # intervening `.sudo().write()` on old_sub invalidates portions of
        # the cache, so paranoidly sudo here too. Avoids drift between this
        # log line's reliability and the rest of the endpoint's guarantees.
        new_sub = smarthome.sudo().subscription_id
        _logger.info(
            "[SH] change_plan done sh=%s old_sub=%s new_sub=%s",
            smarthome_id, old_sub.id,
            new_sub.id if new_sub else None,
        )

        return {
            "success": True,
            "data": {
                "refund_amount": refund,
                "new_plan": plan.name,
                "subscription": smarthome._get_subscription_info(),
            },
        }

    @route(
        "/api/smart-home/templates",
        auth="user", methods=["POST"], type="json",
    )
    def api_smarthome_templates(self, **kwargs: Any) -> dict[str, Any]:
        """List active Smart Home templates available to end users.

        Catalog data — readable by any authenticated user. Mirrors the
        cloud-service template/plan discovery shape so the frontend uses a
        single mental model for both service types.
        """
        SmartHomeTemplate = request.env[
            "woow_paas_platform.smart_home_template"
        ].sudo()
        templates = SmartHomeTemplate.search([("is_active", "=", True)])
        data = []
        for tmpl in templates:
            data.append({
                "id": tmpl.id,
                "name": tmpl.name,
                "slug": tmpl.slug,
                "description": tmpl.description,
            })
        return {"success": True, "data": data}

    @route(
        "/api/smart-home/templates/<int:template_id>/plans",
        auth="user", methods=["POST"], type="json",
    )
    def api_smarthome_template_plans(
        self, template_id: int, **kwargs: Any,
    ) -> dict[str, Any]:
        """Return subscription plans bound to a Smart Home template.

        Hard fails when the template is missing, inactive, or has no plans
        linked — the previous fallback to ``Plan.search(name ilike ...)`` was
        explicitly rejected during brainstorming because it hides admin
        misconfiguration. If you see "Template has no subscription plans",
        link plans to the template under Settings → Woow PaaS → Smart Home →
        Templates.

        Reads through ``.sudo()`` because portal users lack ACL on
        ``sh.subscription.plan`` — the records are catalog data, not
        user-owned, so sudo here is a deliberate, narrow widening
        (mirrors how cloud-service plan listing works on paas.py).
        """
        SmartHomeTemplate = request.env[
            "woow_paas_platform.smart_home_template"
        ].sudo()
        template = SmartHomeTemplate.browse(template_id)
        if not template.exists() or not template.is_active:
            return {
                "success": False,
                "error": "Smart Home template not found or inactive",
            }
        if not template.subscription_plan_ids:
            return {
                "success": False,
                "error": "Template has no subscription plans configured",
            }

        data = []
        # #330 Wave C：base plan 附掛隱藏席數變體（xmlid 反查、無變體＝[]）。
        variants_by_base = template.subscription_plan_ids._attach_variants()
        # #368：group_key 配對月/年同 tier、specs 供前台卡片顯示
        group_keys = template.subscription_plan_ids._group_keys()
        # #336：前台 plans 一律排除 storefront_hidden（既存訂閱不受影響）
        for plan in template.subscription_plan_ids.filtered(
            lambda p: not p.storefront_hidden
        ):
            data.append({
                "id": plan.id,
                "name": plan.name,
                "price": plan.sh_plan_price,
                "duration": plan.sh_duration,
                "unit": plan.sh_unit,
                "has_trial": plan.sh_trial,
                "trial_duration": plan.sh_trial_duration if plan.sh_trial else 0,
                "trial_unit": plan.sh_trial_unit if plan.sh_trial else None,
                "variants": variants_by_base.get(plan.id, []),
                "group_key": group_keys.get(plan.id),
                "specs": plan._get_display_specs(),
            })
        return {"success": True, "data": data}

    @route(
        "/api/workspaces/<int:workspace_id>/smarthomes/<int:smarthome_id>/skip-trial-estimate",
        auth="user", methods=["POST"], type="json",
    )
    def api_smarthome_skip_trial_estimate(
        self, workspace_id: int, smarthome_id: int, **kwargs: Any,
    ) -> dict[str, Any]:
        """Side-effect-free upcoming-invoice breakdown for the skip-trial modal.

        Mirrors paas.py:api_service_skip_trial_estimate so the SH detail page
        can use the same SkipTrialModal as the service detail page. The actual
        calculation lives on subscription_lifecycle_mixin._skip_trial_estimate
        and is shared with the dashboard's next_bill_estimate so amounts stay
        consistent across surfaces.

        Requires the caller to be workspace owner or admin: skip-trial commits
        the workspace owner's wallet to a charge, so a regular member must
        not be able to trigger billing on someone else's behalf. Mirrors the
        cloud-service role guard at paas.py:947-948.
        """
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {
                "success": False,
                "error": "Workspace not found or access denied",
            }

        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}

        smarthome = self._get_smarthome_or_error(smarthome_id, workspace)
        if not smarthome:
            return {"success": False, "error": "Smart home not found"}

        try:
            estimate = smarthome._skip_trial_estimate()
        except Exception as e:
            _logger.error(
                "Error estimating skip-trial for smarthome %s: %s",
                smarthome_id, e,
            )
            return {"success": False, "error": str(e)}

        return {"success": True, "data": estimate}

    @route(
        "/api/workspaces/<int:workspace_id>/smarthomes/<int:smarthome_id>/skip-trial",
        auth="user", methods=["POST"], type="json",
    )
    def api_smarthome_skip_trial(
        self, workspace_id: int, smarthome_id: int, **kwargs: Any,
    ) -> dict[str, Any]:
        """End trial and start paid billing immediately for a Smart Home.

        Owner/admin only — same rationale as api_smarthome_skip_trial_estimate
        (this is the mutating counterpart that actually charges the wallet).
        """
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {
                "success": False,
                "error": "Workspace not found or access denied",
            }

        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}

        smarthome = self._get_smarthome_or_error(smarthome_id, workspace)
        if not smarthome:
            return {"success": False, "error": "Smart home not found"}

        try:
            invoice_info = smarthome._skip_trial()
        except Exception as e:
            _logger.error(
                "Error skipping trial for smarthome %s: %s", smarthome_id, e,
            )
            return {"success": False, "error": str(e)}

        return {
            "success": True,
            "data": {
                "invoice": invoice_info,
                "subscription": smarthome._get_subscription_info(),
            },
        }

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
        template_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a new smart home in the workspace.

        Requires both template_id and plan_id, and validates that the chosen
        plan is in the template's subscription_plan_ids — mirrors the
        validation on paas.py:_create_service so cross-template plan abuse
        is rejected at the same layer for both service types.
        """
        if not name:
            return {"success": False, "error": "Name is required"}
        if not template_id:
            return {"success": False, "error": "Smart Home template is required"}
        if not plan_id:
            return {"success": False, "error": "Subscription plan is required"}

        # Validate template
        SmartHomeTemplate = request.env[
            "woow_paas_platform.smart_home_template"
        ].sudo()
        template = SmartHomeTemplate.browse(template_id)
        if not template.exists() or not template.is_active:
            return {
                "success": False,
                "error": "Smart Home template not found or inactive",
            }

        # Pre-check that the template actually has plans linked. Without
        # this guard the next branch ("plan not in template.subscription_
        # plan_ids") would fire and tell the admin "selected plan is not
        # available for this template", which misdirects them to the plan
        # they picked rather than the empty M2M on the template itself.
        # The text mirrors api_smarthome_template_plans so admins see the
        # same diagnosis whether the failure surfaces during list or create.
        if not template.subscription_plan_ids:
            return {
                "success": False,
                "error": "Template has no subscription plans configured",
            }

        # Validate plan and its membership in the template's plan list
        Plan = request.env["sh.subscription.plan"].sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {"success": False, "error": "Subscription plan not found"}
        if plan not in template.subscription_plan_ids:
            return {
                "success": False,
                "error": "Selected plan is not available for this template",
            }

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
            _logger.info(
                "[SH] create_smarthome sh=%s workspace=%s partner=%s plan=%s template=%s",
                smarthome.id, workspace.id, partner.id, plan.id, template.id,
            )
            smarthome._create_and_confirm_subscription(partner, plan)
            return {"success": True, "data": smarthome.to_dict()}
        except Exception as e:
            _logger.exception("[SH] Failed to create smart home: %s", e)
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
