import logging
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..services.naming import make_smarthome_subdomain

from ..services.paas_operator import (
    get_paas_operator_client,
    PaaSOperatorError,
    PaaSOperatorConnectionError,
)

_logger = logging.getLogger(__name__)


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
# `models/cloud_service.py` 頂端 `_INTERNAL_PROVISION` 的註解（本段刻意逐字沿用
# 它的形狀，不另發明；`ir.default` 的持久化載體、`call_kw` 整包換掉 context、
# su env 假綠三個陷阱在那裡有實測紀錄）。
class _InternalProvision:
    """平台伺服器端開通碼的 marker type —— 見上方註解。"""

    __slots__ = ()

    def __repr__(self):
        return '<internal provision>'


_INTERNAL_PROVISION = _InternalProvision()

#: 唯一的逃生口 context key。key 本身不是判準（呼叫端當然抄得到這個字串），
#: 判準是它的**值身分**，見 `_is_internal_provision`。
#: ⚠ key 刻意與 `cloud_service.py` 的不同名：兩個 model 的閘互不授權，共用字串會
#: 讓「開了 A 的閘順便開了 B 的」變成可能。
_INTERNAL_PROVISION_CTX_KEY = 'woow_smart_home_internal_provision'


def _internal_provision(records):
    """回傳「唯一被允許建立 smart_home」的那個 context 底下的 ``records``。

    開閘掛在**會讓記錄落地的那一次呼叫**上，不掛在 model handle 的綁定處——
    唯讀 handle 一律不帶這個 context，逃生口的表面積就只有真正的建立點。

    ``with_context(**kwargs)`` 是 merge，所以 `.sudo()` / `.with_user()` /
    後續 `with_context(foo=...)` 都保留這個把手。
    """
    return records.with_context(
        **{_INTERNAL_PROVISION_CTX_KEY: _INTERNAL_PROVISION})


def _is_internal_provision(env):
    """判準：**只**比對 context 裡那個值的物件身分（`is`），不讀 vals／su／user。"""
    return (env.context.get(_INTERNAL_PROVISION_CTX_KEY)
            is _INTERNAL_PROVISION)


class ProvisionGateError(AccessError):
    """#524：這一筆 smart_home 不是從平台的開通流程來的。

    獨立型別的用途同 `cloud_service.ProvisionGateError`——**讓測試斷言例外型別
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
    """create() 側：丟掉呼叫端自帶的身分錨點。

    `super().create()` 不會繞經 `write()`，所以只靠 write 守衛會留下一個
    create-time squat：一筆全新的列在還沒 provision 之前就綁上受害者的 tunnel
    ——而 `action_provision` 的 `if self.tunnel_id:` 分支會因此**跳過**建立 tunnel。
    對**所有人**生效（含 superuser），sentinel context 是唯一豁免。

    採「剝除」而非 raise：`copy()` 與一般資料載入要能繼續運作——一份不帶錨點的
    複本正是複本該有的樣子。
    """
    if _is_internal_identity_write(env):
        return
    for vals in vals_list:
        for field in _IDENTITY_ANCHOR_FIELDS:
            vals.pop(field, None)


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

    # Timestamps
    deployed_at = fields.Datetime(string='Deployed At')

    @api.model_create_multi
    def create(self, vals_list):
        """#524：建立閘 ＋ 身分錨點的 create 側。

        閘門是整個方法的第一件事，刻意的——後面每一段（錨點剝除、
        `super().create()` 的 INSERT）都有副作用或成本，被拒絕的呼叫不該碰到
        其中任何一段。完整論證見模組頂端 `_INTERNAL_PROVISION` 的註解。

        對所有入口一律生效、**無 su／group 豁免**（否則 TransactionCase 假綠，
        見 `docs/decisions/patterns/su-env-exempt-guard-false-green.md`）。
        """
        if not _is_internal_provision(self.env):
            raise ProvisionGateError(_(
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
                return

            anchored.write(dict(
                tunnel_vals,
                state='active',
                deployed_at=fields.Datetime.now(),
            ))

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

    def action_delete(self):
        """Delete the smart home and its Cloudflare Tunnel.

        Teardown + fail-loud orphan guard are delegated to unlink(): if the
        tunnel cannot be deleted, the record is kept and a UserError surfaces,
        so a billed tunnel is never left orphaned in Cloudflare with no DB row
        to drive a retry (F-ENV-4).
        """
        self.ensure_one()
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
            client.delete_tunnel(self.tunnel_id)
            _logger.info(
                "[SH] tunnel deleted sh=%s tunnel=%s", self.id, self.tunnel_id,
            )
            # #524：清空錨點也是寫錨點 —— 走同一個 sentinel 逃生口。
            _internal_identity(self).write({'tunnel_id': False})
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
        """
        # #322: 比照 CloudService（#318）— super().unlink() 前捕獲 in_progress
        # 訂閱，刪除成功後結算（取消＋按比例退點）。共用 _settle_on_entity_delete。
        # 捕獲在 fail-loud 之前、結算在 super().unlink() 之後：tunnel fail-loud
        # raise 時不結算，全交易 rollback。
        subs_to_settle = self.sudo().mapped('subscription_id').filtered(
            lambda s: s.state == 'in_progress'
        )
        for record in self:
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

    def to_dict(self):
        """Serialize smart home data for API responses."""
        self.ensure_one()
        return {
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
