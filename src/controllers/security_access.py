"""Security Access API controller.

JSON-RPC endpoints for Security Access CRUD within a workspace context.
Mirrors controllers/smart_home.py gating: _get_workspace_or_error for
visibility and owner/admin role checks for mutations. Internal callers retain
ORM rules; Portal uses the private, context-cleaned PaaS capability boundary.
All results remain explicit DTOs, not raw ORM reads.
"""
from __future__ import annotations

from ..services.access_state import access_mutation_result
import logging
from typing import Any

from odoo.http import request, route, Controller

from ..models.workspace_access import ROLE_OWNER, ROLE_ADMIN
from ..services.paas_capability import _PaaSCapability
# #536：sync-control 的兩個逃生門只認模組私有 sentinel（`call_kw` 把呼叫端
# context 整包換進 env，只判 truthy 的 key 租戶自己就打得開）。
from ..models.security_access import _rule_replace, _skip_route_sync
# 建立閘（同 smart_home #524）：本檔是 security_access 唯一的租戶側建立點。
from ..models.security_access import _sa_platform_order
from odoo import _

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
                    "error": _("Workspace not found or access denied")}

        if action == "list":
            return self._list_security_accesses(workspace)
        elif action == "create":
            access = _PaaSCapability(request.env).access(workspace)
            if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {"success": False, "error": _("Insufficient permissions")}
            return self._create_security_access(
                workspace, name,
                kwargs.get("plan_id"), kwargs.get("template_id"),
            )
        return {"success": False, "error": _("Unknown action: %(action)s", action=action)}

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
                    "error": _("Workspace not found or access denied")}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": _("Security access not found")}

        if action == "get":
            # 概覽頁的 tunnel 欄位改用即時值（只讀，見 _live_tunnel_status）；
            # 拿不到時沿用 DB 裡上一次寫入的值。
            data = sa.to_dict()
            data.update(sa._live_tunnel_status())
            return {"success": True, "data": data}

        # mutations below require owner/admin. refresh_status is grouped here
        # (not with read 'get'): when tunnel_id is set, action_refresh_status
        # does sa.write(...) which the SA write ir.rule
        # (security_access_rule_owner_admin_manage) only grants to
        # owner/admin — a role='user' member would hit a raw AccessError that
        # str(e) would then leak (model technical name / rule name) and make
        # behaviour state-dependent (succeeds while unprovisioned, fails once
        # provisioned). Gate it consistently with the other mutations.
        access = _PaaSCapability(request.env).access(workspace)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": _("Insufficient permissions")}

        if action == "refresh_status":
            try:
                sa.action_refresh_status()
            except Exception:
                _logger.exception(
                    "[SA] refresh_status failed sa=%s", sa.id)
                return {"success": False,
                        "error": _("Failed to refresh status. Please try again.")}
            return {"success": True, "data": sa.to_dict()}
        elif action == "release_binding":
            # #1766：鏡像 smart_home 的同名 action（兩側必須同步，見
            # `two-layer-acl-lockstep`）。解綁只是讓下一台能接手；對方手上的權杖
            # 仍然有效，要斷開只能換權杖。
            sa.action_release_binding()
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
            data = sa.to_dict()
            if sa.state == "error":
                return {"success": False, "data": data,
                        "error": sa.error_message or _("Provisioning failed")}
            return {"success": True, "data": data}
        elif action == "delete":
            try:
                sa._check_operation_allowed(operation='write')
                sa.action_delete()
            except Exception as e:
                return {"success": False, "error": str(e)}
            return {"success": True,
                    "data": {"message": _("Security access deleted")}}
        return {"success": False, "error": _("Unknown action: %(action)s", action=action)}

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
                    "error": _("Workspace not found or access denied")}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": _("Security access not found")}

        if action == "list":
            return {"success": True,
                    "data": [r.to_dict() for r in sa.route_ids]}

        if action == "health":
            # #1234：唯讀，與 list 同權限（workspace 成員即可）。探測目標只取自
            # DB 裡的 route.hostname，不接受任何呼叫端給的網址。
            return {"success": True,
                    "data": sa._probe_route_health(force=bool(kwargs.get("force")))}

        if action == "traffic":
            # #1234：唯讀，同 list／health 的權限。
            return {"success": True, "data": sa._route_traffic()}

        access = _PaaSCapability(request.env).access(workspace)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": _("Insufficient permissions")}
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
        return {"success": False, "error": _("Unknown action: %(action)s", action=action)}

    # ==================== Availability (platform-level) ====================

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>"
        "/tunnel-token",
        auth="user", methods=["POST"], type="json",
    )
    def api_security_access_tunnel_token(
        self, workspace_id: int, sa_id: int, **kwargs: Any,
    ) -> dict[str, Any]:
        """把 tunnel token 交給租戶（#1196）。

        為什麼需要這個端點：`security_access.tunnel_token` 的欄位定義帶
        ``groups='base.group_system'``，租戶在 ORM 層就讀不到；而既有的
        `/api/security-access/accesses/<id>/tunnel-token` 是 ``auth='none'`` +
        OAuth scope，設計給 HA custom component 這類**機器**消費者呼叫的。
        結果是想用 Cloudflared Web GUI addon／`docker run cloudflared` 自行接線
        的使用者，平台沒有任何路徑把 token 交給他（實測時只能去讀資料庫）。

        tunnel 可任意轉移是本產品刻意的定位（見 #1196），而「能複製 token」
        是轉移的前提，所以這條路必須存在。

        **刻意不放寬欄位的 groups**：那會讓 token 在所有 ORM 讀取路徑上對一般
        使用者現形（含 to_dict、報表、export）。這裡只開一個出口，並且比照
        route 的寫入權限收斂到 owner/admin——能改 route 的人本來就能刪掉整個
        SA 重建拿到新 token，給他讀取權不擴大實際攻擊面。
        """
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": _("Workspace not found or access denied")}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": _("Security access not found")}

        access = _PaaSCapability(request.env).access(workspace)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": _("Insufficient permissions")}

        # sudo() 只為了跨過 `tunnel_token` 欄位的 `groups='base.group_system'`；
        # 上面三道閘門已經確認呼叫者是這個 workspace 的 owner/admin 且這個 SA
        # 屬於該 workspace。整個端點**只有這一個** sudo 呼叫點（repo-lint 的
        # controller-sudo baseline 以此為準）。
        sa_su = sa.sudo()
        token = sa_su.tunnel_token or ""
        if not token:
            return {"success": False,
                    "error": _("Tunnel is not provisioned yet")}
        return {
            "success": True,
            "data": {
                "tunnel_token": token,
                "tunnel_id": sa_su.tunnel_id or "",
                "tunnel_name": sa_su.tunnel_name or "",
            },
        }

    @route(
        "/api/security-access/routes/check",
        auth="user", methods=["POST"], type="json",
    )
    def api_route_check(self, prefix: str | None = None,
                        **kwargs: Any) -> dict[str, Any]:
        if not prefix:
            return {"success": False, "error": _("prefix is required")}
        return self._check_route_prefix(prefix)

    # ==================== Templates / Plans (platform-level) ============

    @route(
        "/api/security-access/templates",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_templates(self, **kwargs: Any) -> dict[str, Any]:
        """Catalog data — sudo justified (mirrors smart_home templates)."""
        Template = request.env[
            "woow_paas_platform.security_access_template"].with_context({}).sudo()
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
            "woow_paas_platform.security_access_template"].with_context({}).sudo()
        template = Template.browse(template_id)
        if not template.exists() or not template.is_active:
            return {"success": False,
                    "error": _("Security Access template not found or inactive")}
        if not template.subscription_plan_ids:
            return {"success": False,
                    "error": _("Template has no subscription plans configured")}
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
                    "error": _("Workspace not found or access denied")}
        access = _PaaSCapability(request.env).access(workspace)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": _("Insufficient permissions")}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": _("Security access not found")}
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
                    "error": _("Workspace not found or access denied")}
        access = _PaaSCapability(request.env).access(workspace)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": _("Insufficient permissions")}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": _("Security access not found")}
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
                           plan_id: int | None = None, dry_run: bool = False,
                           **kwargs: Any) -> dict[str, Any]:
        """完全鏡像 smart_home.api_smarthome_change_plan 的流程與 sudo 理由：
        refund 計算 → 退 wallet → close 舊訂閱 → 開新訂閱。"""
        if not plan_id:
            return {"success": False, "error": _("plan_id is required")}
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": _("Workspace not found or access denied")}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": _("Security access not found")}
        access = _PaaSCapability(request.env).access(workspace)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": _("Insufficient permissions")}

        # sudo: portal users lack ACL on sh.subscription.plan; catalog data,
        # and the workspace+role check above already gates who can change a
        # security access, so this widening is narrow and intentional
        # (mirrors smart_home.api_smarthome_change_plan).
        Plan = request.env["sh.subscription.plan"].with_context({}).sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {"success": False, "error": _("Plan not found")}
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
                    "error": _("Selected plan is not available for this template"),
                }
        elif (plan.service_type != "security_access"
                or "security_access" not in plan._owning_template_families()):
            return {"success": False,
                    "error": _("Selected plan is not a valid Security Access plan")}

        # sudo: workspace owners typically lack ACL on
        # sh.subscription.subscription; the workspace+role check above already
        # gates who drives change_plan, so this narrow widening mirrors
        # smart_home.api_smarthome_change_plan's cascade.
        old_sub = sa.sudo().subscription_id
        if not old_sub or old_sub.state != "in_progress":
            return {"success": False, "error": _("No active subscription")}
        if not old_sub.sh_trial:
            if not old_sub.sh_end_date or not old_sub.sh_start_date:
                return {"success": False,
                        "error": _("Subscription dates are incomplete")}

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
        # #1261 的「0 席 ＋ 仍受保護」那一種拒絕**不適用** SA（SA 的 Access 分頁不看
        # 席位、永遠關得掉保護），由 `_SEAT_GATED_PROTECTION_ANCHORS` 排除，不是漏接。
        _denial = request.env['woow_paas_platform.security_access_access_policy']\
            ._seat_gate_denial(sa, plan)
        if _denial:
            # #1261：`data` 原樣轉發 error 以外的欄位（`code`／current_seats／
            # target_seats，保護那種另帶 `protected_hostnames`）。不逐 key 挑：
            # gate 多一種拒絕時三個端點各挑一次就是漂移的起點。
            return {'success': False,
                    'error': _denial['error'],
                    'data': {k: v for k, v in _denial.items() if k != 'error'}}

        # #1261 route 數降級 gate：與上面 #773 席位 gate 同構、同位置（第一個寫入
        # ——退點——之前）。prod E2E 實撞：3 條 route 從「3 組」降到「1 組」成功，
        # 記錄永久卡在「3 / 1 routes」——route 配額守衛是 route 寫入時才跑的
        # constraint，換方案不寫 route ⇒ 從來沒被叫醒。⛔ 系統不自動刪 route
        # （同 #773 裁決），只擋住並回報要刪到幾條；判準／0＝不准的語意見
        # `_route_gate_denial` docstring。排在席位 gate 之後只是維持既有錯誤優先序，
        # 兩者都不寫 DB，順序不影響正確性。
        _route_denial = sa._route_gate_denial(plan)
        if _route_denial:
            return {'success': False,
                    'error': _route_denial['error'],
                    'data': {'current_routes': _route_denial['current_routes'],
                             'target_routes': _route_denial['target_routes']}}

        refund = old_sub._compute_unused_refund()
        # AUDITFIX change-plan-insufficient-balance（#1677 A1）：錢包＋這次退點付不起新方案
        # 首期（含稅）就擋下——什麼都不寫（不退點、不關舊訂閱、不建新訂閱、不開發票）。
        # `dry_run=True`：通過同一組閘之後只回估算（前端停用「確認變更」並顯示差額）。
        # 判準收在 mixin 的 `_change_plan_balance_gate`，三個入口共用、這裡不另寫。
        _balance = sa._change_plan_balance_gate(plan, refund, dry_run=dry_run is True)
        if _balance:
            return _balance

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
            # #1555：舊訂閱那一期的發票同步開貸項並以退點沖銷（只記帳，失敗不外拋）。
            old_sub._woow_record_refund_credit_note(refund, 'Plan change')
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
        workspace = _PaaSCapability(request.env).workspace(workspace_id)
        return workspace if workspace and workspace.state == 'active' else None

    def _get_sa_or_error(self, sa_id: int, workspace):
        """Typed SA lookup pinned to a verified workspace.
        Internal callers retain ORM rules; Portal uses the private capability.
        """
        return _PaaSCapability(request.env).security_access(workspace, sa_id)

    def _list_security_accesses(self, workspace) -> dict[str, Any]:
        SecurityAccess = workspace.env["woow_paas_platform.security_access"]
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
            return {"success": False, "error": _("Name is required")}
        if not template_id:
            return {"success": False,
                    "error": _("Security Access template is required")}
        if not plan_id:
            return {"success": False,
                    "error": _("Subscription plan is required")}

        Template = request.env[
            "woow_paas_platform.security_access_template"].with_context({}).sudo()
        template = Template.browse(template_id)
        if not template.exists() or not template.is_active:
            return {"success": False,
                    "error": _("Security Access template not found or inactive")}
        if not template.subscription_plan_ids:
            return {"success": False,
                    "error": _("Template has no subscription plans configured")}

        # sudo: catalog data, portal users lack plan ACL (mirrors smart_home)
        Plan = request.env["sh.subscription.plan"].with_context({}).sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {"success": False, "error": _("Subscription plan not found")}
        if plan not in template.subscription_plan_ids:
            return {"success": False,
                    "error": _("Selected plan is not available for this template")}

        SecurityAccess = workspace.env["woow_paas_platform.security_access"]
        try:
            # 建立閘只開在這一次 create 上（模型層 `_PLATFORM_SA_ORDER`）；
            # 之後的操作改用不帶開閘 context 的 handle，逃生口的表面積只留在
            # 真正的落地點。internal 沿用呼叫者 ORM/rules；Portal 的窄範圍
            # recordset 已由 capability＋本端點驗過 workspace／owner-admin，
            # 保留原 uid，但不可把其 sudo 誤稱為仍由 ir.rule 授權。
            created = _sa_platform_order(SecurityAccess).create({
                "name": name,
                "workspace_id": workspace.id,
                "template_id": template.id,
            })
            sa = SecurityAccess.browse(created.id)
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
                    "error": _("Failed to create security access. Please try again.")}

    def _check_route_prefix(self, prefix: str) -> dict[str, Any]:
        """Format check first, then two-layer availability: Odoo DB, Cloudflare DNS."""
        Route = request.env[
            "woow_paas_platform.security_access_route"].with_context({}).sudo()
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
                             "reason": _("already used by another route")}}
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
                                 "reason": _("Could not confirm availability "
                                             "(operator not configured); "
                                             "try again later.")}}
            result = client.check_route_availability(prefix)
            # Operator returns status: free / taken / unknown. unknown
            # means the CF API errored (no longer silently "available").
            if result.get("status") == "unknown":
                return {"success": True,
                        "data": {"prefix": prefix, "available": False,
                                 "reason": _("Could not confirm availability "
                                             "(Cloudflare check unavailable); "
                                             "try again.")}}
            if not result.get("available", True):
                return {"success": True,
                        "data": {"prefix": prefix, "available": False,
                                 "reason": _("DNS record already exists")}}
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
                             "reason": _("Could not confirm availability "
                                         "(operator unavailable); try again.")}}
        return {"success": True,
                "data": {"prefix": prefix, "available": True}}

    def _create_route(self, sa, subdomain_prefix, service_url,
                      http_host_header) -> dict[str, Any]:
        if not subdomain_prefix:
            return {"success": False, "error": _("subdomain_prefix is required")}

        # two-layer quota: constraint re-checks at ORM layer
        sub = sa.sudo().subscription_id
        if sub and sub.sh_subscription_plan_id:
            quota = sub.sh_subscription_plan_id.sa_max_routes
            # #1054：與 model 端 `_check_route_quota` 同形的 `len(o2m)`（同一
            # 個機制，見那邊的註解）。這一層更不會少算：呼叫者一律先過
            # `_get_workspace_or_error()`（要有該 workspace 的 workspace_access
            # 列）⇒ 必是成員 ⇒ route 的 ir.rule 讓他看得到整池。⛔ 兩層要
            # lock-step：model 端哪天補上 sudo，這裡也要一起補，否則前置與
            # constraint 的口徑分歧會是靜默的。守線＝
            # `src/tests/test_quota_len_o2m_reach_1054.py`。
            count = len(sa.route_ids)
            if count >= quota:
                return {"success": False,
                        "error": _("Route quota exceeded: plan allows %d "
                                   "route(s)") % quota}

        check = self._check_route_prefix(subdomain_prefix)
        if not check["data"]["available"]:
            return {"success": False,
                    "error": _("Subdomain prefix is not available: %s")
                             % check["data"].get("reason", "taken")}

        Route = sa.env["woow_paas_platform.security_access_route"]
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
            return {"success": False, "error": _("Route not found")}
        vals = {}
        if "service_url" in kwargs and kwargs["service_url"]:
            vals["service_url"] = kwargs["service_url"]
        if "http_host_header" in kwargs:
            vals["http_host_header"] = bool(kwargs["http_host_header"])
        if "is_protected" in kwargs:
            vals["is_protected"] = bool(kwargs["is_protected"])
        # #1196：改名。DB 有 UNIQUE(subdomain_prefix) 擋得住平台內部撞名，但擋不住
        # 「這個名字在 Cloudflare 上已經有一筆別的 DNS 記錄」——那種情況要等到
        # operator 同步時才炸，而那時 savepoint 會把改名整個回捲、使用者只看到一句
        # 看不懂的 operator 錯誤。所以在寫入前先問一次可用性，與 create 同一條
        # `_check_route_prefix`（格式 + DB + Cloudflare 三層一次問完）。
        new_prefix = kwargs.get("subdomain_prefix")
        if new_prefix and new_prefix != route_rec.subdomain_prefix:
            check = self._check_route_prefix(new_prefix)
            if not check.get("success"):
                return check
            if not check["data"]["available"]:
                return {"success": False,
                        "error": _("Subdomain prefix is not available: %s")
                                 % check["data"].get("reason", "taken")}
            vals["subdomain_prefix"] = new_prefix
        try:
            if vals:
                route_rec.write(vals)
            return {"success": True, "data": route_rec.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _delete_route(self, sa, route_id) -> dict[str, Any]:
        route_rec = sa.route_ids.filtered(lambda r: r.id == route_id)
        if not route_rec:
            return {"success": False, "error": _("Route not found")}
        try:
            route_rec.unlink()
            return {"success": True, "data": {"message": _("Route deleted")}}
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
                    "error": _("Workspace not found or access denied")}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": _("Security access not found")}
        access = _PaaSCapability(request.env).access(workspace)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": _("Insufficient permissions")}
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
                    "error": _("Workspace not found or access denied")}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": _("Security access not found")}

        if action == "list":
            # The resolved SA supplies the environment: internal ORM rules,
            # or Portal's context-cleaned capability. Always pin to this SA;
            # never query an unrestricted sudo policy pool.
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
            policies = sa.env[
                "woow_paas_platform.security_access_access_policy"].search(
                    domain)
            return {"success": True,
                    "data": [p.to_dict() for p in policies]}

        access = _PaaSCapability(request.env).access(workspace)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": _("Insufficient permissions")}
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
        return {"success": False, "error": _("Unknown action: %(action)s", action=action)}

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
                    "error": _("Workspace not found or access denied")}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": _("Security access not found")}
        access = _PaaSCapability(request.env).access(workspace)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": _("Insufficient permissions")}
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
        return _PaaSCapability(request.env).security_access_route(sa, rid)

    def _update_access_config(self, sa, kwargs) -> dict[str, Any]:
        # Whitelist only: never pass raw kwargs to write() — access_app_id and
        # other server-owned fields must not be client-settable. P2.5 maps the
        # legacy SA-level master toggle (access_enabled) onto the primary
        # route's is_protected.
        route = self._sa_access_route(sa)
        if not route:
            return {"success": False,
                    "error": _("No application route to configure yet")}
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
            return access_mutation_result(route, sa.to_dict())
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
            return {"success": False, "error": _("Route not found")}
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
            return access_mutation_result(route, route.to_dict())
        except Exception as e:
            _logger.exception("[SA] Failed to update route access: %s", e)
            return {"success": False, "error": str(e)}

    def _create_policy(self, sa, kwargs) -> dict[str, Any]:
        if not kwargs.get("name"):
            return {"success": False, "error": _("name is required")}
        # P2.5 Phase 3: a client may target ANY of the gated SA's routes via
        # route_id (pinned to the SA). A cross-SA / unknown route_id is rejected
        # — never written to another SA's route. Absent route_id, fall back to
        # the SA's primary route (P2.5 bridge for the not-yet-migrated frontend).
        if kwargs.get("route_id"):
            route = self._sa_route(sa, kwargs["route_id"])
            if not route:
                return {"success": False, "error": _("Route not found")}
        else:
            route = self._sa_access_route(sa)
        if not route:
            return {"success": False,
                    "error": _("No application route to attach a policy to yet")}
        # two-layer quota: constraint re-checks at ORM layer (lock-step with
        # the model's _check_policy_quota — an SA-LEVEL total). sudo on the
        # subscription_id read: portal owners lack ACL on the subscription.
        #
        # #896：`existing` 的計數也走 `sudo()`，與 model 端
        # `_check_policy_quota` 的池計數對齊（同一天一起改）。額度是**平台
        # 成本**，不是「這位呼叫者看得到多少」——`ir.rule` 只讓呼叫者看到池中
        # 一部分 policy 時，裸 `search_count` 會少算，而少算的症狀是「配額安靜
        # 地變寬」，與「方案的政策數上限很大」在輸出上完全不可區分。
        # Internal calls keep their ORM environment. Portal creation uses
        # the resolved SA capability only after the dispatcher role gate;
        # model quota constraints still run in both environments.
        # （domain 以 `security_access_id` 收斂 ⇒ sudo 只放寬可見度，不會把別
        # 的 SA／租戶的 policy 算進來。）
        #
        # quota 0 = unlimited (#743), in step with the model constraint. This
        # pre-check runs BEFORE Policy.create(), so without the exemption here
        # the model's exemption is unreachable from the UI: `existing >= quota`
        # is 0 >= 0 on the very first policy of a zero-quota plan, and the
        # request never gets far enough for the constraint to have an opinion.
        # (This is only about sa_max_access_policies — the route pre-check above
        # keeps sa_max_routes at 0 = none allowed.)
        sub = sa.sudo().subscription_id
        Policy = sa.env[
            "woow_paas_platform.security_access_access_policy"]
        if sub and sub.sh_subscription_plan_id:
            quota = sub.sh_subscription_plan_id.sa_max_access_policies
            existing = Policy.sudo().search_count(
                [("security_access_id", "=", sa.id)])
            if quota and existing >= quota:
                return {"success": False,
                        "error": _("Access policy quota exceeded: plan allows "
                                   "%d polic(ies)") % quota}
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
            return access_mutation_result(route, pol.to_dict())
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
            return {"success": False, "error": _("policy_ids must be a list")}
        reordered = []
        routes = sa.env["woow_paas_platform.security_access_route"]
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
            return access_mutation_result(routes, {"reordered": reordered})
        except Exception as e:
            _logger.exception("[SA] Failed to reorder policies: %s", e)
            return {"success": False, "error": str(e)}

    def _sa_policy(self, sa, policy_id):
        """A policy under this SA (any of its routes), looked up via the stored
        related security_access_id so it cannot reach across SAs."""
        return _PaaSCapability(request.env).security_access_policy(sa, policy_id)

    def _update_policy(self, sa, policy_id, kwargs) -> dict[str, Any]:
        pol = self._sa_policy(sa, policy_id)
        if not pol:
            return {"success": False, "error": _("Policy not found")}
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
            return access_mutation_result(pol.route_id, pol.to_dict())
        except Exception as e:
            _logger.exception("[SA] Failed to update access policy: %s", e)
            return {"success": False, "error": str(e)}

    def _delete_policy(self, sa, policy_id) -> dict[str, Any]:
        pol = self._sa_policy(sa, policy_id)
        if not pol:
            return {"success": False, "error": _("Policy not found")}
        route = pol.route_id
        try:
            pol.unlink()
            return access_mutation_result(route, {"message": _("Policy deleted")})
        except Exception as e:
            _logger.exception("[SA] Failed to delete access policy: %s", e)
            return {"success": False, "error": str(e)}
