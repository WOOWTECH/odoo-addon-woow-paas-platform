import logging
import re
import uuid

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services.naming import make_security_access_subdomain

from ..services.paas_operator import (
    get_paas_operator_client,
    PaaSOperatorError,
    PaaSOperatorConnectionError,
)

_logger = logging.getLogger(__name__)

#: #528 的待回收帳本。用字串而不是 import 具體 class：三腿都要用它，而
#: `env[...]` 這條路不受 model 檔的 import 順序影響。
_CF_REAP_MODEL = 'woow_paas_platform.cf_access_policy_reap'

# A subdomain prefix is a SINGLE DNS label: 3-63 chars of lowercase letters,
# digits and hyphens only — NO dots (a dot would split it into multiple labels)
# and no other symbols/uppercase/spaces; must not start or end with a hyphen.
_PREFIX_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$')


# ---------------------------------------------------------------------- #
#  Identity anchors                                                      #
# ---------------------------------------------------------------------- #
#
# An identity anchor is a field whose value decides WHICH external
# (Cloudflare-side) object this row is bound to, or from which that binding is
# derived. Moving one is not "editing a setting": it re-aims the whole record —
# its tunnel, its DNS records, its Access application — at somebody else's
# resource, while the row keeps its own workspace's ir.rule blessing.
#
# These tuples are the SINGLE declarative source for the create()/write()
# guards on both models below. #522 happened because protection was applied per
# field by hand — create() popped two literals, write() blocked the same two —
# so `tunnel_id`, added later, was simply never covered and a plain
# base.group_user workspace owner could repoint a Security Access at an
# arbitrary tunnel over /web/dataset/call_kw. Adding one more literal would not
# have stopped the next one; deriving the guards from a list means a new anchor
# is protected the moment it is listed here.
#
# NOT anchors, deliberately:
#   * route.subdomain_prefix — user-CHOSEN at create (so it must not be
#     stripped there), only immutable afterwards; it keeps its own guard below;
#   * connector_id / connector_type / connector_version / tunnel_status /
#     tunnel_uptime / edge_connection_count — poll-refreshed status, not
#     bindings; forging one changes what a screen displays, not what any
#     Cloudflare call targets.
_IDENTITY_ANCHOR_FIELDS = (
    'canonical_name',   # the tunnel's CF name + the subdomain stamp
    'reference_id',     # hash salt the canonical_name/subdomain derive from
    'tunnel_id',        # WHICH Cloudflare tunnel this row drives (#522)
    'tunnel_name',      # its CF-side name, mirrored back from create_tunnel
    'tunnel_token',     # the tunnel credential itself
)

_ROUTE_IDENTITY_ANCHOR_FIELDS = (
    'dns_record_id',    # WHICH CF DNS record this route reconciles/deletes
    'access_app_id',    # WHICH CF Access application this route configures
)


class _InternalIdentityWrite:
    """Marker type for the identity-anchor escape hatch — see below."""
    __slots__ = ()

    def __repr__(self):
        return '<internal identity write>'


# The single deliberate escape hatch: server-owned provisioning/sync code that
# legitimately stores the ids Cloudflare just handed back opts in by putting
# THIS OBJECT in the context.
#
# The value has to be the sentinel rather than True, and that is a security
# property, not a style choice: /web/dataset/call_kw hands the CALLER's context
# straight to the ORM — web/controllers/dataset.py passes the client's kwargs
# through untouched and odoo/api.py::call_kw does
#   context = kwargs.pop('context', None) or {}; recs = recs.with_context(context)
# — so any guard keyed on a JSON-expressible value is opened by adding one key
# to the RPC payload. Measured on 18.0 before this change: a plain
# base.group_user tenant sending context={'sa_internal_identity_write': true}
# rewrote canonical_name, and the new value was committed to Postgres. JSON
# cannot carry object identity, so the sentinel closes that door while every
# in-process call site keeps working exactly as before.
#
# Deliberately NOT an `env.su` exemption either: su would silently disable the
# guard inside every TransactionCase (su env) and every sudo()'d controller
# path — precisely where a test would otherwise prove that it works.
_INTERNAL_IDENTITY_WRITE = _InternalIdentityWrite()


def _internal_identity(records):
    """Return ``records`` in the one context allowed to write identity anchors."""
    return records.with_context(
        sa_internal_identity_write=_INTERNAL_IDENTITY_WRITE)


def _is_internal_identity_write(env):
    return (env.context.get('sa_internal_identity_write')
            is _INTERNAL_IDENTITY_WRITE)


# ---------------------------------------------------------------------- #
#  Sync-control escape hatches (#536)                                    #
# ---------------------------------------------------------------------- #
#
# 三個「這一次 ORM 操作要怎麼跟 operator 互動」的內部開關。與上面的身分錨點
# 逃生門**同一個威脅模型、同一個修法**（#522 修的是那一支，#536 是剩下的三支）：
# `/web/dataset/call_kw` 把呼叫端的 context 整包換進 env，所以任何只判「truthy」
# 的 context key，租戶在 JSON-RPC payload 加一行就打得開。
#
# 三個 key 各自的能力刻意分開（不合併成一把萬能鑰匙）——理由與
# `sa_internal_prefix_write` 相同：需要抑制同步的 migration，不該因此也能繞過
# 「≥1 include」驗證。**共用同一個 sentinel 值**則是安全上等價的：真正把租戶
# 擋在外面的是「JSON 表達不出物件同一性」，而**哪一種能力**由 key 決定。
#
#   * sa_skip_route_sync   —— 這一次 create/write/unlink 只搬 DB 狀態，不對
#                             operator 做宣告式推送（migration / provision 的
#                             大量 row 搬移，CF 由那些流程自己的顯式 re-sync
#                             收斂）。偽造後果：租戶標為 is_protected 的 route
#                             的 Access app 建立正好是被抑制的那一步，而路由
#                             推送是全量的 ⇒ 之後任一次合法異動會把這條 route
#                             以「沒有 Access gate」的狀態送上線。
#   * sa_rule_replace      —— 正在做整組 rule 替換（controller 的 (5,0,0) +
#                             creates）：抑制 rule 層的過渡同步，**並且**跳過
#                             rule.unlink() 裡的「≥1 include」檢查（替換途中
#                             必然經過空集）。偽造後果不只是不同步——那條
#                             `@api.constrains('rule_ids')` 對「直接 unlink 一
#                             條 rule」根本不會觸發，unlink 裡的顯式呼叫是唯一
#                             防線 ⇒ 租戶可留下 0 條 include 的 policy。
#   * sa_force_access_sync —— 繞過 Access 推送 liveness gate 的**狀態那一半**
#                             （tunnel_id 仍然必要）：provision / resume 這些
#                             收斂點跑在 SA 狀態還沒回到 'active' 之前。偽造
#                             後果：suspended / deleting 狀態仍能把自己 route
#                             的 Access 設定推去 Cloudflare。
#                             ⚠ 這個 key 背後還有一個**更大的、本次沒處理的**
#                             家族問題：「停權未全面阻斷 operator 呼叫」。堵住
#                             這把鑰匙只是拿掉其中一條路，不等於停權中的租戶
#                             就碰不到 operator 了 —— 那是另案（#536 明列）。
#
# ⚠ 這三個 key 的 KEY 名沒有改，改的是**值**：`with_context(key=False)` 的
# 「關掉」語意因此原封不動，而 `with_context(key=True)` 從此不再開閘。後者是
# fail-safe 的方向（多打一次冪等的全量推送，而不是漏掉一次），但它是**無聲**
# 的，所以下面的判定函式在看到「truthy 但不是 sentinel」時會 WARNING —— 那既是
# 偽造的訊號，也是「未來有人照舊寫法傳 True」的訊號。
class _InternalSyncControl:
    """Marker type for the sync-control escape hatches — see above."""
    __slots__ = ()

    def __repr__(self):
        return '<internal sync control>'


_INTERNAL_SYNC_CONTROL = _InternalSyncControl()

_CTX_SKIP_ROUTE_SYNC = 'sa_skip_route_sync'
_CTX_RULE_REPLACE = 'sa_rule_replace'
_CTX_FORCE_ACCESS_SYNC = 'sa_force_access_sync'


def _sync_control(records, *keys):
    """Return ``records`` in the one context allowed to steer operator sync."""
    return records.with_context(
        **{key: _INTERNAL_SYNC_CONTROL for key in keys})


def _skip_route_sync(records):
    """開閘：這一次操作只搬 DB 狀態，不對 operator 做宣告式推送。"""
    return _sync_control(records, _CTX_SKIP_ROUTE_SYNC)


def _rule_replace(records):
    """開閘：整組 rule 替換途中，抑制逐條同步與「≥1 include」過渡檢查。"""
    return _sync_control(records, _CTX_RULE_REPLACE)


def _force_access_sync(records):
    """開閘：繞過 Access 推送 liveness gate 的狀態那一半（仍需 tunnel_id）。"""
    return _sync_control(records, _CTX_FORCE_ACCESS_SYNC)


def _is_sync_control(env, key):
    """這個 sync-control 開關是不是**伺服器端程式碼**設的（而非呼叫端偽造的）。

    只有 sentinel 物件算數。JSON 表達不出物件同一性，所以租戶送
    ``context={"sa_skip_route_sync": true}`` 進 ``call_kw`` 時走的是下面那條
    WARNING —— 守衛不開閘，操作照常推送／照常驗證。
    """
    value = env.context.get(key)
    if value is _INTERNAL_SYNC_CONTROL:
        return True
    if value:
        _logger.warning(
            "Ignoring non-sentinel %r in context (value=%r, uid=%s): this "
            "hatch only opens for in-process callers using the module's "
            "helper — see _INTERNAL_SYNC_CONTROL (#536). Either a forged RPC "
            "context, or in-process code still passing True.",
            key, value, env.uid)
    return False


def _server_owned_anchor_value(model, field_name):
    """一個身分錨點在 create 當下該落地的**伺服器端值**。

    有欄位級 ``default`` 的走它（``reference_id`` 的 uuid4），其餘壓 ``False``。

    刻意讀 ``field.default`` 而不是在守衛旁邊自己複製一份 uuid4：那正是
    ``odoo/models.py::default_get`` 的**第 3 順位**，也是三個載體裡唯一呼叫端
    碰不到的那個（第 1 是 context 的 ``default_<field>``、第 2 是 ``ir.default``
    的列，兩者都由租戶決定——見 ``_strip_identity_anchors``）。從欄位定義推導也
    讓「將來新增一個帶 default 的錨點」自動正確，不必在守衛旁邊維護第二張對照
    表——#522 的成因正是逐欄位手列而漏掉 ``tunnel_id``。

    ⚠ 目前所有錨點都是純量欄位（Char）。若哪天出現 x2many 錨點，這裡要補
    ``default_get`` 尾段那套 ``Command`` 轉換。
    """
    field = model._fields[field_name]
    return field.default(model) if field.default else False


def _strip_identity_anchors(model, vals_list, anchors):
    """create()-side half of the guard: 把身分錨點**壓成伺服器端值**，不是 pop。

    ``super().create()`` does not route through ``write()``, so the write guard
    alone would leave a create-time squat open — a brand-new row bound to a
    victim's tunnel / DNS record / Access app before anything is provisioned
    (action_provision's ``if self.tunnel_id:`` branch would then skip tunnel
    creation and push this tenant's routes onto it). Applies to EVERYONE
    including superuser; the sentinel context is the only exemption. 壓值而不是
    raise：``copy()`` 與一般資料載入照樣要能跑，而一份不帶錨點的複本正是複本
    該有的樣子。

    🔴 **為什麼是壓值而不是 ``vals.pop(field, None)``**（#753，實測紅過）：

    ``super().create()`` 內部會呼叫 ``_add_missing_default_values(vals)``，把
    「vals 裡**缺席**」的欄位從三個載體補回來（``default_get`` 的順位）：

        1. ``default_<field>`` context —— ``/web/dataset/call_kw`` 把呼叫端
           context 整包換進 env（``odoo/api.py::call_kw``），這正是本檔
           ``_INTERNAL_IDENTITY_WRITE`` 自述的威脅模型；
        2. ``ir.default`` 的列 —— 租戶對它有完整 CRUD（base 的
           ``access_ir_default_group_user`` = 1,1,1,1），``_get_model_defaults``
           收 ``user_id = 自己`` 那些列。**這條與呼叫端 payload 無關**，走
           ``type="json"`` 的 BFF 端點（context 來自 session）一樣生效；
        3. 欄位自身的 ``default=`` —— 伺服器端程式碼，呼叫端碰不到。

    ``pop`` 恰恰把 anchor 變成**缺席** ⇒ 前兩個（租戶可控的）載體在剝除**之後**
    才落地。把 key 留在 vals 裡並給定伺服器端值，才是一次涵蓋所有載體的做法：
    ``_add_missing_default_values`` 只補「不在 vals 裡」的欄位，而且結尾是
    ``defaults.update(values)``（vals 的明確值永遠壓過 default）。

    ⚠ 「先 ``_add_missing_default_values(vals)`` 解析、再從解析結果 pop」**同樣
    是錯的**：pop 之後 key 又缺席，``super().create()`` 內部會再解析一次、再補
    一次。解析後的 vals 適合用來**判斷**，不適合用來**剝除**。

    ⚠ 壓的值取自 ``_server_owned_anchor_value`` 而不是一律 ``False``：SA 的
    ``reference_id`` 是 ``required=True`` ＋ ``default=uuid4``，壓 False 會讓
    required 檢查在 ``super().create()`` 內炸掉，**破壞所有合法 create**——而
    「攻擊者的值沒落地」那幾條斷言對「create 直接失敗」同樣是綠的，所以這個炸點
    只有陰性對照抓得到（見 ``test_sa_anchor_default_carriers_753.py`` 的 D 系列）。

    同型缺陷本 repo 已修過兩次：``smart_home._strip_identity_anchors``（PR #738
    覆核 round1）與 ``cloud_service._strip_cs_access_identity_anchors``（#705 F1
    ／PR #752）。兩者的錨點都沒有欄位級 default，所以那邊壓 ``False`` 就夠。
    """
    if _is_internal_identity_write(model.env):
        return
    for vals in vals_list:
        for field in anchors:
            vals[field] = _server_owned_anchor_value(model, field)


def _reject_identity_anchor_writes(env, vals, anchors):
    """write()-side half of the guard: refuse to move an anchor, loudly.

    Hard invariant for EVERYONE including superuser — see
    ``_INTERNAL_IDENTITY_WRITE`` for why neither ``env.su`` nor a plain truthy
    context value may open it.
    """
    if _is_internal_identity_write(env):
        return
    for field in anchors:
        if field in vals:
            raise UserError(
                "Field '%s' is immutable after creation." % field
            )


class SecurityAccess(models.Model):
    _name = 'woow_paas_platform.security_access'
    _inherit = ['woow_paas_platform.subscription_lifecycle_mixin']
    _description = 'Security Access Instance'
    _order = 'create_date desc'

    # Identity
    name = fields.Char(
        string='Name',
        required=True,
        help='User-defined nickname for this security access (e.g. '
             'secure-gateway). The Cloudflare-facing identity is '
             'canonical_name, which never changes.',
    )
    reference_id = fields.Char(
        string='Reference ID',
        required=True,
        copy=False,
        index=True,
        default=lambda self: str(uuid.uuid4()),
        help='Unique reference ID for hash-based naming. Immutable.',
    )
    canonical_name = fields.Char(
        string='Canonical Name',
        readonly=True,
        copy=False,
        index=True,
        help='paas-sa-{ws_hash}-{res_hash}. Computed ONCE at create and '
             'stored — the naming hash salts on the (mutable) nickname, so '
             'recomputing would drift the subdomain and break routes. Used '
             'as tunnel name and stamped on every Cloudflare object for '
             'traceability.',
    )
    workspace_id = fields.Many2one(
        comodel_name='woow_paas_platform.workspace',
        string='Workspace',
        required=True,
        ondelete='cascade',
        help='Workspace this security access belongs to',
    )
    template_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access_template',
        string='Template',
        help='Catalog template this instance was created from',
    )

    # State
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('provisioning', 'Provisioning'),
            ('active', 'Active'),
            # D1: tunnel provisioned but has zero application routes — reachable
            # at nothing yet (catch-all 404). Distinct from 'active' so monitoring
            # can tell a routed tunnel from an empty one.
            ('active_no_route', 'Active (No Route)'),
            ('error', 'Error'),
            ('suspended', 'Suspended'),
            ('deleting', 'Deleting'),
        ],
        string='State',
        default='pending',
        required=True,
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
    tunnel_status = fields.Selection(
        selection=[
            ('connected', 'Connected'),
            ('disconnected', 'Disconnected'),
            ('error', 'Error'),
        ],
        string='Tunnel Status',
        default='disconnected',
    )
    tunnel_uptime = fields.Char(
        string='Connector Opened At',
        help="Connector connection open time (Cloudflare opened_at ISO "
             "timestamp), NOT a pre-computed duration. Consumers derive the "
             "uptime as now() - opened_at.",
    )
    connector_version = fields.Char(
        string='Connector Version',
        help="cloudflared client version reported by the active connector "
             "(from the operator tunnel-status connections payload).",
    )
    edge_connection_count = fields.Integer(
        string='Edge Connections',
        default=0,
        help="Number of active edge connections the connector holds "
             "(len of the operator tunnel-status connections list).",
    )

    # NOTE: there is deliberately NO SA-level "primary_hostname" field, and no
    # auto-created primary route. A Security Access IS a bare Cloudflare Tunnel
    # with N user-managed application routes (each forwarded to the user's
    # reverse proxy); Cloudflare Tunnel has no primary/default hostname concept.
    # canonical_name is only the tunnel's CF identity (its name), never surfaced
    # as "the hostname"; a freshly-provisioned tunnel has zero routes and zero
    # DNS until the user adds a route.

    # Access Control（P2.5；下放到 route 子模型 per-route）
    #
    # P2.5 removed the SA-LEVEL service-wide Access fields (access_enabled /
    # login_google / login_email_otp / session_duration / access_app_id /
    # access_unresolved_idp_types / access_policy_ids). Access is now per-route:
    # each Application Route is its own Cloudflare Access application with its
    # own protection toggle (route.is_protected), login methods, session and
    # policy set. See SecurityAccessRoute below. The only SA-level Access helper
    # that survives is _teardown_route_access (tears down a deleted route's app).

    route_ids = fields.One2many(
        comodel_name='woow_paas_platform.security_access_route',
        inverse_name='security_access_id',
        string='Application Routes',
    )
    route_count = fields.Integer(
        compute='_compute_route_count', string='Routes',
    )

    # Timestamps
    deployed_at = fields.Datetime(string='Deployed At')

    # ------------------------------------------------------------------ #
    #  Computes / helpers                                                #
    # ------------------------------------------------------------------ #

    @api.depends('route_ids')
    def _compute_route_count(self):
        for record in self:
            record.route_count = len(record.route_ids)

    def _recompute_route_state(self):
        """D1: flip a provisioned tunnel between 'active' (has ≥1 route) and
        'active_no_route' (bare). Only touches those two states — never
        pending/provisioning/error/suspended/deleting. Called from the route
        sub-model's create()/unlink() so adding the first route activates the
        SA and deleting the last one drops it back to no-route."""
        for sa in self:
            if sa.state in ('active', 'active_no_route'):
                target = 'active' if sa.route_ids else 'active_no_route'
                if sa.state != target:
                    sa.state = target

    def _get_operator_client(self):
        """Get PaaS Operator client instance."""
        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(
                "PaaS Operator is not configured. "
                "Please set Operator URL and API Key in Settings → Woow PaaS."
            )
        return client

    def _get_paas_domain(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io'
        )

    @api.model
    def _get_default_service_url(self):
        """後台可設定的預設反向代理 URL（spec §6）。"""
        return self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.sa_default_service_url', 'http://localhost:80'
        )

    def _generate_canonical_name(self):
        """#677：hash 種子是 workspace 的 **stored** `ws_hash`，不是 `slug`。

        `slug` 由租戶提供的 `name` 逐字決定，而舊種子是它的 `md5[:8]`（32-bit
        截斷）⇒ 租戶搜得出一個 name，讓自己的 SA 子網域落在受害者的 ws_hash 段上。
        原本「slug 空就退回 `name.lower()`」的 fallback 一併移除：`workspace_id` 是
        `required=True`，而靜默退回 name 推導正是這個洞的形狀；ws_hash 缺值交給
        `naming.py::_require_ws_hash` fail-loud。
        """
        self.ensure_one()
        # L3: 注入 env token（prod=空 token、stg=paas-stg-sa-）。env 來源為顯式
        # config_parameter，不從 namespace_prefix 隱式推導。token 只在 create 首算
        # 一次寫入（見 create()），事後 immutable（write guard），故跨 prod/stg 不漂移。
        deploy_env = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.deploy_env', 'prod')
        return make_security_access_subdomain(
            self.workspace_id.ws_hash, self.reference_id, self.name, env=deploy_env)

    @api.model_create_multi
    def create(self, vals_list):
        # Identity anchors must not be attacker-injectable at create time — a
        # workspace member calling ORM create directly could otherwise squat a
        # victim-namespace canonical_name (decides tunnel name, subdomain
        # prefix, DNS comment stamp and the public FQDN) or bind the new row
        # straight onto a victim's tunnel. Which fields those are lives in
        # _IDENTITY_ANCHOR_FIELDS, shared with the write() guard below, so a
        # future anchor is covered by both the moment it is listed.
        # canonical_name is computed once below; reference_id falls back to its
        # uuid4 default. Closes the create+write immutable chain.
        #
        # #753：傳的是 `self` 而不是 `self.env`——守衛要讀 `_fields` 才知道哪個
        # 錨點有伺服器端 default（`reference_id` 的 uuid4）該壓進去，而不是一律
        # 壓 False 把 required 炸掉。
        _strip_identity_anchors(self, vals_list, _IDENTITY_ANCHOR_FIELDS)
        records = super().create(vals_list)
        for record in records:
            if not record.canonical_name:
                # compute ONCE and store; never recompute (name is mutable).
                # The write() guard below blocks canonical_name writes for
                # everyone (incl. superuser) — this initial assignment is the
                # one legitimate internal write, opted in via the sentinel
                # escape hatch (not an env.su exemption: see write() comment).
                _internal_identity(record).write({
                    'canonical_name': record._generate_canonical_name(),
                })
        return records

    def write(self, vals):
        # Identity anchors (_IDENTITY_ANCHOR_FIELDS): subdomain & tunnel naming
        # derive from canonical_name/reference_id and drift would break
        # routing, while tunnel_id/tunnel_name/tunnel_token decide WHICH
        # Cloudflare tunnel this row drives — repointing them hands this
        # tenant's ingress to somebody else's tunnel (#522). Hard invariant for
        # EVERYONE including superuser: an env.su exemption would silently skip
        # this guard in TransactionCase (su env) and any sudo()'d path, giving
        # false-green tests (same trap the route prefix guard documents). The
        # sentinel context is the single deliberate escape hatch — only
        # server-owned provisioning/sync code can set it.
        _reject_identity_anchor_writes(
            self.env, vals, _IDENTITY_ANCHOR_FIELDS)
        # P2.5: the SA layer no longer carries any service-wide Access config —
        # Access is per-route now, so a route's create/write/unlink fires its
        # OWN _sync_access_to_operator. There is no SA-level access write trigger
        # any more (the route/policy sub-models own the savepoint + liveness
        # gate). A plain write here only persists SA identity/tunnel/state fields.
        result = super().write(vals)
        return result

    # ------------------------------------------------------------------ #
    #  Provision / status / delete                                       #
    # ------------------------------------------------------------------ #

    def action_provision(self):
        """Provision: create dedicated tunnel + primary route.

        Idempotency guard: this is a two-stage provision (create_tunnel, then
        a re-sync that carries http_host_header). If stage 2 fails the record
        lands in 'error' but the tunnel already exists in Cloudflare and
        tunnel_id is stored. A retry from the 'error' state must NOT call
        create_tunnel again — that would leave the first tunnel orphaned in CF
        (live token, billed, no DB reference). When tunnel_id is already set we
        only re-run the declarative route sync and restore the active state.

        tunnel_token ACL note: the write below sets tunnel_token, which is a
        ``groups='base.group_system'`` field. This method runs in the CALLER's
        context (the controller invokes it WITHOUT .sudo()), mirroring
        smart_home.action_provision exactly. Consequences:
        - the field-group write only succeeds for a system/admin caller; a
          plain base.group_user owner triggering provision would hit
          AccessError on the tunnel_token write. That is the intended
          fail-closed behaviour — the token must never be written (or read)
          by a non-system principal.
        - do NOT "fix" a portal AccessError here by wrapping the whole method
          in .sudo(): that would widen the token write/read surface to any
          workspace owner and defeat the groups= field mask. The defence is
          deliberately layered: groups= on the field + to_dict() omits the
          token + a dedicated sudo'd tunnel-token endpoint is the only read
          path. The with_user provision test locks this semantic.
        """
        self.ensure_one()
        client = self._get_operator_client()

        # Already has a live tunnel (e.g. retry after a stage-2 sync failure):
        # never recreate it, just finish the route sync.
        if self.tunnel_id:
            self.write({'state': 'provisioning', 'error_message': False})
            try:
                # 軸2: bare tunnel — no primary route to self-heal. Just re-run
                # the declarative route sync (zero routes -> empty sync, tunnel
                # keeps catch-all 404) + per-route Access for any user routes.
                self._sync_routes_to_operator()
                # P2.5: if any route was set is_protected (+ policies) on the SA
                # while it was still pending, the route Access sync never reached
                # the operator (no live tunnel then). The tunnel exists now, so
                # push every protected route's own Access app here — otherwise a
                # route marked protected stays silently inert (no CF Access app)
                # until some route access field is re-toggled.
                self._sync_all_routes_access()
                self.write({
                    'state': 'active' if self.route_ids else 'active_no_route',
                })
            except PaaSOperatorConnectionError as e:
                self.write({
                    'state': 'error',
                    'error_message':
                        f"Cannot connect to PaaS Operator: {e.message}",
                })
                _logger.error(
                    "[SA] provision re-sync connection error sa=%s: %s",
                    self.id, e.message,
                )
            except PaaSOperatorError as e:
                self.write({
                    'state': 'error',
                    'error_message': f"Route sync failed: {e.message}",
                })
                _logger.error(
                    "[SA] provision re-sync error sa=%s: %s",
                    self.id, e.message,
                )
            except AccessError:
                # A field-group (tunnel_token) AccessError is the deliberate
                # fail-closed signal for a non-system caller — it must propagate
                # to the caller, NOT be downgraded to a silent state='error'
                # (which would mask the security denial and report a benign
                # provisioning failure instead). Re-raise; see the tunnel_token
                # ACL note in the method docstring. Symmetric with the
                # first-provision branch below.
                raise
            except Exception as e:
                # Any non-operator failure (route rebuild ValidationError,
                # DB error, …) must also fail loud into 'error' — tunnel_id
                # is retained so the next retry can finish (symmetric with the
                # broad except in the first-provision branch below).
                self.write({
                    'state': 'error',
                    'error_message': f"Provision re-sync failed: {e}",
                })
                _logger.exception(
                    "[SA] provision re-sync unexpected error sa=%s", self.id,
                )
            return

        self.write({'state': 'provisioning', 'error_message': False})

        try:
            default_url = self._get_default_service_url()

            # 軸2: build a BARE tunnel — no hostname, no auto-created primary
            # route, no DNS. canonical_name is only the tunnel identity (its CF
            # name); application routes (and their DNS) are added on demand by
            # the user afterwards. create_tunnel with no hostname configures
            # the tunnel with a catch-all 404 ingress only.
            result = client.create_tunnel(
                name=self.canonical_name,
                service_url=default_url,
            )

            # The one legitimate anchor write: storing the ids Cloudflare just
            # handed back. Opted in via the sentinel — see
            # _INTERNAL_IDENTITY_WRITE. Note this does NOT widen the
            # tunnel_token field-group ACL: super().write() still enforces
            # groups='base.group_system', which is why a non-system caller
            # reaching here still fails closed with AccessError (below).
            _internal_identity(self).write({
                'tunnel_id': result.get('tunnel_id'),
                'tunnel_token': result.get('tunnel_token'),
                'tunnel_name': result.get('tunnel_name'),
                'deployed_at': fields.Datetime.now(),
            })

            # Push whatever routes already exist — a user may have added routes
            # on the still-pending SA via the configure-then-provision flow.
            # Zero routes -> empty declarative sync -> tunnel keeps the
            # catch-all 404 only (the operator accepts an empty route list).
            self._sync_routes_to_operator()

            # P2.5: push per-route Access too if any route was set protected
            # pre-provision. The configure-then-provision flow lets a user mark
            # a route protected (+ add policies) on a 'pending' SA (BFF gates on
            # subscription state, not provisioning state); those route syncs
            # never reached the operator (no live tunnel). Now that the tunnel
            # exists, converge each protected route's CF Access app.
            self._sync_all_routes_access()

            # D1: a tunnel with zero application routes is reachable at nothing
            # yet -> distinct 'active_no_route' state; once it has a route it is
            # 'active'.
            self.write({
                'state': 'active' if self.route_ids else 'active_no_route',
            })

            _logger.info(
                "[SA] provisioned sa=%s canonical=%s tunnel=%s",
                self.id, self.canonical_name, result.get('tunnel_id'),
            )

        except PaaSOperatorConnectionError as e:
            self.write({
                'state': 'error',
                'error_message': f"Cannot connect to PaaS Operator: {e.message}",
            })
            _logger.error("[SA] provision connection error: %s", e.message)
        except PaaSOperatorError as e:
            self.write({
                'state': 'error',
                'error_message': f"Tunnel creation failed: {e.message}",
            })
            _logger.error("[SA] provision error: %s", e.message)
        except AccessError:
            # The stage-2 tunnel_token write is a groups='base.group_system'
            # field write; for a non-system caller it raises AccessError. That
            # is the deliberate fail-closed defense (see the tunnel_token ACL
            # note in the docstring) — a non-system principal must never write
            # the token. Re-raise so the denial surfaces to the caller; do NOT
            # downgrade it to a silent state='error' (which would mask the
            # security boundary as an ordinary provisioning failure and let the
            # endpoint claim a benign error). The with_user provision test locks
            # this. (create_tunnel already ran here, but the controller never
            # drives provision as a bare portal user — system/admin context or a
            # narrowly-sudo'd path is the only legitimate caller.)
            raise
        except Exception as e:
            # Stage-2 (write / primary-route create / re-sync) can raise
            # non-operator exceptions: a ValidationError from
            # _check_prefix_format, a DB IntegrityError, or a UNIQUE clash on
            # the primary route. The narrow operator-only excepts above would
            # let those propagate while state was already flipped to 'active'
            # and tunnel_id stored — silent CF↔DB drift. Catch everything and
            # fail loud into 'error' (tunnel_id is retained, so the idempotent
            # retry-from-error path can finish without orphaning the tunnel),
            # mirroring _do_suspend / _do_resume / _do_cleanup's bare except.
            self.write({
                'state': 'error',
                'error_message': f"Provisioning failed: {e}",
            })
            _logger.exception("[SA] provision unexpected error sa=%s", self.id)

    def action_refresh_status(self):
        """Refresh tunnel connector status from the PaaS Operator."""
        self.ensure_one()
        if not self.tunnel_id:
            return
        client = self._get_operator_client()
        try:
            result = client.get_tunnel_status(self.tunnel_id)
            connections = result.get('connections', [])
            update_vals = {
                'tunnel_status': 'connected' if connections else 'disconnected',
                'edge_connection_count': len(connections),
            }
            if connections:
                conn = connections[0]
                update_vals['connector_id'] = conn.get('connector_id', '')
                update_vals['connector_type'] = conn.get('type', '')
                # cloudflared client version (key varies by operator/CF payload)
                update_vals['connector_version'] = (
                    conn.get('client_version')
                    or conn.get('version') or '')
                if conn.get('opened_at'):
                    update_vals['tunnel_uptime'] = conn['opened_at']
            else:
                update_vals['connector_version'] = ''
            self.write(update_vals)
        except PaaSOperatorError as e:
            self.write({
                'tunnel_status': 'error',
                'error_message': f"Status refresh failed: {e.message}",
            })
            _logger.warning(
                "[SA] status refresh failed sa=%s: %s", self.id, e.message,
            )

    def action_delete(self):
        """User-initiated delete: full Cloudflare teardown then unlink."""
        self.ensure_one()
        self.write({'state': 'deleting'})
        self._cleanup_cloudflare()
        self.unlink()

    def unlink(self):
        """Direct-unlink guard: Cloudflare resources must never outlive the
        record (cloud-service-unlink-override-cleanup pattern).

        Fail-loud if cleanup left a live tunnel. _cleanup_cloudflare swallows
        operator/CF errors and deliberately keeps tunnel_id for a retry; if we
        unconditionally unlinked we would drop the DB row while a billed,
        token-still-valid tunnel lives on in Cloudflare with no DB reference
        left to drive a retry — a permanent orphan. Raise instead so the
        controller surfaces a truthful failure and both the DB row and the CF
        tunnel survive for the user / cron _do_cleanup to retry. This aligns
        the direct-delete path with the fail-loud contract _do_cleanup already
        establishes for the cron path.

        #784：cleanup 不再以 ``tunnel_id`` 為前提。Access app 與 tunnel 是兩種
        不同的 CF 物件，operator 拆 Access 的 payload 從頭到尾不碰 tunnel
        （``enabled=False`` 只吃 ``existing_app_id`` / ``existing_policy_ids``）
        ⇒ 「tunnel 已經刪掉、Access 拆到一半失敗」這個**再正常不過的中間態**，
        舊寫法會整段跳過 cleanup，把該 route 的 app ＋ 全部 policy cf id 靜默
        丟掉。``_cleanup_cloudflare`` 自己已經是冪等的（沒東西要拆就直接
        return），所以無條件呼叫是純加法。"""
        # #322: 比照 CloudService（#318）— super().unlink() 前捕獲 in_progress
        # 訂閱，刪除成功後結算（取消＋按比例退點）。共用 _settle_on_entity_delete。
        subs_to_settle = self.sudo().mapped('subscription_id').filtered(
            lambda s: s.state == 'in_progress'
        )
        for record in self:
            record._cleanup_cloudflare()
            if record.tunnel_id:
                raise UserError(
                    "Cloudflare cleanup failed; the tunnel is still live and "
                    "billed. The record was kept so the deletion can be "
                    "retried. Please try again shortly."
                )
        # 軸2: routes are freely deletable (no primary guard), so cascade-delete
        # them directly as part of tearing down the whole SA.
        self.route_ids.unlink()
        res = super().unlink()
        for sub in subs_to_settle:
            sub._settle_on_entity_delete(reason='Security Access deleted')
        return res

    # ------------------------------------------------------------------ #
    #  Route sync (declarative)                                          #
    # ------------------------------------------------------------------ #

    def _build_route_payload(self, override_service_url=None):
        """Desired-state payload for sync_tunnel_routes.

        Args:
            override_service_url: when set (e.g. 'http_status:503'),
                every route's service is replaced — used by suspend.
        """
        self.ensure_one()
        payload = []
        for route in self.route_ids.sorted(key=lambda r: r.id):
            payload.append({
                'subdomain': route.subdomain_prefix,
                'service_url': override_service_url or route.service_url,
                'http_host_header': (
                    route.http_host_header and not override_service_url
                ),
            })
        return payload

    def _sync_routes_to_operator(self, override_service_url=None):
        """Push the full desired route list to the operator (idempotent)."""
        self.ensure_one()
        if not self.tunnel_id:
            return
        client = self._get_operator_client()
        result = client.sync_tunnel_routes(
            self.tunnel_id,
            routes=self._build_route_payload(override_service_url),
            comment=self.canonical_name,
        )
        # store DNS record ids back on routes
        dns_by_sub = {
            r['subdomain']: r.get('dns_record_id')
            for r in result.get('routes', [])
        }
        for route in self.route_ids:
            rec_id = dns_by_sub.get(route.subdomain_prefix)
            if rec_id and route.dns_record_id != rec_id:
                # server-owned anchor: the CF DNS record id just returned
                _internal_identity(route).dns_record_id = rec_id

    # ------------------------------------------------------------------ #
    #  Access sync (declarative; P2.5 per-route — see SecurityAccessRoute) #
    # ------------------------------------------------------------------ #

    def _sync_all_routes_access(self):
        """P2.5 convergence point: push every protected route's own Access app.

        Called once the tunnel is live (provision / resume) so per-route Access
        that was configured on a 'pending' SA — when no route sync could reach
        the operator — gets pushed, AND so a route disabled while suspended is
        torn down at resume. The route sync still requires a live tunnel; the
        `_force_access_sync` hatch bypasses only the state half of the gate
        because these convergence points run before the SA state is settled to
        'active'/'error'. Each route builds its own single-route payload, so an
        unprotected route with no stored app id is a cheap no-op."""
        self.ensure_one()
        for route in self.route_ids:
            _force_access_sync(route)._sync_access_to_operator()

    def _teardown_route_access(self, app_id, policy_ids, app_name, hostname,
                               anchor_model=None, anchor_id=None):
        """Tear down ONE route's Cloudflare Access app (+ reusable policies)
        when the route row no longer exists (deleted) or during SA cleanup. The
        route may be gone, so the caller passes the captured ids; we build a
        minimal enabled=False payload here. Best-effort — a teardown failure is
        logged, not raised, so it does not abort the surrounding unlink/cleanup.
        Returns True on a clean teardown (so the caller may clear the stored
        app_id), False on failure (so a surviving route keeps its id and the SA
        lands re-entrant in 'error' instead of reporting a false clean).

        #784：``anchor_model`` / ``anchor_id`` 是這條 route 的身分（列可能正在
        消失，所以傳純量）。有給的時候 operator 回報「送去刪但刪不掉」的
        ``orphaned_policy_ids`` 會**落進待回收帳本**——舊寫法只 log 一行然後回
        ``True``，而呼叫端拿到 ``True`` 就會把列上的 cf id 清掉／讓列被刪掉，那
        幾顆 id 從此沒有任何記錄指向它們（與 #528 完全同型，只是發生在 teardown
        這一側）。帳的回收者見 ``cf_access_policy_reap._cron_reap_dead_anchors``。

        ⚠ **「best-effort、只回 bool」有一個明載的例外**（#791 覆核 F4）：記帳段
        的 ``OperationalError``（serialization failure / 連線類）會**往外拋**，
        不會被收成 ``False``。那是刻意的——吞掉它 Odoo 的 ``retrying()`` 就永遠
        看不到，而此刻 PG 交易已經 aborted，硬回一個 bool 只會讓後續 flush 再炸
        一次（#518）。呼叫端不必自己接：request 級的 retry 會用新的 snapshot 重播
        整支 unlink／cleanup，而重播是安全的（CF 的 DELETE 把 404 當成功）。
        """
        self.ensure_one()
        if not app_id and not policy_ids:
            return True
        try:
            client = self._get_operator_client()
            result = client.sync_access_app({
                'enabled': False,
                'canonical_name': self.canonical_name,
                'app_name': app_name or self.canonical_name,
                'primary_hostname': hostname or '',
                'protected_hostnames': [],
                'session_duration': '24h',
                'login_idp_types': [],
                'policies': [],
                'existing_app_id': app_id or None,
                'existing_policy_ids': policy_ids or [],
            })
        except Exception as e:
            _logger.warning(
                "[SA] route access teardown failed sa=%s app=%s: %s",
                self.id, app_id, e)
            return False
        # ── 提交點之後 ─────────────────────────────────────────────────
        # operator 回 2xx ＝ CF 上這些物件已經沒了，回捲不掉。以下純記帳
        # （[[post-commit-bookkeeping-rollback-unit]]）：記帳失敗**不得**翻轉
        # 回傳值——回 False 會讓呼叫端保住 `access_app_id`，而那個 id 指向一顆
        # CF 上已經不存在的 app，下一次 sync 拿它去 PUT 會 404。
        orphaned = result.get('orphaned_policy_ids') or []
        if orphaned:
            _logger.warning(
                "[SA] route access teardown sa=%s app=%s left %d orphan "
                "policy(ies): %s", self.id, app_id, len(orphaned),
                ', '.join(orphaned))
        if anchor_model and anchor_id:
            self.env[_CF_REAP_MODEL]._settle_teardown_after_commit(
                anchor_model, anchor_id, app_id or None, policy_ids or [],
                orphaned, _logger,
                '[SA] route access teardown sa=%s app=%s' % (self.id, app_id))
        return True

    # ------------------------------------------------------------------ #
    #  Subscription lifecycle: suspend / resume / cleanup                #
    # ------------------------------------------------------------------ #

    def _do_suspend(self):
        """503-freeze every route's ingress; keep DNS / tunnel / token."""
        self.ensure_one()
        _logger.info(
            "[SA] _do_suspend sa=%s state=%s sub=%s",
            self.id, self.state,
            self.subscription_id.id if self.subscription_id else None,
        )
        # 'deleting' is terminal-ish (cleanup already ran): never flip it back
        # to 'suspended' — that reverse transition makes a cleaned-up record
        # re-advertise as merely suspended and triggers needless re-cleanup
        # churn on every cron pass while the subscription stays cancelled.
        if self.state in ('suspended', 'deleting'):
            return
        if self.tunnel_id:
            try:
                self._sync_routes_to_operator(
                    override_service_url='http_status:503')
            except Exception as e:
                _logger.warning("[SA] _do_suspend failed sa=%s: %s", self.id, e)
                self.write({'state': 'error'})
                return
        self.write({
            'pre_suspend_state': (
                self.state if self.state in ('active', 'active_no_route', 'error') else 'active'
            ),
            'state': 'suspended',
        })
        _logger.info("[SA] _do_suspend done sa=%s", self.id)

    def _do_resume(self):
        """Restore every route's ingress to its stored service_url."""
        self.ensure_one()
        _logger.info(
            "[SA] _do_resume sa=%s state=%s pre_suspend=%s",
            self.id, self.state, self.pre_suspend_state,
        )
        if self.state != 'suspended':
            return
        if not self.tunnel_id:
            # Never successfully provisioned (suspended while pending/
            # provisioning/error before a tunnel existed). pre_suspend_state
            # was coerced to 'active' by _do_suspend's fallback, which would
            # mislead the UI into showing "running" with no tunnel. Restore to
            # 'pending' so the user is prompted to (re-)provision.
            self.write({'state': 'pending', 'pre_suspend_state': False})
            _logger.info(
                "[SA] _do_resume sa=%s no tunnel → restored to 'pending'",
                self.id,
            )
            return
        try:
            self._sync_routes_to_operator()
            # P2.5: re-assert per-route Access on resume too. Suspend only
            # 503-freezes route ingress (it does not tear Access apps down), but
            # resuming is also the convergence point for any per-route Access
            # config edited while suspended or set pre-provision — push every
            # route's own desired state so CF matches the live route set.
            #
            # Converge BOTH directions per route: _sync_access_to_operator is
            # idempotent. A route flipped is_protected→False while suspended (no
            # write trigger fired — state not in ('active','error')) still
            # carries a stored access_app_id, so its resume sync sends an
            # enabled=False teardown payload; a route still protected re-asserts
            # its app. A live tunnel is guaranteed here (the `not tunnel_id`
            # branch above returned), so push unconditionally.
            self._sync_all_routes_access()
        except Exception as e:
            _logger.warning("[SA] _do_resume failed sa=%s: %s", self.id, e)
            self.write({'state': 'error'})
            return
        restore_state = self.pre_suspend_state or 'active'
        resume_vals = {'pre_suspend_state': False}
        if restore_state == 'error':
            # The re-sync above just succeeded, so the tunnel/ingress is
            # healthy right now — restoring the stale pre-suspend 'error'
            # would leave a working service permanently flagged 'error' (cron
            # branch 2 only resumes 'suspended', so it never gets revisited)
            # with a misleading old error_message. Heal to 'active' instead.
            resume_vals['state'] = 'active'
            resume_vals['error_message'] = False
            restore_state = 'active'
        else:
            resume_vals['state'] = restore_state
        self.write(resume_vals)
        _logger.info("[SA] _do_resume done sa=%s state=%s",
                     self.id, restore_state)

    def _do_cleanup(self):
        """Cron-driven permanent removal of Cloudflare resources.

        Only mark 'deleting' once the tunnel is actually gone. If
        _cleanup_cloudflare failed it deliberately keeps tunnel_id (re-entrant
        retry); writing 'deleting' then would be a misleading success signal
        that hides a still-live CF tunnel and lets ops mistake "clean" for
        "clean failed". Land in 'error' + error_message instead, fail-loud and
        observable like _do_suspend / _do_resume."""
        self.ensure_one()
        _logger.info("[SA] _do_cleanup sa=%s tunnel=%s",
                     self.id, self.tunnel_id)
        self._cleanup_cloudflare()
        # P2.5: a lingering per-route Access app is a stale route.access_app_id.
        # #784：判準改用 `_has_access_teardown()`（單一裁決處）——「app_id 空但
        # policy 還帶著 cf id」也是沒清乾淨，舊寫法把它讀成 clean。
        lingering_access = self.route_ids.filtered(
            lambda r: r._has_access_teardown())
        if self.tunnel_id or lingering_access:
            # cleanup did not finish — either the tunnel is still live in
            # Cloudflare, OR one of the per-route Access teardowns failed while
            # the tunnel delete succeeded (ordering-dependent orphan: a stale
            # route.access_app_id means that route's CF Access app + reusable
            # policies still exist). Either way, writing 'deleting' would be a
            # misleading success signal that hides a lingering CF resource and
            # lets ops mistake "clean" for "clean failed". Land in 'error' +
            # error_message instead, fail-loud and re-entrant: the next cron
            # pass re-runs _cleanup_cloudflare, which retries whichever leg is
            # still outstanding.
            self.write({
                'state': 'error',
                'error_message': "Cloudflare cleanup failed; %s still live. "
                                 "Will retry on the next cron pass." % (
                                     'tunnel' if self.tunnel_id
                                     else 'Access app'),
            })
            _logger.warning(
                "[SA] _do_cleanup incomplete sa=%s tunnel=%s lingering_access=%s "
                "(state=error)",
                self.id, self.tunnel_id, lingering_access.ids,
            )
            return
        self.write({'state': 'deleting'})

    def _cleanup_cloudflare(self):
        """Idempotent CF teardown. delete_tunnel cascades: operator removes
        every DNS record pointing at the tunnel, then deletes the tunnel
        (token invalidated)."""
        self.ensure_one()
        # #784 相鄰出口 A：舊判準是 `filtered('access_app_id')` ⇒ 「app_id 已空
        # 但 policy 仍帶著 cf id」的 route 從頭到尾不會被看見（那是**合法的持久
        # 狀態**：teardown 只在成功時才一併清 policy 的 cf id，而 P2.5 migration
        # 也造得出這個形狀）。判準改成「手上還有沒有 id 要拆」，與 unlink 的捕獲
        # 迴圈、`_do_cleanup` 的殘留判定共用同一個 `_has_access_teardown()`。
        access_routes = self.route_ids.filtered(
            lambda r: r._has_access_teardown())
        if not self.tunnel_id and not access_routes:
            return
        # P2.5: tear down each route's own Cloudflare Access app (+ reusable
        # policies) first. Per-route now — loop every route that still carries an
        # access_app_id and push its own enabled=False teardown. Each is wrapped
        # (best-effort inside _route_teardown via _teardown_route_access) so one
        # Access teardown failure does NOT abort the tunnel delete below — the
        # tunnel is the billed resource and must always get its delete attempt.
        # The teardown only needs existing_app_id + existing_policy_ids, NOT the
        # tunnel, so we still attempt it even when tunnel_id is already gone — a
        # prior partial cleanup (tunnel deleted, an Access teardown failed) would
        # otherwise leave that route's Access app/policies as permanent orphans
        # with no retry path. A route's access_app_id is cleared only on a
        # successful teardown, so a failure keeps it set and _do_cleanup lands
        # the SA in 'error' (re-entrant) instead of reporting a false clean.
        for route in access_routes:
            # 動作端：送出去的集合可以多帶帳本舊帳（挑 route 的 filter 仍是列狀態）
            app_id, pol_ids = route._access_teardown_payload_ids()
            ok = self._teardown_route_access(
                app_id=app_id,
                policy_ids=pol_ids,
                app_name=route.subdomain_prefix,
                hostname=route.hostname,
                anchor_model=route._name,
                anchor_id=route.id,
            )
            if ok:
                _internal_identity(route).access_app_id = False
                for pol in route.access_policy_ids:
                    if pol.cf_policy_id:
                        pol.cf_policy_id = False
        if not self.tunnel_id:
            return
        try:
            client = self._get_operator_client()
            client.delete_tunnel(self.tunnel_id)
            _logger.info("[SA] tunnel deleted sa=%s tunnel=%s",
                         self.id, self.tunnel_id)
            _internal_identity(self).write({'tunnel_id': False})
        except Exception as e:
            # cleanup is re-entrant: log and keep tunnel_id so a later
            # retry (cron or manual) can finish the job.
            _logger.warning("[SA] cleanup failed sa=%s: %s", self.id, e)

    # ------------------------------------------------------------------ #
    #  Serialization                                                      #
    # ------------------------------------------------------------------ #

    def to_dict(self):
        """Serialize for API responses."""
        self.ensure_one()
        return {
            'id': self.id,
            'name': self.name,
            'canonical_name': self.canonical_name or '',
            'workspace_id': self.workspace_id.id,
            'state': self.state,
            'tunnel_id': self.tunnel_id or '',
            'tunnel_name': self.tunnel_name or '',
            'tunnel_status': self.tunnel_status or 'disconnected',
            'tunnel_uptime': self.tunnel_uptime or '',
            'connector_id': self.connector_id or '',
            'connector_type': self.connector_type or '',
            'connector_version': self.connector_version or '',
            'edge_connection_count': self.edge_connection_count,
            'error_message': self.error_message or '',
            'deployed_at': self.deployed_at.isoformat() if self.deployed_at else '',
            'created_at': self.create_date.isoformat() if self.create_date else '',
            # P2.5: Access config is per-route now. Each route's to_dict()
            # carries its own is_protected / login methods / session / policies
            # / fail-open signal. The SA layer no longer exposes service-wide
            # Access fields.
            'routes': [r.to_dict() for r in self.route_ids],
            'subscription': self._get_subscription_info(),
        }


class SecurityAccessRoute(models.Model):
    _name = 'woow_paas_platform.security_access_route'
    _description = 'Security Access Application Route'
    _order = 'id'

    _sql_constraints = [
        (
            'unique_subdomain_prefix',
            'UNIQUE(subdomain_prefix)',
            'Subdomain prefix must be unique across the platform.',
        ),
    ]

    security_access_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access',
        string='Security Access',
        required=True,
        ondelete='cascade',
        index=True,
    )
    subdomain_prefix = fields.Char(
        string='Subdomain Prefix',
        required=True,
        help='User-defined prefix; hostname = {prefix}.{paas_domain}. '
             'Flat platform-wide namespace, first come first served. '
             'Immutable after create (delete + recreate to rename).',
    )
    hostname = fields.Char(
        string='Hostname',
        compute='_compute_hostname',
    )
    service_url = fields.Char(
        string='Service URL',
        required=True,
        default=lambda self: self.env[
            'woow_paas_platform.security_access']._get_default_service_url(),
        help='Backend target. Default assumes the user runs a reverse '
             'proxy (NPM) on localhost:80 that splits by Host header.',
    )
    http_host_header = fields.Boolean(
        string='Send HTTP Host Header',
        default=True,
        help='Send the public hostname as the HTTP Host header to the '
             'origin so the user-side reverse proxy can route by Host.',
    )
    is_protected = fields.Boolean(
        string='Protected',
        default=False,
        help='Per-route Access protection toggle (P2.5). True = this route is '
             'its own Cloudflare Access application; False = publicly reachable '
             'through the tunnel with no Access gate.',
    )
    dns_record_id = fields.Char(string='DNS Record ID')

    # ------------------------------------------------------------------ #
    #  P2.5 per-route Access config (three pieces: policies / login /    #
    #  session) + CF Access app id, all downshifted from the SA layer.   #
    # ------------------------------------------------------------------ #
    session_duration = fields.Selection(
        selection=[
            ('30m', '30 minutes'), ('1h', '1 hour'), ('8h', '8 hours'),
            ('24h', '24 hours'), ('168h', '7 days'),
        ],
        string='Session Duration',
        default='24h',
        help='How long users stay authenticated before re-login (this route).',
    )
    login_google = fields.Boolean(string='Login: Google', default=False)
    login_email_otp = fields.Boolean(string='Login: Email OTP', default=False)
    access_app_id = fields.Char(
        string='CF Access App ID', copy=False,
        help='Cloudflare Access Application id for THIS route (set on sync).',
    )
    access_policy_ids = fields.One2many(
        comodel_name='woow_paas_platform.security_access_access_policy',
        inverse_name='route_id',
        string='Access Policies',
    )
    access_unresolved_idp_types = fields.Char(
        string='Unresolved Login Methods', copy=False,
        help='Requested login methods (e.g. google) with no matching '
             'Cloudflare account IdP at the last sync of THIS route. Non-empty '
             'means the login restriction silently widened — if it emptied the '
             'resolved set, the Access app falls back to ALL available IdPs. '
             'Surfaced so the fail-open widening is visible, not dropped.',
    )

    @api.depends('subdomain_prefix')
    def _compute_hostname(self):
        domain = self.env['woow_paas_platform.security_access'
                          ]._get_paas_domain()
        for route in self:
            route.hostname = (
                f"{route.subdomain_prefix}.{domain}"
                if route.subdomain_prefix else False
            )

    @api.model
    def _subdomain_prefix_error(self, prefix):
        """Single source of truth for subdomain-prefix validation.

        Shared by the create/write constraint (`_check_prefix_format`) AND the
        availability endpoint (`controllers/security_access._check_route_prefix`)
        so the two layers never diverge — previously availability only checked
        DB/Cloudflare collision and reported a bad-format prefix as "Available",
        then create rejected it. Returns a human-readable error message, or
        None when the prefix is valid.
        """
        prefix = prefix or ''
        if not _PREFIX_RE.match(prefix):
            return (
                "Subdomain must be 3-63 chars of lowercase letters, digits and "
                "hyphens only — no dots or other symbols, and must not start or "
                "end with a hyphen."
            )
        return None

    @api.constrains('subdomain_prefix')
    def _check_prefix_format(self):
        for route in self:
            err = self._subdomain_prefix_error(route.subdomain_prefix)
            if err:
                raise ValidationError("%s: %r" % (err, route.subdomain_prefix or ''))

    @api.constrains('security_access_id')
    def _check_route_quota(self):
        """Plan quota: total route count <= plan.sa_max_routes.

        軸2: every application route counts (no more free auto primary route).
        Existing subscriptions are grandfathered in the .38 migration (sa_max_routes
        +1) so they don't lose a slot when their old primary route is downgraded.

        No subscription (admin/legacy) → unrestricted, mirroring
        subscription_lifecycle_mixin._is_subscription_active. The
        controller layer re-checks (two-layer enforcement).
        """
        for route in self:
            sa = route.security_access_id
            sub = sa.sudo().subscription_id
            if not sub or not sub.sh_subscription_plan_id:
                continue
            quota = sub.sh_subscription_plan_id.sa_max_routes
            count = len(sa.route_ids)
            if count > quota:
                raise ValidationError(
                    "Route quota exceeded: plan '%s' allows %d "
                    "route(s)." % (sub.sh_subscription_plan_id.name, quota)
                )

    @api.model_create_multi
    def create(self, vals_list):
        # Same create-time squat the SA model closes, one level down: without
        # this a member could hand a brand-new route a victim's dns_record_id /
        # access_app_id, and the next reconcile or teardown would act on the
        # victim's Cloudflare objects. subdomain_prefix is deliberately NOT in
        # this list — it is the one identity field the user legitimately
        # chooses here (write() below makes it immutable afterwards).
        # #753：route 的兩個錨點都沒有欄位級 default，所以這裡壓出來的一律是
        # False——但仍走同一支守衛（同一份宣告式清單），不另開一條路。
        _strip_identity_anchors(
            self, vals_list, _ROUTE_IDENTITY_ANCHOR_FIELDS)
        # Atomicity scope: the savepoint makes the Odoo side atomic — a
        # create+sync where sync raises rolls the DB row back so there is no
        # "ghost route" committed in the DB but absent in CF that a later sync
        # would silently push public, and the re-raise lets the controller
        # report a truthful failure. It does NOT make the operator side
        # atomic: sync_tunnel_routes rewrites ingress then reconciles DNS with
        # no compensating action, so a mid-reconcile failure can leave CF
        # partially mutated even though the DB row rolled back. That residual
        # CF↔DB divergence converges only via a later idempotent re-sync (the
        # endpoint is declarative full-state), not via this savepoint.
        with self.env.cr.savepoint():
            routes = super().create(vals_list)
            # one sync per affected SA (declarative full-state push).
            # action_provision opens sa_skip_route_sync (via the sentinel
            # helper `_skip_route_sync`) because it issues its own explicit,
            # http_host_header-carrying re-sync right after — so
            # this override's sync would be a redundant extra operator call.
            #
            # Sync whenever a tunnel exists, including state=='error': a
            # stage-2 provision failure lands the SA in 'error' with a live
            # tunnel_id, yet the subscription stays active so the BFF still
            # lets owners CRUD routes. Skipping the push for 'error' (sync is
            # an idempotent full-state push) would silently diverge the DB
            # from Cloudflare until someone re-provisions, while the endpoint
            # reports success — so include 'error' here (and in write/unlink).
            if not _is_sync_control(self.env, _CTX_SKIP_ROUTE_SYNC):
                for sa in routes.mapped('security_access_id'):
                    if sa.state in ('active', 'active_no_route', 'error') and sa.tunnel_id:
                        sa._sync_routes_to_operator()
            # P2.5: a newly-created is_protected route is its own Cloudflare
            # Access application — push its own Access state. The route sync
            # carries its own liveness gate (no-op without a live tunnel / when
            # not protected), so just call it on every fresh route; an
            # unprotected route's sync is a cheap no-op.
            if not _is_sync_control(self.env, _CTX_SKIP_ROUTE_SYNC):
                for route in routes:
                    route._sync_access_to_operator()
        # D1: first route on a bare tunnel flips it active_no_route -> active.
        routes.mapped('security_access_id')._recompute_route_state()
        return routes

    def write(self, vals):
        # Server-owned CF object ids (_ROUTE_IDENTITY_ANCHOR_FIELDS) — same
        # invariant, same sentinel hatch, same list the create() strip above
        # reads. The controller's _update_access_config / _update_route_access
        # whitelists used to be the SOLE defense for these two Chars; they are
        # now the outer of two layers, so a path that forgets the whitelist no
        # longer hands the ORM a forged app id.
        _reject_identity_anchor_writes(
            self.env, vals, _ROUTE_IDENTITY_ANCHOR_FIELDS)
        # Hard invariant for everyone incl. superuser: hostname / DNS /
        # ingress all derive from the prefix, and no internal path
        # legitimately renames it (delete + recreate is the rename). The
        # sentinel context is a deliberate escape hatch for future data-repair
        # migrations only — an `env.su` exemption would silently skip this
        # guard in TransactionCase (su env) and any sudo()'d controller path,
        # and a plain truthy context value would be forgeable straight off the
        # RPC payload (see _INTERNAL_IDENTITY_WRITE). Its own key, deliberately
        # not the identity-anchor one: a data-repair migration that needs to
        # rename a prefix should not thereby also be able to move tunnel ids.
        if ('subdomain_prefix' in vals
                and self.env.context.get('sa_internal_prefix_write')
                is not _INTERNAL_IDENTITY_WRITE):
            for route in self:
                if route.subdomain_prefix != vals['subdomain_prefix']:
                    raise UserError(
                        "Subdomain prefix is immutable. Delete the route "
                        "and create a new one to rename."
                    )
        # Atomicity scope (see create): the savepoint guarantees ONLY the
        # Odoo side — on a sync failure the DB write (service_url/header) rolls
        # back so the row never claims a value the operator never accepted.
        # It does NOT roll back the operator/Cloudflare side: sync_tunnel_routes
        # rewrites ingress then reconciles DNS with no compensating action, so a
        # mid-reconcile failure (multi-route create/delete) can leave CF
        # partially changed while the DB rolls back. That residual CF↔DB
        # divergence is converged only by a later idempotent re-sync (the
        # operator endpoint is declarative full-state), not by this savepoint.
        # For a pure service_url update (hostnames unchanged) reconcile is a
        # no-op, so there the lock-step is exact.
        with self.env.cr.savepoint():
            result = super().write(vals)
            if any(k in vals for k in ('service_url', 'http_host_header')):
                for sa in self.mapped('security_access_id'):
                    if sa.state in ('active', 'active_no_route', 'error') and sa.tunnel_id:
                        sa._sync_routes_to_operator()
            # P2.5: toggling is_protected, or changing this route's own login
            # methods / session duration, changes its own Cloudflare Access app
            # — re-push THIS route's Access state. is_protected=True builds/
            # updates the app; is_protected=False tears it down. The route sync
            # carries the liveness gate (no-op without a live tunnel), so the
            # write trigger just forwards. Done per route in self.
            #
            # sa_skip_route_sync: bulk row moves (migration / provision-time)
            # set this so the write only re-homes DB state and does NOT call the
            # operator — CF converges via the explicit re-sync those flows issue,
            # not per-write here. Symmetric with create()/policy create+write+
            # unlink, which already honour the key. WITHOUT this guard the P2.5
            # migration's login/session re-home (post-migrate.py writes
            # login_google/login_email_otp/session_duration under the key) would
            # fire a live per-route CF Access app build mid-upgrade on stg's
            # active+live-tunnel SAs, defeating the deferral to Phase 5 — and an
            # unreachable operator during the upgrade window would re-raise the
            # savepoint and abort the whole migration.
            if (not _is_sync_control(self.env, _CTX_SKIP_ROUTE_SYNC)
                    and any(k in vals for k in (
                        'is_protected', 'login_google',
                        'login_email_otp', 'session_duration'))):
                for route in self:
                    route._sync_access_to_operator()
        return result

    def unlink(self):
        # 軸2: no primary route, so any route is deletable (incl. the last one,
        # which drops the SA back to 'active_no_route' via _recompute_route_state
        # below). The old is_primary delete guard is gone.
        affected = self.mapped('security_access_id')
        # P2.5: a protected route is its OWN Cloudflare Access app. Deleting the
        # route must tear that app (+ its reusable policies) down, but
        # super().unlink() drops the row first, taking access_app_id and every
        # policy cf_policy_id with it — after that the operator has no id to
        # delete and the app/policies leak as permanent CF orphans. Capture each
        # doomed route's teardown ids BEFORE the unlink, then push an
        # enabled=False teardown sync carrying them.
        #
        # #784：捕獲的判準是「**手上還有沒有 id 要拆**」，不是「這條 route 現在
        # 推得動嗎」。舊寫法有兩道 continue（`is_protected` ＋ SA liveness），
        # 被擋掉的 route 一路刪到底而 operator 一次都沒被呼叫——全程無 log、無
        # 例外、無狀態欄位，與乾淨刪除在輸出上完全不可區分。兩道都可達：
        #
        #   * `is_protected`：程式自己承認「未保護但仍持有 stored app_id」是
        #     **合法的持久狀態**（見 `_sync_access_to_operator` docstring 與
        #     `_do_resume` 的註解），而這一道恰好把它判成「沒東西要拆」。
        #   * SA liveness：停權中刪 route（刪除端點看的是**訂閱**狀態不是 SA
        #     state），`_do_suspend` 完全不碰 Access ⇒ app_id 與全部 cf id 都
        #     還活著。
        #
        # 拿掉閘是安全的：operator 的 teardown 分支（`enabled=False`）**只吃
        # id**，不碰 tunnel、不需要 hostname，CF 的 DELETE 又把 404 當成功
        # （`extra/paas-operator/src/services/cloudflare.py`）⇒ 「tunnel 已經不
        # 在但 app 還在」照樣拆得掉。而真的沒東西要拆時 `_teardown_route_access`
        # 本來就直接回 True，沒有多餘的 operator 呼叫。
        #
        # 那道 liveness 閘沒有消失，只是換了工作：它現在決定**拆失敗時誰是重試
        # 錨點**（`retry_anchor_is_row`），見下方 teardown 迴圈。
        teardown_by_route = {}
        for route in self:
            # 🔴 **閘用列狀態版、內容用 payload 版**（#792 三輪覆核項 2）。
            #
            # 舊寫法拿 payload 版（列 ∪ 帳本）當「拆不拆」的閘，正好違反
            # `_access_teardown_ids()` docstring 自己立的禁令——同一份 diff 裡規則
            # 與唯一的範例互相打臉。而且那不只是措辭問題，它有一條**可達**的危險
            # 路徑：一條「列已乾淨、但帳本還有欠款」的 route 會因此被捕獲，而它若
            # 是 `is_protected=True` ＋ SA 有 live tunnel ＋ state 在
            # ('active','active_no_route','error') 內，`_access_sync_skip_reason()`
            # 就回 None ⇒ 落進 **pass 1** ⇒ 它的 teardown 一失敗就 raise，把同批
            # 其他 route **已經對 CF 發出、回捲不掉**的刪除一起捲回去（#794 那條
            # 無聲 fail-open 的燃料）。
            #
            # 那個狀態怎麼來的（可達性，非推測）：`_do_cleanup` → `_cleanup_
            # cloudflare` 的 Access teardown 成功但 operator 回報 orphaned ⇒
            # `if ok:` 清掉列上的 app_id 與 policy cf id、orphaned 落帳；同一輪
            # tunnel 刪除失敗 ⇒ `tunnel_id` 保留 ⇒ `_do_cleanup` 寫
            # `state='error'`。'error' 正在允許清單內、tunnel_id 也還在
            # ⇒ 三個條件同時成立。
            #
            # 捕獲判準因此只看**列**；帳本上的舊帳仍然會跟著送出去（下一行），
            # 只是不再有資格把一條列已乾淨的 route 拉進批次。
            if not route._has_access_teardown():
                continue
            app_id, pol_ids = route._access_teardown_payload_ids()
            teardown_by_route[route.id] = {
                'sa': route.security_access_id,
                'app_id': app_id,
                'policy_ids': pol_ids,
                'app_name': route.subdomain_prefix,
                'hostname': route.hostname,
                # None ＝ 這條 route 現在推得動 ⇒ 回滾保住的列是一個**真的會被
                # 再次推送**的重試錨點。非 None ＝ 推不動：保住列救不了任何人，
                # 因為沒有任何 sync 會再為它跑（那正是本 issue 本身）。
                'retry_anchor_is_row': route._access_sync_skip_reason() is None,
            }
        # Atomicity scope — the most dangerous direction: super().unlink()
        # drops the DB row, then the sync removes it from CF. The savepoint
        # rolls the unlink back on sync failure so the row survives — without
        # it a failed sync would leave the route gone from the DB but still
        # serving publicly via DNS/ingress, with no DB row left to drive a
        # desired-state push (a permanent orphan). The guarantee is Odoo-side
        # only: a mid-reconcile operator failure can still leave CF partially
        # mutated (e.g. ingress rewritten but a DNS delete failed). Because the
        # DB row is preserved, the next idempotent re-sync re-asserts the full
        # desired state and converges CF — which is exactly why preserving the
        # row matters. The re-raised error reaches the controller as a truthful
        # failure.
        with self.env.cr.savepoint():
            result = super().unlink()
            for sa in affected:
                if (sa.exists() and sa.state in ('active', 'active_no_route', 'error')
                        and sa.tunnel_id):
                    sa._sync_routes_to_operator()
            # P2.5: tear down each deleted protected route's own Access app
            # using the ids captured before the unlink (the rows are gone now).
            #
            # Fail-loud on teardown failure, do NOT swallow it. _teardown_route_
            # access best-effort-catches and returns False (not raise), but here
            # the row is already gone via super().unlink(); ignoring the False
            # would leave the route's CF Access app + reusable policies as
            # permanent orphans with NO retry anchor (the deleted row can never
            # re-enter _cleanup_cloudflare's route_ids.filtered('access_app_id'))
            # and NO error signal. Instead, raise so the savepoint rolls the
            # unlink back: the row (+ its access_app_id) survives as the retry
            # anchor, mirroring the sibling route-sync leg and policy-unlink
            # resync, and consistent with _cleanup_cloudflare's `if ok:` honour
            # of the same True/False teardown contract ([[two-stage-remote-
            # provision-idempotency]]: every failure path keeps the stored id so
            # retry is safe).
            #
            # #784：失敗的處置**分兩格**，判準是「保住這一列到底救不救得了它」。
            #
            #   * `retry_anchor_is_row=True`（捕獲當下這條 route 推得動）——
            #     回滾把列救回來，而列活著就會被下一次 sync 再推一次 ⇒ 列是一個
            #     真的會被走到的重試錨點。維持既有的 fail-loud（行為不變，
            #     `test_protected_route_unlink_teardown_failure_rolls_back_keeps_
            #     row` 鎖著）。
            #   * `False`（停權／沒有 live tunnel／未保護但仍持有 stored id）——
            #     保住列**救不了任何人**：沒有任何 sync 會再為這條 route 跑，
            #     這正是本 issue 的形狀。此時 raise 只是把「靜默孤兒」換成「刪
            #     不掉」，兩邊都沒有人去刪 CF 上那些物件。改成落帳＋照常刪除，
            #     重試錨點換成帳本，回收者是
            #     `cf_access_policy_reap._cron_reap_dead_anchors`（dead anchor
            #     專收，見該方法 docstring 的「誰、何時、憑什麼」）。
            #
            # 落帳寫在 savepoint 裡是對的：走到 raise 的話這幾筆會跟著回滾，而
            # 那時列還活著、cf id 還在列上，本來就不需要帳（同
            # `security_access_access_policy.unlink()` 的同一段推理）。
            #
            # 🔴 **兩個 pass 的順序是安全屬性，不是整潔**（#791 覆核 F3）。
            #
            # 這一段的 raise 會讓 savepoint 把 `super().unlink()` 整個回捲，
            # 但**回捲不掉已經對 CF 發出的刪除**。所以一批多條 route 時，先被拆
            # 掉的那幾條會以「列復活、但列上的 cf id 指向 CF 已不存在的物件」
            # 收場。逐路徑追下去（operator 的 teardown 分支只吃 id、CF 的 DELETE
            # 把 404 當成功，見 `extra/paas-operator/src/services/cloudflare.py`
            # 的 `_delete_access_app` / `delete_reusable_policy`）：
            #
            #   * 使用者重試刪除 ⇒ 送同一批 id ⇒ 404＝成功 ⇒ 收斂；
            #   * 任何一次 SA 層 cleanup（`_do_cleanup` / `action_delete` /
            #     SA unlink）⇒ 同上 ⇒ 收斂；
            #   * 復活的列**未保護**時，下一次 sync 送 `enabled=False`
            #     ⇒ 走 teardown 分支 ⇒ 404＝成功 ⇒ id 被清掉 ⇒ 收斂；
            #   * 復活的列**仍保護**時，下一次 sync 送 `enabled=True` ＋ 死掉的
            #     `existing_app_id` ⇒ operator PUT `/apps/<dead>` 拿到 404 ⇒
            #     `raise CloudflareException` ⇒ **這條每次都失敗**（會出聲，不是
            #     靜默；解法是把那條 route 刪掉，那條路徑會收斂）。
            #
            # 最後那一格的**可達性**在 develop 上本來就有：要落進它，被拆又復活
            # 的那條 route 必須是 protected（未保護的下一次 sync 送
            # `enabled=False`、走 teardown 分支、404＝成功 ⇒ 自癒），而那正是舊碼
            # 捕獲條件的前提。⚠ 精確地說，pass 1 的成員判準
            # （`_access_sync_skip_reason() is None`）是舊捕獲條件
            # （`is_protected AND SA-live`）的**嚴格超集**——它多收「未保護但仍
            # 持有 stored app_id」那一類。那一類復活後自癒，所以不落進最後那格；
            # 但「pass 1 的成員範圍變大了」這件事本身要說清楚，不是「一模一樣」。
            #
            # 本 PR **新捕獲**的那些 route（未保護仍持有殘 id／停權中）如果被拆了
            # 再回捲，就是本 PR 自己加的新殘留。所以把它們排到 pass 2：pass 1 有
            # 任何失敗就 raise，pass 2 一根手指都還沒動過 CF。刪除語意仍然是全有
            # 全無（不是 partial success，沒有產品可見變更）。
            #
            # ⚠ pass 1 內部**不是** short-circuit：下面那個 list comprehension 是
            # eager 的，同批第一條失敗之後，**後面每一條 pass-1 route 仍會被真的
            # 送去 CF 拆掉**，才一起回捲。這與 develop 舊碼同構（舊碼也是整批拆完
            # 才 raise），不是本 PR 的回歸；但它正是 #794 那條無聲 fail-open 的
            # 燃料，所以寫在這裡而不是讓後人以為「第一條失敗就停了」。
            Reap = self.env[_CF_REAP_MODEL]

            def _push(route_id, info):
                # `info['sa']` 一定還在：`security_access_id` 是 required ＋
                # ondelete='cascade'，而唯一會刪 SA 的 Python 路徑
                # （`SecurityAccess.unlink`）是先呼叫 `route_ids.unlink()` 才
                # `super().unlink()`；PG 層的 cascade 則整支 Python 都不會跑到
                # 這裡。（舊版在這裡有一道 `if not sa.exists(): continue` 的防禦
                # 碼，覆核判定為死碼——會誤導讀者的防禦碼本身就是陷阱，故移除，
                # 理由留在這裡。）
                return info['sa']._teardown_route_access(
                    app_id=info['app_id'],
                    policy_ids=info['policy_ids'],
                    app_name=info['app_name'],
                    hostname=info['hostname'],
                    anchor_model=self._name,
                    anchor_id=route_id,
                )

            # pass 1：拆失敗 ⇒ 回滾保住列（列是真的重試錨點）。eager，不 short-
            # circuit——見上面那段 ⚠。
            failed = [info['app_name'] or info['app_id']
                      for route_id, info in teardown_by_route.items()
                      if info['retry_anchor_is_row'] and not _push(route_id, info)]
            if failed:
                raise UserError(
                    "Cloudflare Access teardown failed for route(s): %s. The "
                    "route was kept so the deletion can be retried; its Access "
                    "app is still live in Cloudflare." % ', '.join(
                        str(f) for f in failed)
                )
            # pass 2：拆失敗 ⇒ 落帳照常刪除（保住列救不了它，沒有 sync 會再跑）
            for route_id, info in teardown_by_route.items():
                if info['retry_anchor_is_row'] or _push(route_id, info):
                    continue
                Reap._record_teardown(
                    self._name, route_id, info['app_id'], info['policy_ids'])
        # D1: deleting the last route drops the SA back to 'active_no_route'.
        affected.exists()._recompute_route_state()
        return result

    # ------------------------------------------------------------------ #
    #  P2.5 per-route Access sync (declarative; one CF Access app/route)  #
    # ------------------------------------------------------------------ #

    def _access_teardown_ids(self):
        """這條 route 現在有哪些 CF Access 物件 id 需要拆：``(app_id, [policy])``。

        #784：**刪除路徑上「要不要拆」的唯一裁決處**，三個呼叫端共用
        （`unlink()` 的捕獲迴圈、`SecurityAccess._cleanup_cloudflare` 挑 route、
        `_do_cleanup` 判殘留）。之前三處各自寫判準，於是各自漏掉不同的一格
        ——`unlink` 漏 `is_protected=False`＋停權，另兩處漏「app_id 空但 policy
        還帶 cf id」。

        🔴 判準是「**手上有沒有 id**」，永遠不是 `is_protected`：
        「未保護但仍持有 stored app_id / cf_policy_id」是這支程式自己承認的合法
        持久狀態（`_sync_access_to_operator` 的 docstring 與 `_do_resume` 的註解
        都逐字寫著），拿保護旗標當判準會把那一格讀成「沒東西要拆」。

        🔴 **本方法只讀這一列的狀態，不看帳本**（#792 二輪覆核 F6）。這裡回答的
        問題是「**這一列乾淨了沒**」，而不是「這一趟 teardown 該送哪些 id」——
        兩個問題長得像但答案的用途相反，用同一個方法回答會出事：

        `_do_cleanup` 拿本方法的 bool 版當「清乾淨了沒」的閘。一旦把帳本聯集進來，
        只要 operator 回報過某顆 policy 刪不掉（`orphaned_policy_ids` 非空），那筆
        帳就掛在帳本上——而錨點還活著、dead-anchor cron 依設計不收它 ⇒ 這個閘對該
        route **永遠**回 True ⇒ SA 永久卡在 `state='error'`，每一輪 lifecycle cron
        白打一次 operator，七天後只多一行 stuck warning，無解。

        要送出去的 id 集合請用 `_access_teardown_payload_ids()`。

        ⚠ 精確一點：禁的是「拿 payload 版當**判準**」（要不要拆／清乾淨了沒），
        不是「不准一起用」。`unlink()` 的捕獲迴圈就是兩個都用——先用本方法的
        bool 版當閘決定捕不捕獲，捕獲之後才用 payload 版決定送哪些 id。
        """
        self.ensure_one()
        return (self.access_app_id or None,
                [p.cf_policy_id for p in self.access_policy_ids
                 if p.cf_policy_id])

    def _has_access_teardown(self):
        """這條 route 的**列上**還有沒有 CF Access 物件要拆（給 `filtered()` 用）。

        ⚠ 這是「列乾淨了沒」的判準，用在 `_cleanup_cloudflare` 挑 route 與
        `_do_cleanup` 判殘留兩處。**刻意不含帳本**，理由見
        `_access_teardown_ids()` 的 docstring（含帳本會讓 SA 永久卡 error）。
        """
        self.ensure_one()
        app_id, pol_ids = self._access_teardown_ids()
        return bool(app_id or pol_ids)

    def _access_teardown_payload_ids(self):
        """這一趟 teardown **該送出去**的 id：列上的 ∪ 帳本上這個錨點還欠的。

        #791 覆核 F6 的原意：列一消失，帳就變成 dead anchor 得等 cron 下一輪；
        既然這一趟本來就要對 operator 送一份 teardown，順手把舊帳一起帶出去比
        留到下一小時好。**但這只是「多送幾顆 id」，不可以反過來變成「要不要拆」
        或「清乾淨了沒」的判準**（#792 二輪覆核 F6：那會讓 SA 永久卡 error）。

        🔴 這裡讀 `_pending_ids()` 而不是 `_pending_for()`：兩者現在等價
        （#783 已把 `_pending_for()` 的 context 那條腿整個拿掉，帳本是唯一來源），
        留著這個選擇是因為本方法拿的是 `(model, id)` 語意的舊帳，而 `_pending_for`
        要一個活著的 record——teardown 路徑上錨點列不保證還在。
        """
        self.ensure_one()
        app_id, ids = self._access_teardown_ids()
        for cf_id in self.env[_CF_REAP_MODEL]._pending_ids(
                self._name, self.id):
            if cf_id and cf_id not in ids:
                ids.append(cf_id)
        return (app_id, ids)

    def _build_existing_policy_ids(self):
        """CF reusable-policy ids the operator must delete-then-recreate for
        THIS route. Two sources, both server-side: the surviving rows'
        cf_policy_id, plus whatever this anchor still owes on the reap ledger
        (`cf_access_policy_reap._pending_for`).

        A policy unlink runs super().unlink() before the resync, so the
        just-deleted rows' cf ids are already gone from the DB — they would
        never enter this list and the operator (whose only delete path is this
        list) would leak them as permanent CF orphans. They reach this list via
        the LEDGER: unlink() calls `Reap._record_ids()` inside the same
        savepoint, before it triggers the resync.

        #528：帳要離開一次性的通道、落到 DB。那一次 resync 可能被下面的 liveness
        gate 整個擋掉——只活一次呼叫的東西被擋掉之後就沒有任何記錄指向那些 id
        了（永久 CF 孤兒）。帳本讓它們活到下一次推得動為止。

        🔴 #783：`_pending_for()` **不讀任何 context**。這個回傳值會變成 operator
        的帳號級刪除清單，而 `call_kw` 把呼叫端 context 整包換進 env ⇒ 舊的
        `sa_unlinked_cf_policy_ids` 通道已整條移除（repo-lint 第 28 條
        `status='removed'` 擋它回來）。"""
        self.ensure_one()
        ids = [p.cf_policy_id for p in self.access_policy_ids if p.cf_policy_id]
        for cf_id in self.env[_CF_REAP_MODEL]._pending_for(self):
            if cf_id and cf_id not in ids:
                ids.append(cf_id)
        return ids

    def _build_access_payload(self):
        """Desired-state payload for the operator's /api/access/sync — scoped to
        THIS single route. Each protected route is its own Cloudflare Access
        application: domain = destinations = this route's hostname only."""
        self.ensure_one()
        sa = self.security_access_id
        login_idp_types = []
        if self.login_google:
            login_idp_types.append('google')
        if self.login_email_otp:
            login_idp_types.append('onetimepin')
        hostname = self.hostname or ''
        protected = [hostname] if (self.is_protected and hostname) else []
        return {
            'enabled': bool(self.is_protected),
            'canonical_name': sa.canonical_name,
            # Per-route app display name: distinct per route so multiple apps
            # under one tunnel never collide on a shared name.
            'app_name': self.subdomain_prefix or sa.canonical_name,
            'primary_hostname': hostname,
            'protected_hostnames': protected,
            'session_duration': self.session_duration or '24h',
            'login_idp_types': login_idp_types,
            'policies': [{
                'name': p.name, 'decision': p.decision,
                'precedence': p.sequence,
                'include': p._rules_by_group('include'),
                'require': p._rules_by_group('require'),
                'exclude': p._rules_by_group('exclude'),
            } for p in self.access_policy_ids.sorted(
                key=lambda x: (x.sequence, x.id))],
            'existing_app_id': self.access_app_id or None,
            'existing_policy_ids': self._build_existing_policy_ids(),
        }

    def _sync_access_to_operator(self):
        """Push THIS route's full desired Access state to the operator
        (idempotent). Liveness gate matches the route/SA sync: only fires once
        the SA has a live tunnel and is in ('active', 'active_no_route', 'error') — a stage-2
        provision failure leaves the SA in 'error' with a live tunnel while the
        subscription stays active, so an Access edit in that state must still
        converge CF, not silently diverge it. is_protected=False sends an
        enabled=False payload (teardown via existing_app_id), then clears the
        stored app id; with no app id it is a harmless no-op.

        The `_force_access_sync` hatch bypasses the STATE half of the gate
        (tunnel_id is still required, and #536: only the module's own sentinel
        opens it — a caller-supplied `sa_force_access_sync: true` does not):
        provision/resume are deliberate
        convergence points that run BEFORE the SA state is flipped back to
        'active' (resume) or while it is being restored, so the create/write
        liveness gate would otherwise skip the very push they exist to do —
        e.g. converging a route disabled while suspended (its stored app_id must
        be torn down at resume even though state is still 'suspended' here).

        #528：三道 gate 收斂成 `_access_sync_skip_reason()` 一個裁決處，並在
        略過時回 ``False``（推出去了回 ``True``）。理由不是整潔——原本三道各自
        裸 `return`，「被擋掉」與「推了而且成功」在呼叫端輸出上完全不可區分，
        而第四道 gate 只要有人再加一次就會再漏一次（同 [[reaped-since-single-
        path-stamp-gc-immunity]]：判準集中一處，不要逐路徑補）。"""
        self.ensure_one()
        sa = self.security_access_id
        skip = self._access_sync_skip_reason()
        if skip:
            self.env[_CF_REAP_MODEL]._warn_if_deferred(
                self, _logger,
                'Security Access route %s (%s, sa=%s)' % (
                    self.id, self.subdomain_prefix, sa.id if sa else None),
                skip)
            return False
        client = sa._get_operator_client()
        payload = self._build_access_payload()
        result = client.sync_access_app(payload)
        # Surface old reusable policies the operator could not delete after
        # repointing the app — permanent Cloudflare orphans (cost/clutter) that
        # no later re-sync reconciles, so make them observable in the log.
        orphaned = result.get('orphaned_policy_ids') or []
        if orphaned:
            _logger.warning(
                "Security Access route %s (%s, sa=%s): operator left %d orphan "
                "CF Access policy(ies) after sync: %s", self.id,
                self.subdomain_prefix, sa.id, len(orphaned),
                ', '.join(orphaned))
        # #528：operator 回 2xx ＝ 這一輪送出去的 existing_policy_ids 已經有答案
        # 了。沒被列進 orphaned 的確認刪掉 ⇒ 消帳；被列進去的現在就是 CF 孤兒 ⇒
        # 記進帳等下一輪（上面那行 warning 照發，但不再是「說完就忘」）。
        #
        # #791 覆核 F7：包 post-commit savepoint——這一行跑在 operator 已回 2xx
        # 之後，記帳失敗不該把下面那幾行「把 CF 剛回的 id 寫回列上」一起拖垮。
        # F9：`trusted_ids` 只給**列上真實存在的** cf id，`existing_policy_ids`
        # 裡混進來的 context 值不得經 orphaned 回流落帳（#783）。
        Reap = self.env[_CF_REAP_MODEL]
        Reap._settle_after_commit(
            self._name, self.id,
            payload.get('existing_policy_ids') or [], orphaned,
            Reap._trusted_ids_for(self), _logger,
            'Security Access route %s (%s, sa=%s)' % (
                self.id, self.subdomain_prefix, sa.id))
        # write back CF ids onto this route (server-owned identity anchor)
        _internal_identity(self).access_app_id = result.get('app_id') or False
        # Persist the operator's fail-open login-widening signal (requested
        # login methods with no matching CF IdP). Surfaced via to_dict().
        unresolved = result.get('unresolved_idp_types') or []
        new_unresolved = ', '.join(unresolved) if unresolved else False
        if self.access_unresolved_idp_types != new_unresolved:
            self.access_unresolved_idp_types = new_unresolved
        # map returned policy ids back onto policy rows in (sequence,id) order
        returned = result.get('policy_ids') or []
        policies = self.access_policy_ids.sorted(
            key=lambda x: (x.sequence, x.id))
        for pol, cf_id in zip(policies, returned):
            if pol.cf_policy_id != cf_id:
                pol.cf_policy_id = cf_id
        if not result.get('app_id'):
            # torn down → clear stale cf ids on this route's policies
            for pol in self.access_policy_ids:
                if pol.cf_policy_id:
                    pol.cf_policy_id = False
        return True

    def _access_sync_skip_reason(self):
        """``None`` ＝ 這一輪推得動；否則回一句人話說明為什麼不推（#528）。

        本方法是這條腿**所有** liveness gate 的唯一裁決處。分出來不是為了整潔：
        呼叫端要能在略過時說出「略過的是哪一格」，而把判斷留在 sync 裡面就只能
        回一個裸 `return`——那正是 #528 讓人把「CF 一次都沒被碰過」讀成「同步
        成功」的原因。新增 gate 一律加在這裡，訊號自動跟上。

        `_force_access_sync` 只繞過 **state 那半邊**（`tunnel_id` 仍是硬前提），
        這個不對稱是既有語意，不要在這裡「順手補齊」。#536：只有模組自己的
        sentinel 開得了這道閘——呼叫端塞 `sa_force_access_sync: true` 進 context
        打不開，見 `_is_sync_control`。
        """
        self.ensure_one()
        sa = self.security_access_id
        forced = _is_sync_control(self.env, _CTX_FORCE_ACCESS_SYNC)
        if not sa or not sa.tunnel_id:
            return 'the Security Access has no live tunnel'
        if not forced and sa.state not in ('active', 'active_no_route',
                                           'error'):
            return "Security Access state is %r" % sa.state
        # Nothing to do for an unprotected route that was never an app.
        return (None if (self.is_protected or self.access_app_id)
                else 'the route is unprotected and never had an Access app')

    def to_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'subdomain_prefix': self.subdomain_prefix,
            'hostname': self.hostname or '',
            'service_url': self.service_url,
            'http_host_header': self.http_host_header,
            'is_protected': self.is_protected,
            # P2.5 per-route Access config
            'session_duration': self.session_duration,
            'login_google': self.login_google,
            'login_email_otp': self.login_email_otp,
            # Fail-open login-widening signal from this route's last sync.
            'access_unresolved_idp_types': self.access_unresolved_idp_types or '',
            'access_policies': [p.to_dict() for p in self.access_policy_ids],
        }
