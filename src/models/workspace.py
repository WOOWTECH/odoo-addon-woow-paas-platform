from __future__ import annotations

from typing import Any, Union

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
import re
import secrets

from .workspace_access import ROLE_ADMIN, ROLE_OWNER, ROLE_HIERARCHY

# #1295：封存工作區被「底下還有未刪除服務」擋下時的結構化錯誤碼（controller 原樣
# 回給前端，前端據此列出要先刪除的服務）。
ERR_WORKSPACE_HAS_SERVICES = 'workspace_has_services'

# #1295：封存守衛要檢查的子服務 model，以及各自代表「已刪除」的 state。
#
# 三個 model 的使用者刪除都走**真 unlink**（record 離場）；值集裡沒有 deleted／
# terminated。唯一留下記錄的「已刪除」形態是 `state='deleting'`：
# - SA／SH：cron `_do_cleanup()`（訂閱到期）只有在 Cloudflare 資源確定拆乾淨之後
#   才寫 `deleting`；拆不乾淨會落 `error`（＝仍有活資源 ⇒ 這裡算「未刪除」）。
#   `action_delete()` 在 savepoint 內先寫 `deleting` 再 unlink，失敗整段回捲。
# - cloud_service：與 `cloud_service.TERMINAL_STATES`（＝`('deleting',)`，配額計數
#   的既有判準）同值——刪除端點先寫 `deleting`、背景 helm uninstall 完再 unlink。
#   ⚠ 刻意抄值而不 import：從 workspace.py import cloud_service 會把整串 model
#   class 的定義順序提前（`models/__init__.py` 的 workspace 排在前面），兩者相等
#   由 `test_workspace_archive_guard_1295.py` 的測試鎖住。
# 其他所有 state（pending／provisioning／active／error／suspended…）都還可能
# 掛著 tunnel／helm release／訂閱，一律算「未刪除」。
# ⚠ 已知殘留窗口：cloud_service 的 `deleting` 也可能是「刪除端點剛寫下、背景 helm
# uninstall 還在跑」的過渡態，uninstall 失敗會寫回 `error`。若租戶剛好在那幾十秒內
# 封存工作區，失敗的那台會留在已封存的工作區裡。不把 `deleting` 算未刪除是因為
# cron `_do_cleanup`（訂閱到期）拆完之後也停在 `deleting`、記錄不離場——那種列
# 租戶刪不掉（端點回「already being deleted」），算未刪除會讓工作區永遠封存不了。
#
# (type key, model, 代表已刪除的 state)；順序即錯誤清單的排列順序。三個 model
# 都有必填的 `name` 欄位，錯誤清單直接用它。
_ARCHIVE_GUARD_SERVICE_MODELS = (
    ('security_access', 'woow_paas_platform.security_access', ('deleting',)),
    ('smart_home', 'woow_paas_platform.smart_home', ('deleting',)),
    ('cloud_service', 'woow_paas_platform.cloud_service', ('deleting',)),
)


class WorkspaceHasServicesError(UserError):
    """#1295：工作區底下還有未刪除的服務，拒絕封存。

    獨立型別讓 controller 能把它轉成結構化回應（`code` ＋ 服務清單），也讓測試
    斷言例外**型別**而非文案片段。`services` 是 `[{type, id, name, state}, ...]`。
    """

    code = ERR_WORKSPACE_HAS_SERVICES

    def __init__(self, message: str, services: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.services = services or []


# `ws_hash` 的位元組數。4 bytes → 8 個小寫 hex，與舊 `md5(slug)[:8]` **同字集同長度**
# ⇒ K8s namespace（DNS-1123）與 operator 的 `namespace.replace('paas-ws-','')` 契約
# 都不需要動（`services/naming.py::WS_HASH_RE` 是同一個形狀的權威定義）。
_WS_HASH_BYTES = 4
# 隨機抽值撞到既有列的重抽上限。撞的機率是 N/2^32（N＝現有 workspace 數；N=10,000
# 時約 2.3e-6），所以這個迴圈實務上永遠只跑一輪；它存在是為了讓那個天文數字級的
# 例外變成重抽而不是使用者眼前的 IntegrityError，**不是**安全邊界（見
# `_generate_ws_hash` 的 docstring）。
_WS_HASH_MAX_ATTEMPTS = 8

# #1516：同名 workspace 的 slug 後綴；3 bytes → 6 個小寫 hex。
_SLUG_SUFFIX_BYTES = 3
_SLUG_SUFFIX_MAX_ATTEMPTS = 8


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
        # Python 層即使用 sudo 查重，也無法避免查詢與 INSERT 之間的 TOCTOU；
        # DB constraint 不看 ir.rule，且保證唯一性在寫入當下成立。slug／ws_hash
        # 的全域查重只降低撞號機率，不能取代各自的 DB UNIQUE。
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

        租戶仍可透過 `name` 影響 base slug：`_generate_slug()` 做小寫化、
        刪除非 word/空白/連字號字元，再把空白／連字號序列併成單一 `-`。
        #1516 改為 sudo 全域查重；未佔用時保留 base slug，撞名（含不可見孤兒）
        才加伺服器產生的 6 碼隨機 hex 後綴。因此 `name` 可影響 slug，卻不保證
        逐字決定最終 slug，且顯示用的 `name` 不變。

        #677 已把 namespace／subdomain 的推導從 `slug` 移到了 `ws_hash`（下一段），
        `slug` 只是給人看的識別字，不是基礎設施身分。守門測試：
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
        """由 name 正規化 slug；全域撞名時加隨機 hex，不更動顯示名稱。"""
        if not name:
            return ''
        slug = re.sub(r'[^\w\s-]', '', name.lower())
        slug = re.sub(r'[-\s]+', '-', slug).strip('-')
        # #1516：必須看見其他租戶與孤兒，與 DB UNIQUE(slug) 的範圍一致。
        if not self.sudo().search_count([('slug', '=', slug)]):
            return slug
        for _attempt in range(_SLUG_SUFFIX_MAX_ATTEMPTS):
            candidate = f"{slug}-{secrets.token_hex(_SLUG_SUFFIX_BYTES)}"
            if not self.sudo().search_count([('slug', '=', candidate)]):
                return candidate
        raise ValidationError(
            _("Could not allocate a free workspace slug after %s attempts. "
              "Please try again.") % _SLUG_SUFFIX_MAX_ATTEMPTS
        )

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

        🔴 **這個迴圈不是安全邊界，不要那樣讀它。** 未提權的 `search_count` 會
        受 ir.rule 限制、看不到其他租戶；這裡已用 sudo，#1516 起 slug 查重也同樣
        全域可見，但查詢與 INSERT 之間仍有 TOCTOU。真正的保證是
        `_sql_constraints` 的 `UNIQUE(ws_hash)`：DB constraint 不看 ir.rule，
        且保證唯一性在寫入當下成立。這裡的 `sudo()` 只是為了讓 N/2^32 機率的隨機
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

        `name` / `description` / `state` 刻意**不**錨（不是身分錨點）：前兩個
        owner/admin 本來就該能改，`state` 另有 owner-only 的角色守衛（見
        `_check_state_change_by_owner`），交給 ir.rule 那一層（`paas.py::_update_workspace` 已要求
        `required_role=ROLE_ADMIN`，ORM 層則由
        `workspace_rule_owner_admin_manage` 收到同一組角色）。
        ⚠ 兩層的**角色門檻**相同，但**射程**不同，不要讀成「完全等價」：
        `_update_workspace` 只寫得到 `name` / `description`（它自己組
        `update_vals`），ORM 這一層放行的是 owner/admin 對本 model 的**任何**
        非錨定欄位，包含 `state`（＝ archive）與 `member_count`。其中 `state`
        對 admin 的那格落差（產品面 `_delete_workspace` 是 owner-only、
        `get_role_permissions()` 說 `ROLE_ADMIN.can_delete=False`、沒有還原端點）
        後來由 `_check_state_change_by_owner` 收掉：admin 經 ORM 封存／還原一律
        拒絕，owner 照舊。

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
                self._check_state_change_by_owner(rec, vals)
        if vals.get('state') == 'archived':
            self._check_archivable()
        return super().write(vals)

    # ------------------------------------------------------------------ #
    # Archive guard (#1295)                                               #
    # ------------------------------------------------------------------ #
    def _get_undeleted_services(self) -> list[dict[str, Any]]:
        """本 workspace 底下所有「未刪除」的服務（判準見 `_ARCHIVE_GUARD_SERVICE_MODELS`）。

        用 `sudo()` 搜：這是資料不變式（「封存的工作區底下不得有活服務」），不能
        取決於呼叫者看得到哪些列——呼叫者看不到的服務一樣在計費。呼叫端
        （`_check_archivable`）已先過 `check_access('write')`，只有能封存這個
        workspace 的人（owner／admin，或 superuser）才走得到這裡。
        """
        self.ensure_one()
        services = []
        for type_key, model, deleted_states in _ARCHIVE_GUARD_SERVICE_MODELS:
            records = self.env[model].sudo().search([
                ('workspace_id', '=', self.id),
                ('state', 'not in', list(deleted_states)),
            ], order='id')
            services.extend(
                {'type': type_key, 'id': rec.id, 'name': rec.name or '',
                 'state': rec.state or ''}
                for rec in records
            )
        return services

    def _check_archivable(self) -> None:
        """#1295：工作區底下還有未刪除的服務時拒絕封存。

        封存只是 `state='archived'`，不會拆任何子資源；而封存之後所有租戶端點都
        要求 workspace `state='active'`，也沒有還原端點 ⇒ 放行的話，服務、
        Cloudflare tunnel／DNS／Access app、helm release 與訂閱全部留著繼續扣點，
        租戶卻再也看不到也刪不掉它們。

        掛在 `write()` 而不是只掛 `action_archive()`：租戶（owner／admin）可以經
        `/web/dataset/call_kw` 直接 `write({'state': 'archived'})`，只守
        `action_archive()` 的話那條通道整道繞過（invariant 收斂到 write hook，同
        `docs/decisions/patterns/invariant-recompute-on-orm-write-hook.md`）。

        **不**豁免 superuser／`base.group_system`（同
        `docs/decisions/patterns/su-env-exempt-guard-false-green.md`）：這不是授權
        判準，是資料不變式——sudo 封存一個還掛著活服務的工作區，孤兒化的後果
        一模一樣。真的要連服務一起清掉的路徑是 `unlink()`（會先走各子 model 的
        teardown）。也刻意不提供 context 逃生門（裸 context key 可偽造，#536／#783）。

        先 `check_access('write')`：沒有寫入權的人（guest／user／非成員）照舊拿到
        ir.rule 的 AccessError，不會因為這道守衛先跑而改拿到服務清單（歸因不變，
        也不對沒權限的人列出服務名稱）。
        """
        self.check_access('write')
        for workspace in self:
            if workspace.state == 'archived':
                continue  # 已封存 ⇒ no-op write，不重複判
            services = workspace._get_undeleted_services()
            if services:
                raise WorkspaceHasServicesError(
                    _("This workspace still has services that have not been "
                      "deleted: %(services)s. Delete these services first, "
                      "then delete the workspace.",
                      services=', '.join(
                          s['name'] or '#%s' % s['id'] for s in services)),
                    services,
                )

    def _check_state_change_by_owner(self, rec, vals: dict[str, Any]) -> None:
        """`state`（封存／還原）只有 owner 能改——補上面 docstring 記的那格落差。

        ir.rule `workspace_rule_owner_admin_manage` 讓 **admin** 也寫得了 `state`，
        而產品面封存（`paas.py::_delete_workspace`）是 owner-only、
        `get_role_permissions()` 寫 `ROLE_ADMIN.can_delete=False`，且沒有任何
        還原端點。經 `/web/dataset/call_kw` 的實際後果：admin 把 owner 的整個
        workspace 封存（`/api/workspaces` 清單、SA／SH／HA 端點都以
        `workspace_id.state = 'active'` 過濾 ⇒ 所有成員連 owner 都看不到、用不了），
        owner 沒有任何 UI 能還原；`action_archive()`／`action_restore()` 是 public
        method，同樣只是 `write({'state': …})`，一併由這裡擋。

        只攔 **admin**：role=user／guest／非成員交給 ir.rule 拒絕（歸因不被本守衛
        搶走，`test_workspace_authz_658.py` 的 X2 斷言的正是 ir.rule 那一層）。
        owner 刻意放行——封存走的是 owner 的產品端點；owner 經 RPC 還原自己封存的
        workspace 沒有跨租戶或計費後果（封存本身不停任何訂閱），是否禁止屬產品
        決策，不在本守衛射程。

        ⚠ 只加角色判準，不碰 `action_archive()` 的業務邏輯（「有服務時拒絕封存」
        由 #1295 另案處理）。
        """
        if 'state' not in vals or vals['state'] == rec.state:
            return
        access = rec.sudo().check_user_access(self.env.user)
        if access and access.role == ROLE_ADMIN:
            raise AccessError(
                _("Only the workspace owner can archive or restore workspace %s.")
                % rec.id
            )

    def action_archive(self) -> None:
        """Archive the workspace.

        #1295：底下還有未刪除的服務時 `write()` 會丟 `WorkspaceHasServicesError`
        （守衛住在 `write()`，見 `_check_archivable`）。
        """
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
