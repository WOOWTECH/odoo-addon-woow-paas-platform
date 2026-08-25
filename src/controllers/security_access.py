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
# #536：sync-control 的兩個逃生門只認模組私有 sentinel（`call_kw` 把呼叫端
# context 整包換進 env，只判 truthy 的 key 租戶自己就打得開）。
from ..models.security_access import _rule_replace, _skip_route_sync

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

        # mutations below require owner/admin. refresh_status is grouped here
        # (not with read 'get'): when tunnel_id is set, action_refresh_status
        # does sa.write(...) which the SA write ir.rule
        # (security_access_rule_owner_admin_manage) only grants to
        # owner/admin — a role='user' member would hit a raw AccessError that
        # str(e) would then leak (model technical name / rule name) and make
        # behaviour state-dependent (succeeds while unprovisioned, fails once
        # provisioned). Gate it consistently with the other mutations.
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}

        if action == "refresh_status":
            try:
                sa.action_refresh_status()
            except Exception:
                _logger.exception(
                    "[SA] refresh_status failed sa=%s", sa.id)
                return {"success": False,
                        "error": "Failed to refresh status. Please try again."}
            return {"success": True, "data": sa.to_dict()}
        elif action == "update":
            vals = {}
            if kwargs.get("name"):
                vals["name"] = kwargs["name"]
            if vals:
                sa.write(vals)
            return {"success": True, "data": sa.to_dict()}
        elif action == "provision":
            # #805 (b)：驅動 `action_provision()` 的是**平台身分**，不是租戶身分。
            #
            # 修前這裡拿的是 `_get_sa_or_error` 回來的非 sudo 租戶 recordset，而
            # `action_provision` 內部那次 `_internal_identity(self).write(...)`
            # 含 `tunnel_token`——一個 `groups='base.group_system'` 的欄位
            # （`models/security_access.py` 的 `tunnel_token` 定義；行號會漂，
            # grep 符號名）。Odoo 的 `write()` 會
            # `check_field_access_rights('write', vals.keys())`，其唯一的
            # early-return 是 `if self.env.su:` ⇒ **只掛 `base.group_user` 的
            # 租戶打這個端點必定 AccessError**，而 `create_tunnel` 已經先跑過
            # ⇒ 租戶經 `/woow` 的 provision 從來沒有成功過。與 #739／PR #797 在
            # sibling model smart_home 上裁定的同一個形狀（那一側已於
            # `controllers/smart_home.py::_provision_smarthome` 改成平台身分）。
            #
            # 改用平台身分是對的層次，因為那次寫入的**值不是租戶給的**：整包來自
            # operator 對 `create_tunnel` 的回應加上伺服器端的時間戳。憑證是平台
            # 鑄造的，本來就該由平台身分寫下去；租戶不需要、也不該擁有寫那個欄位
            # 的權限。
            #
            # ⚠ **作用範圍要講精確**：`sa.sudo().action_provision()` 之後，
            # `action_provision` body 內每一次 ORM 操作都在 `env.su` 底下——對
            # model 而言這就是「整個 method 走 sudo」。但 sudo 只加在**這一個
            # 呼叫點**：`action_provision` 是 public method（無底線前綴、無
            # `@api.private`），Odoo 原生的 `/web/dataset/call_kw` 也到得了它，
            # 那條路不經過本 controller ⇒ 欄位遮罩在那裡照樣 fail-closed
            # （`tests/test_sa_provision_tenant_rpc_805.py` 的 C1 釘住）。
            #
            # 三道閘都**留在 sudo 之外**，一條都沒有被順帶關掉（
            # `orm-vs-endpoint-acl-test-coverage` 要求的對等 endpoint test 見
            # 同檔 B 系列，逐條點名）：
            #   * workspace 歸屬 —— 上面的 `_get_workspace_or_error`（B2）。
            #     它的 domain 另外帶 `workspace_id.state = 'active'`，而 ir.rule
            #     `security_access_rule_owner_admin_manage` 不管 workspace 狀態
            #     ⇒ **這一格比 ir.rule 嚴的是它，不是下面那道角色閘**
            #   * owner/admin 角色 —— 上面的 `check_user_access` 閘（B1）。它只
            #     比對 workspace_access 的 role，與上述 ir.rule 的 domain 條件
            #     等價（`models/workspace.py` 的 `check_user_access` 整支不讀
            #     workspace.state）
            #   * 訂閱生效 —— 下一行的 `_check_operation_allowed`（B4）。Odoo 的
            #     `sudo()` **不改變 `env.user`**，所以它開頭那個
            #     `has_group('base.group_system')` 提早 return 不會因此變 True
            # 身分錨點不可變守衛明示無 su 豁免，sudo 打不開（B7）。
            #
            # 🔴 讀側：**不要寫成「完全沒動」**——那句話在本 repo 已被實測推翻，
            # 精確版與 odoo shell 實測記在 `controllers/smart_home.py` 的
            # `_provision_smarthome` docstring（#739／PR #797 留下的，刻意不在
            # 這裡複述以免兩份漂移）。一句話版：su 那次呼叫會把 token 餵進同一筆
            # 交易內跨 env 共用的欄位快取，之後**非 sudo** recordset 的**屬性
            # 直讀**會命中快取而不再查遮罩 ⇒ 成立的是「**同一個請求裡沒有任何
            # 程式碼去讀那個屬性**」，不是「讀側沒動」。這裡靠的是：`to_dict()`
            # 不含 token（B6），而經 RPC 的 `read()` 一定重查遮罩（B5）。
            #
            # ⚠ 未隨本修法消失的既有形狀：下面的 `except Exception` 仍會把任何
            # 例外收成 `{"success": False}` 回 HTTP 200，而 `action_provision`
            # 開頭那次 `state='provisioning'` 已落地 ⇒ 記錄仍可能停在中途狀態。
            # 本票只拿掉了那個形狀在 #805 (b) 上的**觸發源**，沒有動形狀本身
            # （補償邏輯由 issue-leader 2026-08-22 裁定另案）。
            try:
                sa._check_operation_allowed(operation='write')
                sa.sudo().action_provision()
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
        # #330 Wave C：base plan 附掛隱藏席數變體（xmlid 反查、無變體＝[]）。
        variants_by_base = template.subscription_plan_ids._attach_variants()
        # #368：group_key 配對月/年同 tier、specs 供前台卡片顯示
        group_keys = template.subscription_plan_ids._group_keys()
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
                "variants": variants_by_base.get(p.id, []),
                "group_key": group_keys.get(p.id),
                "specs": p._get_display_specs(),
            }
            # #336：前台 plans 一律排除 storefront_hidden（既存訂閱不受影響）
            for p in template.subscription_plan_ids.filtered(lambda p: not p.storefront_hidden)
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
        # 這道守衛真正要防的是「開出與這個實例不相符的 subscription／invoice
        # line」（invoice line 文案由 plan 的 product 決定）。role gate 只擋
        # 誰能改方案，不擋 plan 歸屬。
        #
        # 有 template：template 就是最精準的代理——與 _create 路徑的
        # `plan not in template.subscription_plan_ids` 對稱，行為不變。
        #
        # template-less（admin/legacy 在 BFF 之外建立的 SA）：#750 之前是
        # `if sa.template_id and ...`，整段被跳過 ⇒ 任何 plan 都換得成。改成
        # 退一階直接量守衛的目標本身，兩條都要成立：
        #   1. plan.service_type == 'security_access'（plan 屬於 SA 家族）；
        #   2. 且該 plan 掛在**某個** SA template 的白名單內。
        # 第 2 條擋的是「孤兒 SA plan」——service_type 對、卻不掛任何 template。
        # 那是合法存在的狀態（sh_subscription_plan_service_type 的
        # _check_service_type_matches_template_family 明文放行），所以第 1 條
        # 擋不到它。疊上去的依據是族群量測：2026-08-25 stg／prod 兩環境
        # service_type='security_access' 各 88 筆、孤兒各 0 筆 ⇒ 不會擋掉任何
        # 現存的合法 plan。
        #
        # sudo 必要性（判準換了，重新確認過，不沿用舊註解的說法）：
        #   - `plan.service_type` **不需要**額外 sudo——plan 是從上面
        #     `Plan = request.env[...].sudo()` browse 出來的 recordset。
        #   - `sa.sudo().template_id.subscription_plan_ids` 仍需要：M2M 走訪
        #     落在 sh.subscription.plan 這個租戶沒有 ACL 的 catalog model 上。
        #   - 第 2 條的 template 反查同理，其 sudo 收在 model 層的
        #     `_owning_template_families()`（理由寫在該 method 的 docstring）。
        # 上面的 workspace＋role 檢查已經管好誰到得了這裡。
        if sa.template_id:
            if plan not in sa.sudo().template_id.subscription_plan_ids:
                return {
                    "success": False,
                    "error": "Selected plan is not available for this template",
                }
        elif (plan.service_type != "security_access"
                or "security_access" not in plan._owning_template_families()):
            return {"success": False,
                    "error": "Selected plan is not a valid Security Access plan"}

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
            # #318: refund-typed + subscription link for reconciliation.
            partner.sudo().refund_wallet_points(
                amount=refund,
                description="Plan change refund: %s"
                            % old_sub.sh_subscription_plan_id.name,
                subscription=old_sub,
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
        """Format check first, then two-layer availability: Odoo DB, Cloudflare DNS."""
        Route = request.env[
            "woow_paas_platform.security_access_route"].sudo()
        # Fail-fast on format using the SAME validator the create/write
        # constraint uses (single source of truth) so availability never
        # reports a prefix as "Available" that create would then reject.
        fmt_err = Route._subdomain_prefix_error(prefix)
        if fmt_err:
            return {"success": True,
                    "data": {"prefix": prefix, "available": False,
                             "reason": fmt_err}}
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
            if not client:
                # Operator NOT configured -> get_paas_operator_client returns
                # None (only logs a warning). This is "cannot confirm", not
                # "free": treat it the same as the operator-unreachable except
                # branch below (fail-CLOSED). Falling through to available=True
                # here would be MORE permissive for the un-configured case than
                # for the configured-but-unreachable case, contradicting that
                # branch's own contract and handing the user a prefix the
                # platform never confirmed is free.
                return {"success": True,
                        "data": {"prefix": prefix, "available": False,
                                 "reason": "Could not confirm availability "
                                           "(operator not configured); "
                                           "try again later."}}
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
            # fail-CLOSED, symmetric with the status=='unknown' branch above.
            # Operator-unreachable (PaaSOperatorConnectionError / timeout) is a
            # strictly more severe failure than a transient CF API error, so it
            # must not be MORE permissive: returning available=True here would
            # let a user reserve a prefix the platform could not confirm is
            # free, during exactly the window an attacker could race a squat.
            # The actual create path stays authoritative (DB UNIQUE + CF-side
            # create error); this pre-check is best-effort UX feedback and must
            # not pose as a permissive authority.
            return {"success": True,
                    "data": {"prefix": prefix, "available": False,
                             "reason": "Could not confirm availability "
                                       "(operator unavailable); try again."}}
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
            count = len(sa.route_ids)
            if count >= quota:
                return {"success": False,
                        "error": "Route quota exceeded: plan allows %d "
                                 "route(s)" % quota}

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

    # ==================== Access Control ====================

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>/access-config",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_access_config(self, workspace_id: int, sa_id: int,
                             **kwargs: Any) -> dict[str, Any]:
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
        try:
            sa._check_operation_allowed(operation='write')
        except Exception as e:
            return {"success": False, "error": str(e)}
        return self._update_access_config(sa, kwargs)

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>/access-policies",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_access_policies(self, workspace_id: int, sa_id: int,
                               action: str = "list",
                               **kwargs: Any) -> dict[str, Any]:
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": "Security access not found"}

        if action == "list":
            # No .sudo(): ir.rule filters policies to workspace members
            # (orm-vs-endpoint-acl-test-coverage pattern). A non-member never
            # reaches here (_get_workspace_or_error gates first), and a member
            # sees only their own SA's policies via the row-level rule.
            #
            # P2.5 per-route: policies bind to routes. List every policy across
            # the SA's routes via the stored related security_access_id. An
            # optional route_id filter (pinned to the gated SA) lets the
            # per-route UI scope the list to one Application Route; an unknown /
            # cross-SA route_id yields an empty list (never another SA's rows).
            domain = [("security_access_id", "=", sa.id)]
            if kwargs.get("route_id"):
                route = self._sa_route(sa, kwargs["route_id"])
                if not route:
                    return {"success": True, "data": []}
                domain = [("route_id", "=", route.id)]
            policies = request.env[
                "woow_paas_platform.security_access_access_policy"].search(
                    domain)
            return {"success": True,
                    "data": [p.to_dict() for p in policies]}

        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}
        try:
            sa._check_operation_allowed(operation='write')
        except Exception as e:
            return {"success": False, "error": str(e)}

        if action == "create":
            return self._create_policy(sa, kwargs)
        elif action == "update":
            return self._update_policy(sa, kwargs.get("policy_id"), kwargs)
        elif action == "delete":
            return self._delete_policy(sa, kwargs.get("policy_id"))
        elif action == "reorder":
            return self._reorder_policies(sa, kwargs)
        return {"success": False, "error": f"Unknown action: {action}"}

    # ==================== Per-route Access config (P2.5 Phase 3) ====

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>/routes/<int:route_id>/access-config",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_route_access_config(self, workspace_id: int, sa_id: int,
                                   route_id: int,
                                   **kwargs: Any) -> dict[str, Any]:
        """Per-route Access config (P2.5 Phase 3): set THIS route's own
        is_protected / login methods / session duration. Each protected route
        is its own Cloudflare Access application, so the config is route-scoped.
        route_id comes from the URL and is pinned to the gated SA (route must be
        in sa.route_ids); a cross-SA route_id is rejected, never written."""
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
        try:
            sa._check_operation_allowed(operation='write')
        except Exception as e:
            return {"success": False, "error": str(e)}
        return self._update_route_access(sa, route_id, kwargs)

    # ---- Access Control helpers ----
    #
    # P2.5: Access config + policies are per-route now (each protected route is
    # its own Cloudflare Access app). The per-route endpoints below take a
    # route_id pinned to the gated SA. The legacy SA-level helpers
    # (_update_access_config / _create_policy without route_id) remain as a
    # P2.5 bridge that acts on the SA's primary (is_primary) route so the
    # not-yet-migrated Phase 4 frontend keeps working; the migration re-homed
    # legacy SA-level policies onto the primary route, so the bridge is
    # consistent with the migrated data.
    #
    # NOTE: Phase 4 has now shipped (service.js drops updateAccessConfig/
    # updateRoute and always passes route_id), so NO in-repo client calls this
    # SA-level bridge anymore — it is retained only for in-flight browsers
    # carrying pre-deploy cached JS during the rollout window. Removal is tracked
    # as F-SA-6 (docs/tracking/followups/follow-ups.md): delete this bridge (+ its
    # ~5 security tests) once stg/prod are deployed and the in-flight window has
    # passed. The bridge is NOT a divergent attack surface — same _get_sa_or_error
    # cross-SA pin + same whitelist + same Policy.create as the per-route path.

    def _sa_access_route(self, sa):
        """The route the legacy SA-level Access helpers act on (P2.5 bridge).
        軸2 removed the auto primary route, so this falls back to the SA's
        first route; returns an empty recordset if the SA has no route yet so
        callers can surface a clean error. (Full bridge removal: F-SA-6.)"""
        return sa.route_ids[:1]

    def _sa_route(self, sa, route_id):
        """Resolve a client-supplied route_id, PINNED to the gated SA: the
        route must belong to this SA (route.security_access_id == sa.id, i.e.
        route_id in sa.route_ids). A cross-SA / unknown route_id returns an
        empty recordset so callers deny it — a client can never write a policy
        or flip access config on another SA's route. sa.route_ids is itself
        ir.rule-filtered (no .sudo()), so this also denies routes the caller
        can't see."""
        try:
            rid = int(route_id)
        except (TypeError, ValueError):
            return sa.route_ids.browse()
        return sa.route_ids.filtered(lambda r: r.id == rid)[:1]

    def _update_access_config(self, sa, kwargs) -> dict[str, Any]:
        # Whitelist only: never pass raw kwargs to write() — access_app_id and
        # other server-owned fields must not be client-settable. P2.5 maps the
        # legacy SA-level master toggle (access_enabled) onto the primary
        # route's is_protected.
        route = self._sa_access_route(sa)
        if not route:
            return {"success": False,
                    "error": "No application route to configure yet"}
        vals = {}
        if "access_enabled" in kwargs:
            vals["is_protected"] = bool(kwargs["access_enabled"])
        for f in ("login_google", "login_email_otp"):
            if f in kwargs:
                vals[f] = bool(kwargs[f])
        if kwargs.get("session_duration"):
            vals["session_duration"] = kwargs["session_duration"]
        try:
            if vals:
                route.write(vals)
            return {"success": True, "data": sa.to_dict()}
        except Exception as e:
            _logger.exception("[SA] Failed to update access config: %s", e)
            return {"success": False, "error": str(e)}

    def _update_route_access(self, sa, route_id, kwargs) -> dict[str, Any]:
        """Per-route Access config write (P2.5 Phase 3). route_id is pinned to
        the gated SA via _sa_route. Strict whitelist: only is_protected /
        login_google / login_email_otp / session_duration are client-settable.
        The server-owned per-route access_app_id / access_unresolved_idp_types
        (set by the operator sync) are never written from kwargs — the route
        write() immutable guard only covers subdomain_prefix, so this whitelist
        is the sole defense for those Chars (mirrors _update_access_config)."""
        route = self._sa_route(sa, route_id)
        if not route:
            return {"success": False, "error": "Route not found"}
        vals = {}
        # Accept both 'is_protected' (per-route native) and the legacy
        # 'access_enabled' alias so an in-flight Phase 4 client can use either.
        if "is_protected" in kwargs:
            vals["is_protected"] = bool(kwargs["is_protected"])
        elif "access_enabled" in kwargs:
            vals["is_protected"] = bool(kwargs["access_enabled"])
        for f in ("login_google", "login_email_otp"):
            if f in kwargs:
                vals[f] = bool(kwargs[f])
        if kwargs.get("session_duration"):
            vals["session_duration"] = kwargs["session_duration"]
        try:
            if vals:
                route.write(vals)
            return {"success": True, "data": route.to_dict()}
        except Exception as e:
            _logger.exception("[SA] Failed to update route access: %s", e)
            return {"success": False, "error": str(e)}

    def _create_policy(self, sa, kwargs) -> dict[str, Any]:
        if not kwargs.get("name"):
            return {"success": False, "error": "name is required"}
        # P2.5 Phase 3: a client may target ANY of the gated SA's routes via
        # route_id (pinned to the SA). A cross-SA / unknown route_id is rejected
        # — never written to another SA's route. Absent route_id, fall back to
        # the SA's primary route (P2.5 bridge for the not-yet-migrated frontend).
        if kwargs.get("route_id"):
            route = self._sa_route(sa, kwargs["route_id"])
            if not route:
                return {"success": False, "error": "Route not found"}
        else:
            route = self._sa_access_route(sa)
        if not route:
            return {"success": False,
                    "error": "No application route to attach a policy to yet"}
        # two-layer quota: constraint re-checks at ORM layer (lock-step with
        # the model's _check_policy_quota — an SA-LEVEL total). sudo on
        # subscription_id read only: portal owners lack ACL on the subscription.
        #
        # quota 0 = unlimited (#743), in step with the model constraint. This
        # pre-check runs BEFORE Policy.create(), so without the exemption here
        # the model's exemption is unreachable from the UI: `existing >= quota`
        # is 0 >= 0 on the very first policy of a zero-quota plan, and the
        # request never gets far enough for the constraint to have an opinion.
        # (This is only about sa_max_access_policies — the route pre-check above
        # keeps sa_max_routes at 0 = none allowed.)
        sub = sa.sudo().subscription_id
        Policy = request.env[
            "woow_paas_platform.security_access_access_policy"]
        if sub and sub.sh_subscription_plan_id:
            quota = sub.sh_subscription_plan_id.sa_max_access_policies
            existing = Policy.search_count(
                [("security_access_id", "=", sa.id)])
            if quota and existing >= quota:
                return {"success": False,
                        "error": "Access policy quota exceeded: plan allows "
                                 "%d polic(ies)" % quota}
        # Whitelist only: cf_policy_id / security_access_id / access_app_id are
        # server-owned and never taken from kwargs. route_id is set from the
        # already-resolved, SA-pinned `route` recordset (NOT the raw kwargs id),
        # so the client's value only selects a route within the gated SA and can
        # never bind the policy to another SA's route.
        create_vals = {
            "route_id": route.id,
            "name": kwargs["name"],
            "decision": kwargs.get("decision", "allow"),
        }
        # Sparse precedence: omit sequence unless the client sets it so the model
        # assigns max+10 within the route (no all-collide-on-10). The reorder
        # endpoint rewrites sequences explicitly.
        if "sequence" in kwargs:
            create_vals["sequence"] = kwargs["sequence"]
        rule_commands = self._build_rule_commands(kwargs)
        if rule_commands is not None:
            create_vals["rule_ids"] = rule_commands
        else:
            # Legacy flat (transitional): the Policy.create compat shim expands
            # rule_type/rule_value into one include rule. Removed with the shim
            # once the frontend (Phase 4) sends include/require/exclude.
            create_vals["rule_type"] = kwargs.get("rule_type", "email_domain")
            create_vals["rule_value"] = kwargs.get("rule_value", "")
        try:
            pol = Policy.create(create_vals)
            return {"success": True, "data": pol.to_dict()}
        except Exception as e:
            _logger.exception("[SA] Failed to create access policy: %s", e)
            return {"success": False, "error": str(e)}

    # Rule groups accepted on the policy create/update payload (P-rule three
    # groups). Each rule line is whitelisted to group/rule_type/rule_value — a
    # client can never smuggle cf_policy_id / policy_id / arbitrary fields onto a
    # sub-rule (mass-assignment guard, same discipline as the policy whitelist).
    _RULE_GROUPS = ("include", "require", "exclude")

    def _build_rule_commands(self, kwargs):
        """Translate an include/require/exclude payload into One2many create
        commands ((0, 0, vals)). Returns None when no group key is present so
        the caller can fall back to the legacy flat rule_type/rule_value path."""
        if not any(g in kwargs for g in self._RULE_GROUPS):
            return None
        commands = []
        for grp in self._RULE_GROUPS:
            for r in kwargs.get(grp) or []:
                if not isinstance(r, dict):
                    continue
                commands.append((0, 0, {
                    "group": grp,
                    "rule_type": r.get("rule_type"),
                    "rule_value": r.get("rule_value"),
                }))
        return commands

    def _reorder_policies(self, sa, kwargs) -> dict[str, Any]:
        """Rewrite policy precedence (sequence) to the given order. Each id is
        pinned to the gated SA via _sa_policy — a cross-SA / unknown policy_id is
        silently skipped, never written, so a caller cannot tamper with another
        tenant's evaluation order. owner/admin role + _check_operation_allowed are
        already enforced by the dispatcher. Per-write operator sync is suppressed
        and a single re-sync issued at the end so CF sees the final order once."""
        policy_ids = kwargs.get("policy_ids") or []
        if not isinstance(policy_ids, list):
            return {"success": False, "error": "policy_ids must be a list"}
        reordered = []
        routes = request.env["woow_paas_platform.security_access_route"]
        try:
            seq = 0
            for pid in policy_ids:
                pol = self._sa_policy(sa, pid)
                if not pol:
                    continue  # cross-SA / unknown → skip (never write foreign)
                # Counter over the PINNED policies (not raw index) so a skipped
                # foreign id leaves no gap; sparse 10s keep room to insert.
                seq += 10
                _skip_route_sync(pol).write({"sequence": seq})
                reordered.append(pol.id)
                routes |= pol.route_id
            for route in routes:
                route._sync_access_to_operator()
            return {"success": True, "data": {"reordered": reordered}}
        except Exception as e:
            _logger.exception("[SA] Failed to reorder policies: %s", e)
            return {"success": False, "error": str(e)}

    def _sa_policy(self, sa, policy_id):
        """A policy under this SA (any of its routes), looked up via the stored
        related security_access_id so it cannot reach across SAs."""
        return request.env[
            "woow_paas_platform.security_access_access_policy"].search(
                [("id", "=", policy_id),
                 ("security_access_id", "=", sa.id)], limit=1)

    def _update_policy(self, sa, policy_id, kwargs) -> dict[str, Any]:
        pol = self._sa_policy(sa, policy_id)
        if not pol:
            return {"success": False, "error": "Policy not found"}
        # Whitelist only: drop cf_policy_id / security_access_id / route_id and
        # any other key the client tries to smuggle through.
        vals = {}
        for f in ("name", "decision", "sequence"):
            if f in kwargs:
                vals[f] = kwargs[f]
        write_pol = pol
        rule_commands = self._build_rule_commands(kwargs)
        if rule_commands is not None:
            # Replace the whole rule set. delete-before-create ((5,0,0) + creates)
            # so the per-policy email-quota count sees only the NEW rules; the
            # transient empty state would trip the rule-level "≥1 include" check,
            # so sa_rule_replace suppresses it — the policy-level @api.constrains
            # validates the final state (include present, email cap).
            vals["rule_ids"] = [(5, 0, 0)] + rule_commands
            write_pol = _rule_replace(pol)
        elif "rule_type" in kwargs or "rule_value" in kwargs:
            # Legacy flat single-rule update (transitional): the policy no longer
            # has flat rule fields, so replace its rules with one include rule.
            vals["rule_ids"] = [(5, 0, 0), (0, 0, {
                "group": "include",
                "rule_type": kwargs.get("rule_type", "email_domain"),
                "rule_value": kwargs.get("rule_value", ""),
            })]
            write_pol = _rule_replace(pol)
        try:
            if vals:
                write_pol.write(vals)
            return {"success": True, "data": pol.to_dict()}
        except Exception as e:
            _logger.exception("[SA] Failed to update access policy: %s", e)
            return {"success": False, "error": str(e)}

    def _delete_policy(self, sa, policy_id) -> dict[str, Any]:
        pol = self._sa_policy(sa, policy_id)
        if not pol:
            return {"success": False, "error": "Policy not found"}
        try:
            pol.unlink()
            return {"success": True, "data": {"message": "Policy deleted"}}
        except Exception as e:
            _logger.exception("[SA] Failed to delete access policy: %s", e)
            return {"success": False, "error": str(e)}
