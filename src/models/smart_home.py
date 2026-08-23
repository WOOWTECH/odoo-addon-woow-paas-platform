import logging
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..services.naming import (
    make_sm_access_canonical_name,
    make_smarthome_subdomain,
)

from ..services.paas_operator import (
    get_paas_operator_client,
    PaaSOperatorError,
    PaaSOperatorConnectionError,
)

# 單向相依：`smart_home_route` 只用 model 名指回本檔，不 import 它，所以這裡
# 直接 import 不會成環（`models/__init__.py` 也是先 smart_home 後 route）。
# #798：policy 列上 `cf_policy_id` 的唯一寫入豁免（sentinel／opener 住在 SA 那支）。
from .security_access import _internal_policy_cf_write
from .smart_home_route import (
    _internal_sm_route_identity,
    _sm_forced_access_sync,
    _sm_route_cascade_unlink,
)

_logger = logging.getLogger(__name__)

#: #528 的待回收 CF Access 物件帳本（三腿共用；本檔只用它的 #794 補償入口）。
_CF_REAP_MODEL = 'woow_paas_platform.cf_access_policy_reap'

#: #740：`unlink()` 這條路徑真的會消耗到 unlink 權限的**第二張**表。
#: 推導方式（不要照抄結論，改 `unlink()` 時重推一次）：讀 `unlink()` 本體，找出每一
#: 個會走到 Odoo `check_access('unlink')` 的位置——只有兩個，route 那次顯式的
#: `_sm_route_cascade_unlink(record.route_ids).unlink()`，以及本 model 的
#: `super().unlink()`。
_SM_ROUTE_MODEL = 'woow_paas_platform.smart_home_route'


# ------------------------------------------------------------------------- #
#  #524 第 1 節：smart_home 的建立閘（private sentinel）                      #
# ------------------------------------------------------------------------- #
#
# 不變式：**沒有任何 RPC 呼叫端**（裸 ORM／XML-RPC／JSON-RPC／`call_kw`，不論
# 身分、不論送什麼 vals 或 context）能讓一筆 smart_home 落地。只有明示開閘的
# 平台伺服器端 in-process 碼建得出來。閘門在 `create()` 的第一行。
#
# 為什麼 ir.rule 那層擋不到（#519 對 cloud_service 的裁決，本 model 同構）：
# owner 資格是**可自我滿足**的——任何 `base.group_user` 帳號免費自建 workspace
# → `workspace.py` 用 `.sudo()` 自動補一列 role=owner 的 access → 滿足
# `smart_home_rule_owner_admin_manage`。「自建 workspace 即為 owner」是產品設計、
# 不是缺陷，所以不動那一層。
#
# 為什麼繞過下單流程建出來的孤兒列是有害的（#524 實測，不是推論）：
# `subscription_lifecycle_mixin._is_subscription_active()` 對「沒有訂閱」回
# **True**（原文註解：`No subscription = unrestricted (admin/legacy)`）
# ⇒ 一筆 `subscription_id` 為空的 smart_home 通得過
# `_check_operation_allowed(operation='write')` ⇒ 租戶可以用它打
# `/api/workspaces/<ws>/smarthomes/<id>` 的 `action='provision'`，
# **開出一條真的、在計費的 Cloudflare tunnel，而平台側零訂閱零扣款**。
#
# 判準**只讀** context 裡那個值的**物件身分**（`is` 比對），刻意不讀
# vals／`env.su`／`env.user`／workspace role——完整論證見
# `models/cloud_service.py` 頂端 `_PLATFORM_ORDER` 的註解（本段刻意逐字沿用
# 它的形狀，不另發明；`ir.default` 的持久化載體、`call_kw` 整包換掉 context、
# su env 假綠三個陷阱在那裡有實測紀錄）。
class _PlatformOrder:
    """平台伺服器端開通碼的 marker type —— 見上方註解。"""

    __slots__ = ()

    def __repr__(self):
        return '<platform order>'


_PLATFORM_ORDER = _PlatformOrder()

#: 唯一的逃生口 context key。key 本身不是判準（呼叫端當然抄得到這個字串），
#: 判準是它的**值身分**，見 `_is_platform_order`。
#: ⚠ key 刻意與 `cloud_service.py` 的不同名：兩個 model 的閘互不授權，共用字串會
#: 讓「開了 A 的閘順便開了 B 的」變成可能。
_PLATFORM_ORDER_CTX_KEY = 'woow_smart_home_platform_order'


# 🔴 為什麼這一組不叫 cloud_service 那組的名字 —— 這是**必要**的命名分歧，
# 不要「順手改成一致」：
#
# `scripts/audit/repo-lint.py` 的 `check_provision_sentinel_allowlist`（#519）
# 判準是 cloud_service 那組開閘符號的**字面比對**（正則寫在該 check 上方），命中
# 即要求該檔列進它的 allowlist。而那份 allowlist 的語意是「**這個檔會替
# cloud_service 開閘**」——本檔與 `controllers/smart_home.py` 都不是。
#
# 沿用同名（含「在原名後面加後綴」這種最自然的寫法——那仍然是**子字串命中**）
# 會有兩個後果：
#   1. 這兩個檔被迫列進 cloud_service 的 allowlist ⇒ gate 對它們吐出一句為假的話；
#   2. 更糟：從此以後**真的**在這兩個檔裡新增一條 cloud_service 開閘路徑，
#      #519 的 gate 不會再紅（已在 allowlist 上）⇒ 靜默失去守線。
# ⇒ 不同的不變式要用不同的符號名，讓兩道 gate 各自保有鑑別力。
#
# 實測兩次（都不是推論）：第一版沿用同名 ⇒ `repo-lint.py --all` 立刻對這兩個檔
# 報 2 條違規；改名後，光是**在註解裡引用那幾個字面符號來解釋原因**又報了 1 條
# ——所以上面這段刻意只描述、不寫出那幾個名字。


def _platform_order(records):
    """回傳「唯一被允許建立 smart_home」的那個 context 底下的 ``records``。

    開閘掛在**會讓記錄落地的那一次呼叫**上，不掛在 model handle 的綁定處——
    唯讀 handle 一律不帶這個 context，逃生口的表面積就只有真正的建立點。

    ``with_context(**kwargs)`` 是 merge，所以 `.sudo()` / `.with_user()` /
    後續 `with_context(foo=...)` 都保留這個把手。
    """
    return records.with_context(
        **{_PLATFORM_ORDER_CTX_KEY: _PLATFORM_ORDER})


def _is_platform_order(env):
    """判準：**只**比對 context 裡那個值的物件身分（`is`），不讀 vals／su／user。"""
    return (env.context.get(_PLATFORM_ORDER_CTX_KEY)
            is _PLATFORM_ORDER)


class SmartHomeOrderGateError(AccessError):
    """#524：這一筆 smart_home 不是從平台的開通流程來的。

    獨立型別的用途同 `cloud_service.SmartHomeOrderGateError`——**讓測試斷言例外型別
    而非文案片段**。這裡尤其必要：基底 `AccessError` 同時也是 ACL／`ir.rule`
    拒絕會丟的型別，只斷言 `AccessError` 分不出「被本閘擋下」與「被 rule 擋下」，
    而本 PR 恰好同時新增了那兩道守線。
    """


# ------------------------------------------------------------------------- #
#  #524 第 2 節：tunnel 身分錨點（形態抄 #522／PR #537 的 security_access）    #
# ------------------------------------------------------------------------- #
#
# 錨點＝**決定這一列驅動的是「哪一個」Cloudflare 物件**的欄位。移動它不是「改一個
# 設定」，是把整列重新瞄準別人的資源，而該列仍保有自己 workspace 的 ir.rule 祝福。
#
# 這道守衛與上面的 record rule **層次不同，缺一不可**（#524 實測，mock 掉 operator
# client，沒有真的對外送）：
#
#     1. 租戶 A 以非成員身分讀到租戶 B 的 `tunnel_id`（本 PR 第 1 節已擋）
#     2. 租戶 A 把那個 UUID 寫進**自己**那一筆記錄——這一步 record rule 擋不到，
#        因為他對自己的記錄本來就有合法 write 權
#     3. 租戶 A 走正常刪除端點 → `unlink()` 先跑 `_cleanup_tunnel()`，
#        平台拿**平台級 API key** 送出 `delete_tunnel(<B 的 UUID>)`
#     4. 之後才因 ACL `unlink=0` 回失敗
#
# ⚠ 第 3 與第 4 的順序是這條路徑成立的關鍵：`_cleanup_tunnel()` 在
# `super().unlink()`（ACL 檢查所在）**之前**呼叫 ⇒ 「租戶刪不掉自己的記錄」這個
# 既有限制攔不住外部副作用。端點回的是失敗，Cloudflare 側的動作已經發生。
#
# #522 在 sibling model `security_access` 上是同一個缺口，已由 PR #537 以
# `_IDENTITY_ANCHOR_FIELDS` + sentinel 修掉；**smart_home 是那次沒覆蓋到的同型
# 缺口**（`security_access.py:37` 的註解自陳 tunnel_id「added later, was simply
# never covered」——同一句話今天原封不動適用於本 model）。
#
# NOT 錨點，刻意的（判準＝「改了它會不會改變某個 Cloudflare 呼叫打向哪裡」）：
#   * `connector_id` / `connector_type` / `tunnel_status` / `tunnel_uptime` /
#     `tunnel_route`——poll 回來的狀態或顯示字串，偽造只改畫面
#   * `ha_port`——使用者自選，指向租戶自己主機上的埠
#   * `subdomain` / `reference_id`——`subdomain` 確實會進
#     `configure_tunnel(hostname=...)`，但 `tunnel_id` 一旦鎖住，它只能重指
#     **自己**那條 tunnel（且 `subdomain` 有 DB UNIQUE，搶不走別人的）；
#     `reference_id` 是它的 hash 種子，同理。兩者屬**非跨租戶**的次級問題，
#     未在本單實測，依工單紀律不順手改——已回報 leader4 另評。
_IDENTITY_ANCHOR_FIELDS = (
    'tunnel_id',      # WHICH Cloudflare tunnel this row drives (#524)
    'tunnel_name',    # its CF-side name, mirrored back from create_tunnel
    'tunnel_token',   # the tunnel credential itself
)


class _InternalIdentityWrite:
    """Marker type for the identity-anchor escape hatch — see below."""

    __slots__ = ()

    def __repr__(self):
        return '<internal identity write>'


#: 唯一的逃生口：伺服器端 provision／teardown 碼把 Cloudflare 剛回傳的 id 存進來時
#: 明示開閘。值必須是**這個物件**而不是 `True`／字串——`/web/dataset/call_kw` 把
#: 呼叫端的 context 整包換進 env，任何 JSON 表達得出的判準，呼叫端在 payload 裡多加
#: 一個 key 就打開了。這不是推論：#522 在 18.0 上實測過，一個只有 `base.group_user`
#: 的租戶送 `context={"sa_internal_identity_write": true}` 就改寫了 SA 的
#: `canonical_name`，而且新值真的 commit 進 Postgres。JSON 載不了物件身分。
#:
#: 同樣刻意**不是** `env.su` 豁免：su 會在每個 `TransactionCase`（su env）與每條
#: `sudo()` 路徑上靜默停用守衛，正是測試本該證明它有效的地方
#: （`docs/decisions/patterns/su-env-exempt-guard-false-green.md`）。
_INTERNAL_IDENTITY_WRITE = _InternalIdentityWrite()

_INTERNAL_IDENTITY_CTX_KEY = 'woow_smart_home_internal_identity_write'


def _internal_identity(records):
    """回傳「唯一被允許寫入身分錨點」的那個 context 底下的 ``records``。"""
    return records.with_context(
        **{_INTERNAL_IDENTITY_CTX_KEY: _INTERNAL_IDENTITY_WRITE})


def _is_internal_identity_write(env):
    return (env.context.get(_INTERNAL_IDENTITY_CTX_KEY)
            is _INTERNAL_IDENTITY_WRITE)


def _strip_identity_anchors(env, vals_list):
    """create() 側：把身分錨點**明示寫成空**，讓它不可能由呼叫端決定。

    `super().create()` 不會繞經 `write()`，所以只靠 write 守衛會留下一個
    create-time squat：一筆全新的列在還沒 provision 之前就綁上受害者的 tunnel
    ——而 `action_provision` 的 `if self.tunnel_id:` 分支會因此**跳過**建立 tunnel。
    對**所有人**生效（含 superuser），sentinel context 是唯一豁免。

    🔴 **為什麼是 `vals[field] = False` 而不是 `vals.pop(field)`**（PR #738 覆核
    round1 的 HIGH，實測紅過）：

    `super().create()` 內部會呼叫 `_add_missing_default_values(vals)`，把「vals 裡
    **缺席**」的欄位從 `ir.default` / `default_<field>` context / 欄位 default 補
    回來。`pop` 恰恰把 anchor 變成**缺席** ⇒ 預設值在剝除**之後**才落地：

        pop（key 消失）→ super().create() → _add_missing_default_values 補回來

    實測（沙盒、真 HTTP、只掛 `base.group_user` 的租戶）：
    `ir.default.set('woow_paas_platform.smart_home', 'tunnel_id', <受害者 UUID>,
    user_id=True)` 之後走**正常下單端點**建立，記錄一出生 `tunnel_id` 就是攻擊者
    選的值——繞過的是 create 的預設值通道，不是 write 守衛。

    ⚠ 「先 `_add_missing_default_values(vals)` 解析、再從解析結果 pop」**同樣是錯
    的**：pop 之後 key 又缺席，`super().create()` 內部會再解析一次、再補一次。
    解析後的 vals 適合用來**判斷**（`cloud_service` 的 #705 F1 就是拿它做前置檢查），
    不適合用來**剝除**。

    把 key **留在 vals 裡並給定 falsy 值**才是一次涵蓋所有載體的做法：
    `_add_missing_default_values` 只補「不在 vals 裡」的欄位，而且結尾是
    `defaults.update(values)`（呼叫端的值永遠壓過 default）⇒ 兩層都擋住，
    而且不必逐個載體去堵（逐載體補正是 #657 同一行修兩次的原因）。

    採「寫空」而非 raise：`copy()` 與一般資料載入要能繼續運作——一份不帶錨點的
    複本正是複本該有的樣子。
    """
    if _is_internal_identity_write(env):
        return
    for vals in vals_list:
        for field in _IDENTITY_ANCHOR_FIELDS:
            vals[field] = False


def _reject_identity_anchor_writes(env, vals):
    """write() 側：拒絕移動錨點，而且要大聲。

    對**所有人**生效（含 superuser）——理由見 `_INTERNAL_IDENTITY_WRITE`。
    """
    if _is_internal_identity_write(env):
        return
    for field in _IDENTITY_ANCHOR_FIELDS:
        if field in vals:
            raise UserError(
                "Field '%s' is immutable after creation." % field
            )


class SmartHome(models.Model):
    _name = 'woow_paas_platform.smart_home'
    _inherit = ['woow_paas_platform.subscription_lifecycle_mixin']
    _description = 'Smart Home Instance'
    _order = 'create_date desc'

    _sql_constraints = [
        (
            'unique_subdomain',
            'UNIQUE(subdomain)',
            'Subdomain must be unique across all smart homes.',
        ),
    ]

    # Identity
    name = fields.Char(
        string='Name',
        required=True,
        help='User-defined name for this smart home',
    )
    reference_id = fields.Char(
        string='Reference ID',
        required=True,
        copy=False,
        index=True,
        default=lambda self: str(uuid.uuid4()),
        help='Unique reference ID for hash-based naming',
    )
    workspace_id = fields.Many2one(
        comodel_name='woow_paas_platform.workspace',
        string='Workspace',
        required=True,
        ondelete='cascade',
        help='Workspace this smart home belongs to',
    )

    # State
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('provisioning', 'Provisioning'),
            ('active', 'Active'),
            ('error', 'Error'),
            ('suspended', 'Suspended'),
            ('deleting', 'Deleting'),
        ],
        string='State',
        default='pending',
        required=True,
    )

    # Store original service_url for resume after suspend
    original_service_url = fields.Char(
        help='Original service URL before suspension, used for resume',
    )
    error_message = fields.Text(
        string='Error Message',
        help='Error details if provisioning failed',
    )

    # Tunnel info
    tunnel_id = fields.Char(string='Tunnel ID')
    tunnel_token = fields.Char(
        string='Tunnel Token',
        groups='base.group_system',
    )
    tunnel_name = fields.Char(string='Tunnel Name')
    connector_id = fields.Char(string='Connector ID')
    connector_type = fields.Char(string='Connector Type')
    tunnel_route = fields.Char(string='Tunnel Route URL')
    tunnel_status = fields.Selection(
        selection=[
            ('connected', 'Connected'),
            ('disconnected', 'Disconnected'),
            ('error', 'Error'),
        ],
        string='Tunnel Status',
        default='disconnected',
    )
    tunnel_uptime = fields.Char(string='Uptime')

    # Config
    ha_port = fields.Integer(
        string='HA Port',
        default=8123,
        help='Home Assistant service port',
    )
    subdomain = fields.Char(
        string='Subdomain',
        help='Assigned subdomain for this smart home tunnel',
    )

    # Application routes（#768）——每一列＝一個可掛 Cloudflare Access 的對外
    # hostname。今天恰好一列（`is_primary`，provision 當下建立）。
    route_ids = fields.One2many(
        comodel_name='woow_paas_platform.smart_home_route',
        inverse_name='smart_home_id',
        string='Application Routes',
    )

    # Timestamps
    deployed_at = fields.Datetime(string='Deployed At')

    @api.model_create_multi
    def create(self, vals_list):
        """#524：建立閘 ＋ 身分錨點的 create 側。

        閘門是整個方法的第一件事，刻意的——後面每一段（錨點剝除、
        `super().create()` 的 INSERT）都有副作用或成本，被拒絕的呼叫不該碰到
        其中任何一段。完整論證見模組頂端 `_PLATFORM_ORDER` 的註解。

        對所有入口一律生效、**無 su／group 豁免**（否則 TransactionCase 假綠，
        見 `docs/decisions/patterns/su-env-exempt-guard-false-green.md`）。
        """
        if not _is_platform_order(self.env):
            raise SmartHomeOrderGateError(_(
                'Smart Home 只能由平台的開通流程建立（%(model)s）。'
                '請改從 Smart Home 下單流程建立，不要直接寫入這個 model。',
                model=self._name))
        _strip_identity_anchors(self.env, vals_list)
        return super().create(vals_list)

    def write(self, vals):
        """#524：身分錨點的 write 側 —— 見模組頂端 `_IDENTITY_ANCHOR_FIELDS`。"""
        _reject_identity_anchor_writes(self.env, vals)
        return super().write(vals)

    def _get_operator_client(self):
        """Get PaaS Operator client instance."""
        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(
                "PaaS Operator is not configured. "
                "Please set Operator URL and API Key in Settings → Woow PaaS."
            )
        return client

    def _naming_ws_hash(self):
        """Workspace 的 **stored** namespace 身分碼，Cloudflare 命名的 hash 種子。

        #677 之前這裡回的是 `workspace.slug`，而 slug 由租戶提供的 `name` 逐字決定、
        namespace 是它的 `md5[:8]`（32-bit 截斷）⇒ 租戶可以讓自己的子網域落在別人
        的 ws_hash 段上。現在回 stored `ws_hash`（伺服器產生、DB `UNIQUE`）。

        原本那個「`slug` 取不到就退回 `name.lower()`」的 fallback 一併移除：
        `workspace_id` 是 `required=True`，該分支在空 recordset 上本來就會炸
        （`False.lower()`）；而讓它靜默退回 name 推導，正是這個洞的形狀。ws_hash
        缺值時交給 `naming.py::_require_ws_hash` fail-loud（fail-closed，不會產出
        `paas-sm-False-...`）。
        """
        return self.workspace_id.ws_hash

    def _generate_subdomain(self):
        """Generate the public subdomain (= user-facing hostname base).

        Deliberately env-token-FREE: the subdomain is the external URL and is
        immutable, so prod/stg are told apart by per-env ``paas_domain``, not by
        mutating the hostname (DP2). The env token rides the internal tunnel
        name instead — see ``_generate_tunnel_name``.
        """
        return make_smarthome_subdomain(self._naming_ws_hash(), self.reference_id, self.name)

    def _generate_tunnel_name(self):
        """Generate the Cloudflare *tunnel display name*, carrying the deploy-env token.

        A SmartHome owns a dedicated Cloudflare tunnel on the prod/stg-shared
        account; with no env token in its name — and, unlike CloudService, no
        shared-tunnel CNAME ``content`` clue — it cannot self-identify its
        environment (the orphan-attribution gap from cf-ns-orphan-no-account-gc).
        We stamp the env onto the *internal* tunnel name (e.g. ``paas-stg-sm-…``)
        while the public subdomain stays clean, mirroring SecurityAccess where
        the token rides the internal ``canonical_name`` rather than the route's
        user-facing ``subdomain_prefix``. The operator keeps the tunnel name and
        the DNS hostname independent (DNS is derived from the hostname, never the
        name — see operator tunnels.py), so the user-facing URL is unchanged.
        Teardown reads the stored ``tunnel_id``, never a recomputed name, so the
        token is reverse-lookup-safe (external-resource-env-token-naming 守則 3/4).
        """
        env = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.deploy_env', 'prod')
        return make_smarthome_subdomain(
            self._naming_ws_hash(), self.reference_id, self.name, env=env)

    def _get_paas_domain(self):
        """Get PaaS domain from system settings."""
        return self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io'
        )

    def _access_canonical_name(self):
        """本 SH 在 Cloudflare Access 上的命名根（帶 env token，#768）。

        單一裁決處：route 的 sync 與 teardown 兩條 payload 都讀它，兩邊各算一次
        就是漂移的起點。公式與「為什麼不能反推自 stored `subdomain`」寫在
        `services/naming.py::make_sm_access_canonical_name`。
        """
        self.ensure_one()
        deploy_env = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.deploy_env', 'prod')
        return make_sm_access_canonical_name(
            self._naming_ws_hash(), self.reference_id, self.name,
            env=deploy_env)

    def _ensure_primary_route(self):
        """確保這台 SH 有它的 primary route（冪等）。

        provision 成功後呼叫。route 是 Access 的錨點載體，沒有它租戶買了席位也
        沒有地方可以掛 policy。用 `search` 而非 `route_ids` 判存在：ir.rule 過濾
        後的 `route_ids` 對某些身分可能是空的，而「看不到」不等於「不存在」，
        誤判會撞上 primary 的唯一性守衛。
        """
        self.ensure_one()
        Route = self.env['woow_paas_platform.smart_home_route'].sudo()
        existing = Route.search([
            ('smart_home_id', '=', self.id), ('is_primary', '=', True),
        ], limit=1)
        if existing:
            return existing
        return Route.create({'smart_home_id': self.id, 'is_primary': True})

    def _teardown_routes_access(self, torn_down=None):
        """拆掉本 SH 所有 route 的 Cloudflare Access。回傳失敗的 route 標籤清單。

        刪除與 cron 清理兩條路徑共用。**不 raise**——兩個呼叫端對失敗的處置不同
        （`unlink` 要 fail-loud 讓交易回滾保住重試錨點；`_do_cleanup` 要落
        `error` 等下一次 cron）。

        #794：`torn_down` 是給 `unlink()` 用的**累加器**（``[(model, id), …]``），
        真的對 CF 拆成功的 route 錨點會 append 進去。刻意做成 out-parameter 而
        不是改回傳型別：回傳「失敗標籤清單」是兩個呼叫端共用的既有契約，改它會
        連帶動到 `_do_cleanup` 與四條既有測試，而那些跟本修法無關。
        ⚠ 為什麼 `unlink()` 需要它：拆成功之後這裡清掉的 `access_app_id` 只是
        DB 那半，CF 那半回捲不掉 ⇒ 同批後面任何一步 raise 都會讓這幾條 route
        帶著 `is_protected=True` 與死 app id 復活（#794 的形狀）。
        """
        self.ensure_one()
        Route = self.env['woow_paas_platform.smart_home_route'].sudo()
        failed = []
        for route in self.sudo().route_ids:
            info = route._capture_access_teardown()
            # #785：這個 `continue` 只吃得到「列上沒有任何 id 要拆」。「有東西
            # 但推不動」曾經也回 `None` 而從這裡被靜默吃掉、不計入 `failed`
            # ⇒ 兩個呼叫端（`unlink` 的 fail-loud、`_do_cleanup` 的落 error）
            # 都判成「這一輪乾淨」。那道預測性 liveness 閘已經拆掉，見
            # `smart_home_route._capture_access_teardown()` 的 docstring。
            if not info:
                continue
            # `torn_down` 交給 `_push_access_teardown` 在 operator 回 2xx 當下
            # 就記（見該方法的 [HIGH] 註解）——**不能**放在 `if ...:` 裡：落帳撞到
            # `OperationalError` 時它拿不到 True。
            if Route._push_access_teardown(info, torn_down=torn_down):
                _internal_sm_route_identity(route).access_app_id = False
                # #798：包 recordset 不是包 record。
                for pol in _internal_policy_cf_write(route.access_policy_ids):
                    if pol.cf_policy_id:
                        pol.cf_policy_id = False
            else:
                failed.append(info['label'])
        return failed

    def action_provision(self):
        """Provision the smart home by creating a dedicated Cloudflare Tunnel.

        **Idempotency guard（#484）**：這個 method 原本是**單段**的——``create_tunnel``
        一次做完全部，所以「``error`` ⇒ 遠端沒有資源」恆成立，retry 無腦重跑也安全，
        不需要 guard（``two-stage-remote-provision-idempotency`` 那份 pattern doc 正是
        拿本 method 當「單段免 guard」的對照組）。

        本次改動把它變成**雙結果**：operator 的 ``dns_failed=True`` 代表 tunnel 已經在
        Cloudflare 誕生、但 DNS record 沒建成，於是 ``error`` 從此可能與一條**活著且在
        計費**的 tunnel 共存。那份 pattern doc 的原話是「單段 method 的『免 guard 安全』
        是它的單段性換來的，不是天生的；一旦加第二段，這份安全就得用 guard 重新買回
        來」——所以下面的 stored-id guard 是這個修法的必要組成，不是附帶重構。

        **與 SecurityAccess 的 guard 有一處刻意不同**：SA 的 guard 分支可以只重跑第二段
        （declarative route sync）並收斂回 ``active``；SmartHome 沒有這個選項——operator
        只有 ``create_tunnel`` 與 routes sync 兩處會建 DNS record，前者會建**新** tunnel，
        後者需要 SmartHome 尚未建模的 route payload。既然平台無法修好 DNS，就不能假裝
        可以：guard 分支 fail-loud，把「已經有一條 tunnel、重跑會多漏一條」講清楚，並指出
        兩條安全出路（人工補 DNS，或刪掉重建——``action_delete`` 會連 tunnel 一起拆）。

        順帶：``/api/smart-homes`` 的 ``action='provision'`` **沒有 state 閘**（只驗
        OWNER/ADMIN ＋ 訂閱），所以對一個已經 ``active`` 的 Smart Home 再打一次
        provision，修前同樣會建第二條 tunnel 並蓋掉 ``tunnel_id``。同一道 guard 一併關掉。
        """
        self.ensure_one()

        # stored-id guard —— 必須在任何 write 之前（含 state='provisioning'），
        # 否則失敗的重試會把記錄推離 error 卻什麼也沒做成。
        if self.tunnel_id:
            raise UserError(
                "This Smart Home already has a Cloudflare tunnel (%s). "
                "Re-provisioning would create a second, separately billed tunnel "
                "and leave this one orphaned with no record pointing at it. "
                "If its DNS record is missing, ask an administrator to create it "
                "for %s; otherwise delete this Smart Home (which tears the tunnel "
                "down) and create it again."
                % (self.tunnel_id,
                   self.tunnel_route or self.subdomain or 'the configured hostname')
            )

        client = self._get_operator_client()

        self.write({'state': 'provisioning', 'error_message': False})

        try:
            # Generate subdomain
            subdomain = self._generate_subdomain()
            domain = self._get_paas_domain()
            hostname = f"{subdomain}.{domain}"

            # Create tunnel via operator. The tunnel *name* carries the deploy-env
            # token (internal traceability on the shared CF account); the
            # *hostname* stays env-clean so the user-facing URL is unchanged.
            result = client.create_tunnel(
                name=self._generate_tunnel_name(),
                hostname=hostname,
                service_url=f"http://localhost:{self.ha_port}",
            )

            # tunnel 已經誕生且在計費——不論下面判成功或失敗，這些欄位都要落地，
            # 否則就是一條沒有任何 DB row 指向的永久孤兒（F-ENV-4 守線）。
            tunnel_vals = {
                'tunnel_id': result.get('tunnel_id'),
                'tunnel_token': result.get('tunnel_token'),
                'tunnel_name': result.get('tunnel_name'),
                'tunnel_route': f"https://{hostname}",
                'subdomain': subdomain,
            }

            # #524：`tunnel_vals` 含身分錨點（tunnel_id / tunnel_name /
            # tunnel_token）⇒ 這是那道守衛唯一該開的閘之一。開閘掛在**這幾次
            # 寫入**上，不掛在 `self` 上，逃生口的表面積就只有真正的存值點。
            anchored = _internal_identity(self)

            if result.get('dns_failed'):
                # operator 把 CF 的 DNS 例外就地吞成 HTTP 200 ＋ dns_failed=True
                # （api/tunnels.py）——平台側 client 只在 HTTP >= 400 才 raise，所以
                # 這條路無例外可接，只能讀這個欄位。hostname 不會解析，服務實際上
                # 不可達，**不得**宣告 active。
                #
                # 注意 operator 註解假設「rely entirely on a later re-sync to
                # self-heal」——那個假設對 SmartHome 不成立：src/data/ 底下沒有任何
                # smart-home cron，沒有東西會回來補這筆 DNS。所以這裡必須留下
                # 人看得懂、修得動的訊息，而不是一句籠統的失敗。
                anchored.write(dict(
                    tunnel_vals,
                    state='error',
                    error_message=(
                        "Tunnel created but its DNS record could not be created, "
                        "so %s will not resolve. The tunnel (%s) is kept so it is "
                        "not orphaned — an administrator must create the DNS "
                        "record, or delete and re-create this Smart Home."
                        % (hostname, result.get('tunnel_id'))
                    ),
                ))
                _logger.error(
                    "[SH] provision left without DNS sh=%s tunnel=%s hostname=%s "
                    "— record kept in 'error' with the tunnel anchored",
                    self.id, result.get('tunnel_id'), hostname,
                )
                # #768：這條路徑也要建 route。tunnel 活著、subdomain 已落地、
                # 訂閱仍有效，租戶照樣能編輯 Access 設定（route 的 liveness gate
                # 刻意包含 `error`）；不建的話 Access 分頁會長出來卻沒有錨點。
                self._ensure_primary_route()
                return

            anchored.write(dict(
                tunnel_vals,
                state='active',
                deployed_at=fields.Datetime.now(),
            ))
            # #768：Access 錨點載體。subdomain 必須先落地（route 的 hostname 由
            # 它 compute），所以放在 write 之後。冪等，重跑不會長出第二條。
            self._ensure_primary_route()

            _logger.info(
                "Smart Home '%s' provisioned successfully. Tunnel ID: %s",
                self.name, result.get('tunnel_id'),
            )

        except PaaSOperatorConnectionError as e:
            self.write({
                'state': 'error',
                'error_message': f"Cannot connect to PaaS Operator: {e.message}",
            })
            _logger.error("Smart Home provision connection error: %s", e.message)
        except PaaSOperatorError as e:
            self.write({
                'state': 'error',
                'error_message': f"Tunnel creation failed: {e.message}",
            })
            _logger.error("Smart Home provision error: %s", e.message)

    def _assert_unlink_authorized(self):
        """#740：授權前置檢查 —— 送出第一個外部副作用**之前**，把這次刪除會用到的
        每一個 unlink 權限先驗完。

        ## 為什麼只能前置，不能換順序

        `unlink()` 裡「Cloudflare teardown 在 DB 列消失之前」是**刻意的**：反過來
        的話，刪除失敗就留下一顆沒有任何 DB 錨點可以驅動重試的永久孤兒——
        `docs/tracking/pitfalls/cf-ns-orphan-no-account-gc.md` 記的正是那次事故
        （本 model 當年無條件 unlink 造出 3 條孤兒 tunnel）。
        而 Odoo 的 ACL／record rule 檢查在 `super().unlink()` 的第一行才發生
        （`odoo/models.py::BaseModel.unlink` → `check_access('unlink')`）
        ⇒ 「外部副作用排在授權檢查前面」是這兩個約束的結構性後果，不是寫錯順序。
        既然順序不能換，就只能把授權檢查**提前**到副作用之前再做一次。

        ## 枚舉用提權身分，檢查用呼叫者身分（兩件事，刻意分開）

        * **枚舉**（`self.sudo().route_ids`）：teardown 迴圈走的就是提權後的集合
          （`_teardown_routes_access()` 迴圈的是 `self.sudo().route_ids`）。改用呼叫者
          看得見的集合，被 record rule 濾掉的那幾條就變成「看不到 ⇒ 不用檢查」，
          那是 fail-open，方向與本 issue 相同。
        * **檢查**（`self.env[...]`，非 sudo）：判準必須是**呼叫者**有沒有權限。
          形狀取自本 repo 既有的前置檢查前例
          `cloud_service._assert_reverse_binding_authorized()`（#698）：同樣是
          「sudo 收集要檢查的那一批 → 用呼叫者身分 `check_access`」。

        這兩件事必須分開的**機制理由**（不是風格）：`_check_access()` 的
        `ir.rule` 那一半只對真實 id 生效（`if any(self._ids):`）⇒ 送進去的 id 集合
        決定 record rule 會不會被評估。用呼叫者看得見的 `route_ids`，被 rule 濾掉的
        那幾條就根本不會被檢查，而 teardown 迴圈照樣會走到它們。

        ## 檢查範圍就是 `_SM_ROUTE_MODEL` 上方註解推導出來的那兩張，逐一列名

        **不含** `woow_paas_platform.security_access_access_policy`：policy 列是被
        route 的 `ondelete='cascade'` 在 **DB 層**帶走的，Python `unlink()` 根本不會
        被呼叫（`unlink()` 裡那段 ForeignKeyViolation 的註解記著同一件事）⇒ 這條
        路徑沒有消耗任何 policy 的 unlink 權限。而且它對 `base.group_user` 是
        `1,1,1,1` ⇒ 加進來是一個**永遠通過**的檢查：零鑑別力，還會讓下一個人以為
        DB cascade 那條有守。
        **不含** `woow_paas_platform.cf_access_policy_reap`：那張表**沒有**
        `base.group_user` 的 ACL 列（只有 `base.group_system` 那列），而本檔用得到
        的每一個存取點都自己提權 ⇒ 呼叫者身分從來不會被拿去驗它。

        ## 對提權路徑是 no-op

        `check_access()` 的第一行是 `if not self.env.su`（Odoo 18 `odoo/models.py`）
        ⇒ 平台自己驅動的刪除（`workspace.unlink()` 那條 `sudo().unlink()`）與 cron
        一律不受影響。下面第一行的 `if self.env.su: return` 因此**不改變語意**——它
        省掉的只是提權之下沒必要的那次 `route_ids` 讀取，並把豁免寫在看得見的地方。
        🔴 連帶：本守衛在 `TransactionCase` 的預設 env（su）底下是 no-op ⇒ 用預設
        env 寫的「守住了」測試**測不到它**
        （`docs/decisions/patterns/su-env-exempt-guard-false-green.md` 的形狀）。
        租戶那一側的回歸測試因此一律走真 HTTP，見
        `tests/test_sh_unlink_authz_preflight_740.py`。

        ⚠ 本方法**不改變授權姿態**：ACL 表、record rule、端點身分一律沒動 ⇒ 租戶的
        刪除仍然會失敗。它保證的是「失敗得乾淨」（零外部呼叫、零持久欄位變更），
        不是「會成功」。租戶到底該不該刪得掉是 #739 家族的題目。
        """
        if self.env.su:
            return
        # 主表先驗：被拒時的訊息才會點名租戶真正在刪的那個東西，而不是它從沒聽過
        # 的 route 子表（修前 B／C 兩形態回的就是後者）。
        self.check_access('unlink')
        # 🔴 **刻意沒有 `if route_ids:` 這個分支**，兩個理由：
        #  1. `_check_access()` 的模型級 `ir.model.access` 檢查是**無條件**跑的，
        #     只有 `ir.rule` 那一半被 `if any(self._ids):` 擋掉（Odoo 18
        #     `odoo/models.py::_check_access`）⇒ 空 recordset 仍然會問「你到底有沒有
        #     刪 route 的權限」。那正是我們要問的問題，與這一筆有沒有 route 無關。
        #     **這一行對「沒有 route 的記錄」不是死碼**，不要順手把它包進條件式。
        #  2. 加上分支就多一個 fail-open 面：`route_ids` 只要因為任何理由靜默變空
        #     （`active_test` 之類，#716 ② 在 CS 那腿實際發生過），檢查就整段被跳過。
        #     今天兩種寫法對所有身分的判定結果相同（唯一有 unlink 的群組兩張表都有），
        #     所以這裡選 fail-closed 的那個。
        self.env[_SM_ROUTE_MODEL].browse(
            self.sudo().route_ids.ids).check_access('unlink')

    def action_delete(self):
        """Delete the smart home and its Cloudflare Tunnel.

        Teardown + fail-loud orphan guard are delegated to unlink(): if the
        tunnel cannot be deleted, the record is kept and a UserError surfaces,
        so a billed tunnel is never left orphaned in Cloudflare with no DB row
        to drive a retry (F-ENV-4).

        #740：授權前置檢查在**第一行**，`state` 那次 write 之前。那次 write 不在
        `unlink()` 的 savepoint 裡（savepoint 在 `unlink()` 內部才開，而
        `_FlushingSavepoint.__init__` 是先 `cr.flush()` 再 `SAVEPOINT`
        ⇒ 這個 UPDATE 落在 savepoint **之外**、回捲不到）⇒ 修前一次被拒的刪除會
        留下 `state='deleting'` 跟著請求一起 commit。呼叫兩次（這裡 ＋ `unlink()`
        第一行）是刻意的：本方法要擋在自己那次 write 之前，而 `unlink()` 還有
        `action_delete` 以外的呼叫端。檢查是純權限判斷、無副作用，重複執行無害。
        """
        self.ensure_one()
        self._assert_unlink_authorized()
        if self.tunnel_id:
            self.write({'state': 'deleting'})
        self.unlink()

    def _cleanup_tunnel(self):
        """Best-effort idempotent Cloudflare tunnel teardown.

        Clears tunnel_id on success and KEEPS it on failure, so unlink() / a
        retry can detect a still-live tunnel and avoid orphaning it (symmetric
        with SecurityAccess._cleanup_cloudflare).
        """
        self.ensure_one()
        if not self.tunnel_id:
            return
        try:
            client = self._get_operator_client()
            # #531：claim 取自本列鏡回來的 CF name。⚠ 三個錨點是在 delete **成功
            # 之後**才一起清（見下方註解），所以呼叫當下 `tunnel_name` 一定還在。
            client.delete_tunnel(self.tunnel_id, tunnel_name=self.tunnel_name)
            _logger.info(
                "[SH] tunnel deleted sh=%s tunnel=%s", self.id, self.tunnel_id,
            )
            # #524：清空錨點也是寫錨點 —— 走同一個 sentinel 逃生口。
            #
            # **三個錨點一起清**（PR #738 覆核 round1 LOW）：tunnel 已經不存在了，
            # 留著 `tunnel_name` / `tunnel_token` 是過期的憑證與名稱。前者會經
            # `to_dict()` 出現在 API 回應（cron 的 `_do_cleanup` 路徑會把記錄留在
            # `state='deleting'`，那筆資料因此活得比 tunnel 久）；後者是 tunnel 憑證
            # 本身，雖有 `groups='base.group_system'` 遮罩、也不驅動任何後續呼叫，
            # 但沒有任何理由留一份指向已刪除資源的憑證。
            #
            # ⚠ 順序不變：**清空只在 `delete_tunnel` 成功之後**。失敗時三個都保留，
            # `unlink()` 的 fail-loud guard 與 `_do_cleanup` 的重試分支都判
            # `if self.tunnel_id:`，語意不受本次擴充影響。
            _internal_identity(self).write({
                field: False for field in _IDENTITY_ANCHOR_FIELDS})
        except Exception as e:
            _logger.warning(
                "[SH] tunnel cleanup failed sh=%s: %s — kept for retry",
                self.id, e,
            )

    def unlink(self):
        """Direct-unlink guard: the Cloudflare tunnel must never outlive the
        record.

        Without this, a transient delete_tunnel failure would drop the DB row
        while a billed, token-still-valid tunnel lives on in Cloudflare with no
        DB reference left to drive a retry — a permanent orphan
        (cf-ns-orphan-no-account-gc: SmartHome's unconditional unlink was the
        source of 3 such orphan tunnels). Mirrors SecurityAccess.unlink: attempt
        cleanup, then fail-loud if the tunnel is still live so both the DB row
        and the CF tunnel survive for the user / cron to retry.

        #740：第一行是授權前置檢查，而且刻意放在下面那個 `try:`／savepoint
        **之外**——被拒的呼叫不該進入補償路徑，也不該碰到後面任何一段有副作用或
        有成本的碼。理由與範圍全寫在 `_assert_unlink_authorized()` 的 docstring。
        """
        self._assert_unlink_authorized()
        # #322: 比照 CloudService（#318）— super().unlink() 前捕獲 in_progress
        # 訂閱，刪除成功後結算（取消＋按比例退點）。共用 _settle_on_entity_delete。
        # 捕獲在 fail-loud 之前、結算在 super().unlink() 之後：tunnel fail-loud
        # raise 時不結算，全交易 rollback。
        subs_to_settle = self.sudo().mapped('subscription_id').filtered(
            lambda s: s.state == 'in_progress'
        )
        # 🔴 #794：本段**新增一層 savepoint**（本 method 原本沒有）。與 CS 那腿
        # 同一個理由，而且這裡的後果更重：`_teardown_routes_access()` 成功之後
        # 只清得掉 DB 那半，CF 上的 Access app 已經沒了、回捲不掉；接著任何一步
        # raise（tunnel 刪不掉那格尤其可達）都會讓整筆交易被丟棄 ⇒ route 帶著
        # `is_protected=True` 與死 app id 復活，**而 tunnel 正是因為刪不掉才活著**
        # ⇒ hostname 照常服務、CF 上沒有任何登入強制、UI 顯示已保護。
        #
        # savepoint 在這裡是**功能性**的：補償寫跑在獨立連線上，只要外層交易對
        # 那幾列的 UPDATE 還活著，獨立交易就會撞 PG 寫寫衝突等到 lock_timeout。
        # 先回捲、再補償，是補償寫得進去的前提。
        torn_down = []
        Reap = self.env[_CF_REAP_MODEL]
        try:
            with self.env.cr.savepoint():
                for record in self:
                    # #768：Cloudflare Access app 與 reusable policy 必須在 route 列消失
                    # 之前拆掉——`ondelete='cascade'` 是 **DB 層**的，child 的 Python
                    # `unlink()` 根本不會被呼叫（SA 也是為此在 `SecurityAccess.unlink`
                    # 顯式呼叫 `self.route_ids.unlink()`）。漏掉的話每刪一台 SH 就永久
                    # 孤兒一組 CF 物件，而且刪掉的列連重試錨點都不剩。
                    failed = record._teardown_routes_access(
                        torn_down=torn_down)
                    if failed:
                        raise UserError(
                            "Cloudflare Access teardown failed for route(s): %s. The "
                            "Smart Home was kept so the deletion can be retried; its "
                            "Access app is still live in Cloudflare."
                            % ', '.join(str(f) for f in failed)
                        )
                    # 🔴 route（連帶它底下的 policy）必須在 `super().unlink()` **之前**
                    # 顯式刪掉，不能交給 DB 的 `ON DELETE CASCADE`。實測（本機 paas-odoo
                    # image，`test_D2`）：
                    #
                    #     DELETE FROM woow_paas_platform_smart_home WHERE id IN (297)
                    #     → ForeignKeyViolation: insert or update on table
                    #       "…_security_access_access_policy" violates foreign key
                    #       constraint "…_smart_home_route_id_fkey"
                    #       DETAIL: Key (smart_home_route_id)=(322) is not present…
                    #
                    # 成因是同一次 cascade 裡有**兩條方向不同的邊**：policy 的
                    # `smart_home_id` 是 stored related M2O，Odoo 給它的是預設的
                    # `ondelete='set null'` ⇒ 刪 SH 時 PG 要對 policy 列發一次 UPDATE，
                    # 而那一刻 route 列已經被另一條 cascade 帶走了。SA 在
                    # `SecurityAccess.unlink` 顯式 `self.route_ids.unlink()` 就是為了同
                    # 一件事（那邊還多一個理由：child 的 Python unlink 在 DB cascade 下
                    # 根本不會被呼叫）。
                    #
                    # 這一步不會重複打 operator：上面的 `_teardown_routes_access()` 成功
                    # 後已經把 `access_app_id` 與每條 policy 的 `cf_policy_id` 清空，
                    # route 的 `_capture_access_teardown()` 因此回 None。
                    _sm_route_cascade_unlink(record.route_ids).unlink()
                    if record.tunnel_id:
                        record._cleanup_tunnel()
                    if record.tunnel_id:
                        raise UserError(
                            "Cloudflare cleanup failed; the tunnel is still live and "
                            "billed. The record was kept so the deletion can be "
                            "retried. Please try again shortly."
                        )
                res = super().unlink()
                for sub in subs_to_settle:
                    sub._settle_on_entity_delete(reason='Smart Home deleted')
        except Exception:
            Reap._compensate_fail_open_after_rollback(
                torn_down, _logger,
                '[SH] smart home unlink (sh=%s)' % (self.ids,))
            raise
        return res

    def action_refresh_status(self):
        """Refresh tunnel status from the PaaS Operator."""
        self.ensure_one()
        if not self.tunnel_id:
            return

        client = self._get_operator_client()

        try:
            result = client.get_tunnel_status(self.tunnel_id)

            # Parse connections
            connections = result.get('connections', [])
            update_vals = {
                'tunnel_status': 'connected' if connections else 'disconnected',
            }

            if connections:
                conn = connections[0]
                update_vals['connector_id'] = conn.get('connector_id', '')
                update_vals['connector_type'] = conn.get('type', '')
                if conn.get('opened_at'):
                    update_vals['tunnel_uptime'] = conn['opened_at']

            self.write(update_vals)

        except PaaSOperatorError as e:
            self.write({
                'tunnel_status': 'error',
                'error_message': f"Status refresh failed: {e.message}",
            })
            _logger.warning(
                "Failed to refresh tunnel status for Smart Home '%s': %s",
                self.name, e.message,
            )

    def _get_mcp_integration(self):
        """Return the MCP integration string granted by the active plan, or ''.

        Mirrors _get_subscription_info's ``self.sudo().subscription_id`` read:
        subscription_id is ACL-restricted, so a non-sudo caller (portal user)
        would hit AccessError without the sudo. Derivation follows the SA
        pattern (sub.sh_subscription_plan_id.<field>).
        """
        self.ensure_one()
        sub = self.sudo().subscription_id
        plan = sub.sh_subscription_plan_id if sub else False
        return (plan.mcp_integration or '') if plan else ''

    def to_dict(self, include_details=False):
        """Serialize smart home data for API responses.

        ``include_details``（#768）：只有 **detail** 端點帶 `routes`。list 端點會
        為 workspace 的每一台 SH 各 walk 一次 `access_policy_ids`，而 Access 分頁
        只存在於詳情頁——CS 早就為同一個理由把 `access` 限定在
        `include_details=True` 那一側（`controllers/paas.py::_format_service`）。
        顯示條件本身（`subscription.ac_max_access_seats`）走 subscription，兩側都
        有，所以清單頁不會因此少東西。
        """
        self.ensure_one()
        data = {
            'id': self.id,
            'name': self.name,
            'workspace_id': self.workspace_id.id,
            'state': self.state,
            'tunnel_id': self.tunnel_id or '',
            'tunnel_name': self.tunnel_name or '',
            'tunnel_route': self.tunnel_route or '',
            'tunnel_status': self.tunnel_status or 'disconnected',
            'tunnel_uptime': self.tunnel_uptime or '',
            'connector_id': self.connector_id or '',
            'connector_type': self.connector_type or '',
            'ha_port': self.ha_port,
            'subdomain': self.subdomain or '',
            'error_message': self.error_message or '',
            'deployed_at': self.deployed_at.isoformat() if self.deployed_at else '',
            'created_at': self.create_date.isoformat() if self.create_date else '',
            'subscription': self._get_subscription_info(),
            'mcp': self._get_mcp_integration(),
        }
        if include_details:
            data['routes'] = [r.to_dict() for r in self.route_ids]
        return data

    # ------------------------------------------------------------------ #
    #  Subscription lifecycle: suspend / resume / cleanup                #
    # ------------------------------------------------------------------ #

    def _do_suspend(self):
        """Update tunnel ingress to return 503."""
        self.ensure_one()
        _logger.info(
            "[SH] _do_suspend sh=%s name=%s state=%s sub=%s",
            self.id, self.name, self.state,
            self.subscription_id.id if self.subscription_id else None,
        )
        if self.state == 'suspended':
            return

        if self.tunnel_id:
            try:
                client = self._get_operator_client()
                hostname = f"{self.subdomain}.{self._get_paas_domain()}"
                client.configure_tunnel(
                    self.tunnel_id,
                    hostname=hostname,
                    service_url="http_status:503",
                    tunnel_name=self.tunnel_name,  # #531 歸屬主張
                )
                _logger.info(
                    "[SH] _do_suspend tunnel 503 sh=%s tunnel=%s",
                    self.id, self.tunnel_id,
                )
            except Exception as e:
                _logger.warning(
                    "[SH] _do_suspend tunnel failed sh=%s: %s",
                    self.id, e,
                )
                self.write({'state': 'error'})
                return

        self.write({
            'original_service_url': f"http://localhost:{self.ha_port}",
            'pre_suspend_state': self.state if self.state in ('active', 'error') else 'active',
            'state': 'suspended',
        })
        _logger.info("[SH] _do_suspend done sh=%s new_state=suspended", self.id)

    def _do_resume(self):
        """Restore tunnel ingress to original service URL."""
        self.ensure_one()
        _logger.info(
            "[SH] _do_resume sh=%s name=%s state=%s pre_suspend=%s",
            self.id, self.name, self.state, self.pre_suspend_state,
        )
        if self.state != 'suspended':
            return

        service_url = self.original_service_url or f"http://localhost:{self.ha_port}"
        if self.tunnel_id:
            try:
                client = self._get_operator_client()
                hostname = f"{self.subdomain}.{self._get_paas_domain()}"
                client.configure_tunnel(
                    self.tunnel_id,
                    hostname=hostname,
                    service_url=service_url,
                    tunnel_name=self.tunnel_name,  # #531 歸屬主張
                )
                _logger.info(
                    "[SH] _do_resume tunnel restored sh=%s tunnel=%s",
                    self.id, self.tunnel_id,
                )
            except Exception as e:
                _logger.warning(
                    "[SH] _do_resume tunnel failed sh=%s: %s",
                    self.id, e,
                )
                self.write({'state': 'error'})
                return

        restore_state = self.pre_suspend_state or 'active'
        self.write({
            'state': restore_state,
            'pre_suspend_state': False,
        })
        # #768：停權期間 route 的 Access 設定可能被改過（BFF 沒有 state 閘），
        # 而 route 的 liveness gate 刻意排除 `suspended`（那時 ingress 已是 503，
        # Access app 留著無害）。resume 是刻意的收斂點，補一次 forced sync；沒有
        # 它的話「停權期間被關掉保護的 route」永遠不會被推回 Cloudflare，而症狀
        # 與「保護正常運作」完全一樣。
        for route in self.route_ids:
            _sm_forced_access_sync(route)._sync_access_to_operator()
        _logger.info(
            "[SH] _do_resume done sh=%s new_state=%s",
            self.id, restore_state,
        )

    def _do_cleanup(self):
        """Delete tunnel permanently (cron path).

        Mirrors SecurityAccess._do_cleanup fail-loud contract: the cron calls
        _do_cleanup (not unlink), so this path is NOT covered by the unlink
        guard and must not silently swallow a delete_tunnel failure then write
        'deleting' (a false success signal — the subscription is already
        cancelled so the cron never revisits, leaving a billed orphan tunnel).
        _cleanup_tunnel keeps tunnel_id on failure; if it is still set, land in
        'error' so the next cron pass retries (F-ENV-4 M1).
        """
        self.ensure_one()
        _logger.info(
            "[SH] _do_cleanup sh=%s name=%s tunnel=%s",
            self.id, self.name, self.tunnel_id,
        )
        # #768：cron 清理路徑不走 `unlink()`，所以也不會經過那裡的 Access
        # teardown。漏掉的話「訂閱取消 → cron 拆 tunnel」會留下一組活著的 CF
        # Access app／reusable policy，而 cron 之後永遠不會再回來看這筆。
        failed_access = self._teardown_routes_access()
        # #815：**事後複查**——不信 `_teardown_routes_access()` 的回傳值，teardown
        # 之後回頭看列上還有沒有 Access 物件 id（SA 那腿的 `_do_cleanup` 早就有
        # 這道，見 `security_access.py` 的 `lingering_access`）。
        #
        # ⚠ 這**不是**在修一個今天會出事的 bug：那支 helper 裡「push 回 True」與
        # 「清 id」綁在同一個 `if` 的同一段、失敗才 `failed.append(...)` ⇒ 今天
        # 「`failed` 是否為空」與「事後查 `_has_access_teardown()`」是等價的兩把
        # 尺，量同一件事。補的是**冗餘**：下一個改 `_teardown_routes_access()`
        # 的人只要讓「清 id」與「計 failed」分家，兩把尺才會分歧，而分歧的方向
        # 是這裡無聲放過殘留（#785 原症狀同型）。
        #
        # 用 `self.sudo().route_ids` 而不是裸 `route_ids`（SA 那腿是後者）：對照
        # 的必須是 helper 真的走過的那一批——它迴圈的是 `self.sudo().route_ids`
        # （:497）。ir.rule 過濾後的 `route_ids` 對某些身分可能少幾條，「看不到」
        # 不等於「不存在」，而漏看的方向是 fail-open：teardown 迴圈處理過的 route
        # 事後複查看不見。`_has_access_teardown()` 還要讀 `access_policy_ids`，
        # 同一層問題再一次。
        #
        # ⚠ **今天這一個 `.sudo()` 是 no-op，不是在補一個現行洞**：本 method 的
        # 唯一呼叫端是 `subscription_lifecycle_cron.py` 的
        # `_cron_enforce_subscription_lifecycle()`，而那筆 `ir.cron`
        # （`data/subscription_lifecycle_cron.xml`）沒有指定 `user_id` ⇒ 取安裝
        # 當下的 env user，也就是 `__system__`（uid 1）。Odoo 用
        # `api.Environment(cr, job['user_id'], {})` 起 cron 的 env，而
        # `Environment.__new__` 對 `uid == SUPERUSER_ID` 直接 `su = True`
        # ⇒ ir.rule 整段不套用。寫 `.sudo()` 買的是「兩把尺的存取姿態一致」這條
        # 不變式：`user_id` 是可改欄位（ir.cron 的 Scheduler User），改成非
        # superuser 的那天，沒有 `.sudo()` 的那一把才會開始漏看。
        #
        # 🔴 這個差別**測不出來**：`TransactionCase` 的 env 本身就是 su ⇒ sudo 與
        # 非 sudo 輸出相同（`docs/decisions/patterns/su-env-exempt-guard-false-green.md`
        # 的形狀）。所以它靠這段註解與 review 守，不靠測試。
        lingering_access = self.sudo().route_ids.filtered(
            lambda r: r._has_access_teardown())
        if failed_access or lingering_access:
            # 兩格共用同一個 `state='error'`，所以訊息必須分得開——否則新防線真
            # 的響時輸出與舊分支一模一樣，等於加了一道永遠不知道有沒有響過的守
            # 衛。`failed_access` 那條的措辭**逐字不動**（#785／PR #813 的成敗語
            # 意表在斷言它）；lingering-only 才走新措辭。
            if failed_access:
                error_message = (
                    "Cloudflare Access teardown failed for route(s): %s. Will "
                    "retry on the next cron pass." % ', '.join(
                        str(f) for f in failed_access)
                )
            else:
                error_message = (
                    "Cloudflare Access teardown reported success but route(s) "
                    "%s still hold Access object id(s); the CF Access app and "
                    "its reusable policies are still live. Will retry on the "
                    "next cron pass." % ', '.join(
                        str(rid) for rid in lingering_access.ids)
                )
            self.write({'state': 'error', 'error_message': error_message})
            _logger.warning(
                "[SH] _do_cleanup access teardown incomplete sh=%s routes=%s "
                "lingering_access=%s (state=error)",
                self.id, failed_access, lingering_access.ids)
            return
        # ⚠ 位置：上面那道閘必須留在這一行**之前**。SA 把複查放在
        # `_cleanup_cloudflare()` 之後是因為它把「拆 access」與「刪 tunnel」包在
        # 同一個 call 裡；SH 是兩段，照抄位置語意會變成「先把 tunnel 刪掉、再抱
        # 怨 access 沒清乾淨」，與 #785 C2 鎖住的「access 拆不掉絕不到達
        # delete_tunnel」相反（那顆 tunnel 還在計費，是重試唯一的錨點）。
        self._cleanup_tunnel()
        if self.tunnel_id:
            self.write({
                'state': 'error',
                'error_message': (
                    "Cloudflare cleanup failed; the tunnel is still live and "
                    "billed. Will retry on the next cron pass."
                ),
            })
            _logger.warning(
                "[SH] _do_cleanup incomplete sh=%s tunnel=%s (state=error)",
                self.id, self.tunnel_id,
            )
            return
        self.write({'state': 'deleting'})
