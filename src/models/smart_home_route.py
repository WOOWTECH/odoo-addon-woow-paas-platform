"""Smart Home Application Route（#768）—— SH 的 Cloudflare Access 錨點載體。

## 這張表存在的理由

裁決 2（#768）把 SH 的 Access 錨點粒度定成「per-route 多錨點（比照 SA）」，
**預留**一台 SH 底下多個對外服務各自 gate。SH 在此之前沒有 route 概念，只有單一
系統生成的 `smart_home.subdomain`（`models/smart_home.py`，DB `UNIQUE`）。

本表就是那個 N 錨點的載體：每一列＝一個「可以掛 Cloudflare Access application
的對外 hostname」。今天每台 SH 恰好一列（`is_primary=True`），對應它 provision
當下烙進 tunnel 的那個 hostname。

## 為什麼本單不順便做「第二條 route」

CF Access app 是**帳號級、以 hostname 為錨**的物件（operator 的 `/api/access/sync`
payload 完全沒有 tunnel 欄位），所以「把既有 hostname 掛上 Access」與「這條 tunnel
有幾條 ingress」是正交的。加第二條 route 需要的是另一組東西：把 provision 從
`create_tunnel(hostname=…)` 改成 bare tunnel ＋ `sync_tunnel_routes`、重新設計
#484 的 `dns_failed` 失敗模型、補 `dns_record_id` 持久化、並動到 HA component 讀
`subdomain` 的跨 repo 契約。那些與 Access 無關，另票處理。

⇒ **本單交付後的誠實邊界**：多 route 在資料模型與 UI 上成立，在實際流量上還不
成立（第二條 route 沒有 ingress／DNS）。

## 🔴 `is_primary` 是與 SA 的**刻意分歧**，不要順手對齊

SA 在「軸2」把 primary route 的概念**移除**了——那邊任何 route 都可刪，刪到剩零
條就把 SA 落回 `active_no_route`（見 `models/security_access.py` 的
`SecurityAccessRoute.unlink` 與 `_recompute_route_state`）。

SH 不能照抄，理由是載體不同：SA 的 tunnel 是 bare tunnel、hostname 全部由
`sync_tunnel_routes` 宣告式推上去，所以「零條 route」是一個合法狀態；SH 的主
hostname 是 `action_provision` 呼叫 `create_tunnel(hostname=…)` 當下**烙進 tunnel
ingress** 的，刪掉那一列不會讓 Cloudflare 少一條 ingress，只會讓平台失去唯一能
描述它的那筆記錄（＝ Access app 從此無錨點可掛、也無從 teardown）。

所以這裡 primary route 是**不可刪、不可改綁**的。看到「SA 沒有 primary 為什麼 SH
有」時，答案在上一段，不是漏改。
"""
from ..services.access_state import AccessProtectionError, AccessPublicationError, access_configuration_ready, require_access_sync_receipt
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# #798：policy 列上 `cf_policy_id` 的唯一寫入豁免。sentinel／opener 住在 SA 那支
# （`security_access` 不 import 任何 sibling model ⇒ 這個方向無循環）。
from .security_access import _internal_policy_cf_write
from .smart_home_access_revision import access_security_fingerprint

_logger = logging.getLogger(__name__)
_DEFER_ACCESS_RELEASE = object()

#: #528 的待回收 CF Access policy 帳本（三腿共用）。
_CF_REAP_MODEL = 'woow_paas_platform.cf_access_policy_reap'
#: 自動撤權 v1 的撤權帳本（只能 insert；見該 model 的 docstring）。
_ACCESS_REVISION_MODEL = 'woow_paas_platform.smart_home_access_revision'


# ------------------------------------------------------------------------- #
#  身分錨點（形態抄 security_access 的 `_ROUTE_IDENTITY_ANCHOR_FIELDS`）      #
# ------------------------------------------------------------------------- #
#
# 錨點＝**決定這一列驅動的是「哪一個」Cloudflare 物件**的欄位。移動它不是「改一個
# 設定」，是把整列重新瞄準別人的資源，而該列仍保有自己 workspace 的 ir.rule 祝福。
#
# 🔴 **為什麼不共用 `smart_home.py` 那一把 sentinel**（這是本檔最容易被「順手
# 統一」掉、而且統一之後所有守衛靜默失效的一處）：
# `src/tests/smart_home_fixture.py::sh_model()` 回的 handle **同時**開了 SH 的
# 建立閘與 SH 的身分錨點閘。SH 的既有 fixture 幾乎全部走它——若本表沿用同一把
# sentinel，那些 fixture 會連帶把**本表的**守衛一起關掉，而症狀是「守衛看起來
# 有測試、實際上從未被執行」。分開之後，要開本表的閘只能顯式呼叫
# `_internal_sm_route_identity(...)`。
#
# ⚠ 命名刻意不與 `cloud_service` / `smart_home` 兩組同名、也不互為子字串：
# `scripts/audit/repo-lint.py` 的 `check_provision_sentinel_allowlist` 判準是
# 字面比對，同名會讓 gate 對本檔吐出一句為假的話，之後真的在本檔開 cloud_service
# 的閘也不會再紅（見 `models/smart_home.py` 頂端同一段論證）。
_SM_ROUTE_IDENTITY_ANCHOR_FIELDS = (
    'access_publish_pending',  # server-owned publication state
    'access_token_revoke_pending',
    'access_app_id',    # WHICH CF Access application this route configures
)


class _SmRouteIdentityWrite:
    """Marker type for the identity-anchor escape hatch — see below."""

    __slots__ = ()

    def __repr__(self):
        return '<sm route identity write>'


#: 唯一的逃生口。值必須是**這個物件**而不是 `True`／字串——`/web/dataset/call_kw`
#: 把呼叫端 context 整包換進 env，任何 JSON 表達得出的判準，呼叫端在 payload 裡
#: 多加一個 key 就打開了（#522 在 18.0 實測過）。同樣刻意**不是** `env.su` 豁免：
#: su 會在每個 `TransactionCase` 與每條 `sudo()` 路徑上靜默停用守衛，正是測試本該
#: 證明它有效的地方（`docs/decisions/patterns/su-env-exempt-guard-false-green.md`）。
_INTERNAL_SM_ROUTE_IDENTITY_WRITE = _SmRouteIdentityWrite()

_SM_ROUTE_IDENTITY_CTX_KEY = 'woow_sm_route_access_identity_write'


def _internal_sm_route_identity(records):
    """回傳「唯一被允許寫入本表身分錨點」的那個 context 底下的 ``records``。"""
    return records.with_context(
        **{_SM_ROUTE_IDENTITY_CTX_KEY: _INTERNAL_SM_ROUTE_IDENTITY_WRITE})


def _is_internal_sm_route_identity_write(env):
    return (env.context.get(_SM_ROUTE_IDENTITY_CTX_KEY)
            is _INTERNAL_SM_ROUTE_IDENTITY_WRITE)


def _server_owned_anchor_value(model, field_name):
    """一個身分錨點在 create 當下該落地的**伺服器端值**（#753 的形狀）。

    有欄位級 ``default`` 的走它，其餘壓 ``False``。刻意讀 ``field.default`` 而不
    是在守衛旁邊自己複製一份預設值：那是 ``default_get`` 的**第 3 順位**，也是
    三個載體裡唯一呼叫端碰不到的那個（第 1 是 context 的 ``default_<field>``、
    第 2 是 ``ir.default`` 的列）。從欄位定義推導也讓「將來新增一個帶 default 的
    錨點」自動正確，不必在守衛旁邊維護第二張對照表。
    """
    field = model._fields[field_name]
    return field.default(model) if field.default else False


def _strip_sm_route_identity_anchors(model, vals_list):
    """create() 側：把身分錨點**壓成伺服器端值**，不是 pop（#753）。

    ``super().create()`` 不會繞經 ``write()``，只靠 write 守衛會留下一個
    create-time squat：一列全新的 route 在還沒 sync 之前就綁上受害者的 Access
    app id，之後的 teardown 會拿平台級 API key 去刪別人的 app。

    🔴 **為什麼是壓值而不是 ``vals.pop(field, None)``**：``super().create()`` 內部
    會呼叫 ``_add_missing_default_values(vals)``，把「vals 裡**缺席**」的欄位從三
    個載體補回來，其中兩個（``default_<field>`` context 與 ``ir.default`` 的列）
    都由租戶決定。``pop`` 恰恰把 anchor 變成缺席 ⇒ 那兩個載體在剝除**之後**才落
    地。把 key 留在 vals 裡並給定伺服器端值才一次涵蓋所有載體
    （``_add_missing_default_values`` 只補不在 vals 裡的欄位）。

    ⚠ 「先解析再 pop」同樣是錯的：pop 之後 key 又缺席，``super().create()`` 內部
    會再解析、再補一次。解析後的 vals 適合**判斷**，不適合**剝除**。
    """
    if _is_internal_sm_route_identity_write(model.env):
        return
    for vals in vals_list:
        for field in _SM_ROUTE_IDENTITY_ANCHOR_FIELDS:
            vals[field] = _server_owned_anchor_value(model, field)


def _reject_sm_route_identity_writes(env, vals):
    """write() 側：拒絕移動錨點，而且要大聲。對所有人生效（含 superuser）。"""
    if _is_internal_sm_route_identity_write(env):
        return
    for field in _SM_ROUTE_IDENTITY_ANCHOR_FIELDS:
        if field in vals:
            raise UserError(
                _("Field '%s' is immutable after creation.") % field)


# ------------------------------------------------------------------------- #
#  sync-control 逃生門（#787；形制抄 security_access 的 `_INTERNAL_SYNC_CONTROL`）#
# ------------------------------------------------------------------------- #
#
# 本檔有三把 context key 是「這一次操作要不要／能不能對 Cloudflare 動手」的**內部
# 憑證**，判定原本只看 truthy。`/web/dataset/call_kw` 把呼叫端 context 整包換進
# env（`odoo/api.py::call_kw` 的 `recs = recs.with_context(context)`）⇒ 租戶送一行
# JSON 就開得了閘。#522 在 18.0 上實測成功過；#536（SA）與 #787 第 1 把（CS）各
# 修過一批，本段是 SH 這一腿。
#
# 三把偽造後各自的後果（可達性由 #787 的 `sh-787-reach` 探針**實測**，非推論）：
#
#   * `sm_skip_access_sync`     write 後的 Access 推送整段被跳過 ⇒ DB 記著「已保護」
#                               而 Cloudflare 上什麼都沒有。**fail-open 且無聲**：
#                               write 回 True、端點回 success、UI 開關是開的。
#   * `sm_route_cascade_unlink` primary route 不可刪的守衛被關掉。刪除本身另有
#                               ACL（`base.group_user` 的 `unlink=0`）擋死，所以
#                               這把是 **defense-in-depth**——ACL 哪天鬆一格，它
#                               就是最後一道。實測簽名是**例外型別翻轉**：守衛擋
#                               的是 `UserError`，被繞過後往下撞 ACL 變 `AccessError`。
#   * `sm_force_access_sync`    liveness gate 的**狀態**那一半被繞過 ⇒ 停權中的 SH
#                               仍把 Access 設定推去 Cloudflare 並把 `app_id` 回寫。
#
# 🔴 **為什麼另立一把，不共用本檔既有的 `_INTERNAL_SM_ROUTE_IDENTITY_WRITE`**：
# 那把守的是「這一列驅動**哪一個** CF 物件」（身分錨點），這三把守的是「這一次要
# 不要對 CF 動手」（同步控制）——兩個不同的信任域，只是剛好住在同一個檔。共用一
# 個 marker 之後，registry 的 `sentinel` 欄位與 runtime WARNING 都會指向錯的那一
# 把，而「本檔到底開了哪一種閘」在 code 上就不再讀得出來。
#
# ⚠ 命名刻意**不與任何既有 marker 同名、也不互為子字串**（雙向都機械查過：
# security_access / cloud_service / smart_home 三個檔的 sentinel 與它們的 helper，
# 外加 #519 那組開閘符號）。理由是
# `scripts/audit/repo-lint.py::check_provision_sentinel_allowlist` 的判準是**整檔
# 字面比對**：互為子字串會讓 gate 對本檔吐出一句為假的話，而且之後真的在本檔開
# 別人的閘也不會再紅（見本檔頂端身分錨點那段的同一條論證）。
# 🔴 連**註解裡照抄那幾個符號名**都會撞這條 gate——本次落地時實際撞到一次，
#    所以上面用描述而不是逐字列名。
#
# key 名一律**不變**，改的只是判定方式 ⇒ `with_context(key=False)` 的「不要抑制」
# 語意原封不動。
class _SmSyncControl:
    """SH route sync-control 逃生門的 marker type —— 見上方註解。"""

    __slots__ = ()

    def __repr__(self):
        return '<sm route sync control>'


_INTERNAL_SM_SYNC_CONTROL = _SmSyncControl()

#: key 本身不是判準（呼叫端當然抄得到這些字串），判準是它們的**值身分**。
_CTX_SM_SKIP_ACCESS_SYNC = 'sm_skip_access_sync'
_CTX_SM_ROUTE_CASCADE_UNLINK = 'sm_route_cascade_unlink'
_CTX_SM_FORCE_ACCESS_SYNC = 'sm_force_access_sync'


def _sm_sync_hatch(records, *keys):
    """回傳「唯一被允許操縱 Access 同步」的那個 context 底下的 ``records``。"""
    return records.with_context(
        **{key: _INTERNAL_SM_SYNC_CONTROL for key in keys})


def _sm_skip_access_sync(records):
    """開閘：這一次 create／write 只搬 DB 狀態，不對 operator 做 Access 推送。

    生產碼裡今天一個呼叫端都沒有（這把只有測試佈景在用），helper 仍然要存在：
    它是 ``with_context(sm_skip_access_sync=True)`` 這種**可偽造寫法**的唯一替代
    品。少了它，下一個真的需要抑制推送的人只會照舊寫 ``=True``，然後在一個只會
    印 WARNING 的無聲失效裡 debug。
    """
    return _sm_sync_hatch(records, _CTX_SM_SKIP_ACCESS_SYNC)


def _sm_route_cascade_unlink(records):
    """開閘：SH 自己的 cascade 刪除途中，primary route 可以跟著走。"""
    return _sm_sync_hatch(records, _CTX_SM_ROUTE_CASCADE_UNLINK)


def _sm_forced_access_sync(records):
    """開閘：繞過 Access 推送 liveness gate 的狀態那一半（仍需 tunnel_id）。"""
    return _sm_sync_hatch(records, _CTX_SM_FORCE_ACCESS_SYNC)


def _is_sm_sync_hatch_open(env, key):
    """這個 sync-control 開關是不是**伺服器端程式碼**設的（而非呼叫端偽造的）。

    只有 sentinel 物件算數。JSON 表達不出物件同一性，所以租戶送
    ``context={"sm_skip_access_sync": true}`` 進 ``call_kw`` 時走的是下面那條
    WARNING —— 守衛不開閘，這一次操作照常推送／照常擋下來。

    「truthy 但不是 sentinel」之所以要 WARNING 而不是靜靜回 False：那既是**偽造
    的訊號**，也是「還有 in-process 的碼照舊傳 ``True``」的訊號。少了它，本修法
    唯一的無聲失效面（傳 True 從此不開閘）永遠不會有人知道。
    """
    value = env.context.get(key)
    if value is _INTERNAL_SM_SYNC_CONTROL:
        return True
    if value:
        _logger.warning(
            "Ignoring non-sentinel %r in context (value=%r, uid=%s): this "
            "hatch only opens for in-process callers using the module's "
            "helper — see _INTERNAL_SM_SYNC_CONTROL (#787). Either a forged "
            "RPC context, or in-process code still passing True.",
            key, value, env.uid)
    return False


class SmartHomeRoute(models.Model):
    _name = 'woow_paas_platform.smart_home_route'
    _description = 'Smart Home Application Route'
    _order = 'is_primary desc, id'

    smart_home_id = fields.Many2one(
        comodel_name='woow_paas_platform.smart_home',
        string='Smart Home',
        required=True,
        ondelete='cascade',
        index=True,
    )
    is_primary = fields.Boolean(
        string='Primary Route',
        default=False,
        help="True = the hostname of this route IS the Smart Home's own "
             "subdomain, created by the platform at provision time; it can "
             "neither be deleted nor re-pointed (see the module header).",
    )
    subdomain_prefix = fields.Char(
        string='Subdomain Prefix',
        help='User-chosen prefix for non-primary routes; hostname = '
             '{prefix}.{paas_domain}. Always empty at this stage — a second '
             'route requires the tunnel ingress migration, tracked separately '
             '(see the module header).',
    )
    hostname = fields.Char(
        string='Hostname',
        compute='_compute_hostname',
        help='Public FQDN. For a primary route it is **derived from** '
             'smart_home.subdomain rather than copied: a copy would be a second '
             'source of truth, and there is no signal whatsoever when the two '
             'disagree.',
    )
    is_protected = fields.Boolean(
        string='Protected',
        default=False,
        help='Per-route Access protection switch. True = this route is a '
             'Cloudflare Access application in its own right; False = publicly '
             'reachable through the tunnel with no Access gate.',
    )
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
        help='Cloudflare Access Application id for THIS route (written back on '
             'sync). Identity anchor: tenants cannot write it.',
    )
    access_policy_ids = fields.One2many(
        comodel_name='woow_paas_platform.security_access_access_policy',
        inverse_name='smart_home_route_id',
        string='Access Policies',
    )
    access_unresolved_idp_types = fields.Char(
        string='Unresolved Login Methods', copy=False,
        help='Requested login methods with no matching Cloudflare account IdP '
             'at the last sync. Non-empty means the login restriction '
             '**silently widened** (if the resolved set emptied, the Access app '
             'falls back to ALL available IdPs). Surfaced so the fail-open '
             'widening is not silent.',
    )
    access_publish_pending = fields.Boolean(
        string='Access Publication Pending', default=False, readonly=True,
        help='Access settings are saved, but the origin publication result is unconfirmed. Retry.')
    access_token_revoke_pending = fields.Boolean(
        string='Access Token Revocation Pending', default=False, readonly=True)
    access_protection_lost = fields.Boolean(
        string='Protection Not In Effect', copy=False, default=False,
        help='True = the Cloudflare Access application for this route has been '
             'torn down while the row itself is still here (the delete '
             'transaction rolled back; the Cloudflare half cannot be). '
             'is_protected is then only an **intent**: nothing on CF is forcing '
             'a login, and the hostname is still served through the tunnel. '
             'Cleared by the next successful Access sync. Deliberately kept '
             'apart from is_protected — lowering is_protected would mean the '
             'platform changing a setting the tenant can see (a product-visible '
             'change), which is not decided yet (#794).',
    )

    # ------------------------------------------------------------------ #
    #  compute / constraints                                             #
    # ------------------------------------------------------------------ #

    @api.depends('is_primary', 'subdomain_prefix', 'smart_home_id.subdomain')
    def _compute_hostname(self):
        domain = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')
        for route in self:
            if route.is_primary:
                base = route.smart_home_id.subdomain
            else:
                base = route.subdomain_prefix
            route.hostname = f"{base}.{domain}" if base else False

    #: partial unique index 的名字。model 的 `init()` 與
    #: `migrations/18.0.1.0.150/post-migrate.py` 共用同一個常數值（後者是為了
    #: 讓升級路徑上「有沒有建起來」可被驗證）。
    _PRIMARY_INDEX = 'woow_paas_platform_smart_home_route_one_primary'

    def init(self):
        """一台 SH 至多一條 primary route —— **DB 層**的唯一性守線。

        🔴 為什麼不放在 migration 裡就好：migration 只在**升級**時跑。只放那邊
        的話 fresh install（`-i`）永遠不會有這個 index，症狀是「舊環境有守線、
        新環境沒有」——而兩邊的行為測試都會綠（Python constraint 在兩邊都在）。
        `init()` 由 Odoo 在每次 install/update 的 `_auto_init` 之後呼叫，兩條路徑
        都涵蓋得到。

        partial unique index 表達不進 `_sql_constraints`（那裡只吃 constraint 語
        法，沒有 `WHERE`），所以走 `init()` ＋ 裸 SQL，這是 Odoo core 自己對同類
        需求的做法。`IF NOT EXISTS` ⇒ 冪等，與 migration 的補建互不衝突。
        """
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS %s
                ON %s (smart_home_id) WHERE is_primary
            """ % (self._PRIMARY_INDEX, self._table))

    @api.model
    def _primary_conflict_message(self, smart_home):
        """單一文案來源：create 前置檢查與 `@api.constrains` 兩個呼叫點共用。"""
        return _(
            'Smart Home %(name)s already has a primary route. Its primary '
            'hostname is baked into the Cloudflare tunnel at provision time, '
            'so there can only be one.',
            name=smart_home.name or smart_home.id)

    @api.model
    def _assert_single_primary_vals(self, vals):
        """create 側的唯一性檢查——必須在 ``super().create()`` 的 INSERT 之前。

        為什麼不能只靠底下的 ``@api.constrains``：Odoo 的 ``_create()`` **先送
        INSERT、之後**才跑 Python constraint，所以 create 方向永遠是 PostgreSQL
        的 partial unique index 先炸——拿到的是 `UniqueViolation`（訊息只有索引
        名，說不出錯在哪），而且交易當場 abort。write 方向相反
        （``write()`` 把值排進 ``towrite`` 延後 flush，``_validate_fields``
        先跑），那一側交給 constrains 就夠。這是 policy 的 `anchor_arc` 已經記
        錄過的同一個不對稱。

        ⚠ 傳進來的 vals 必須是**框架解析後**的（``_add_missing_default_values``）：
        ``default_is_primary`` 之類是在 ``super().create()`` 內部才展開的，只看
        raw vals 會讓「用 context default 塞 is_primary」整道跳過（#705 F1 同
        形狀）。
        """
        if not vals.get('is_primary') or not vals.get('smart_home_id'):
            return
        if self.search_count([
            ('smart_home_id', '=', vals['smart_home_id']),
            ('is_primary', '=', True),
        ]):
            raise ValidationError(self._primary_conflict_message(
                self.env['woow_paas_platform.smart_home'].browse(
                    vals['smart_home_id'])))

    @api.constrains('is_primary', 'smart_home_id')
    def _check_single_primary(self):
        """一台 SH 至多一條 primary route（write 側）。

        與 `init()` 建的 partial unique index 是**同一條不變式的兩層**，不是二
        選一：index 是唯一擋得住併發與裸 SQL 的那層，本方法負責在 write 方向給
        出講得出「錯在哪」的訊息（PG 的 unique 違反只回索引名）。
        """
        for route in self.filtered('is_primary'):
            other = self.search_count([
                ('smart_home_id', '=', route.smart_home_id.id),
                ('is_primary', '=', True),
                ('id', '!=', route.id),
            ])
            if other:
                raise ValidationError(
                    self._primary_conflict_message(route.smart_home_id))

    # ------------------------------------------------------------------ #
    #  ORM overrides                                                      #
    # ------------------------------------------------------------------ #

    @api.model_create_multi
    def create(self, vals_list):
        _strip_sm_route_identity_anchors(self, vals_list)
        for vals in vals_list:
            # 必須在 `super().create()` 之前——INSERT 一旦送出去，擋下來的就是
            # PG 的 partial unique index（訊息不可讀且交易 abort）。
            self._assert_single_primary_vals(
                self._add_missing_default_values(vals))
        # Atomicity scope：savepoint 只保證 **Odoo 側**——sync 失敗時 DB 這一列
        # 回滾，不會留下一個 DB 有、Cloudflare 沒有的「幽靈 route」讓之後某次
        # sync 靜默把它推公開。它**不**保證 operator 側原子性：`sync_access_app`
        # 是宣告式 full-state push 且沒有補償動作，中途失敗仍可能讓 CF 半改；
        # 那份殘留由下一次冪等的 full-state re-sync 收斂，不是由這個 savepoint。
        with self.env.cr.savepoint():
            routes = super().create(vals_list)
            if not _is_sm_sync_hatch_open(
                    self.env, _CTX_SM_SKIP_ACCESS_SYNC):
                for route in routes:
                    route._sync_access_to_operator()
        return routes

    def write(self, vals):
        _reject_sm_route_identity_writes(self.env, vals)
        # primary route 的綁定與 primary 旗標本身都是不可變的：hostname 衍生自
        # 它、Access app 掛在 hostname 上，改綁等於把整列重新瞄準另一台 SH 的
        # Cloudflare 物件（而該列仍保有原 workspace 的 ir.rule 祝福）。
        for field in ('smart_home_id', 'is_primary'):
            if field not in vals:
                continue
            for route in self:
                if not route.is_primary:
                    continue
                # ⚠ M2O 要比 id 不能比 recordset：`route['smart_home_id']` 是
                # recordset 而 vals 送進來的是 int，直接 `!=` 在 Odoo 18 只會發
                # 一個 UserWarning 然後回 True ⇒ 守衛看起來有效、其實是對每一次
                # write 都成立（連「寫入同一個值」都會被擋）。
                current = (route.smart_home_id.id if field == 'smart_home_id'
                           else bool(route.is_primary))
                incoming = (vals[field] if field == 'smart_home_id'
                            else bool(vals[field]))
                if current != incoming:
                    raise UserError(
                        _("Field '%s' is immutable on a primary Smart Home "
                          "route.") % field)
        unprotecting = self.browse()
        if (not _is_sm_sync_hatch_open(
                    self.env, _CTX_SM_SKIP_ACCESS_SYNC)
                and vals.get('is_protected') is False):
            unprotecting = self.filtered(
                lambda route: route.is_protected and route.access_app_id)
        with self.env.cr.savepoint():
            # The operator may delete Access before its response is validated;
            # park ingress first while the old protected identity is intact.
            for home in unprotecting.mapped('smart_home_id'):
                home._sync_access_ingress(park=True)
            if unprotecting:
                _internal_sm_route_identity(
                    unprotecting).access_publish_pending = True
            result = super().write(vals)
            if (not _is_sm_sync_hatch_open(
                        self.env, _CTX_SM_SKIP_ACCESS_SYNC)
                    and any(k in vals for k in (
                        'is_protected', 'login_google', 'login_email_otp',
                        'session_duration'))):
                deferred_release = self.filtered('access_publish_pending')
                for route in self:
                    route._sync_access_to_operator(
                        _DEFER_ACCESS_RELEASE if deferred_release else None)
                from ..services.paas_operator import PaaSOperatorError
                for home in deferred_release.mapped('smart_home_id'):
                    routes = deferred_release.filtered(
                        lambda route: route.smart_home_id == home)
                    _internal_sm_route_identity(
                        routes).access_publish_pending = False
                    try:
                        home._sync_access_ingress()
                    except (PaaSOperatorError, AccessPublicationError) as error:
                        _internal_sm_route_identity(
                            routes).access_publish_pending = True
                        _logger.error(
                            "[SH] Access batch publication pending routes=%s: %s",
                            routes.ids, error)
        return result

    def unlink(self):
        """primary route 不可刪；非 primary 刪除時要拆掉它的 Access app。

        🔴 拆除的 id 必須在 ``super().unlink()`` **之前**捕獲：那一步會把
        ``access_app_id`` 與每一條 policy 的 ``cf_policy_id`` 一起帶走，之後
        operator 沒有 id 可刪，app／policy 就變成永久 CF 孤兒，而且**沒有任何
        DB 列可以當重試錨點**。
        """
        primary = self.filtered('is_primary')
        if primary and not _is_sm_sync_hatch_open(
                self.env, _CTX_SM_ROUTE_CASCADE_UNLINK):
            raise UserError(_(
                'The primary route of a Smart Home cannot be deleted — its '
                'hostname is served by the Cloudflare tunnel itself. Delete '
                'the Smart Home instead.'))
        teardown = [r._capture_access_teardown() for r in self]
        # 🔴 #794：savepoint 回捲得掉 DB 那半，回捲不掉「已經對 Cloudflare 發出
        # 的 Access 刪除」⇒ 先被拆掉的那幾條會以「列復活、`is_protected=True`、
        # `access_app_id` 指向一顆 CF 上已不存在的 app」收場，UI 讀 is_protected
        # 顯示已保護、實際免登入即可存取。`torn_down` 記下真的拆成功的錨點，
        # `except` 那一格（savepoint 已回捲、例外尚未外拋）交給獨立交易補償。
        torn_down = []
        Reap = self.env[_CF_REAP_MODEL]
        try:
            with self.env.cr.savepoint():
                result = super().unlink()
                # #794：**第一條失敗就停**。舊寫法是 eager 的 list comprehension，
                # 同批第一條失敗之後後面每一條仍會被真的送去 CF 拆掉才一起回捲
                # ——而 SH 這一腿連 SA 的 pass1/pass2 分組都沒有，整批都會被拆，
                # 窗口比 SA 更寬。代價：錯誤訊息只列得出第一條。
                failed = None
                for info in teardown:
                    # #785：這個 `continue` 只吃得到「列上沒有任何 id 要拆」。
                    # 「有東西但推不動」曾經也回 `None` 而從這裡被靜默吃掉、
                    # 不計入 `failed` ⇒ 下面的 fail-loud 判成「這一輪乾淨」。
                    # 那道預測性 liveness 閘已經拆掉，見
                    # `_capture_access_teardown()` 的 docstring。
                    if not info:
                        continue
                    # `torn_down` 由 `_push_access_teardown` 在 operator 回
                    # 2xx 當下就 append（見該方法的 [HIGH] 註解）。
                    if self._push_access_teardown(info, torn_down=torn_down):
                        continue
                    failed = info['label']
                    break
                if failed:
                    # fail-loud：列已經沒了，吞掉這個失敗會留下一組 CF Access app
                    # ＋ reusable policy 永久孤兒，而且沒有重試錨點也沒有錯誤訊號。
                    # raise 讓 savepoint 把 unlink 回滾，列（與它的 access_app_id）
                    # 留下來當重試錨點。
                    raise UserError(_(
                        'Cloudflare Access teardown failed for route(s): '
                        '%(names)s. The route was kept so the deletion can be '
                        'retried; its Access app is still live in Cloudflare.',
                        names=failed))
        except Exception:
            Reap._compensate_fail_open_after_rollback(
                torn_down, _logger,
                '[SH] route unlink (routes=%s)' % (self.ids,))
            raise
        return result

    # ------------------------------------------------------------------ #
    #  Cloudflare Access sync（宣告式；一條 route 一個 CF Access app）     #
    # ------------------------------------------------------------------ #

    def _access_is_live(self, forced=False):
        """本 route 現在推得動 Cloudflare 嗎。

        判準取 **SA 那條（`tunnel_id`）而不是 CS 那條（`subdomain`）**：SH 與 SA
        一樣是 dedicated tunnel，hostname 要等 tunnel ＋ DNS 都建起來才解析得到。

        `error` 必須包含在內：`action_provision` 的 `dns_failed` 分支會留在
        `error` 而 tunnel 活著、訂閱仍有效、租戶仍能編輯設定——把 `error` 排除
        會讓 DB 與 Cloudflare 靜默分歧，而端點照樣回報成功。

        `suspended` 刻意**排除**：停權時 ingress 已被打成 `http_status:503`，
        Access app 留著無害；resume 時 `smart_home._do_resume` 會帶 forced 旗標
        補一次收斂（否則「停權期間被關掉保護的 route」永遠不會被推回去）。
        """
        self.ensure_one()
        sh = self.smart_home_id
        if not sh or not sh.tunnel_id or not self.hostname:
            return False
        if not forced and sh.state not in ('active', 'error'):
            return False
        return True

    def _build_existing_policy_ids(self):
        """operator 必須 delete-then-recreate 的 CF reusable policy id。

        兩個來源，兩個都是伺服器端讀到的：存活列的 ``cf_policy_id``，加上這個錨點
        在待回收帳本上還欠的（``cf_access_policy_reap._pending_for``）。

        policy 的 ``unlink()`` 會先跑 ``super().unlink()`` 再 re-sync，那些剛被刪
        掉的列的 cf id 已經不在 DB 裡、永遠進不了這份清單，而 operator 唯一的刪除
        通道就是它 ⇒ 每刪一條漏一顆永久孤兒。它們是**經帳本**進來的：``unlink()``
        在同一個 savepoint 內先 ``Reap._record_ids()`` 落帳，才觸發 re-sync。

        #528：帳要離開一次性的通道、落到 DB——那一次 re-sync 會被 `_access_is_live`
        的 state 閘擋掉（SH 的 `suspended` 刻意排除在外），只活一次呼叫的東西被擋
        掉之後就沒有任何記錄指向那些 id 了。帳本讓它們活到下一次推得動為止。

        🔴 #783：`_pending_for()` **不讀任何 context**（舊的
        `sa_unlinked_cf_policy_ids` 通道已整條移除，理由見該函式的 docstring）。
        """
        self.ensure_one()
        ids = [p.cf_policy_id for p in self.access_policy_ids if p.cf_policy_id]
        for cf_id in self.env[_CF_REAP_MODEL]._pending_for(self):
            if cf_id and cf_id not in ids:
                ids.append(cf_id)
        return ids

    def _build_access_payload(self):
        """operator `/api/access/sync` 的 desired-state payload（限本 route）。

        ⚠ **逐欄位鏡射 SA route / CS 的同名方法不是重複**：operator 只有一個
        `AccessSyncRequest` schema，三份 payload 的形狀分家只會在 422 才發現，
        而 422 說不出是哪一邊錯。
        """
        self.ensure_one()
        sh = self.smart_home_id
        login_idp_types = []
        if self.login_google:
            login_idp_types.append('google')
        if self.login_email_otp:
            login_idp_types.append('onetimepin')
        hostname = self.hostname or ''
        protected = [hostname] if (self.is_protected and hostname) else []
        return {
            'enabled': bool(self.is_protected),
            'canonical_name': sh._access_canonical_name(),
            'app_name': self._access_app_name(),
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
            'revoke_tokens': bool(self.access_app_id and self.access_token_revoke_pending),
        }

    def _mark_access_token_revoke_pending(self):
        existing = self.filtered('access_app_id')
        if existing:
            _internal_sm_route_identity(existing).write({
                'access_token_revoke_pending': True,
                'access_publish_pending': True,
            })

    def _access_app_name(self):
        """CF Access app 的顯示名：一台 SH 底下多條 route 不得撞名。"""
        self.ensure_one()
        if self.is_primary:
            return self.smart_home_id.subdomain or (
                self.smart_home_id.name or 'smart-home')
        return self.subdomain_prefix or ('route-%s' % self.id)

    def _sync_access_to_operator(self, release_control=None):
        """把本 route 的完整 Access desired-state 推給 operator（冪等）。

        `is_protected=False` 送 `enabled=False` 的 payload（帶 `existing_app_id`
        做 teardown），推完清掉存下來的 app id；沒有 app id 時整支是無害的 no-op。

        #528：兩道 gate 收斂成 `_access_sync_skip_reason()` 一個裁決處，略過時回
        ``False``、推出去了回 ``True``。原本兩道各自裸 `return`，「被擋掉」與
        「推了而且成功」在呼叫端輸出上完全不可區分——而 SH 的 state 閘正是
        #528 在這條腿上的實際觸發點（停權期間刪 policy）。
        """
        self.ensure_one()
        skip = self._access_sync_skip_reason()
        if skip:
            # #794 補洞：列已經不宣稱保護、也沒有 app ⇒ CF 上「沒有 app」正是
            # 這一列要的狀態，旗標的前提（宣稱受保護、app 卻已被拆掉）不成立。
            # 這一格推不出去也不必推（沒有東西可拆），所以在略過時就地清掉——否則
            # 「保護失效後直接改成公開」會永遠掛著「保護未生效」，#803 cron 也每輪
            # 把它算成 gated。只看列自己的意圖與 app id，與 liveness gate 無關：
            # 停權中的列改成公開，同樣不再「宣稱保護」。
            if (self.access_protection_lost and not self.is_protected
                    and not self.access_app_id):
                self.access_protection_lost = False
            self.env[_CF_REAP_MODEL]._warn_if_deferred(
                self, _logger,
                '[SH] route %s (sh=%s)' % (self.id, self.smart_home_id.id),
                skip)
            return False
        home = self.smart_home_id
        forced = _is_sm_sync_hatch_open(self.env, _CTX_SM_FORCE_ACCESS_SYNC)
        if (self.is_protected and
                (not self.access_app_id or self.access_protection_lost or self.access_publish_pending or self.access_token_revoke_pending) and
                not forced):
            home._sync_access_ingress(park=True)
        needs_release = self.access_publish_pending or (self.is_protected and (not self.access_app_id or self.access_protection_lost))
        # 自動撤權 v1：這一輪之前 CF 上沒有生效中的 app（首次保護／關掉後重開／
        # protection_lost 後重建）⇒ 那段期間任何人都可能已連上，必須強制記一版。
        gate_was_open = self.is_protected and (
            not self.access_app_id or self.access_protection_lost)
        client = home._get_operator_client()
        payload = self._build_access_payload()
        fence_claim = None
        if payload.get('enabled') and not payload.get('existing_app_id'):
            Fence = self.env['woow_paas_platform.access_dispatch_fence'].sudo()
            Fence._ensure_prepared(self, payload)
            fence_claim = Fence._claim_send(self, payload)
            if fence_claim['mode'] == 'adopt_only':
                # .271's operator has no idempotent create/adopt primitive.
                # A reviewed exact app id can safely use the normal PUT path;
                # otherwise stop before provider IO. Never turn uncertainty
                # into a second POST.
                if not fence_claim.get('provider_app_id'):
                    _logger.error(
                        '[SH] Access create fenced pending provider review '
                        'route=%s app=%s hostname=%s', self.id,
                        payload.get('app_name'), payload.get('primary_hostname'))
                    return False
                payload['existing_app_id'] = fence_claim['provider_app_id']
        result = client.sync_access_app(payload)
        require_access_sync_receipt(result, payload)
        if fence_claim and result.get('app_id'):
            Fence._record_provider_identity(
                fence_claim['fence_id'], result['app_id'])
        if self.is_protected and not result.get('app_id'):
            raise AccessProtectionError(_(
                "Access protection was not established. Origin publication "
                "is not confirmed; fix the login configuration and retry."))
        if payload.get('enabled'):
            # 自動撤權 v1：CF 已確認新的收緊（或同樣的）desired state ⇒ 指紋有變
            # 就在**獨立交易**記一版，讓 HA 重設既有連線。只記 `enabled=True`：
            # 關保護／刪 route 是放寬存取，不需要斷線（契約「刻意不做」）。
            # 寫不進去會 raise（fail-loud），理由見
            # `smart_home_access_revision._record_confirmed_state`。
            self.env[_ACCESS_REVISION_MODEL].sudo()._record_confirmed_state(
                home.id, self.id, access_security_fingerprint(payload),
                force=gate_was_open)
        orphaned = result.get('orphaned_policy_ids') or []
        if orphaned:
            # operator 重指 app 之後刪不掉的舊 reusable policy＝永久 CF 孤兒
            # （成本／雜訊），沒有任何後續 re-sync 會收斂它們 ⇒ 至少讓它可見。
            _logger.warning(
                "[SH] route %s (sh=%s): operator left %d orphan CF Access "
                "policy(ies) after sync: %s", self.id, self.smart_home_id.id,
                len(orphaned), ', '.join(orphaned))
        # #528：operator 回 2xx ⇒ 這一輪送出去的 existing_policy_ids 已經有答案。
        # 沒被列進 orphaned 的確認刪掉 ⇒ 消帳；被列進去的現在就是 CF 孤兒 ⇒ 記進
        # 帳等下一輪（上面那行 warning 照發，但不再是「說完就忘」）。
        # #791 覆核 F7：包 post-commit savepoint（記帳失敗不得拖垮下面的 id 回存）。
        # F9：`trusted_ids` 只給列上真實存在的 cf id——`existing_policy_ids` 裡
        # 混進來的 context 值不得經 orphaned 回流落帳（#783）。
        Reap = self.env[_CF_REAP_MODEL]
        Reap._settle_after_commit(
            self._name, self.id,
            payload.get('existing_policy_ids') or [], orphaned,
            Reap._trusted_ids_for(self), _logger,
            '[SH] route %s (sh=%s)' % (self.id, self.smart_home_id.id))
        _internal_sm_route_identity(self).access_app_id = (
            result.get('app_id') or False)
        if fence_claim and result.get('app_id'):
            Fence._resolve_after_commit(
                fence_claim['fence_id'], self._name, self.id,
                result.get('app_id'))
        unresolved = result.get('unresolved_idp_types') or []
        new_unresolved = ', '.join(unresolved) if unresolved else False
        if self.access_unresolved_idp_types != new_unresolved:
            self.access_unresolved_idp_types = new_unresolved
        # #794：推成功了 ⇒ CF 上的狀態就是這一列宣稱的狀態，fail-open 旗標的
        # 前提（app 被拆掉但列還在）已經不成立。旗標由 sync 結果驅動，與
        # `access_unresolved_idp_types` 同一個形狀。
        if self.access_protection_lost:
            self.access_protection_lost = False
        returned = result.get('policy_ids') or []
        # #798：包 recordset 不是包 record。⚠ 下面是**兩個不同的迴圈**，各包一次。
        policies = _internal_policy_cf_write(self.access_policy_ids).sorted(
            key=lambda x: (x.sequence, x.id))
        for pol, cf_id in zip(policies, returned):
            if pol.cf_policy_id != cf_id:
                pol.cf_policy_id = cf_id
        if not result.get('app_id'):
            for pol in _internal_policy_cf_write(self.access_policy_ids):
                if pol.cf_policy_id:
                    pol.cf_policy_id = False
        defer_release = release_control is _DEFER_ACCESS_RELEASE
        revoke_pending = bool(self.access_token_revoke_pending)
        _internal_sm_route_identity(self).access_publish_pending = bool(
            revoke_pending or (defer_release and needs_release))
        if revoke_pending:
            return False
        if defer_release:
            return True
        if needs_release and not forced:
            from ..services.paas_operator import PaaSOperatorError
            try:
                home._sync_access_ingress()
            except (PaaSOperatorError, AccessPublicationError) as error:
                _internal_sm_route_identity(self).access_publish_pending = True
                _logger.error("[SH] Access publication pending route=%s: %s", self.id, error)
                return False
        return True

    def _access_sync_skip_reason(self):
        """``None`` ＝ 這一輪推得動；否則回一句人話說明為什麼不推（#528）。

        本腿**所有** liveness gate 的唯一裁決處（`_access_is_live` 仍是判準本身，
        teardown 路徑也用它；本方法只負責把「不推」翻成呼叫端說得出口的理由）。
        新增 gate 一律加在這裡，訊號自動跟上。

        兩格的可達性：`_access_is_live` 的 **state 閘**帶得著待回收 id
        （`_do_suspend` 完全不碰 Access app，停權期間 `cf_policy_id` 全都還在，
        而刪 policy 的端點只看訂閱狀態、不看 SH state）；`tunnel_id` / `hostname`
        兩格與「未保護且沒有 app id」那格帶不著。
        """
        self.ensure_one()
        forced = _is_sm_sync_hatch_open(
            self.env, _CTX_SM_FORCE_ACCESS_SYNC)
        if not self._access_is_live(forced=forced):
            sh = self.smart_home_id
            return (
                "the route is not live (smart home state=%r, tunnel=%s, "
                "hostname=%s, forced=%s)" % (
                    sh.state if sh else None, bool(sh and sh.tunnel_id),
                    bool(self.hostname), forced))
        # 從沒建過 app 的未保護 route 沒有任何事要做
        return (None if (self.is_protected or self.access_app_id or self.access_publish_pending)
                else 'the route is unprotected and never had an Access app')

    # ------------------------------------------------------------------ #
    #  teardown（刪除路徑；id 必須在列消失之前捕獲）                        #
    # ------------------------------------------------------------------ #

    def _access_teardown_ids(self):
        """這條 route 現在有哪些 CF Access 物件 id 需要拆：``(app_id, [policy])``。

        #785：**刪除路徑上「要不要拆」的唯一裁決處**，形狀取 SA 那一腿的同名
        方法（`security_access.py`）。判準是「**手上有沒有 id**」，永遠不是
        liveness、也不是 `is_protected`——「未保護但仍持有 stored app_id /
        cf_policy_id」是這支程式自己承認的合法持久狀態（見
        `_sync_access_to_operator` 的 docstring）。

        🔴 **本方法只讀這一列的狀態，不查待回收帳本**。SA 那一腿把「這一趟該送
        哪些 id」另外命名成 `_access_teardown_payload_ids()`（列 ∪ 帳本），因為
        把帳本聯集進**判準**會讓「清乾淨了沒」永遠回 True ⇒ 錨點還活著、
        dead-anchor cron 依設計不收 ⇒ 永久卡在 `error`（#792 二輪覆核 F6）。
        SH 這一腿目前**沒有** payload 版（`_capture_access_teardown()` 送出去的
        就是列上的 id），要移植就照 SA 的形狀新開一個方法，**不要**把帳本查詢
        加進本方法。
        """
        self.ensure_one()
        return (self.access_app_id or None,
                [p.cf_policy_id for p in self.access_policy_ids
                 if p.cf_policy_id])

    def _has_access_teardown(self):
        """這條 route 的**列上**還有沒有 CF Access 物件要拆。

        三腿同名同義（SA `security_access.py`、CS `cloud_service.py`）：判準是
        列上還有沒有 id。刻意不含帳本，理由見 `_access_teardown_ids()`。
        """
        self.ensure_one()
        app_id, pol_ids = self._access_teardown_ids()
        return bool(app_id or pol_ids)

    def _capture_access_teardown(self):
        """回傳拆除這條 route 的 Access 所需的一切，或 ``None``。

        🔴 **``None`` 只有一個語意：這一列沒有任何 id 要拆**（#785）。

        這裡原本有第二個 `return None`——`if not self._access_is_live(forced=
        True)`，語意是「有東西要拆，但我判斷現在推不動」。兩種語意共用同一個回
        傳值，而兩個消費端（本 model 的 `unlink()` 與
        `smart_home._teardown_routes_access()`）都是 `if not info: continue`、
        **不計入 `failed`** ⇒ 呼叫端的 fail-loud（`unlink` 回滾保住重試錨點）與
        落 `error`（`_do_cleanup`）雙雙判定「這一輪乾淨」，繼續往下刪列。結果是
        CF 上的 Access app ＋ reusable policy 留著、DB 列沒了、**沒有重試錨點也
        沒有任何訊號**——與乾淨刪除在輸出上完全不可區分（#528 那一族的形狀）。

        **為什麼是刪掉那道閘，而不是把它改成 fail-loud**：它預測的「推不動」對
        teardown 方向根本不成立。operator 的 teardown 分支（`enabled=False`）只
        吃 `existing_app_id` / `existing_policy_ids`，**不碰 tunnel、不讀
        hostname**——見 `extra/paas-operator/src/services/cloudflare.py` 的
        `sync_access_app()`：`if not enabled or not protected_hostnames:` 那一段
        只呼叫 `_delete_access_app()` 與 `delete_reusable_policy()`，
        `primary_hostname` 在該分支完全沒被讀到；而 `_delete_access_app()` 把
        404 當成功（`status_code not in (200, 202, 404)` 才 raise）。所以「tunnel
        已經不在但 app 還在」照樣拆得掉。把一個前提為假的預測改成 fail-loud，只
        是把無聲漏拆換成無謂擋刪；正解是**真的推一次**，讓
        `_push_access_teardown()` 的成敗去決定要不要 fail-loud——失敗那格本來就
        已經 `_logger.warning` ＋ 回 ``False`` ＋ 進 `failed`。SA 那一腿在 #784／
        PR #791 已經這樣做（`_access_teardown_ids()` / `_has_access_teardown()`），
        本方法對齊它；CS 從來沒有這道閘。三腿判準因此一致。

        liveness 沒有消失，它換了工作：推不動的那一格現在**出聲**（下面的
        warning）。它同時是「清 `tunnel_id` 之前一定先清掉 `cf_policy_id`」這條
        跨方法順序不變式被破壞時的現場訊號——那條不變式本身由
        `src/tests/test_sh_teardown_capture_semantics_785.py` 的 **C 系列**鎖住
        （B 系列鎖的是兩個呼叫端不再把「推不動」判成「這一輪乾淨」）。
        """
        self.ensure_one()
        if not self._has_access_teardown():
            return None
        app_id, policy_ids = self._access_teardown_ids()
        if not self._access_is_live(forced=True):
            sh = self.smart_home_id
            _logger.warning(
                "[SH] Access teardown captured on a route that is NOT live "
                "route=%s sh=%s app=%s policies=%s (tunnel=%s hostname=%s) — "
                "pushing anyway; these ids are the only handle left on the "
                "Cloudflare objects",
                self.id, sh.id if sh else None, app_id, policy_ids,
                bool(sh and sh.tunnel_id), bool(self.hostname))
        return {
            'smart_home': self.smart_home_id,
            # #784：錨點用純量帶著走——推的時候這一列可能已經不在了，而
            # `_settle_teardown` 要記／消的帳就掛在這個 (model, id) 上。
            'anchor_model': self._name,
            'anchor_id': self.id,
            'app_id': app_id,
            'policy_ids': policy_ids,
            'app_name': self._access_app_name(),
            'hostname': self.hostname or '',
            'label': self._access_app_name(),
        }

    @api.model
    def _push_access_teardown(self, info, torn_down=None):
        """把一份捕獲好的 teardown 推給 operator。成功回 ``True``。

        best-effort ＋ 回 bool（不 raise）：呼叫端自己決定要 fail-loud 還是留給
        下一輪 cron 重試——兩條路徑對「失敗」的處置不同（unlink 要回滾保住重試
        錨點；cron 的 `_do_cleanup` 要落 `error` 等下一次）。

        ⚠ **這個「不 raise」有一個明載的例外**（#791 覆核 F4）：提交點之後的記帳段
        若拋 ``OperationalError``（serialization failure / 連線類）會**往外拋**。
        吞掉它 Odoo 的 ``retrying()`` 就永遠看不到，而此刻 PG 交易已 aborted，硬回
        一個 bool 只會讓後續 flush 再炸一次（#518）。呼叫端不必自己接：request 級
        retry 會用新 snapshot 重播，而重播是安全的（CF 的 DELETE 把 404 當成功）。
        """
        sh = info['smart_home']
        try:
            client = sh._get_operator_client()
            payload = {
                'enabled': False,
                'canonical_name': sh._access_canonical_name(),
                'app_name': info['app_name'],
                'primary_hostname': info['hostname'],
                'protected_hostnames': [],
                'session_duration': '24h',
                'login_idp_types': [],
                'policies': [],
                'existing_app_id': info['app_id'],
                'existing_policy_ids': info['policy_ids'],
            }
            result = client.sync_access_app(payload)
            require_access_sync_receipt(result, payload)
        except Exception as e:  # noqa: BLE001 — 呼叫端決定嚴重性
            _logger.warning(
                "[SH] Access teardown failed sh=%s app=%s: %s — kept for retry",
                sh.id, info['app_id'], e)
            return False
        # ── 提交點之後 ─────────────────────────────────────────────────
        # #784：operator 回報刪不掉的那幾顆現在就是 CF 孤兒，而呼叫端拿到 True
        # 之後列就會被刪掉 ⇒ 舊寫法（連 log 都沒有）之後再也沒有任何記錄指向
        # 它們。落帳，由 dead-anchor cron 回收。記帳失敗不翻轉回傳值
        # （[[post-commit-bookkeeping-rollback-unit]]）。
        # 🔴 #794 二輪覆核 [HIGH]：「CF 已經拆成功」要在**這裡**就記，不能等到
        # `return True`——下面的 `_settle_teardown_after_commit` 明文 re-raise
        # `OperationalError`，落帳一撞到並發例外本方法就不會 `return True`，
        # 呼叫端的 `if ...:` 不執行 ⇒ 補償清單空 ⇒ 補償空轉，而 CF 上的 app 已經
        # 真的沒了。回傳值語意不動（它仍表示「這一趟整體成不成功」）。
        if (torn_down is not None
                and info.get('anchor_model') and info.get('anchor_id')):
            torn_down.append((info['anchor_model'], info['anchor_id']))
        orphaned = (result or {}).get('orphaned_policy_ids') or []
        if orphaned:
            _logger.warning(
                "[SH] Access teardown sh=%s app=%s left %d orphan "
                "policy(ies): %s", sh.id, info['app_id'], len(orphaned),
                ', '.join(orphaned))
        if info.get('anchor_model') and info.get('anchor_id'):
            self.env[_CF_REAP_MODEL]._settle_teardown_after_commit(
                info['anchor_model'], info['anchor_id'], info['app_id'],
                info['policy_ids'] or [], orphaned, _logger,
                '[SH] Access teardown sh=%s app=%s' % (sh.id, info['app_id']))
        return True

    def _access_mark_protection_lost(self):
        """#794：把「這一列的 CF Access app 已經被拆掉、而列本身還在」寫成列上
        的事實。三腿同名，唯一呼叫端是
        `cf_access_policy_reap._compensate_fail_open_after_rollback`（獨立交易）。

        完整理由（為什麼清 id 不違反 two-stage-remote-provision-idempotency、
        為什麼只清 id 不夠、為什麼不動 `is_protected`）寫在 SA 那一份，見
        `security_access.py` 的同名方法——**不在這裡重述**：三份平行的散文一定
        會漂，而漂掉的那一份讀起來和權威版本一模一樣。
        """
        self.ensure_one()
        _internal_sm_route_identity(self).access_app_id = False
        # #798：包 recordset（同上）。
        for pol in _internal_policy_cf_write(self.access_policy_ids):
            if pol.cf_policy_id:
                pol.cf_policy_id = False
        if not self.access_protection_lost:
            self.access_protection_lost = True

    def to_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'is_primary': self.is_primary,
            'hostname': self.hostname or '',
            'is_protected': self.is_protected,
            'session_duration': self.session_duration,
            'login_google': self.login_google,
            'login_email_otp': self.login_email_otp,
            # fail-open 放寬訊號，必須吐；`access_app_id` 刻意**不吐**（伺服器
            # 擁有的 CF 物件 id，形狀對齊 CS `_access_to_dict()`）。
            'access_unresolved_idp_types': self.access_unresolved_idp_types or '',
            # #794 fail-open 訊號：宣稱受保護、但 CF 上的 app 已經被拆掉。不吐
            # 的話 UI 只讀得到 is_protected 這個**意圖**欄位。
            'access_protection_lost': self.access_protection_lost,
            'access_publish_pending': self.access_publish_pending,
            'access_configuration_ready': access_configuration_ready(self),
            'access_policies': [p.to_dict() for p in self.access_policy_ids],
        }
