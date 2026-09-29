"""Smart Home API controller.

Provides JSON-RPC endpoints for Smart Home CRUD operations
within a workspace context.
"""
from __future__ import annotations

import logging
from typing import Any

from odoo.http import request, route, Controller
from odoo.exceptions import (
    AccessDenied, AccessError, MissingError, UserError, ValidationError,
)
from psycopg2 import OperationalError

from ..models.smart_home import _platform_order
from ..models.workspace_access import ROLE_OWNER, ROLE_ADMIN
# #536：sync-control 的兩個逃生門只認模組私有 sentinel（`call_kw` 把呼叫端
# context 整包換進 env，只判 truthy 的 key 租戶自己就打得開）。
from ..models.security_access import _rule_replace, _skip_route_sync
from odoo import _

_logger = logging.getLogger(__name__)

_POLICY_MODEL = "woow_paas_platform.security_access_access_policy"
_ROUTE_MODEL = "woow_paas_platform.smart_home_route"
_ACCESS_RULE_GROUPS = ("include", "require", "exclude")


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
            return {"success": False, "error": _("Workspace not found or access denied")}

        if action == "list":
            return self._list_smarthomes(workspace)
        elif action == "create":
            access = workspace.check_user_access(request.env.user)
            if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {"success": False, "error": _("Insufficient permissions")}
            plan_id = kwargs.get("plan_id")
            template_id = kwargs.get("template_id")
            return self._create_smarthome(
                workspace, name, ha_port, plan_id, template_id,
            )
        else:
            return {"success": False, "error": _("Unknown action: %(action)s", action=action)}

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
            return {"success": False, "error": _("Workspace not found or access denied")}

        smarthome = self._get_smarthome_or_error(smarthome_id, workspace)
        if not smarthome:
            return {"success": False, "error": _("Smart home not found")}

        if action == "get":
            # #768：`include_details=True` 才帶 `routes`。清單端點刻意不帶——它
            # 會為 workspace 的每一台 SH 各 walk 一次 `access_policy_ids`，而
            # Access 分頁只存在於詳情頁（CS 為同一個理由把 `access` 限定在
            # `include_details` 那一側）。
            #
            # tunnel 欄位改用即時值（只讀，見 `SmartHome._live_tunnel_status`）；
            # 拿不到時沿用 DB 裡上一次寫入的值。與 SA 詳情頁（#1256）同一個作法：
            # DB 值只有 owner/admin 按 Refresh 時才會更新，其他成員看到的是過期的
            # 「Connected」。刻意只做在 detail、不做在 list——list 每一台各打一次
            # operator 太貴，而 Tunnel Information 只存在於詳情頁。
            data = smarthome.to_dict(include_details=True)
            try:
                data.update(smarthome._live_tunnel_status())
            except Exception:
                # 開通已經成功（上面那行的寫入已經在這個交易裡）；疊加失敗只退回
                # DB 值，不得讓下面的 `except Exception` 把一次成功的開通回報成失敗。
                _logger.exception(
                    "Live tunnel status overlay failed after provision sh=%s",
                    smarthome.id)
            return {"success": True, "data": data}

        if action not in ("delete", "provision", "refresh_status"):
            return {"success": False, "error": _("Unknown action: %(action)s", action=action)}

        # 以下全部是 mutation，一律先過 owner/admin。
        #
        # #524：`refresh_status` 從「與 get 同群、無 role gate」搬到這裡。理由與
        # `controllers/security_access.py` 的同一處前例逐字相同：本 PR 把
        # smart_home 的 write 收窄到 `smart_home_rule_owner_admin_manage` 之後，
        # `action_refresh_status()` 內部的 `self.write(update_vals)` 會讓一個
        # role='user' 成員吃到**裸 AccessError** —— `str(e)` 會把 model 技術名與
        # rule 名洩漏給租戶，而且行為變成 state-dependent（`tunnel_id` 還沒設時
        # 提早 return 所以成功、provision 之後才失敗）。
        # 這是 two-layer-acl-lockstep 的要求，不是附帶重構。
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": _("Insufficient permissions")}

        if action == "refresh_status":
            return self._refresh_status(smarthome)

        if action == "delete":
            try:
                smarthome._check_operation_allowed(operation='write')
            except OperationalError:
                raise
            except (AccessDenied, AccessError, MissingError) as e:
                _logger.exception(
                    "Access error checking smart home deletion: %s", e,
                )
                return {
                    "success": False,
                    "error": _("Failed to delete smart home. Please try again."),
                }
            except (ValidationError, UserError) as e:
                return {"success": False, "error": str(e)}
            except Exception as e:
                _logger.exception("Error checking smart home deletion: %s", e)
                return {
                    "success": False,
                    "error": _("Failed to delete smart home. Please try again."),
                }
            return self._delete_smarthome(smarthome)

        try:
            smarthome._check_operation_allowed(operation='write')
        except Exception as e:
            return {"success": False, "error": str(e)}
        return self._provision_smarthome(smarthome)

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
            return {"success": False, "error": _("plan_id is required")}

        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False, "error": _("Workspace not found or access denied")}

        smarthome = self._get_smarthome_or_error(smarthome_id, workspace)
        if not smarthome:
            return {"success": False, "error": _("Smart home not found")}

        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": _("Insufficient permissions")}

        # sudo: portal users lack ACL on sh.subscription.plan; the records are
        # catalog data and the workspace-access check above already gates who
        # can change a smart home, so widening here is narrow and intentional.
        Plan = request.env["sh.subscription.plan"].sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {"success": False, "error": _("Plan not found")}

        # #768：plan 必須屬於某個上架中的 Smart Home template——與 **create 路徑
        # 同一把尺**（`_create_smarthome` 的 `plan not in
        # template.subscription_plan_ids`），CS 也有對應的一條
        # （`paas.py::api_service_change_plan`，註解自陳與 SA 端 lock-step）。
        #
        # 為什麼是本單一起補：`ac_max_access_seats` 在本單之前只是計費欄位，換到
        # 別家的方案是「計費不一致」；本單把它變成**能力 gate**（Access 分頁的
        # 顯示判準）之後，同一條路徑就升級成**能力越權**——租戶可以把自己的 SH
        # 換到任何帶席位的變體，包含 CS／SA 家族那 222 筆。
        #
        # ⚠ 判準不能照抄 CS 的 `service.template_id`：`smart_home` **沒有
        # template FK**（grep 零命中），所以走「屬於任一 active SH template」。
        SmartHomeTemplate = request.env[
            "woow_paas_platform.smart_home_template"
        ].sudo()
        if not SmartHomeTemplate.search_count([
            ("is_active", "=", True),
            ("subscription_plan_ids", "in", plan.ids),
        ]):
            return {"success": False,
                    "error": _("Selected plan is not available for Smart Home")}

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
            return {"success": False, "error": _("No active subscription")}

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
                return {"success": False, "error": _("Subscription dates are incomplete")}

        # #773 降級前置合規 gate：目標 plan 的席數放不下現有用量就**擋住換方案**，
        # 並告訴租戶要刪到幾個。⛔ 系統不自動刪任何存取權（Eugene 裁決，#773 留言 10716）。
        #
        # 位置：所有既有閘之後、**第一個寫入之前**。這一格是硬需求不是風格——
        # 本端點的第一個 DB 副作用是退點（`refund_wallet_points`），接著才關舊訂閱、
        # 建新訂閱；gate 放到那之後只能「跑一半再整包回捲」，而 Eugene 裁的是「擋住」。
        #
        # ⚠ 刻意排在「無有效訂閱」與日期完整性兩閘**之後**（而不是白名單閘後面就插）：
        # 那兩閘的訊息更貼近租戶當下的問題，先跑它們可維持既有錯誤優先序；
        # 三腿的 gate 都落在同一個相對位置，同構好覆核。
        #
        # 尺與寫入守衛共用（`_seat_quota_violation` → `_seat_usage_for_anchor`）：
        # ⛔ 不得在這裡自己數席位，兩把尺漂開會產生「gate 放行但寫入被守衛擋」的死結。
        #
        # 升級天然通過（席數變大 ⇒ 用量 ≤ 席數），所以**不需要**先判斷這次是不是降級。
        #
        # #1261：同一支 gate 也擋「目標 0 席 ＋ 仍有 route 受保護」——0 席方案看不到
        # Access 頁籤，換過去之後保護關不掉、對外 403（prod 2026-09-29 重現）。
        # 判準收在 `_protection_gate_denial`，這裡不另寫。
        _denial = request.env['woow_paas_platform.security_access_access_policy']\
            ._seat_gate_denial(smarthome, plan)
        if _denial:
            # #1261：`data` 原樣轉發 error 以外的欄位（`code`／current_seats／
            # target_seats，保護那種另帶 `protected_hostnames`）。不逐 key 挑：
            # gate 多一種拒絕時三個端點各挑一次就是漂移的起點。
            return {'success': False,
                    'error': _denial['error'],
                    'data': {k: v for k, v in _denial.items() if k != 'error'}}

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
                "error": _("Smart Home template not found or inactive"),
            }
        if not template.subscription_plan_ids:
            return {
                "success": False,
                "error": _("Template has no subscription plans configured"),
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
                "error": _("Workspace not found or access denied"),
            }

        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": _("Insufficient permissions")}

        smarthome = self._get_smarthome_or_error(smarthome_id, workspace)
        if not smarthome:
            return {"success": False, "error": _("Smart home not found")}

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
                "error": _("Workspace not found or access denied"),
            }

        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": _("Insufficient permissions")}

        smarthome = self._get_smarthome_or_error(smarthome_id, workspace)
        if not smarthome:
            return {"success": False, "error": _("Smart home not found")}

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

    # ==================== Access Control API（#768）====================
    #
    # 形狀比照 **SA** 的 access-config／access-policies 組
    # （`controllers/security_access.py`）而不是 CS：CS 一服務一錨點，所以它把
    # 兩支 config 收斂成一支；SH 走 per-route 多錨點（裁決 2），config 因此是
    # **route-pinned**（URL 帶 route_id），policy 那支則是父層 pinned ＋ action
    # 分派，只有 `create` 帶錨點 id——與 SA 逐字相同。
    #
    # 四步骨架與 SA／CS 逐字相同、順序也刻意一致：workspace 可見性 → SH 釘住 →
    # role gate（owner/admin 才寫）→ 訂閱閘。`list` 是唯一跳過後兩步的分支。

    @route(
        "/api/workspaces/<int:workspace_id>/smarthomes/<int:smarthome_id>"
        "/routes/<int:route_id>/access-config",
        auth="user", methods=["POST"], type="json",
    )
    def api_smarthome_route_access_config(
        self, workspace_id: int, smarthome_id: int, route_id: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """單一 route 的 Cloudflare Access 開關、登入方式與 session 長度。"""
        smarthome, error = self._get_workspace_smarthome_or_error(
            workspace_id, smarthome_id)
        if error:
            return error
        sh_route = self._sh_route(smarthome, route_id)
        if not sh_route:
            return {"success": False, "error": _("Route not found")}
        return self._update_sh_route_access_config(sh_route, kwargs)

    @route(
        "/api/workspaces/<int:workspace_id>/smarthomes/<int:smarthome_id>"
        "/access-policies",
        auth="user", methods=["POST"], type="json",
    )
    def api_smarthome_access_policies(
        self, workspace_id: int, smarthome_id: int, action: str = "list",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """本 Smart Home 的 Access policy CRUD ＋ reorder。"""
        if action == "list":
            # 讀：workspace 成員即可，且**不 sudo**——ir.rule 才是守線
            # （orm-vs-endpoint-acl-test-coverage）。非成員在上一步已被擋掉。
            smarthome, error = self._get_workspace_smarthome_or_error(
                workspace_id, smarthome_id, require_manage=False)
            if error:
                return error
            policies = request.env[_POLICY_MODEL].search(
                [("smart_home_id", "=", smarthome.id)])
            return {"success": True, "data": [p.to_dict() for p in policies]}

        smarthome, error = self._get_workspace_smarthome_or_error(
            workspace_id, smarthome_id)
        if error:
            return error
        if action == "create":
            return self._create_sh_access_policy(smarthome, kwargs)
        if action == "update":
            return self._update_sh_access_policy(
                smarthome, kwargs.get("policy_id"), kwargs)
        if action == "delete":
            return self._delete_sh_access_policy(
                smarthome, kwargs.get("policy_id"))
        if action == "reorder":
            return self._reorder_sh_access_policies(smarthome, kwargs)
        return {"success": False, "error": _("Unknown action: %(action)s", action=action)}

    # ---- Access Control helpers ----

    def _get_workspace_smarthome_or_error(
        self, workspace_id: int, smarthome_id: int,
        require_manage: bool = True,
    ):
        """解析 (workspace, smarthome) 並跑完授權四步；失敗回 (None, error)。

        單一裁決處：兩支 access 端點共用，免得其中一支日後少掉一步而另一支還在
        （SA 那邊 `refresh_status` 就曾因為被分在 read 那側而漏掉 role gate）。

        SH 釘在 `workspace_id` 上，所以 URL 換一個別人的 smarthome id 得到的是
        「Smart home not found」而不是別人的資料。
        """
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return None, {"success": False,
                          "error": _("Workspace not found or access denied")}
        smarthome = self._get_smarthome_or_error(smarthome_id, workspace)
        if not smarthome:
            return None, {"success": False, "error": _("Smart home not found")}
        if not require_manage:
            return smarthome, None
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return None, {"success": False,
                          "error": _("Insufficient permissions")}
        try:
            smarthome._check_operation_allowed(operation="write")
        except Exception as e:
            return None, {"success": False, "error": str(e)}
        return smarthome, None

    def _sh_route(self, smarthome, route_id):
        """釘在這台 SH 上的 route——跨 SH／未知 id 一律回空集。

        用 `search` 帶 `smart_home_id` 條件而不是 filter `smarthome.route_ids`：
        兩者等價，但 search 走 ir.rule 過濾後的可見集合，語意上「找不到」與
        「看不到」收斂成同一個回應。
        """
        return request.env[_ROUTE_MODEL].search(
            [("id", "=", route_id),
             ("smart_home_id", "=", smarthome.id)], limit=1)

    def _update_sh_route_access_config(self, sh_route, kwargs):
        """白名單寫入。

        🔴 只有這四個欄位是 client 設定得了的。`access_app_id` 另有 model 層的
        身分錨點守衛（對所有人生效），但**白名單仍然是必要的第一層**：守衛丟的
        是 `UserError`，那會讓一個順手多帶欄位的合法請求整個失敗，而不是靜靜忽
        略它；`access_unresolved_idp_types` 則完全沒有 model 層守衛，白名單是它
        唯一的防線。

        `is_protected` 用 `in kwargs` 判而不是 `kwargs.get()`：關閉保護送的是
        `False`，truthy 判斷會讓「關閉」這個動作整個消失（而回應仍是 success）。
        """
        vals = {}
        if "is_protected" in kwargs:
            vals["is_protected"] = bool(kwargs["is_protected"])
            # 沒有對外 hostname ⇒ 沒有可以掛 Access app 的錨點。不擋的話這個寫入
            # 會**成功**、UI 顯示「已保護」，而 `_sync_access_to_operator` 的第一
            # 道 gate 就 return，Cloudflare 上一個物件都不會有——一個對租戶而言
            # 完全無聲的謊。關閉方向（False）不擋：任何情況下都必須關得掉。
            if vals["is_protected"] and not sh_route.hostname:
                return {"success": False,
                        "error": _("This smart home has no public address to "
                                   "protect yet.")}
        for f in ("login_google", "login_email_otp"):
            if f in kwargs:
                vals[f] = bool(kwargs[f])
        if kwargs.get("session_duration"):
            vals["session_duration"] = kwargs["session_duration"]
        try:
            if vals:
                sh_route.write(vals)
            return {"success": True, "data": sh_route.to_dict()}
        except Exception as e:
            _logger.exception(
                "[SH] Failed to update access config route=%s: %s",
                sh_route.id, e)
            return {"success": False, "error": str(e)}

    def _sh_policy(self, smarthome, policy_id):
        """釘在這台 SH 上的 policy——跨 SH／未知 id 一律回空集。"""
        return request.env[_POLICY_MODEL].search(
            [("id", "=", policy_id),
             ("smart_home_id", "=", smarthome.id)], limit=1)

    def _create_sh_access_policy(self, smarthome, kwargs):
        if not kwargs.get("name"):
            return {"success": False, "error": _("name is required")}
        sh_route = self._sh_route(smarthome, kwargs.get("route_id"))
        if not sh_route:
            return {"success": False, "error": _("Route not found")}
        # 兩層 quota：model 的 `_check_policy_quota` 是權威，這裡是為了回一句人
        # 看得懂的話。**0 = unlimited**（#743）——沒有這個豁免，model 那邊的豁免
        # 從 UI 不可達（`existing >= quota` 在 0 >= 0 時第一條就擋掉），而 SH 的
        # 40 筆 AC 變體 `sa_max_access_policies` 全部走 default 0。
        # `sudo()` 只用在讀訂閱：租戶對 `sh.subscription.subscription` 沒有讀權。
        #
        # #1053：`existing` 的計數也走 `sudo()`，與 model 端 `_check_policy_quota`
        # 的池計數對齊（#896 已對 SA 錨點做同一件事，三個孿生體同進退）。額度是
        # **平台成本**，不是「這位呼叫者看得到多少」——`ir.rule` 只讓呼叫者看到
        # 池中一部分 policy 時，裸 `search_count` 會少算，而少算的症狀是「配額安
        # 靜地變寬」，與「方案的政策數上限很大」在輸出上完全不可區分。
        # ⛔ sudo 只包**這一次計數**：`smarthome` 仍由
        # `_get_workspace_smarthome_or_error` 以呼叫者的 env 把關（非成員在到達
        # 這裡前就被擋掉），`Policy.create()` 以下也一律留在呼叫者 env——本前置
        # 只是快速失敗，真正的守線是 model 那條 constraint。
        # （domain 以 `smart_home_id` 收斂 ⇒ sudo 只放寬可見度，不會把別的 SH／
        # 租戶的 policy 算進來。⚠ 這裡用 `smart_home_id` 而非 create_vals 的
        # `smart_home_route_id`：前者是 SH 級的池，與 model 端 quota 的池同一
        # 根，後者只是這一條 policy 掛在哪條 route。）
        Policy = request.env[_POLICY_MODEL]
        sub = smarthome.sudo().subscription_id
        if sub and sub.sh_subscription_plan_id:
            quota = sub.sh_subscription_plan_id.sa_max_access_policies
            existing = Policy.sudo().search_count(
                [("smart_home_id", "=", smarthome.id)])
            if quota and existing >= quota:
                return {"success": False,
                        "error": _("Access policy quota exceeded: plan allows "
                                   "%d polic(ies)") % quota}
        # 白名單：錨點由**已 gate 過的 route** 決定，不讀 kwargs 的其他欄位——
        # client 送什麼 `cf_policy_id` / 別的錨點都無效。arc 是 arity 檢查不是
        # ownership 檢查，一條只帶別人 route_id 的 policy 完全通得過 arc，所以
        # 這道白名單是唯一擋住它的地方。
        create_vals = {
            "smart_home_route_id": sh_route.id,
            "name": kwargs["name"],
            "decision": kwargs.get("decision", "allow"),
        }
        if "sequence" in kwargs:
            create_vals["sequence"] = kwargs["sequence"]
        rule_commands = self._build_sh_access_rule_commands(kwargs)
        if rule_commands is not None:
            create_vals["rule_ids"] = rule_commands
        try:
            pol = Policy.create(create_vals)
            return {"success": True, "data": pol.to_dict()}
        except Exception as e:
            _logger.exception(
                "[SH] Failed to create access policy sh=%s: %s",
                smarthome.id, e)
            return {"success": False, "error": str(e)}

    def _build_sh_access_rule_commands(self, kwargs):
        """include/require/exclude → One2many create commands。

        逐條白名單到 group/rule_type/rule_value：client 不得在子規則上夾帶
        `cf_policy_id` / `policy_id` / 任何其他欄位。三個 group key 一個都沒有
        ⇒ 回 None（不帶 `rule_ids`，交給 model 的 `_check_at_least_one_include`
        fail-loud）。刻意**不**接 SA 那條 legacy flat `rule_type`/`rule_value`
        相容層——那是 P-rule 過渡期的遺物，新路徑沒有 in-flight client 需要它。
        """
        if not any(g in kwargs for g in _ACCESS_RULE_GROUPS):
            return None
        commands = []
        for grp in _ACCESS_RULE_GROUPS:
            for r in kwargs.get(grp) or []:
                if not isinstance(r, dict):
                    continue
                commands.append((0, 0, {
                    "group": grp,
                    "rule_type": r.get("rule_type"),
                    "rule_value": r.get("rule_value"),
                }))
        return commands

    def _update_sh_access_policy(self, smarthome, policy_id, kwargs):
        pol = self._sh_policy(smarthome, policy_id)
        if not pol:
            return {"success": False, "error": _("Policy not found")}
        vals = {}
        for f in ("name", "decision", "sequence"):
            if f in kwargs:
                vals[f] = kwargs[f]
        write_pol = pol
        rule_commands = self._build_sh_access_rule_commands(kwargs)
        if rule_commands is not None:
            # 整組取代。`(5,0,0)` + creates 讓 email 上限只看新規則；中間那個空集
            # 會踩到「≥1 include」檢查，所以用 `sa_rule_replace` 抑制逐條檢查——
            # policy 層的 @api.constrains 驗最終狀態。
            vals["rule_ids"] = [(5, 0, 0)] + rule_commands
            write_pol = _rule_replace(pol)
        try:
            if vals:
                write_pol.write(vals)
            return {"success": True, "data": pol.to_dict()}
        except Exception as e:
            _logger.exception(
                "[SH] Failed to update access policy %s: %s", policy_id, e)
            return {"success": False, "error": str(e)}

    def _delete_sh_access_policy(self, smarthome, policy_id):
        pol = self._sh_policy(smarthome, policy_id)
        if not pol:
            return {"success": False, "error": _("Policy not found")}
        try:
            pol.unlink()
            return {"success": True, "data": {"message": _("Policy deleted")}}
        except Exception as e:
            _logger.exception(
                "[SH] Failed to delete access policy %s: %s", policy_id, e)
            return {"success": False, "error": str(e)}

    def _reorder_sh_access_policies(self, smarthome, kwargs):
        """重寫 precedence。逐筆寫入抑制 sync，結束後只推一次完整狀態。

        不抑制的話 N 條 policy 會打 N 次 operator，而且中間每一次送出去的都是
        **半排好**的順序。跨 SH／未知的 policy_id 靜默略過（絕不寫到別人的列），
        計數器走「被釘住的那些」而非原索引，略過一筆不會在號碼上留洞。

        推送的對象是**每一條被動到的 route**：SH 是 per-route 多錨點，重排可能
        跨多條 route（雖然今天恰好只有一條），對整台 SH 推一次會漏掉其他 route。
        """
        policy_ids = kwargs.get("policy_ids") or []
        if not isinstance(policy_ids, list):
            return {"success": False, "error": _("policy_ids must be a list")}
        reordered = []
        touched_routes = request.env[_ROUTE_MODEL]
        try:
            seq = 0
            for pid in policy_ids:
                pol = self._sh_policy(smarthome, pid)
                if not pol:
                    continue
                seq += 10
                _skip_route_sync(pol).write({"sequence": seq})
                reordered.append(pol.id)
                touched_routes |= pol.smart_home_route_id
            for sh_route in touched_routes:
                sh_route._sync_access_to_operator()
            return {"success": True, "data": {"reordered": reordered}}
        except Exception as e:
            _logger.exception(
                "[SH] Failed to reorder access policies sh=%s: %s",
                smarthome.id, e)
            return {"success": False, "error": str(e)}

    # ==================== Private Helpers ====================

    def _get_workspace_or_error(self, workspace_id: int):
        """Verify user has access to workspace and return it."""
        user = request.env.user
        # #1261：同 security_access.py 的 `_get_workspace_or_error`——portal/share
        # 帳號在非 sudo search 之前就回 None，否則 search 丟 AccessError、原文與
        # traceback 冒到前端；殘留的 portal 成員列也不再放行。
        # Portal callers are refused before the non-sudo search (#1261).
        if user.share:
            return None
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
            return {"success": False, "error": _("Name is required")}
        if not template_id:
            return {"success": False, "error": _("Smart Home template is required")}
        if not plan_id:
            return {"success": False, "error": _("Subscription plan is required")}

        # Validate template
        SmartHomeTemplate = request.env[
            "woow_paas_platform.smart_home_template"
        ].sudo()
        template = SmartHomeTemplate.browse(template_id)
        if not template.exists() or not template.is_active:
            return {
                "success": False,
                "error": _("Smart Home template not found or inactive"),
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
                "error": _("Template has no subscription plans configured"),
            }

        # Validate plan and its membership in the template's plan list
        Plan = request.env["sh.subscription.plan"].sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {"success": False, "error": _("Subscription plan not found")}
        if plan not in template.subscription_plan_ids:
            return {
                "success": False,
                "error": _("Selected plan is not available for this template"),
            }

        SmartHome = request.env["woow_paas_platform.smart_home"]
        vals = {
            "name": name,
            "workspace_id": workspace.id,
        }
        if ha_port:
            vals["ha_port"] = ha_port

        try:
            # #524：這是全 repo **唯一**合法的 smart_home 建立點——閘門在
            # `SmartHome.create()` 的第一行，開閘掛在這一次呼叫上（不掛在上面
            # 那個 model handle 的綁定處，見 `_platform_order` 的 docstring）。
            # 走到這裡之前，workspace 歸屬與 owner/admin 兩道閘已在
            # `api_smarthomes` 做完，`template`／`plan` 的驗證也已完成。
            smarthome = _platform_order(SmartHome).create(vals)
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
            return {"success": False, "error": _("Failed to create smart home. Please try again.")}

    def _delete_smarthome(self, smarthome) -> dict[str, Any]:
        """Delete a smart home and its tunnel."""
        try:
            smarthome.action_delete()
            return {"success": True, "data": {"message": _("Smart home deleted")}}
        except OperationalError:
            raise
        except (AccessDenied, AccessError, MissingError) as e:
            _logger.exception("Access error deleting smart home: %s", e)
            return {
                "success": False,
                "error": _("Failed to delete smart home. Please try again."),
            }
        except (ValidationError, UserError) as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            _logger.exception("Failed to delete smart home: %s", e)
            return {
                "success": False,
                "error": _("Failed to delete smart home. Please try again."),
            }

    def _provision_smarthome(self, smarthome) -> dict[str, Any]:
        """Provision a smart home tunnel.

        #739：驅動 `action_provision()` 的是**平台身分**，不是租戶身分。

        修前這裡拿的是 `browse(id)` 來的非 sudo 租戶 recordset，而
        `action_provision` 內部那次 `anchored.write(tunnel_vals)` 含
        `tunnel_token`——一個 `groups='base.group_system'` 的欄位
        （`models/smart_home.py` 的 `tunnel_token` 定義；行號會漂，grep 符號名）。
        Odoo 的 `write()` 會 `check_field_access_rights('write', vals.keys())`，
        所以**只掛 `base.group_user` 的租戶打這個端點必定 AccessError**，且
        `state='provisioning'` 已經落地 ⇒ 租戶經 `/woow` 的 provision 從來沒有
        成功過（#739 實測，真 HTTP、mock operator）。

        改用平台身分是對的層次，因為那次寫入的**值不是租戶給的**：
        `tunnel_vals` 整包來自 operator 對 `create_tunnel` 的回應加上伺服器端
        算出的 subdomain/hostname。憑證是平台鑄造的，本來就該由平台身分寫下去；
        租戶不需要、也不該擁有寫那個欄位的權限。

        ⚠ **這一行的作用範圍要講精確，不要縮小它**：`smarthome.sudo()
        .action_provision()` 之後，`action_provision` body 內**每一次 ORM 操作
        都在 `env.su` 底下**。對 model 而言這就是「整個 method 走 sudo」。
        （不要拿「下面的 `to_dict()` 沒有 `.sudo()`」當作範圍很窄的說明——
        `to_dict()` 本來就不在那個 method 裡面，那是錯的對照。）

        📌 **這裡曾經記著一個「兩個 sibling model 已分歧」的警告——#805 (b) 之後
        不再成立，但過程值得留著。** 本修法落地當下，
        `models/security_access.py::action_provision` 的 docstring 明文寫著
        「do NOT ... wrap the whole method in .sudo()」、並稱非 system caller
        撞 AccessError 是 "the intended fail-closed behaviour"；那段文字**沒有
        因為這個改動變紅**（`tests/test_security_access.py` 的
        `test_provision_token_write_fails_closed_for_non_system_owner` 驅動的是
        SA model 的 `with_user` recordset，不經過任何 controller）
        ⇒ **CI 全綠不代表沒有衝突**。#805 (b) 的入口盤點證實 SA 那一側同構
        （租戶經 `/woow` 的 provision 端點四環全通、且建立 SA 的 modal 自動串接
        它），裁定同款修法，`controllers/security_access.py` 的
        `action == "provision"` 分支現在也是 `sa.sudo().action_provision()`
        ⇒ 兩側在「誰驅動 provision」這一點上重新對齊。
        兩側各自的範圍限定仍不同，改任一側前讀該側自己的 docstring。
        完整攤開（含當時對方原文逐字、SH 側查無等價鎖的性質查、以及「`/woow` 的
        租戶 provision 端點該不該存在」這個要產品裁決的問題）寫在 PR #797。

        那段 docstring 點名的傷害是「widen the token write/**read** surface」，
        兩半要分開看：**write 面確實被放寬了**（緩解是「值不由租戶選」，見上
        一段與 B7）；**read 面**它自己列的三層防線（欄位上的 `groups=`、
        `to_dict()` 的省略、專屬 sudo 讀端點）沒有一層在 `action_provision`
        裡面，su 環境隨那次呼叫結束，B5／B6 釘住。

        ⚠ 殘留的那一格，機制要講精確（這裡是實測過的版本，兩個聽起來合理的
        錯誤版本都不要再寫回來）：
          * su 呼叫**內** `self.tunnel_token` 讀得到，是因為 **`su` 把欄位級
            遮罩整條短路掉**（`odoo/models.py:3783-3784`、`odoo/fields.py:956`
            ＋ `odoo/api.py:672`）——**不是**因為快取。
          * 但快取**另外**構成一格真的殘留：su 那次呼叫會把該值餵進同一筆交易
            內跨 env 共用的欄位快取，之後**非 sudo** recordset 用**屬性直讀**
            碰它時，`Field.__get__` 的 cache-hit 分支（`odoo/fields.py:1239`）
            不會再查遮罩；cache **miss** 才會（`:1266` → `_fetch_field`，其第一
            行是 `check_field_access_rights('read', ...)`，`models.py:4099`）。
            ⚠ 只掃 `fields.py` 的 `__get__` 函式體會找不到 `is_accessible`／
            `groups`（檢查在下一層、另一個檔），因而得到「屬性直讀從不查
            groups」這個**相反且錯誤**的結論。
          * `odoo shell` 實測（同一交易、同一筆記錄、租戶只掛 `base.group_user`）：
            cold ⇒ `AccessError`；先用 su 讀一次餵熱快取後同樣的屬性直讀
            ⇒ **不 raise**、拿得到值；快取仍熱但走 `read([...])` ⇒ `AccessError`。
          * ⇒ 「讀側完全沒動」**不成立**；成立的是「**同一個請求裡沒有任何
            程式碼去讀那個屬性**」（`to_dict()` 不含它，經 RPC 的 `read()` 一定
            重查——B5 釘的就是這條）。

        改用平台身分之後，Odoo 那三層在**這個 method 內**確實全部停用
        （`env.su` ⇒ `check_access` 與 `check_field_access_rights` 都提早
        return）。逐條交代誰接手，每一條都有對應的回歸測試
        （`tests/test_sh_provision_tenant_rpc_739.py` 的 B 系列）：

        * `ir.model.access` write —— `base.group_user` 本來就是 `1,1,1,0`，
          停用它不改變任何結果。
        * `ir.rule smart_home_rule_owner_admin_manage`（write）—— 由上面
          `api_smarthome_detail` 的 owner/admin 閘接手，而且**更嚴**：那道閘
          另外要求 workspace `state='active'`（`_get_workspace_or_error`），
          rule 不管這件事。B1 釘住。
        * 「這筆是不是你的」—— 兩道都在 `.sudo()` **之外**：
          `_get_workspace_or_error`（B2）與 `_get_smarthome_or_error`（B3；
          實測是 ir.rule `member_read` 在 `workspace_id` 那次讀上先擋）。
        * 訂閱生效閘 —— `_check_operation_allowed` 也在 `.sudo()` 之外，且
          Odoo 的 `sudo()` **不改變 `env.user`**，所以它開頭那個
          `has_group('base.group_system')` 提早 return 不會因此變 True。B4 釘住。
        * 身分錨點不可變守衛 —— 明示無 su 豁免（`_INTERNAL_IDENTITY_WRITE`
          比對物件身分），sudo 打不開。B7 釘住。
        * 重複 provision 的 stored-id guard —— 純業務邏輯，與身分無關。B8 釘住。

        ⚠ 未隨本修法消失的既有形狀：下面的 `except Exception` 仍會把任何
        非 operator 類例外收成 `{"success": False}` 回 HTTP 200，而
        `state='provisioning'` 已落地 ⇒ 記錄仍可能停在中途狀態。本票只拿掉了
        那個形狀在 #739 上的**觸發源**，沒有動形狀本身（C1 釘住現況）。
        """
        try:
            smarthome.sudo().action_provision()
            # #768：與 detail 端點同一個形狀。provision 成功當下才剛建出 primary
            # route，前端要拿它把 Access 分頁畫出來；回 detail 以外的形狀會讓
            # 呼叫端在「沒有 routes」與「routes 是空的」之間分不出來。
            #
            # 「同一個形狀」也包含 tunnel 欄位的即時疊加（只讀，同 detail `get`）：
            # 詳情頁的 Retry setup 成功後直接 `state.home = result.data`，而收斂
            # （`_converge_existing_tunnel`）只寫 state／error_message／deployed_at、
            # 從不寫 tunnel_status／connector_* ⇒ 不疊的話 Tunnel Information 會從
            # 即時值倒退回上一次 Refresh 寫入的舊值，直到整頁重載。
            data = smarthome.to_dict(include_details=True)
            try:
                data.update(smarthome._live_tunnel_status())
            except Exception:
                # 開通已經成功（上面那行的寫入已經在這個交易裡）；疊加失敗只退回
                # DB 值，不得讓下面的 `except Exception` 把一次成功的開通回報成失敗。
                _logger.exception(
                    "Live tunnel status overlay failed after provision sh=%s",
                    smarthome.id)
            return {"success": True, "data": data}
        except Exception as e:
            _logger.error("Failed to provision smart home: %s", e)
            return {"success": False, "error": str(e)}

    def _refresh_status(self, smarthome) -> dict[str, Any]:
        """Refresh tunnel status."""
        try:
            smarthome.action_refresh_status()
            # #768：同 provision——refresh 是詳情頁按下去的，回的必須是 detail 形狀，
            # 否則按一次 Refresh 就會把前端手上的 routes 洗掉。
            return {"success": True,
                    "data": smarthome.to_dict(include_details=True)}
        except Exception as e:
            _logger.error("Failed to refresh smart home status: %s", e)
            return {"success": False, "error": str(e)}
