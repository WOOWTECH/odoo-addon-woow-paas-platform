from __future__ import annotations

from typing import Any, Union

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
import re
import secrets

from .workspace_access import ROLE_OWNER, ROLE_HIERARCHY

# `ws_hash` 的位元組數。4 bytes → 8 個小寫 hex，與舊 `md5(slug)[:8]` **同字集同長度**
# ⇒ K8s namespace（DNS-1123）與 operator 的 `namespace.replace('paas-ws-','')` 契約
# 都不需要動（`services/naming.py::WS_HASH_RE` 是同一個形狀的權威定義）。
_WS_HASH_BYTES = 4
# 隨機抽值撞到既有列的重抽上限。撞的機率是 N/2^32（N＝現有 workspace 數；N=10,000
# 時約 2.3e-6），所以這個迴圈實務上永遠只跑一輪；它存在是為了讓那個天文數字級的
# 例外變成重抽而不是使用者眼前的 IntegrityError，**不是**安全邊界（見
# `_generate_ws_hash` 的 docstring）。
_WS_HASH_MAX_ATTEMPTS = 8


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

    ws_hash = fields.Char(
        string='Namespace Hash',
        readonly=True,
        index=True,
        help='Server-generated 8-hex identity of this workspace inside K8s / '
             'Cloudflare naming (namespace paas-ws-{ws_hash}, public subdomains '
             'paas-cs-/sa-/sm-{ws_hash}-...). Never derived from tenant input.',
    )

    _sql_constraints = [
        ('unique_slug', 'UNIQUE(slug)', 'Slug must be unique.'),
        # 🔴 這條 SQL constraint 是 #677 的**唯一**真正守線，不是 belt-and-braces。
        # 跨租戶碰撞檢查在 Python 層做不到：`_generate_slug()` 既有的
        # `while self.search_count(...)` 走呼叫者身分、受 ir.rule 限制，而攻擊者正好
        # 站在「看不見受害者」的位置上 ⇒ 那種檢查對他恆回 0、靜默放行。DB constraint
        # 不看 ir.rule，也不怕 TOCTOU（保證在寫入當下成立，不是在寫入之前查一次）。
        ('unique_ws_hash', 'UNIQUE(ws_hash)',
         'Two workspaces must never share a Kubernetes namespace identity.'),
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

        `slug`：擋掉呼叫端**直接指定** slug 的兩條通道（vals 的 `slug` 鍵、context
        的 `default_slug`）。

        🔴 **這道守衛「不」等於「slug 由伺服器決定」——不要那樣讀它。** 下面
        `create()` 產生 slug 的方式是 `_generate_slug(resolved['name'])`，而
        `_generate_slug()` 對 `[a-z0-9_-]` 字串**是恆等函式**（它只做小寫化、
        刪掉非 word/空白/連字號的字元、把空白／連字號序列併成單一 `-`）。⇒ 租戶把同一個字串
        從 `slug` 鍵搬到 `name` 鍵，就得到逐字相同的 slug。`name` 是租戶本來就
        該能自由指定的欄位（`_create_workspace` 只做 `.strip()`），所以這條通道
        **仍然開著**，#677 也刻意沒有關它（關它要動 `_generate_slug`，會波及
        #525 A2 鎖死的孤兒 slug 行為，爆炸半徑不同）。

        ⇒ **`slug` 今天仍然是租戶完全決定的字串。** 之所以不再有份量，是因為
        #677 把 namespace／subdomain 的推導從 `slug` 移到了 `ws_hash`（下一段），
        `slug` 從此只是給人看的識別字。守門測試：
        `test_workspace_authz_658.py::test_X8b_*`。

        `ws_hash`（#677）：K8s namespace 與所有公開子網域的**唯一**輸入。舊的
        推導是 `md5(slug)[:8]` ＝ **32 bit 截斷**，配上「slug 由租戶完全決定」
        ⇒ 租戶離線搜一個 `name` 就能讓自己的服務落進別人的 namespace，而且目標值
        不必猜——它逐字印在受害者每個公開子網域裡（`paas-cs-{ws_hash}-{svc_hash}`）。
        實測通用碰撞在 137,426 個候選內就出現（`ws-83256` 與 `ws-137425` 同為
        `7d82ce00`）；針對特定受害者的 32-bit preimage 期望 2^32 次 md5。

        現在 `ws_hash` 由 `_generate_ws_hash()` 產生、DB 層 `UNIQUE(ws_hash)` 保證
        跨租戶唯一。**這道 create 守衛是那個保證的必要組成**：少了它，攻擊者不必
        搜 preimage，直接把受害者的 ws_hash 填進 vals（或 context 的
        `default_ws_hash`）就行 —— 比原本的洞便宜得多。

        `owner_id`：workspace 的計費／配額歸屬（見 `write()` 的註解）。租戶只能
        建立**自己**擁有的 workspace；`owner_id` 指向別人等於把一個 workspace
        連同它的 owner access 列（`create()` 下面那段 sudo bootstrap 會照
        `workspace.owner_id` 建）種到受害者帳號上。
        """
        if resolved.get('slug'):
            raise AccessError(
                _("Cannot supply a workspace slug: it is derived from the name "
                  "by the server (it feeds the public workspace identifier).")
            )
        if resolved.get('ws_hash'):
            raise AccessError(
                _("Cannot supply a workspace ws_hash: it is generated by the "
                  "server and decides which Kubernetes namespace and public "
                  "subdomains this workspace owns.")
            )
        owner_id = resolved.get('owner_id')
        if owner_id and owner_id != self.env.uid:
            raise AccessError(
                _("Cannot create a workspace owned by another user (owner_id=%s).")
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
            # #677：namespace 身分一律由伺服器產生。privileged 呼叫者（sudo /
            # group_system）若顯式帶了 ws_hash 就尊重它（migration 回填、資料
            # 修復走這條）；其他一切情況產生新值。非 privileged 走到這裡時
            # resolved['ws_hash'] 必為空（否則上面已經 raise）。
            if not resolved.get('ws_hash'):
                vals['ws_hash'] = self._generate_ws_hash()
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

    def _generate_ws_hash(self) -> str:
        """產生本 workspace 在 K8s／Cloudflare 命名裡的身分碼（#677）。

        **為什麼是隨機值，不是 `md5(slug)` 也不是 `hmac(secret, id)`：**

        - `md5(slug)[:8]`（舊做法）＝ 租戶完全決定的字串的 32-bit 截斷 ⇒ 就是
          #677 那個洞本身。
        - `hmac(secret, id)`（`agent_session.project_namespace_hash` 用的形態）在
          那裡是對的——它需要從 `project_id` 可重算。這裡不需要：值一律存在
          `ws_hash` 欄位裡，那就是唯一真相。換來的代價卻是實在的：多一個
          **fail-closed 的必要環境變數**（`agent_session._get_namespace_secret`
          沒設就 raise ⇒ 這裡採用等於讓「建 workspace」在該變數未設時整條壞掉），
          外加該檔自己註記的「secret 一旦輪替就 namespace 漂移」風險。
        - 從 `id` 決定性推導還多一個問題：**可預測**。攻擊者算得出受害者下一個
          workspace 會拿到什麼 hash，可以搶先佔走（DoS／預先蹲點）。隨機值沒有
          這個面。

        🔴 **這個迴圈不是安全邊界，不要那樣讀它。** `search_count` 走的是呼叫者
        身分（受 ir.rule 限制），在攻擊路徑上恆回 0——那正是 #677 的核心觀察。
        真正的保證是 `_sql_constraints` 的 `UNIQUE(ws_hash)`：DB constraint 不看
        ir.rule、也不怕 TOCTOU。這裡加 `sudo()` 只是為了讓那個 N/2^32 機率的隨機
        撞號變成重抽、而不是使用者眼前的 IntegrityError；即使把整個迴圈刪掉，
        跨租戶保證仍然成立（守門測試 `test_workspace_ns_hash_677.py::test_N3_*`
        直接下 raw SQL 繞過所有 Python 層，量的就是 DB 那一層）。
        """
        for _attempt in range(_WS_HASH_MAX_ATTEMPTS):
            candidate = secrets.token_hex(_WS_HASH_BYTES)
            if not self.sudo().search_count([('ws_hash', '=', candidate)]):
                return candidate
        raise ValidationError(
            _("Could not allocate a free workspace namespace identity after %s "
              "attempts. This is astronomically unlikely for a healthy database "
              "(collision odds are workspaces/2^32 per draw) — check whether "
              "ws_hash has been backfilled with a constant.") % _WS_HASH_MAX_ATTEMPTS
        )

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

        `ws_hash` 錨死（#677）：它是 K8s namespace 與所有公開 subdomain 的唯一
        輸入。放行的話，攻擊者連 md5 preimage 都不用搜——直接把受害者的 ws_hash
        寫上去就行（那還會撞上 `UNIQUE(ws_hash)` 拋 IntegrityError，但那是 DB 的
        功勞，而且是把整筆交易炸掉的形態，不是一個守得住的授權判準）。它同時保護
        既有部署，但**機制不是「已部署的名字會跟著變」**：`helm_namespace` /
        `subdomain` / `canonical_name` 都是建立／provision 當下算一次就存起來的
        欄位，既有列一律不重推（四個產名點的呼叫點實查列在
        `src/migrations/18.0.1.0.131/post-migrate.py` 的檔頭）。後果落在**下一次
        推導**：改掉 ws_hash 之後，同一個 workspace 新建的服務會拿當下的值現算
        ⇒ 落進 `paas-ws-{新值}`，與既有服務所在的 namespace 分家。

        `slug` 同樣錨死，但**理由已經跟 namespace 無關**（#677 之後 namespace 不
        再由 slug 推導）：它是對外露出的識別字，且 `UNIQUE(slug)` 讓改寫可以踩掉
        別人的重試路徑（#525 A2）。歷史脈絡見 `_check_tenant_create_anchors`。

        `name` / `description` / `state` 刻意**不**錨：那三個 owner/admin 本來就
        該能改，交給 ir.rule 那一層（`paas.py::_update_workspace` 已要求
        `required_role=ROLE_ADMIN`，ORM 層則由
        `workspace_rule_owner_admin_manage` 收到同一組角色）。
        ⚠ 兩層的**角色門檻**相同，但**射程**不同，不要讀成「完全等價」：
        `_update_workspace` 只寫得到 `name` / `description`（它自己組
        `update_vals`），ORM 這一層放行的是 owner/admin 對本 model 的**任何**
        非錨定欄位，包含 `state`（＝ archive）與 `member_count`。也就是說
        **admin 可以走 ORM archive 一個 workspace**，而產品面的
        `_delete_workspace` 是 owner-only、且沒有對應的 unarchive 端點
        （`action_restore()` 沒有任何 controller 呼叫它）。這是本 PR **沒有**
        改變的既有行為（develop 上 guest 都做得到，現在收到 owner/admin），
        但收窄之後它就成了 admin 相對於產品意圖
        （`get_role_permissions()` 說 `ROLE_ADMIN.can_delete=False`）的落差
        ⇒ 已記在 PR 內文的「沒有做的事」與 follow-up issue，不在本單射程。

        `member_count` 也**不**錨：它是 stored compute 且無 inverse（＝公開
        `write()` 寫得進去、值會落地，實測 owner 身分可寫入 9999），但全 repo
        沒有任何授權或配額判準讀它（只有 `paas.py` 的 API 回應與兩個 OWL
        模板），下一次 `access_ids` 變動就被重算蓋回。判準是「有沒有守衛拿它
        做身分比對」，不是「它是不是 compute」。

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
                        _("Cannot change the owner of workspace %s: ownership "
                          "carries billing and quota attribution.") % rec.id
                    )
                if 'slug' in vals and vals['slug'] != rec.slug:
                    raise AccessError(
                        _("Cannot change the slug of workspace %s: it is the "
                          "workspace's public identifier and is unique platform-"
                          "wide.") % rec.id
                    )
                if 'ws_hash' in vals and vals['ws_hash'] != rec.ws_hash:
                    raise AccessError(
                        _("Cannot change the ws_hash of workspace %s: it determines "
                          "the Kubernetes namespace and public subdomain of every "
                          "service already deployed there.") % rec.id
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
        Workspace) and unlinked with ``sudo()`` so cleanup is complete. The
        caller's unlink ACL and record rules MUST be checked here first:
        raw ORM/RPC calls do not pass through a product controller, and a late
        super().unlink() denial cannot undo external child teardown.
        A child whose tunnel cleanup fails raises UserError;
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
        if self:
            self.check_access('unlink')
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
        # #1261：portal/share 帳號一律不算成員（fail-closed），擺在 search **之前**。
        # 兩個理由：
        #   1. 非 sudo 呼叫（如 paas.py CS 端點的 `workspace.check_user_access(
        #      user)`，user＝request.env.user）時，portal 身分對 `workspace_access` 沒有 ACL，
        #      search 會直接丟 AccessError、原文冒到前端；先回 False 讓端點走
        #      既有的「Workspace not found or access denied」結構化分支。
        #      ⚠ 這裡只守得住「先呼叫本方法」的路徑：SA／SH controller 的
        #      `_get_workspace_or_error` 在本方法之前就自己做非 sudo search，
        #      所以那兩支 helper 另外在 search 前擋 `user.share`（同樣 #1261）。
        #   2. sudo 呼叫（如 `cloud_service._assert_binding_authorized`）時 search
        #      會找到 #1261 之前邀請殘留的 portal 成員列並放行——但 portal 本來就
        #      用不了 workspace，這種列不該再授予任何東西。其他自己做 sudo 成員
        #      查詢的 helper（ha_api `_get_user_workspaces`／`_check_workspace_access`、
        #      ai_assistant `_check_workspace_access`）也各自補了同一條 share 判準。
        # internal 使用者（`share=False`）不經過這條，行為完全不變。
        if user.share:
            return False
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
