from __future__ import annotations

from typing import Any, Union

from odoo import models, fields, api
from odoo.exceptions import AccessError, ValidationError
import re

from .workspace_access import ROLE_OWNER, ROLE_HIERARCHY


class Workspace(models.Model):
    _name = 'woow_paas_platform.workspace'
    _description = 'PaaS Workspace'
    _order = 'create_date desc'

    # Basic Information
    name = fields.Char(string='Name', required=True, index=True)
    description = fields.Text(string='Description')
    slug = fields.Char(
        string='Slug',
        index=True,
        help='URL-friendly unique identifier'
    )

    _sql_constraints = [
        ('unique_slug', 'UNIQUE(slug)', 'Slug must be unique.'),
    ]

    # Ownership
    owner_id = fields.Many2one(
        'res.users',
        string='Owner',
        required=True,
        default=lambda self: self.env.user,
        ondelete='restrict',
        index=True
    )

    # State
    state = fields.Selection([
        ('active', 'Active'),
        ('archived', 'Archived'),
    ], string='State', default='active', required=True, index=True)

    # Access Control
    access_ids = fields.One2many(
        'woow_paas_platform.workspace_access',
        'workspace_id',
        string='Access Controls'
    )

    # Cloud Services
    service_ids = fields.One2many(
        'woow_paas_platform.cloud_service',
        'workspace_id',
        string='Cloud Services',
        help='Cloud services deployed in this workspace',
    )

    # Computed fields
    member_count = fields.Integer(
        string='Member Count',
        compute='_compute_member_count',
        store=True
    )

    @api.depends('access_ids')
    def _compute_member_count(self) -> None:
        for workspace in self:
            workspace.member_count = len(workspace.access_ids)

    # ------------------------------------------------------------------ #
    # Identity anchors (#658)                                             #
    # ------------------------------------------------------------------ #
    def _is_privileged_writer(self) -> bool:
        """本檔兩道守衛共用的豁免判準：superuser 或 base.group_system。

        `env.is_superuser()` 涵蓋 uid == SUPERUSER_ID 與任何 `sudo()` recordset
        （`Workspace.create()` 內建 owner access 列的 bootstrap、`ha_paas.py` 的
        auto-provision、`unlink()` 的子資源清理都走 sudo）。`base.group_system`
        則對齊本 repo 其他 model 的 `*_rule_admin_full` 慣例。

        ⚠ 這是 su-env-exempt-guard 家族：豁免的兩種身分都**不是**租戶可以取得的
        身分，所以測試必須走 HttpCase＋真實租戶帳號。用 `TransactionCase` 寫的
        測試 env.uid == SUPERUSER_ID ⇒ 這裡永遠回 True ⇒ 下面兩道守衛的 deny
        分支結構上不可達，整組測試會是**假綠**。
        """
        return self.env.is_superuser() or self.env.user.has_group('base.group_system')

    def _check_tenant_create_anchors(self, resolved: dict[str, Any]) -> None:
        """租戶 create 方向不得自行指定 `slug` / 別人的 `owner_id`（#658）。

        `resolved` 必須是經 `_add_missing_default_values()` 解析過的 vals ——
        **不可以**直接看 raw vals。context 的 `default_<field>` 是 RPC 用戶端
        可控的（`odoo/api.py::call_kw` 是整份取代 context、不是 merge），而
        default 要到 `super().create()` 內部的 `_prepare_create_values` →
        `_add_missing_default_values` → `default_get` 才會補上，`default_get`
        第一順位讀的就是 context 的 `default_<field>`。所以只看 raw vals 的
        守衛，攻擊者用 `with_context(default_slug=...)` 且 vals 不放該鍵就整道
        繞過（#657 F2／#664 的同型）。這裡呼叫 Odoo 自己那支解析函式而不是逐
        欄位重演它的規則——逐欄位重演會漏下一個欄位（#657 的同一行修了兩次）。

        `slug`：伺服器決定，只能由 `_generate_slug()` 產生。它是
        `make_namespace()` / `make_service_subdomain()`（`src/services/naming.py`）
        的唯一輸入，而兩者都是 `md5(slug)[:8]` ＝ **32 bit 截斷**：`UNIQUE(slug)`
        只擋得住字面相同的 slug，擋不住兩個不同 slug 映到同一個 namespace／
        subdomain。實測通用碰撞在 137,426 個候選內就出現（`ws-83256` 與
        `ws-137425` 同為 `7d82ce00`）；針對特定受害者的 32-bit preimage 期望
        2^32 次 md5，用 python hashlib 單執行緒實測約 0.6 小時，換 C／GPU 是
        分鐘級。⇒ 讓租戶指定 slug ＝ 讓租戶指定自己的服務要部署進哪個 K8s
        namespace。這不是「防漂移」，是跨租戶邊界。

        `owner_id`：workspace 的計費／配額歸屬（見 `write()` 的註解）。租戶只能
        建立**自己**擁有的 workspace；`owner_id` 指向別人等於把一個 workspace
        連同它的 owner access 列（`create()` 下面那段 sudo bootstrap 會照
        `workspace.owner_id` 建）種到受害者帳號上。
        """
        if resolved.get('slug'):
            raise AccessError(
                "Cannot choose a workspace slug: it is server-generated "
                "(it determines the Kubernetes namespace and public subdomain)."
            )
        owner_id = resolved.get('owner_id')
        if owner_id and owner_id != self.env.uid:
            raise AccessError(
                "Cannot create a workspace owned by another user (owner_id=%s)."
                % (owner_id,)
            )

    @api.model_create_multi
    def create(self, vals_list: list[dict[str, Any]]) -> Workspace:
        privileged = self._is_privileged_writer()
        for vals in vals_list:
            resolved = self._add_missing_default_values(vals)
            if not privileged:
                self._check_tenant_create_anchors(resolved)
            # 判準用 `resolved` 而不是 `vals`：privileged 呼叫者若經 context
            # `default_slug` 指定 slug，這裡不該把它蓋掉（vals 不放該鍵，交給
            # `_prepare_create_values` 從 context 補）。非 privileged 走到這裡時
            # resolved['slug'] 必為空（否則上面已經 raise），所以一定會產生。
            # name 也讀 resolved：`default_name` 走 context 時，原本的
            # `vals.get('name', '')` 會拿到空字串 → `_generate_slug('')` 回 ''
            # → slug 落地成 ''，而 `md5('')[:8]` 是固定值 d41d8cd9 ⇒ 所有空 slug
            # 的 workspace 共用同一個 namespace（`UNIQUE(slug)` 只擋第二個，第一
            # 個照樣拿到那個 namespace）。
            if not resolved.get('slug'):
                vals['slug'] = self._generate_slug(resolved.get('name') or '')
        workspaces = super().create(vals_list)
        # Create owner access for each workspace.
        #
        # `.sudo()` is required (#556): the caller of Workspace.create() is
        # often a bare tenant with no existing workspace_access row anywhere
        # in the brand-new workspace, so the model-layer create() guard on
        # workspace_access (_check_assignable_role) would otherwise reject
        # this very bootstrap write (grantor=None ⇒ allowed=[]). Owner is
        # taken from workspace.owner_id (server-decided), not env.user — HA
        # PaaS auto-provision (ha_paas.py) calls this through auth='none'
        # with env.uid a public user, not env.user, so deriving from
        # env.user here would silently mis-attribute that path's owner row.
        for workspace in workspaces:
            self.env['woow_paas_platform.workspace_access'].sudo().create({
                'workspace_id': workspace.id,
                'user_id': workspace.owner_id.id,
                'role': ROLE_OWNER,
            })
        return workspaces

    def _generate_slug(self, name: str) -> str:
        """Generate a URL-friendly slug from name"""
        if not name:
            return ''
        # Convert to lowercase and replace spaces/special chars with hyphens
        slug = re.sub(r'[^\w\s-]', '', name.lower())
        slug = re.sub(r'[-\s]+', '-', slug).strip('-')
        # Ensure uniqueness
        base_slug = slug
        counter = 1
        while self.search_count([('slug', '=', slug)]) > 0:
            slug = f"{base_slug}-{counter}"
            counter += 1
        return slug

    def write(self, vals: dict[str, Any]):
        """Identity anchors：`owner_id` / `slug` 對租戶唯讀（#658）。

        光有 ir.rule 那一層不夠。`workspace_rule_owner_admin_manage` 把寫入收窄到
        owner/admin 之後，**admin 仍然能改 `owner_id`** —— 那等於把 workspace 的
        擁有權連同下面這些後果一起搬走，而且完全繞過 `transfer_ownership()`
        （`workspace_access.py`，它自己有 `role != ROLE_OWNER` 的守衛）：

        - `src/controllers/paas.py` 的 `_create_and_confirm_subscription` 用
          `workspace.owner_id.partner_id` 當帳單 partner（`security_access.py`、
          `smart_home.py` 同型）⇒ 訂閱帳單掛到別人的 partner；
        - `src/models/cloud_service.py` 的 `max_per_partner` 上限與
          `_find_active_for_partner` 都以 `workspace_id.owner_id.partner_id` 計數
          ⇒ 改 owner 即繞過每帳號實例上限；
        - `paas.py::_delete_workspace` 的授權判準就是
          `workspace.owner_id.id != user.id` ⇒ 把自己寫成 owner 就拿到 archive
          該 workspace 的權限。

        `slug` 同樣錨死：它是 K8s namespace 與公開 subdomain 的唯一輸入，且推導是
        `md5(slug)[:8]` 的 32-bit 截斷（碰撞成本見 `_check_tenant_create_anchors`
        的 docstring）。事後改 slug 還會讓既有服務**存下來的** `helm_namespace`
        與重新推導出來的值分岔。

        `name` / `description` / `state` 刻意**不**錨：那三個 owner/admin 本來就
        該能改，交給 ir.rule 那一層（`paas.py::_update_workspace` 已要求
        `required_role=ROLE_ADMIN`，兩層同判準）。

        比對「新值 != 現值」而不是「key 有沒有出現」，這樣前端整批送出未修改欄位的
        no-op write 不會被誤擋（同 `workspace_access.write()` / `project_access.write()`）。
        比較刻意用嚴格相等而不做空值正規化：型別不符（例如把 id 送成字串）會落在
        raise 這一側 ＝ fail-closed。

        ⚠ 這裡**不需要**像 `create()` 那樣走 `_add_missing_default_values`：Odoo 的
        `write()` 從不套用 default_get／context default（`_add_missing_default_values`
        在 `odoo/models.py` 全檔只有一個呼叫點，在 `_prepare_create_values` 裡，而
        那支只有 `create()` 會經過——本次於 odoo-18.0 原始碼逐一確認）。context
        注入是 create 方向獨有的形態。
        """
        if not self._is_privileged_writer():
            for rec in self:
                if 'owner_id' in vals and vals['owner_id'] != rec.owner_id.id:
                    raise AccessError(
                        "Cannot change the owner of workspace %s: ownership "
                        "carries billing and quota attribution." % rec.id
                    )
                if 'slug' in vals and vals['slug'] != rec.slug:
                    raise AccessError(
                        "Cannot change the slug of workspace %s: it determines "
                        "the Kubernetes namespace and public subdomain of every "
                        "service already deployed there." % rec.id
                    )
        return super().write(vals)

    def action_archive(self) -> None:
        """Archive the workspace"""
        self.write({'state': 'archived'})

    def action_restore(self) -> None:
        """Restore archived workspace"""
        self.write({'state': 'active'})

    def unlink(self):
        """Tear down external-resource (Cloudflare tunnel) children through
        their own ORM unlink() BEFORE the DB FK cascade fires, so each child's
        F-ENV-4 fail-loud cleanup guard actually runs.

        SecurityAccess / SmartHome bind ``workspace_id`` with
        ``ondelete='cascade'``; a DB-level cascade executes a raw SQL DELETE and
        BYPASSES the Python ``unlink()`` override on those children (same ORM
        behaviour the ``cloud-service-unlink-override-cleanup`` pattern works
        around). Without this, hard-deleting a workspace would drop SA/SmartHome
        rows that still hold a LIVE, billed Cloudflare tunnel with no DB anchor
        left to drive a retry — a permanent orphan (pitfall:
        cf-ns-orphan-no-account-gc, F-ENV-5).

        Children are resolved by search (no explicit One2many to SA/SmartHome on
        Workspace) and unlinked with ``sudo()`` so cleanup completes regardless
        of the caller's row-level ACL (caller authorisation is enforced upstream
        at the controller). A child whose tunnel cleanup fails raises UserError;
        we deliberately let it PROPAGATE so the whole workspace deletion rolls
        back and the child + its tunnel survive for a retry — never swallow it.

        ``ondelete='cascade'`` is kept as a DB-integrity safety net for paths
        that bypass this override entirely (raw SQL / an upstream cascade chain).

        CloudService (helm release / namespace) is routed too, via
        ``service_ids.unlink()`` → ``CloudService.unlink()``'s own helm
        fail-loud guard (``_cleanup_helm``). A configured-operator helm
        uninstall failure aborts the whole workspace deletion (keeps row +
        release for retry); an already-uninstalled release (controller path)
        or an unconfigured operator is an idempotent no-op. (The locked
        ``test_delete_workspace_preserves_mcp_via_orphaned_project`` only
        verifies project/MCP row survival, NOT helm cleanup, and the MCP it
        keeps is non-sidecar so ``CloudService.unlink``'s sidecar-MCP cleanup
        leaves it intact — routing does not regress it.)
        """
        for workspace in self:
            self.env['woow_paas_platform.security_access'].sudo().search(
                [('workspace_id', '=', workspace.id)]
            ).unlink()
            self.env['woow_paas_platform.smart_home'].sudo().search(
                [('workspace_id', '=', workspace.id)]
            ).unlink()
            # CloudService: route helm/ns teardown through its unlink() helm
            # fail-loud guard (F-ENV-5). service_ids is the explicit One2many.
            workspace.service_ids.sudo().unlink()
        return super().unlink()

    def check_user_access(self, user: Any = None, required_role: str | None = None) -> Union[Any, bool]:
        """
        Check if user has access to this workspace.

        Args:
            user: Target user (defaults to current user)
            required_role: Minimum role required ('guest', 'user', 'admin', 'owner')

        Returns:
            workspace_access record if authorized, False otherwise

        Role hierarchy (low to high): guest < user < admin < owner
        User's role must be >= required_role to pass the check.
        """
        if not self:
            return False
        self.ensure_one()
        user = user or self.env.user
        access = self.env['woow_paas_platform.workspace_access'].search([
            ('workspace_id', '=', self.id),
            ('user_id', '=', user.id),
        ], limit=1)

        if not access:
            return False

        if required_role:
            if ROLE_HIERARCHY.index(access.role) < ROLE_HIERARCHY.index(required_role):
                return False

        return access

    def get_user_role(self, user: Any = None) -> str | None:
        """Get the role of a user in this workspace"""
        self.ensure_one()
        user = user or self.env.user
        access = self.env['woow_paas_platform.workspace_access'].search([
            ('workspace_id', '=', self.id),
            ('user_id', '=', user.id),
        ], limit=1)
        return access.role if access else None
