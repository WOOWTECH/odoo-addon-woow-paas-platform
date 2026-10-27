from ..services.access_state import AccessProtectionError, AccessPublicationError, access_configuration_ready, access_revoke_hold_seconds, access_revoke_operator_reports, access_revoke_provider_accepted, access_revoke_state, require_access_sync_receipt, require_tunnel_routes_receipt, schedule_access_resync
from datetime import timedelta
import json
import logging
import psycopg2
import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services import route_probe
from ..services.naming import make_security_access_subdomain

from ..services.paas_operator import (
    get_paas_operator_client,
    PaaSOperatorError,
    PaaSOperatorConnectionError,
)

_logger = logging.getLogger(__name__)
_DEFER_ACCESS_RELEASE = object()

#: #528 的待回收帳本。用字串而不是 import 具體 class：三腿都要用它，而
#: `env[...]` 這條路不受 model 檔的 import 順序影響。
_CF_REAP_MODEL = 'woow_paas_platform.cf_access_policy_reap'

#: #822：「這次 provision 失敗**可能**在 Cloudflare 留下一顆沒有 DB 參照的 tunnel」
#: 的訊息標記。消費者是既有的 L4 反向對帳（`services/reconciler.py`：左集就是
#: `sa.mapped('tunnel_id')`、`kind='tunnel'` ⇒ id 沒落地的 tunnel 本來就會被列成孤兒
#: 候選）與人工；標記本身**不觸發任何自動刪除**。
#: 🔴 只在真的可能有殘留時才掛（`_RECLAIM_INDETERMINATE` / `_RECLAIM_FAILED` 兩格）。
#: 每一次 sync 失敗都掛上它，人工就再也不會去看它了——`test_..._822.py` 的 C4 是這條
#: 紀律的反向對照。
#: ⚠ 選字前 `grep -rn unreferenced src/ scripts/` 零命中：新標記不得是既有標記的
#: 子字串／超字串，否則 grep 型判準分不開兩者。
_UNREFERENCED_TUNNEL_MARK = 'unreferenced-tunnel'

#: `_reclaim_unreferenced_tunnel` 的五種結果。分這麼細的理由都是**留什麼記錄**不同：
_RECLAIM_NOT_ATTEMPTED = 'not-attempted'   # 還沒呼叫 create_tunnel ⇒ CF 上必然沒有東西
_RECLAIM_INDETERMINATE = 'indeterminate'   # create_tunnel 自己炸 ⇒ 有沒有建成不可知
_RECLAIM_REFERENCED = 'referenced'         # 窗口外：id 已落地，這顆有 DB 參照
_RECLAIM_DONE = 'reclaimed'                # 已 delete_tunnel 收回
_RECLAIM_FAILED = 'unreclaimed'            # 補償自己也失敗 ⇒ 真的留了一顆

#: AccessError 分支寫進 `error_message` 的說明**逐字展開在兩個使用點**（本檔
#: `_do_provision` 的 denial 分支），不抽成模組常數：`_()` 必須在**呼叫當下**求值，
#: 模組載入期沒有 env／lang 可解析，包在常數上會固定拿到 en_US。兩處字面逐字相同
#: ⇒ babel 合併成同一個 msgid。**不是**把 denial 降級——例外照樣 re-raise，這裡只是
#: 讓那一列自己說得出剛才發生過什麼。

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
    'operator_instance_epoch',  # server-observed Operator process identity
)

_ROUTE_IDENTITY_ANCHOR_FIELDS = (
    'access_publish_pending',  # server-owned publication state
    'access_token_revoke_pending',
    'access_token_revoke_accepted_at',  # provider acknowledged the revocation
    'dns_record_id',    # WHICH CF DNS record this route reconciles/deletes
    'access_app_id',    # WHICH CF Access application this route configures
)

#: #1071：route 的**真錨點**——這一列屬於哪個 workspace，只由這一根欄位決定。
#:
#: ⚠ 與上面那張 `_ROUTE_IDENTITY_ANCHOR_FIELDS` 的性質**完全不同**，⛔ 不可合併：
#: 那兩根是「值不可變」（伺服器擁有的 CF 物件 id，沒有合法改值路徑 ⇒ 連 superuser
#: 都擋，用 sentinel 開閘）；本欄位是「值轉移**受限**」（route 本來就搬得動——
#: `test_policy_projection_guard_849.py::test_L1_route_moved_to_another_sa_...`
#: 就是活的合法路徑），只是要 gated。混進那張清單等於封死 L1。
_ROUTE_ANCHOR_FIELD = 'security_access_id'


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


# #798：policy 列上的 `cf_policy_id` 是伺服器擁有的 CF 物件 id（Cloudflare reusable
# policy），守衛與上面那組同形——但**刻意用另一把 context key**，理由是實證的：
#
#   * 現樹有大量既有測試／fixture 已經開著 `sa_internal_identity_write`（例如
#     `tests/test_security_access_access_policy.py` 全檔 `import _internal_identity
#     as _internal`）。共用那一把等於讓本守衛在那些檔裡靜默失效，症狀是「守衛看起來
#     有測試、實際上從未被執行」——同型翻車逐字記在 `models/smart_home_route.py` 的
#     `_SM_ROUTE_IDENTITY_ANCHOR_FIELDS` 上方註解裡。
#   * sentinel **物件**沿用 `_INTERNAL_IDENTITY_WRITE`：真正把租戶擋在外面的是「JSON
#     表達不出物件同一性」，而**哪一種能力**由 key 決定。同一個分法已有先例——
#     `sa_internal_prefix_write` 與身分錨點共用同一顆 sentinel（見 route `write()`）。
#
# ⚠ key 寫成**字面字串**、判定用 `is`，兩者都是 repo-lint 第 28 條
# (`check_internal_ctx_key_registry`) 的判定點來源：它只解得出字面值與 module 層常數，
# key 一旦變成執行期值，這把鑰匙的「判定點計數」會歸零而被判違規。
def _internal_policy_cf_write(records):
    """Return ``records`` in the one context allowed to write ``cf_policy_id``."""
    return records.with_context(
        sa_internal_policy_cf_write=_INTERNAL_IDENTITY_WRITE)


def _is_internal_policy_cf_write(env):
    return (env.context.get('sa_internal_policy_cf_write')
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


# ------------------------------------------------------------------------- #
#  security_access 的建立閘（smart_home #524／cloud_service #519 的同型缺口）  #
# ------------------------------------------------------------------------- #
#
# 缺陷：`security_access_rule_owner_admin_manage` 給 owner／admin create，而
# owner 資格可自我滿足（任何 base.group_user 免費自建 workspace 即為 owner）
# ⇒ 經 `/web/dataset/call_kw` 裸 `create({'name':…, 'workspace_id':…})` 得到
# 一筆 `subscription_id` 為空的 SA。`_is_subscription_active()` 對「沒有訂閱」
# 回 True ⇒ controller 的 `_check_operation_allowed` 放行 ⇒ 再打 /woow 的
# `action='provision'` 就以平台身分開出一條真的、在計費的 Cloudflare tunnel，
# 平台側零訂閱零扣款（smart_home 的 #524 實測過完全相同的鏈；`models/smart_home.py`
# 頂端那段註解）。
#
# 判準：context 裡那個值的**物件身分**（`is`）——`call_kw` 換得掉整包 context，
# JSON 表達不出模組私有物件（#522）。
#
# ⚠ 與 #524（smart_home）刻意不同的一格：本閘**豁免** superuser／sudo 與
# `base.group_system`（同 `workspace.py::_is_privileged_writer`，#658）。
# 理由：(1) 威脅模型是租戶 RPC，租戶取得不了 su／group_system；(2) 全 repo 沒有
# 任何 sudo 路徑會拿租戶輸入建 SA（唯一的建立點是下面這支 controller，它走租戶
# 身分 ＋ 本開閘 helper）；(3) 不豁免要遷移約 90 處 TransactionCase fixture（#524
# 當年遷了 130 處），那是另一個爆炸半徑。代價是 `TransactionCase`（su env）對本閘
# 沒有鑑別力——守線測試因此一律走 HttpCase＋真租戶帳號
# （`tests/test_tenant_orm_lifecycle_guards.py`）。
# key 刻意與 smart_home／cloud_service 的不同名：各 model 的閘互不授權。
class _PlatformSaOrder:
    """平台伺服器端建立 security_access 的 marker type —— 見上方註解。"""

    __slots__ = ()

    def __repr__(self):
        return '<platform security-access order>'


_PLATFORM_SA_ORDER = _PlatformSaOrder()

#: 唯一的逃生口 context key。key 本身不是判準，判準是它的**值身分**。
_PLATFORM_SA_ORDER_CTX_KEY = 'woow_security_access_platform_order'


def _sa_platform_order(records):
    """回傳唯一被允許（以租戶身分）建立 security_access 的 context 底下的 ``records``。

    開閘掛在會讓記錄落地的那一次呼叫上；唯讀 handle 一律不帶。
    """
    return records.with_context(
        **{_PLATFORM_SA_ORDER_CTX_KEY: _PLATFORM_SA_ORDER})


def _is_sa_platform_order(env):
    """判準：**只**比對 context 裡那個值的物件身分（`is`）。"""
    return env.context.get(_PLATFORM_SA_ORDER_CTX_KEY) is _PLATFORM_SA_ORDER


class SecurityAccessOrderGateError(AccessError):
    """這一筆 security_access 不是從平台的下單流程來的。

    獨立型別讓測試斷言例外型別，而不是與 ACL／ir.rule 的 AccessError 混為一談。
    """


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


def _strip_identity_anchors(model, vals_list, anchors,
                            is_open=_is_internal_identity_write):
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

    ⚠ ``is_open``（#798）：**傳的是那把鑰匙的 reader 函式**，不是 key 字串。守衛邏輯
    因此只有一份，而每一把 key 各自保有一個**字面** ``context.get('…') is SENTINEL``
    判定點——repo-lint 第 28 條只解得出字面值／module 常數的 key，改成傳字串會讓
    **連既有那把 `sa_internal_identity_write` 一起**失去判定點而被判成假綠。
    預設值就是既有那支 reader ⇒ 既有四個呼叫端行為零改變。
    """
    if is_open(model.env):
        return
    for vals in vals_list:
        for field in anchors:
            vals[field] = _server_owned_anchor_value(model, field)


def _reject_identity_anchor_writes(env, vals, anchors,
                                   is_open=_is_internal_identity_write):
    """write()-side half of the guard: refuse to move an anchor, loudly.

    Hard invariant for EVERYONE including superuser — see
    ``_INTERNAL_IDENTITY_WRITE`` for why neither ``env.su`` nor a plain truthy
    context value may open it.

    ``is_open``：見 ``_strip_identity_anchors`` 同名參數（#798）。
    """
    if is_open(env):
        return
    for field in anchors:
        if field in vals:
            raise UserError(
                _("Field '%s' is immutable after creation.") % field
            )


def _reject_derived_projection_writes(vals, projections):
    """#849：拒絕**直寫 stored related 投影欄位**，大聲拒絕。

    ``projections``：``{投影欄位: 它的來源錨點欄位}``——來源欄位只進錯誤訊息，
    讓呼叫端知道「該寫的是哪一個」。

    ## 這道守衛在守什麼（已實測，不是推論）

    policy 的 ``security_access_id`` / ``smart_home_id`` 與 rule 的
    ``security_access_id`` / ``cloud_service_id`` / ``smart_home_id`` 都是
    ``related=... , store=True``。``store=True`` ⇒ 值**直接落在該列自己那根
    column 上**，來源列一動也不動（``inverse`` 沒裝——related 欄位的 ``readonly``
    在 ``odoo/fields.py::Field._get_attrs()`` 的 ``related`` 區塊預設為 ``True``，
    ``Field.setup_related()`` 裝 inverse 的條件因此不成立）。

    而那幾根 column **正是 ``security/ir_rules.xml`` 判歸屬用的**（policy 與 rule
    的 member-read／owner-admin-manage 兩條 rule，domain 走
    ``security_access_id`` / ``cloud_service_id`` / ``smart_home_id``，**不走**
    真錨點 ``route_id`` / ``policy_id``）⇒ 直寫它們會造成**歸屬分裂**：真錨點
    仍掛在 X，``ir.rule`` 卻改判給 Y。已量到的後果是**受害者取得跨租戶 read**。

    ⚠ ``readonly=True`` 擋不住這條路：``check_field_access_rights('write', ...)``
    只看欄位的 ``groups=``（``is_accessible``），**不看 ``readonly``**
    ⇒ 走 ``/web/dataset/call_kw`` 的裸 ORM 寫入照樣落地（#849 已端到端實測）。

    ## 三道既有守線對這個 vals 全部不 fire

    ``_reject_identity_anchor_writes``（#798）只看 ``cf_policy_id``；
    ``_assert_anchor_move_authorised``（#848）只跑 ``_ANCHOR_FIELDS``（三個真錨點）；
    ``write()`` 裡 ``if resync:`` 那個 ``check_access('read')`` 排序迴圈（#786）的
    觸發集合不含投影欄位 ⇒ 連跑都不跑。這兩欄是 policy 上唯一的無守線出口。

    ## 為什麼**沒有** ``is_open`` 逃生門（與 #798 的形狀刻意不同）

    投影欄位**沒有任何合法的直寫路徑**：它的值由框架從來源欄位算出來，而框架的
    重算走的是低階 ``_write()``（``Field.compute_value`` → cache → ``flush``），
    **不經過本方法所在的公開 ``write()``**——本 repo 有測試把這件事量成契約
    （``test_policy_projection_guard_849.py`` 的 L 系列：route 搬 SA、policy 換
    錨點、rule 搬 policy、create 三條腿，重算全部不被誤擋）。

    另外掃過全 repo 的合法呼叫端（``src/`` / ``scripts/`` / ``src/migrations/``、
    dict literal、raw SQL、``.sudo().write(``、``_write(``）：對 policy／rule
    這五個投影欄位的寫入**零命中**（唯一形似的命中是
    ``route.security_access_id`` 與 ``smart_home_route.smart_home_id``，那是**來源
    列自己的真欄位**，不經過本守衛）。⇒ 開一道沒有呼叫端的逃生門只會多一個
    可被 context 打開的面。

    對**所有人**生效含 superuser：``env.su`` 豁免會在每個 ``TransactionCase`` 與
    每條 ``sudo()`` 路徑上靜默停用守衛（見 ``_INTERNAL_IDENTITY_WRITE`` 註解與
    ``docs/decisions/patterns/su-env-exempt-guard-false-green.md``）。這一格與
    ``cf_policy_id`` 同側：**沒有**合法改值路徑 ⇒ 任何 su 豁免都是洞。
    """
    for field, source in projections.items():
        if field in vals:
            raise UserError(
                _("Field '%(field)s' is derived from '%(source)s' and cannot "
                  "be written directly; change '%(source)s' instead.")
                % {'field': field, 'source': source}
            )


def _anchor_target_id(value):
    """把一個 Many2one 的 write 值正規化成 id（或 ``False``）。

    只涵蓋**純量** Many2one 的三種合法寫法（id / ``False`` / recordset），刻意
    **不**重演 x2many 的指令語意——本 repo 對「逐一重演框架的解析規則」有明確教訓
    （#657 同一行修了兩次，`cloud_service_binding_guard.py` 檔頭逐字記著）。
    今天服務的四個錨點（policy 的三個 `_ANCHOR_FIELDS`、rule 的 `policy_id`、
    route 的 `_ROUTE_ANCHOR_FIELD`）全是純量 M2O；哪天出現 x2many 錨點，這裡要換成
    `_cs_binding_diff_fields` 那種「比對實際綁定、不解析指令」的形狀，而不是在這裡
    多加幾個 elif。

    ⚠ 最後一行 `int(value)` 看起來會被 `NewId` 炸——**不會**：
    `odoo/api.py::NewId.__bool__` 回 `False`，第一道 `if not value` 就短路；
    new record 的 recordset 走 `value.id`（也是 `NewId`），同樣被呼叫端的
    `if not new_id` 接住。

    ## #1071：這支從 policy module **下沉**到本檔，理由是 import 方向

    原本住在 `security_access_access_policy.py`（#848），rule module 從那裡 import
    （#941 檔頭逐字寫著「⛔ 不複製：那三行 normalize 邏輯漂掉是無聲的」）。route 的
    守衛住在本檔，而本檔**不能** import policy module——policy module 自己
    `from .security_access import ...`（今天已載著 7 個名字），反向再拉一條就是
    A→B→A 的環。⇒ helper 下沉到 DAG 底層，policy module 改成從本檔 import 並保留
    給 rule module 用（`from X import Y` 只看 X 的 namespace 有沒有 Y，
    `models/__init__.py` 的載入順序 security_access → policy → rule 保證 rule
    module 執行 import 時 policy module 早已跑完）。⛔ 不在本檔複製第二份。
    """
    if not value:
        return False
    if isinstance(value, models.BaseModel):
        return value.id
    return int(value)


def _assert_route_anchor_move_authorised(records, vals):
    """契約：**把一條 route 搬到新的 Security Access 底下，要求對那個新 SA 有 owner／admin。**

    #1071，對稱 policy 側的 `_assert_anchor_move_authorised`（#848）與 rule 側的
    `_assert_rule_reparent_authorised`（#941）。四個判準逐一對齊：守單一真錨點、對
    **新**目標做 `check_access('write')`、比對的是「值真的改變」不是「key 在 vals
    裡」、su 自動豁免。

    ## 缺口是 write 側專屬的（與 #848／#941 同一個框架不對稱）

    `BaseModel.write` **只有開頭**那一次 `self.check_access('write')`，評估的是
    **寫入前**的狀態；`super().write()` 落地後零複驗。對照 `BaseModel._create`
    的**尾端**對新建列再跑一次 `records.check_access('create')`（判別事實是「哪個
    函式裡有、哪個沒有」，不是行號）。

    套到攻擊形狀 `R.write({'security_access_id': SA_victim})`：寫入前 R 還掛在攻擊者
    自己的 SA 上 ⇒ route 的 ir.rule walk（`security_access_id.workspace_id.
    access_ids`）判給攻擊者的 workspace ⇒ pre-write 檢查 PASS；落地後 walk 改指
    受害者，但 ir.rule 不再評估 ⇒ 放行。

    ## 🔴 在本單之前，這條路上**一道守線都沒有**（run 2674 實測）

    看起來擋著它的那道其實是**配額守衛的副作用**：`_check_route_quota` 為了數
    `len(sa.route_ids)` 去讀 o2m，順手觸發對父記錄的 `check_access('read')`
    ⇒ 讀不到受害者 SA 的人在那裡撞 `AccessError`。但守衛開頭就有
    `if not sub or not sub.sh_subscription_plan_id: continue` ——**受害者 SA 沒有
    訂閱時整條提前 continue，連 `route_ids` 都碰不到** ⇒ 那道副作用不發生，
    而 write 路徑上沒有任何其他授權層。

    量測（不是推論）：CI run **2674**（job 8018，branch `test/1054-reach`@`0506f937`，
    2026-09-06 03:52Z）跑 3934 條、**唯一**一條紅是
    `test_quota_len_o2m_reach_1054.py::TestRouteQuotaEndToEndReach1054::
    test_B2c_move_into_an_unsubscribed_victim_sa_is_blocked`，形狀
    `AssertionError: Exception not raised`（該格已隨本單移進
    `test_route_anchor_move_guard_1071.py` 當判別式）。同 class 的
    `test_B2_...subscribed...` 綠 ⇒ 兩格合起來把「今天擋住有訂閱那格的是配額守衛的
    read 副作用、不是授權層」釘死。

    可利用性因此落在**兩個 workspace 的共同成員**而不是陌生人：route 的
    member-read rule（`security/ir_rules.xml` 的
    `security_access_route_rule_workspace_member`）domain 沒有 role 條件、
    `perm_write=False`，任何成員都讀得到該 workspace 的 SA id ⇒ 「A 的 owner／admin
    ＋ B 的普通成員」把 A 的 route 寫進 B 的 SA。搬完之後那列進了 B 的 domain、
    `perm_write` 要 owner／admin ⇒ **攻擊者自己也收不回來，是單向注入**。

    ## 為什麼是 `check_access('write')` 而不是自己寫一套 role 比較

    SA model 對 `base.group_user` 的 ACL `perm_write` 是 1，而該 group 上唯一
    `perm_write=True` 的 ir.rule 是 `security_access_rule_owner_admin_manage`
    （member 那條 `perm_write=False`）⇒ `check_access('write')` 就**等於**「對該
    SA 的 workspace 是 owner／admin」，而且沒有第二張會漂的表
    （`cloud_service_binding_guard.py` 檔頭：「不自己寫第二套 role 比較」）。
    `base.group_system` 由 `security_access_rule_admin_full` 全開 ⇒ 平台 admin 照常。

    例外**刻意不包裝**：`check_access` 自己拋的 `AccessError` 已經指名是哪個 model
    的哪個操作被拒，再包一層等於多一個會與 ir.rule 漂開的訊息來源。⇒ 守線測試斷言
    的是 exception **class**（本守衛 `AccessError`／identity-anchor 守衛
    `UserError`／`write()` 之後的 validator `ValidationError`），⛔ 不斷言訊息字串。

    ⚠ 那三類互斥只涵蓋「目標 SA **存在**」的情形。`new_id` 指向一列**不存在**的
    記錄時（已被刪除、或呼叫端猜的整數），這裡不是拋 `AccessError` 而是
    `MissingError`——`check_access` 內部的 `filtered_domain` 會去讀那一列的
    `workspace_id`，`Field.__get__` 對抓不到的 real record 直接拋。⛔ 不要「順手加
    `.exists()`」：空 recordset 的 `check_access` 因 `any(self._ids)` 為假而**整段
    跳過**，那會把「目標不存在」變成**放行**（比現狀糟），要收就得顯式判存在性再
    fail-closed。本函式**刻意維持現狀**：搬移仍然不落地（例外發生在 `super().write()`
    之前），而 #848／#941 兩道同型守衛是同一個形狀——單獨收斂這一道會製造三道之間
    的漂移。⇒ 三道一起收斂，另開單（本單只在此記明，⛔ 不在這裡順手動）。

    ## **舊**錨點刻意不再驗一次（與 #848／#941 同一個依賴，不是遺漏）

    框架開頭那次 `self.check_access('write')` 走的就是 route 自己的 ir.rule，而它的
    domain 正是「舊 SA 的 workspace 是不是我 owner／admin」⇒ 舊錨點方向**已經**被
    要求過，顯式再驗一次是冗餘，而且會多出一把與框架各自演化的尺。這件事不是靠
    推論放著：pull direction（把**別人的** route 拉進自己的 SA）由守線測試的 C6
    釘住，形狀照抄 #941 的
    `test_C6_pull_direction_is_blocked_by_the_framework_pre_check`。

    ## `security_access_id` **不在** resync 觸發集合裡——但受害者仍推得動

    與 #941 的 `policy_id` 不同：本檔 `SecurityAccessRoute.write()` 的兩個 side-effect
    觸發集合是 `('service_url', 'http_host_header')` 與 `('is_protected',
    'login_google', 'login_email_otp', 'session_duration')`，錨點**兩個都不在**
    ⇒ 單獨改錨點不觸發任何 operator 呼叫。

    ⚠ 但混合 payload 會：`for sa in self.mapped('security_access_id')` 在
    `super().write()` **之後**求值 ⇒ 取到的是**新**（受害者）SA，而
    `_build_route_payload()` 迭代的是那顆 SA **全部**的 `route_ids`（含剛搬進去的
    那條）⇒ 一次放行的搬移＋一個 `service_url` 就把注入的 route 推進受害者的
    Cloudflare tunnel ingress。守線測試 A3 因此把「被拒之後受害者的 SA 一次都沒被
    `_sync_routes_to_operator()` 呼叫」斷言成契約（配陽性對照）。

    ## su 豁免是刻意的，這裡與 `_ROUTE_IDENTITY_ANCHOR_FIELDS`（#798）相反

    `check_access()` 在 `env.su` 下自動 no-op。這與同檔 `dns_record_id` /
    `access_app_id` 用 sentinel、對 superuser 也擋**不是**疏漏而是不同的不變量：
    那兩根是「值不可變」（**沒有**合法改值路徑，任何 su 豁免都是洞，見
    `docs/decisions/patterns/su-env-exempt-guard-false-green.md`），本函式是「值
    轉移**受限**」（**有**合法改值路徑，只是要 gated）——
    `test_policy_projection_guard_849.py::test_L1_route_moved_to_another_sa_
    recomputes_projection` 就以 `sudo()` 搬 route 並斷言底下 policy／rule 的投影欄
    重算，那是活的合法路徑，一律封死會打紅它、也會拿掉維運能力。

    代價是本守衛的 deny 分支在 su env 下結構上不可達 ⇒ 守線測試的攻擊格一律走真
    RPC 的**非 su** 租戶，並另有一格（C2）把 su 豁免本身斷言成契約。

    ## 清空（寫 ``False``）不在守備範圍——而且它被 NOT NULL 擋著

    沒有「新 SA」要授權 ⇒ 早退。這一格不是破口：`security_access_id` 宣告
    `required=True`，清空會撞 Postgres 的 NOT NULL 約束。守線測試 E1 把它斷言成
    **性質**而不是 driver 的 class 名（照 #973 F1 的判準：不是我們自己那三層擋的
    ／是 `psycopg2.IntegrityError` ／ `pgcode` 等於 `NOT_NULL_VIOLATION` ／訊息指名
    該 column）。

    ## create 側不加守衛：靠框架 post-check

    `_create` 尾端那次 `check_access('create')` 會擋下「`security_access_id` 指向
    他人 SA」的新列——route model 上對 `base.group_user` 唯一 `perm_create=True`
    的 ir.rule 同樣是 `..._owner_admin_manage`。守線測試 B1／B1b 以真 RPC 與
    `with_user()` 兩種形狀各量一次（共同成員與陌生人兩種身分）。

    ## ⛔ 本函式**擋不到**的兩件事（都在本單範圍外，另開單，⛔ 不要在這裡順手動）

    1. **舊 SA 在搬移後不會被重推、state 也不重算。** `write()` 沒有 policy 側
       #786 那套 `pre_anchor_state` ／ lost-anchor 重推，也沒有 create／unlink 腿
       尾端那次 `_recompute_route_state()` ⇒ 搬走最後一條 route 的舊 SA 會停在
       `'active'` 而不是落回 `'active_no_route'`，而它的 tunnel ingress 仍留著已
       搬走的 hostname。本守衛只管**授權**，不改這件事——su／owner 的合法搬移
       照樣會踩到。
    2. **policy／rule 上同名的 stored related `security_access_id`。** 那是投影
       欄、不是真錨點，寫它由 `_reject_derived_projection_writes`（#849）擋；它
       `related=` 未顯式 `readonly=False` ⇒ 框架不裝 `inverse` ⇒ 值不會回寫到
       route，不經過本函式。⛔ 兩張清單不可互抄（同名不同性質，與 #941 檔頭那條
       警告同型）。
    """
    if _ROUTE_ANCHOR_FIELD not in vals:
        return
    new_id = _anchor_target_id(vals[_ROUTE_ANCHOR_FIELD])
    if not new_id:
        return
    # 「值真的改變」才驗：UI 整批送欄位時 vals 會帶著與現值相同的 SA id，那不是
    # 搬移；而能寫這一列的人，框架開頭那次 `check_access('write')` 已經對它現在掛著
    # 的 SA 要求過 owner／admin（route 與 SA 走同一條 workspace walk）。判準取自
    # `docs/decisions/patterns/scope-flip-write-guard-not-ir-rule.md`。
    #
    # 這條早退由守線測試 E3 守著。⚠ 它的判準**不是**「拿掉會被拒」——拿掉之後功能
    # 不會壞（能寫這一列的人對它現在掛著的 SA 必定有 owner／admin）⇒ 那樣的測試恆
    # 綠。判準是「那次 `check_access` 有沒有真的發生」：測試在 SA model 上掛 spy，
    # 值沒改變時必須錄到零次 write 檢查，配一格「值真的改變時錄得到」的陽性對照。
    if all(rec[_ROUTE_ANCHOR_FIELD].id == new_id for rec in records):
        return
    # comodel 名字從**欄位宣告**取，不寫死字串：欄位改指向別的 model 時這裡自動跟上
    # （與 #848／#941 同一個寫法）。
    records.env[
        records._fields[_ROUTE_ANCHOR_FIELD].comodel_name
    ].browse(new_id).check_access('write')


class SecurityAccess(models.Model):
    _name = 'woow_paas_platform.security_access'
    _inherit = [
        'woow_paas_platform.subscription_lifecycle_mixin',
        # #1766：一台 HA 一條 tunnel（claim／release／第二台偵測）。
        'woow_paas_platform.tunnel.binding.mixin',
    ]
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
    provision_outcome_unknown = fields.Boolean(
        string='Provision Outcome Unknown',
        default=False,
        copy=False,
        groups='base.group_system',
        help=(
            'The operator may have created a tunnel but its identity was not '
            'returned. Reconcile the exact remote object before retrying or deleting.'
        ),
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
            # #833：Cloudflare 上這條 tunnel 已被刪除（軟刪除）。
            # ⚠ 與 'disconnected' 的差別是**要不要動手**：deleted 需要重建，
            # disconnected 只要等連線恢復。少了這一格，兩者在 UI 與 DB 欄位上
            # 是同一個值，沒有人會去查。
            ('deleted', 'Deleted'),
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
    operator_instance_epoch = fields.Char(
        string='Observed Operator Instance Epoch',
        copy=False,
        groups='base.group_system',
        help='Last process epoch observed per Operator replica, as a compact '
             'JSON map {instance_id: epoch}. Replicas answer alternately behind '
             'one Service, so only the same instance reporting a new epoch is a '
             'restart. A bare legacy value is treated as empty.',
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
                _("PaaS Operator is not configured. "
                  "Please set Operator URL and API Key in Settings → Woow PaaS.")
            )
        return client

    def _try_lock_lifecycle_rows(self):
        """Acquire a NOWAIT row lock before any external lifecycle side effect.

        ORM writes can remain buffered while an external request is in flight,
        so state='provisioning' by itself does not serialize another transaction.
        """
        ids = tuple(sorted(set(self.ids)))
        if not ids:
            return True
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    'SELECT id FROM %s WHERE id IN %%s '
                    'ORDER BY id FOR UPDATE NOWAIT' % self._table,
                    [ids],
                )
                locked_ids = {row[0] for row in self.env.cr.fetchall()}
        except (psycopg2.errors.LockNotAvailable,
                psycopg2.errors.SerializationFailure):
            return False
        if not locked_ids:
            return True
        if locked_ids != set(ids):
            raise UserError(_(
                'One or more Security Access records no longer exist.'
            ))
        # Flush our own pending updates only after NOWAIT acquired the row, then
        # discard any pre-lock field cache before checking lifecycle state.
        lifecycle_fields = [
            'state', 'error_message', 'tunnel_id', 'tunnel_name',
        ]
        self.flush_recordset(lifecycle_fields)
        self.invalidate_recordset(lifecycle_fields)
        self.sudo().invalidate_recordset(['provision_outcome_unknown'])
        return True

    def _reject_unresolved_tunnel_outcome(self):
        """Keep the SA row as a reconciliation anchor while tunnel identity is unknown."""
        if self.sudo().filtered('provision_outcome_unknown'):
            raise UserError(_(
                'The tunnel creation outcome is unknown. Reconcile the exact '
                'remote tunnel before retrying provision or deleting this record.'
            ))

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

        未撞名時 `slug` 仍由租戶提供的 `name` 逐字決定（#1516 起撞名會加隨機
        後綴），而舊種子是它的 `md5[:8]`（32-bit 截斷）⇒ 租戶搜得出一個 name，
        讓自己的 SA 子網域落在受害者的 ws_hash 段上。
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
        # 建立閘排在最前面：被拒絕的呼叫不該碰到任何後續成本（見模組層
        # `_PLATFORM_SA_ORDER` 的註解）。
        if not (_is_sa_platform_order(self.env)
                or self._is_platform_lifecycle_writer()):
            raise SecurityAccessOrderGateError(_(
                'Security Access instances can only be created by the platform '
                'ordering flow (%(model)s). Please order from the Security '
                'Access page instead of writing to this model directly.',
                model=self._name))
        #
        # #753：傳的是 `self` 而不是 `self.env`——守衛要讀 `_fields` 才知道哪個
        # 錨點有伺服器端 default（`reference_id` 的 uuid4）該壓進去，而不是一律
        # 壓 False 把 required 炸掉。
        _strip_identity_anchors(self, vals_list, _IDENTITY_ANCHOR_FIELDS)
        records = super().create(vals_list)
        # 落地值的 `subscription_id`（`ir.default`／context default 注入）也要過
        # 綁定判準——見 mixin 的 `_guard_tenant_lifecycle_create`。
        records._guard_tenant_lifecycle_create()
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
        if 'provision_outcome_unknown' in vals and not self.env.su:
            raise AccessError(_(
                'The unresolved tunnel outcome is managed by the platform.'
            ))
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
        # 租戶直打 ORM 的生命週期守衛：停權逃逸（suspended／deleting → 對外服務
        # 的 state）與 `subscription_id` 改綁／清空。理由與豁免見
        # `subscription_lifecycle_mixin` 模組頂端。
        self._guard_tenant_lifecycle_write(vals)
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

    def _reclaim_unreferenced_tunnel(self, client, created, identity_stored,
                                     create_attempted):
        """#822：把「已在 CF 建好、id 卻還沒落進 DB」的那顆 tunnel 收回來。

        **孤兒窗口** ＝ ``create_tunnel`` 回傳之後、``_internal_identity(...).write()``
        落地之前。窗口內任何失敗都會讓冪等守衛 ``if self.tunnel_id:`` 不成立 ⇒ DB 從此
        沒有任何東西指向那顆 tunnel（活著、計費、帶著可用的 token）。

        🔴 **本方法只做 HTTP，一行 DB 都不碰**，而且呼叫端必須在**任何** DB write
        之前呼叫它。窗口內的失敗有一種是 flush 階段的 ``IntegrityError``——那會把
        cursor 打進 aborted 狀態，之後任何 DB 操作都會再炸一次；HTTP 不經 cursor，
        不受影響。先把 CF 側的孤兒斷掉，狀態欄位寫不寫得進去是次要傷害。

        ``identity_stored`` / ``create_attempted`` 是呼叫端的 local flag，刻意**不**
        回頭讀 ``self.tunnel_id`` 來判斷：cursor 已 aborted 時那個讀不可信，而 flag
        是確定的。

        五種結果的差別全在「該留什麼記錄」，見各分支註解與 ``_UNREFERENCED_TUNNEL_MARK``。
        """
        self.ensure_one()
        if not create_attempted:
            # 連 create_tunnel 都還沒送出去（例如 _get_default_service_url 炸掉）
            # ⇒ CF 上必然沒有東西。這一格**不得**掛孤兒標記，否則標記會退化成雜訊。
            return _RECLAIM_NOT_ATTEMPTED
        created_id = (created or {}).get('tunnel_id')
        if not created_id:
            # create_tunnel 自己炸掉（或回了空 id）⇒ CF 上到底有沒有東西**不可知**：
            # HTTP 可能是在 CF 已經建好之後才斷的。這一格補償不了——沒有 id 可刪，而
            # 「找一顆同名的刪掉」是比原缺陷更危險的操作（同名可能是別人的；
            # delete_tunnel 的 #531 歸屬主張要的正是 id 與 name 兩者相符）。
            # 唯一能做的是留下可對帳的記錄，交 L4 反向對帳／#385。
            return _RECLAIM_INDETERMINATE
        if identity_stored:
            # 窗口**外**：id 已落地，這顆 tunnel 有 DB 參照，retry-from-error 會走
            # re-sync 分支把它收斂完。在這裡刪掉它才是缺陷（stage-2 的原始設計就是
            # 靠那顆留著的 tunnel_id 收斂）。
            return _RECLAIM_REFERENCED
        try:
            # #531 歸屬主張：送 CF 自己回的那個 name（create_tunnel 的回傳值），不是
            # canonical_name——權威的那一份是 CF 的（同 _sync_routes_to_operator）。
            client.delete_tunnel(
                created_id, tunnel_name=created.get('tunnel_name'))
        except Exception:
            # 補償自己也會失敗。這裡**不再往上拋**：呼叫端正在處理的是原本那個失敗，
            # 用補償的失敗蓋掉它只會讓根因消失。改成大聲記錄 ＋ 回 _RECLAIM_FAILED，
            # 由呼叫端把那顆收不回來的 id 寫進 error_message。
            _logger.exception(
                "[SA] #822 provision 補償失敗 sa=%s tunnel=%s name=%s"
                " —— CF 上留下一顆沒有 DB 參照的 tunnel，交 L4 反向對帳"
                " (kind='tunnel') / #385",
                self.id, created_id, created.get('tunnel_name'),
            )
            return _RECLAIM_FAILED
        _logger.info(
            "[SA] #822 provision 失敗，已回收剛建的 tunnel sa=%s tunnel=%s",
            self.id, created_id,
        )
        return _RECLAIM_DONE

    def _provision_failure_message(self, base, outcome, created):
        """把補償結果附註到失敗訊息上——**只有兩種結果需要留記錄**。

        ``_RECLAIM_DONE`` / ``_RECLAIM_REFERENCED`` / ``_RECLAIM_NOT_ATTEMPTED``
        三格 CF 上都沒有無主的東西，掛標記只會稀釋它的價值。
        """
        self.ensure_one()
        if outcome == _RECLAIM_INDETERMINATE:
            # 沒有 id，只有名字——而名字就是對帳的鍵（create_tunnel(name=canonical_name)）。
            return '%s [%s: name=%s]' % (
                base, _UNREFERENCED_TUNNEL_MARK, self.canonical_name)
        if outcome == _RECLAIM_FAILED:
            return '%s [%s: %s name=%s]' % (
                base, _UNREFERENCED_TUNNEL_MARK,
                (created or {}).get('tunnel_id'), self.canonical_name)
        return base

    def action_provision(self):
        """Provision: create dedicated tunnel + primary route.

        Idempotency guard: this is a two-stage provision (create_tunnel, then
        a re-sync that carries http_host_header). If stage 2 fails the record
        lands in 'error' but the tunnel already exists in Cloudflare and
        tunnel_id is stored. A retry from the 'error' state must NOT call
        create_tunnel again — that would leave the first tunnel orphaned in CF
        (live token, billed, no DB reference). When tunnel_id is already set we
        only re-run the declarative route sync and restore the active state.

        tunnel_token ACL note: the first-provision write below sets
        tunnel_token, which is a ``groups='base.group_system'`` field. This
        METHOD does not sudo itself — it runs in whatever context its caller
        hands it, so the CALLER decides whether that field-group write
        succeeds. Odoo's ``write()`` calls
        ``check_field_access_rights('write', vals.keys())``, whose only
        early-return is ``if self.env.su:``; a plain base.group_user principal
        therefore raises AccessError on that write.

        #805 (b): the tenant endpoint drives this method under the PLATFORM
        identity (``sa.sudo().action_provision()`` — the
        ``action == "provision"`` branch in controllers/security_access.py),
        because the tenant could otherwise never succeed. The values written
        there are not tenant-supplied: the whole dict comes from the operator's
        create_tunnel response plus a server-side timestamp. Same ruling and
        same shape as #739 / PR #797 on smart_home
        (controllers/smart_home.py::_provision_smarthome), so the two sibling
        models are once again aligned ON THAT ONE POINT — still verify each
        side at its own call site rather than carrying a conclusion across.

        ⚠ Scope of that sudo, stated precisely (it is narrower than "the token
        is now writable by tenants"). Each bullet names the test that locks it,
        in tests/test_sa_provision_tenant_rpc_805.py:
        - It is on ONE call site. ``action_provision`` is a public method (no
          leading underscore, no ``@api.private``), so Odoo's own
          ``/web/dataset/call_kw`` reaches it too; that path does not go
          through the controller, so the field mask still fails closed there
          (C1).
        - Read side: do NOT write "the read surface is untouched" — this repo
          has already falsified that phrasing by experiment. The precise
          version, with the odoo-shell measurement, lives in
          controllers/smart_home.py::_provision_smarthome (#739 / PR #797);
          deliberately not restated here so the two cannot drift. One-line
          version: the su call primes the transaction-wide field cache, and a
          later ATTRIBUTE read on a non-sudo recordset hits that cache without
          re-checking the mask. What holds is "nothing in the same request
          reads that attribute" — ``to_dict()`` omits the token (B6) and a
          ``read()`` over RPC always re-checks the mask (B5). The dedicated
          sudo'd endpoint (controllers/ha_api.py::sa_get_tunnel_token) is
          still the only read path.
        - The gates that decide WHO may drive provision all sit OUTSIDE the
          sudo — workspace membership (B2), the owner/admin role check (B1),
          and ``_check_operation_allowed`` (B4). ``sudo()`` does not change
          ``env.user``, so the ``has_group('base.group_system')`` early-return
          inside ``_check_operation_allowed`` does not become True.
        - The identity-anchor guard (``_reject_identity_anchor_writes``, called
          from this model's ``write()``) has no su exemption, so sudo does not
          open it (B7).
        - The re-sync branch below (``if self.tunnel_id:``) never writes
          tunnel_token at all, so the field mask only ever gated the FIRST
          provision (D1).

        #822 孤兒窗口與補償（first-provision 分支）。``create_tunnel`` 回傳之後、
        ``_internal_identity(...).write()`` 落地之前是一段**窗口**：CF 上的 tunnel
        已經存在（活著、計費、帶著可用 token），DB 還沒有任何東西指向它。窗口內
        任何失敗都會讓上面那道冪等守衛不成立 ⇒ 重試會再開一顆，前一顆沒有任何
        記錄指向它。四支 ``except`` 因此一律先呼叫
        ``_reclaim_unreferenced_tunnel``，而且**排在所有 DB write 之前**（它只做
        HTTP；窗口內的失敗有一種會把 cursor 打進 aborted 狀態，那之後 DB 寫不進去
        但 HTTP 照樣送得出去）。

        三格不是同一回事，記錄也不同：
        - 窗口**內**且拿得到 id ⇒ ``delete_tunnel`` 收回；收得回來就把狀態寫回
          ``'pending'``（CF 上已無此物，這才是真實狀態，Provision 鈕也才會重新
          露出）。收不回來 ⇒ 維持 ``'provisioning'`` 並把那顆 id 寫進
          ``error_message``——**不**寫回 ``'pending'``，那等於邀請下一次重試再開
          一顆而前一顆還在。
        - ``create_tunnel`` **自己**炸掉 ⇒ CF 上有沒有東西**不可知**（可能是建好
          之後回應才斷）。這一格補償不了，也不得依名字盲刪；改為在
          ``error_message`` 留 ``_UNREFERENCED_TUNNEL_MARK`` ＋ ``canonical_name``，
          交既有的 L4 反向對帳（``services/reconciler.py``，``kind='tunnel'``，
          左集就是 ``sa.mapped('tunnel_id')``）與 #385。
        - 窗口**外**（``identity_stored``）⇒ 一行都不刪：那顆 tunnel 有 DB 參照，
          retry-from-error 走 re-sync 分支收斂，原本的兩階段設計就是這樣運作的。

        ⚠ AccessError 的語意一字未動：補償之後照樣 ``raise``，也沒有改寫成
        ``state='error'``。補償記錄的寫入額外包了一層 try——記帳失敗不得蓋掉
        denial（那個分支的契約是「拒絕必須傳到呼叫端」）。
        """
        self.ensure_one()
        # This public method is also reachable through raw ORM/RPC. Refuse an
        # unauthorized caller before reading state or constructing the operator
        # client; authorized callers still pass the upstream lifecycle gates.
        self.check_access('write')
        if not self._try_lock_lifecycle_rows():
            raise UserError(_(
                'Another Security Access lifecycle operation is in progress. '
                'Please retry when it finishes.'
            ))
        self._reject_unresolved_tunnel_outcome()
        # 停權／刪除中一律拒絕，在任何 write 與 operator 呼叫之前（形狀同
        # smart_home.action_provision 的 stored-id guard）。re-sync 分支會把每條
        # route 的真 service_url 推回去 ⇒ 對 `suspended` 放行＝繞過停權的 503
        # 凍結；而本方法是 public（`/web/dataset/call_kw` 到得了，不經 controller
        # 的 `_check_operation_allowed`）。復權的正路是 `_do_resume`。
        if self.state in ('suspended', 'deleting'):
            raise UserError(_(
                "This Security Access is %(state)s and cannot be provisioned. "
                "Renew the subscription to restore service.",
                state=self.state))
        client = self._get_operator_client()

        # Already has a live tunnel (e.g. retry after a stage-2 sync failure):
        # never recreate it, just finish the route sync.
        if self.tunnel_id:
            # 訂閱不在 in_progress 時拒絕重推（同 smart_home
            # `_converge_existing_tunnel`）：`_do_suspend` 推 503 失敗會落
            # `error` 而不是 `suspended`，「error ＋ 訂閱已停」是真的會出現的組合，
            # 放行就是租戶按一下重試（或直接 call_kw）繞過停權。無訂閱＝legacy／
            # admin 建的記錄，照舊允許（清空綁定已由 mixin 守衛擋住）。
            if not self._is_subscription_active():
                raise UserError(_(
                    "This Security Access's subscription is not active, so its "
                    "routes cannot be re-synced. Restore the subscription first."))
            self.write({'state': 'provisioning', 'error_message': False})
            try:
                # 軸2: bare tunnel — no primary route to self-heal. Just re-run
                # the declarative route sync (zero routes -> empty sync, tunnel
                # keeps catch-all 404) + per-route Access for any user routes.
                self._sync_routes_to_operator(park_protected=True)
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
                    'error_message': _(
                        "Cannot connect to PaaS Operator: %(detail)s",
                        detail=e.message),
                })
                _logger.error(
                    "[SA] provision re-sync connection error sa=%s: %s",
                    self.id, e.message,
                )
            except PaaSOperatorError as e:
                self.write({
                    'state': 'error',
                    'error_message': _(
                        "Route sync failed: %(detail)s", detail=e.message),
                })
                _logger.error(
                    "[SA] provision re-sync error sa=%s: %s",
                    self.id, e.message,
                )
            except AccessError:
                # ⚠ This branch writes no tunnel_token at all (the token write
                # lives in the first-provision branch below), so an AccessError
                # here is a record-level denial, not the field mask. Whatever
                # its source, an access denial must propagate to the caller and
                # NOT be downgraded to a silent state='error' — that would mask
                # a security denial as a benign provisioning failure
                # (two-stage-remote-provision-idempotency §守則 2). Re-raise;
                # symmetric with the first-provision branch below.
                raise
            except Exception as e:
                # Any non-operator failure (route rebuild ValidationError,
                # DB error, …) must also fail loud into 'error' — tunnel_id
                # is retained so the next retry can finish (symmetric with the
                # broad except in the first-provision branch below).
                self.write({
                    'state': 'error',
                    'error_message': _(
                        "Provision re-sync failed: %(error)s", error=e),
                })
                _logger.exception(
                    "[SA] provision re-sync unexpected error sa=%s", self.id,
                )
            return

        self.write({'state': 'provisioning', 'error_message': False})

        # #822 孤兒窗口的三個 local flag。刻意用 local 而不是回頭讀 self.tunnel_id：
        # 窗口內的失敗有一種是 flush 階段的 IntegrityError，那之後 cursor 已 aborted、
        # 任何 DB 讀都不可信；flag 是確定的。三個旗標把「CF 上可能有什麼」切成互斥的
        # 五格，判讀在 _reclaim_unreferenced_tunnel。
        created = None
        create_attempted = False
        identity_stored = False

        try:
            default_url = self._get_default_service_url()

            # 軸2: build a BARE tunnel — no hostname, no auto-created primary
            # route, no DNS. canonical_name is only the tunnel identity (its CF
            # name); application routes (and their DNS) are added on demand by
            # the user afterwards. create_tunnel with no hostname configures
            # the tunnel with a catch-all 404 ingress only.
            # ⚠ 這一行**之前**設 create_attempted：create_tunnel 自己炸掉時 CF 上有沒有
            # 東西不可知，那一格與「根本還沒送出請求」必須分得開（前者要留對帳記錄，
            # 後者掛標記就是雜訊）。
            create_attempted = True
            result = client.create_tunnel(
                name=self.canonical_name,
                service_url=default_url,
            )
            # 🔴 從這一行到 identity_stored = True 之間就是 #822 的孤兒窗口：CF 上的
            # tunnel 已經存在（活著、計費、帶著可用 token），DB 還沒有任何東西指向它。
            created = result

            # The one legitimate anchor write: storing the ids Cloudflare just
            # handed back. Opted in via the sentinel — see
            # _INTERNAL_IDENTITY_WRITE. Note the sentinel does NOT widen the
            # tunnel_token field-group ACL (it is a with_context, not a sudo):
            # super().write() still enforces groups='base.group_system'. So
            # whether this line succeeds is decided entirely by the CALLER's
            # identity — the tenant BFF endpoint drives this method under the
            # platform identity since #805 (b), while a caller that reaches the
            # method under a non-system identity (e.g. /web/dataset/call_kw)
            # still fails closed here, via the AccessError branch below.
            _internal_identity(self).write({
                'tunnel_id': result.get('tunnel_id'),
                'tunnel_token': result.get('tunnel_token'),
                'tunnel_name': result.get('tunnel_name'),
                'deployed_at': fields.Datetime.now(),
            })
            # 🔴 這一行必須緊貼上面那次 write：窗口的右界由它定義。中間插任何東西都會
            # 讓「已落地」與「還沒落地」的判讀對不上實情（多插一行＝那一行的失敗會被
            # 當成窗口外、不補償）。
            identity_stored = True

            # Push whatever routes already exist — a user may have added routes
            # on the still-pending SA via the configure-then-provision flow.
            # Zero routes -> empty declarative sync -> tunnel keeps the
            # catch-all 404 only (the operator accepts an empty route list).
            self._sync_routes_to_operator(park_protected=True)

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
            outcome = self._reclaim_unreferenced_tunnel(
                client, created, identity_stored, create_attempted)
            self.sudo().write({
                'state': 'error',
                'error_message': self._provision_failure_message(
                    _("Cannot connect to PaaS Operator: %(detail)s",
                      detail=e.message),
                    outcome, created),
                'provision_outcome_unknown': outcome in (
                    _RECLAIM_INDETERMINATE, _RECLAIM_FAILED),
            })
            _logger.error("[SA] provision connection error: %s", e.message)
        except PaaSOperatorError as e:
            outcome = self._reclaim_unreferenced_tunnel(
                client, created, identity_stored, create_attempted)
            self.sudo().write({
                'state': 'error',
                'error_message': self._provision_failure_message(
                    _("Tunnel creation failed: %(detail)s", detail=e.message),
                    outcome, created),
                'provision_outcome_unknown': outcome in (
                    _RECLAIM_INDETERMINATE, _RECLAIM_FAILED),
            })
            _logger.error("[SA] provision error: %s", e.message)
        except AccessError:
            # The tunnel_token write above is a groups='base.group_system'
            # field write; for a non-system caller it raises AccessError.
            # Re-raise so the denial surfaces to the caller; do NOT downgrade it
            # to a silent state='error' (which would mask the security boundary
            # as an ordinary provisioning failure and let the endpoint claim a
            # benign error) — two-stage-remote-provision-idempotency §守則 2.
            #
            # Who still reaches this branch, after #805 (b): NOT the tenant BFF
            # endpoint — that one now drives this method under the platform
            # identity (see the tunnel_token ACL note in the docstring). It
            # stays live for callers that reach the method under a non-system
            # identity, and there is at least one such path today: Odoo's own
            # /web/dataset/call_kw, since action_provision is a public method
            # (tests/test_sa_provision_tenant_rpc_805.py C1 drives exactly it).
            #
            # #822: create_tunnel has already run when this fires, so a live CF
            # tunnel exists with no DB reference. It is now compensated here —
            # HTTP first, DB after (see _reclaim_unreferenced_tunnel). The
            # denial itself is untouched: still re-raised below, still not
            # rewritten into 'error'. Swallowing it would trade an orphan for a
            # masked security denial and remains forbidden.
            outcome = self._reclaim_unreferenced_tunnel(
                client, created, identity_stored, create_attempted)
            try:
                if outcome in (_RECLAIM_DONE, _RECLAIM_NOT_ATTEMPTED):
                    # CF 上已經什麼都沒有 ⇒ 'pending' 才是這一列的真實狀態，而且
                    # detail page 的 Provision 鈕只認 'pending' / 'error'，寫回去
                    # 才有出口（#822 §2 (A) 的死路）。⚠ 這不是把 denial 降級：
                    # 例外照樣 re-raise，也沒有寫成 'error' 假裝成一般失敗。
                    self.write({
                        'state': 'pending',
                        'error_message': _(
                            "Provisioning denied: this caller may not write "
                            "the tunnel credential (tunnel_token is a "
                            "base.group_system field)."),
                    })
                elif outcome in (_RECLAIM_FAILED, _RECLAIM_INDETERMINATE):
                    # 收不回來／不確定 ⇒ **不**寫回 'pending'：那等於邀請下一次重試
                    # 再開一顆，而前一顆還在 CF 上。只留記錄，狀態維持 'provisioning'。
                    self.sudo().write({
                        'error_message': self._provision_failure_message(
                            _("Provisioning denied: this caller may not write "
                              "the tunnel credential (tunnel_token is a "
                              "base.group_system field)."),
                            outcome, created),
                        'provision_outcome_unknown': True,
                    })
                # _RECLAIM_REFERENCED（identity write 之後才丟的 AccessError，例如
                # _sync_all_routes_access）：tunnel 有參照、state 交給重試路徑，
                # 這裡什麼都不動。
            except Exception:
                # 記帳失敗不得蓋掉 denial：這個分支的契約就是「拒絕必須傳到呼叫端」。
                # 大聲記錄而不是靜默吞掉，然後照樣把原例外丟出去。
                _logger.exception(
                    "[SA] #822 provision denial 的補償記錄寫入失敗 sa=%s"
                    " (outcome=%s) —— 原 AccessError 照常上拋", self.id, outcome,
                )
            raise
        except Exception as e:
            # Stage-2 (write / primary-route create / re-sync) can raise
            # non-operator exceptions: a ValidationError from
            # _check_prefix_format, a DB IntegrityError, or a UNIQUE clash on
            # the primary route. The narrow operator-only excepts above would
            # let those propagate while state was already flipped to 'active'
            # and tunnel_id stored — silent CF↔DB drift. Catch everything and
            # fail loud into 'error', mirroring _do_suspend / _do_resume /
            # _do_cleanup's bare except.
            #
            # #822 修正了這段註解原本的說法。舊版寫「tunnel_id is retained, so the
            # idempotent retry-from-error path can finish without orphaning the
            # tunnel」——那句話**只對窗口外的失敗成立**。identity write 自己炸掉時
            # （IntegrityError / ValidationError / 並發）tunnel_id 根本沒 retained，
            # 那正是 #822 的孤兒形狀。現在兩種形狀分開處理：窗口內補償刪除，窗口外
            # 維持原本「留著 tunnel_id 讓重試收斂」的設計。
            outcome = self._reclaim_unreferenced_tunnel(
                client, created, identity_stored, create_attempted)
            self.sudo().write({
                'state': 'error',
                'error_message': self._provision_failure_message(
                    _("Provisioning failed: %(error)s", error=e),
                    outcome, created),
                'provision_outcome_unknown': outcome in (
                    _RECLAIM_INDETERMINATE, _RECLAIM_FAILED),
            })
            _logger.exception("[SA] provision unexpected error sa=%s", self.id)

    def action_refresh_status(self):
        """Refresh tunnel connector status from the PaaS Operator.

        ⚠ **`deleted_at` falsy 一律落回既有判斷**（#833）。falsy 有兩種來源，
        **兩種都不是訊號**：key 缺席 ＝ 舊版 operator 還沒有這個欄位；值為 `null`
        ＝ Cloudflare 對**未刪除**的 tunnel 就是這樣回的。把缺席當成「不確定 ⇒
        標成 deleted／error」會在 operator 升版當下讓每一列一起翻。通則見
        `docs/tracking/pitfalls/operator-route-none-conflates-disabled-and-failed.md`。

        ⛔ 這裡刻意**不**把「operator 回 404」也映射成 `deleted`：平台看得到的只有
        「HTTP 失敗」，看不到那個失敗是誰發的（`operator_url` 設錯時 FastAPI 自己
        會對未知路徑回 404）。那會讓設定錯誤從吵鬧且正確的 `error` 變成安靜且錯誤
        的「已刪除」—— #833 的鏡像。404 維持走下面的 except。
        """
        self.ensure_one()
        return self._refresh_status()

    def _refresh_status(self, park=True, fail_closed_on_error=True):
        """Shared body of the status refresh and the FAULT-02 probe.

        ``park``: also park the protected routes at the edge when this closes
        them.  ``fail_closed_on_error``: an unreachable Operator closes the
        routes (a user-initiated refresh).  The probe passes ``False`` for both:
        an Operator outage or a blip says nothing about *this* tunnel and exposes
        nothing (Access is enforced at Cloudflare), so it must not mark tenants;
        and a closure the probe finds is parked by the resync leg that follows,
        in a transaction that cannot strand a parked edge without its marker.
        """
        self.ensure_one()
        if not self.tunnel_id:
            return
        client = self._get_operator_client()
        try:
            health = client.health_check()
            epoch = health.get('instance_epoch') if type(health) is dict else None
            if type(epoch) is not str or not epoch.strip() or len(epoch) > 128:
                # An Operator that predates the epoch contract: no restart
                # evidence, but the connector rules below still apply.
                epoch = None
            instance_id = health.get('instance_id') if type(health) is dict else None
            if (epoch is None or type(instance_id) is not str
                    or not instance_id.strip() or len(instance_id) > 128):
                instance_id = None
            result = client.get_tunnel_status(self.tunnel_id)
            vals = self._tunnel_vals_from_status(result)
            connections = result.get('connections') if type(result) is dict else None
            stored_epochs = self.sudo().operator_instance_epoch
            restarted, observed_epochs = self._fault02_epoch_decision(
                stored_epochs, instance_id, epoch)
            invalidate = self._fault02_should_invalidate(restarted, connections)
            # 翻旗標的那一刻記一行（不是每次輪詢都記）——#1766 的現場是「平台顯示
            # 一切正常」，所以第二台接上必須在 log 裡留痕。
            self._tunnel_binding_log_conflict(vals)
            self.write(vals)
            if observed_epochs != stored_epochs:
                # The field is system-only; a workspace owner/admin pressing
                # Refresh is not, and must not hit an AccessError for it.
                _internal_identity(
                    self.sudo()).operator_instance_epoch = observed_epochs
            if invalidate:
                protected = self.route_ids.filtered('is_protected')
                if protected:
                    _internal_identity(protected).access_publish_pending = True
                    if park:
                        # Make the durable state real at the edge too. A
                        # recovered Operator can still write CF config even when
                        # connector inventory is empty; pending routes therefore
                        # park at 503.
                        try:
                            self._sync_routes_to_operator()
                        except (PaaSOperatorError, AccessPublicationError) as error:
                            _logger.warning(
                                '[FAULT02] publication park failed sa=%s: %s',
                                self.id, error)
        except PaaSOperatorError as e:
            if not fail_closed_on_error:
                _logger.warning(
                    '[FAULT02] status unreadable, not treated as a fault sa=%s: %s',
                    self.id, e.message)
                return
            protected = self.route_ids.filtered('is_protected')
            if protected:
                _internal_identity(protected).access_publish_pending = True
            self.write({
                'tunnel_status': 'error',
                'error_message': _(
                    "Status refresh failed: %(detail)s", detail=e.message),
            })
            _logger.warning(
                "[SA] status refresh failed sa=%s: %s", self.id, e.message,
            )

    #: Replicas remembered per SA. The Operator runs a handful of replicas; the
    #: cap only bounds a map that would otherwise grow with every rollout.
    _FAULT02_MAX_INSTANCES = 8

    @api.model
    def _fault02_epoch_decision(self, stored, instance_id, epoch):
        """Pure: was an Operator replica restarted, and what to remember next.

        Several Operator replicas answer behind one Service and every process has
        its own epoch, so successive health checks legitimately alternate between
        epochs.  A restart is the *same* ``instance_id`` (pod) reporting a
        *different* epoch.  A replica seen for the first time is only learned;
        a health payload without an ``instance_id`` (older Operator) gives no
        sound basis to compare and never invalidates.  Returns
        ``(restarted, new_stored)``.
        """
        try:
            known = json.loads(stored) if stored else {}
        except ValueError:
            known = {}
        if type(known) is not dict:
            known = {}
        known = {k: v for k, v in known.items()
                 if type(k) is str and type(v) is str}
        if not instance_id:
            return False, stored or False
        previous = known.pop(instance_id, None)
        known[instance_id] = epoch
        while len(known) > self._FAULT02_MAX_INSTANCES:
            known.pop(next(iter(known)))
        return bool(previous and previous != epoch), json.dumps(
            known, separators=(',', ':'))

    def _fault02_has_pending(self):
        """True while any protected route of this SA is closed pending recovery."""
        self.ensure_one()
        return any(route.access_publish_pending
                   for route in self.route_ids if route.is_protected)

    @api.model
    def _fault02_should_invalidate(self, restarted, connections):
        """Pure fault decision: connector loss or a restarted Operator closes."""
        valid_connections = (
            type(connections) is list and bool(connections) and
            all(type(row) is dict and
                type(row.get('connector_id')) is str and
                bool(row['connector_id'].strip()) for row in connections)
        )
        return bool(restarted) or not valid_connections

    @api.model
    def _tunnel_vals_from_status(self, result):
        """operator ``GET /api/tunnels/{id}`` 的回應 → tunnel 相關欄位值。

        ``action_refresh_status``（寫回 DB）與 ``_live_tunnel_status``（只讀疊在
        API 回應上）共用這一份判讀，兩邊才不會各自演化出不同的「已連線」定義。
        ``deleted_at`` 的語意見 ``action_refresh_status`` docstring（#833）。
        """
        connections = result.get('connections', [])
        if result.get('deleted_at'):
            # 連 connector 記帳一起清：留著上一輪的數字會變成
            # 「Deleted ＋ Edge Connections: 4」這種看起來很正常的混合世代。
            return {
                'tunnel_status': 'deleted',
                'edge_connection_count': 0,
                'connector_id': '',
                'connector_type': '',
                'connector_version': '',
                'connector_count': 0,
                'binding_conflict': False,
            }
        vals = {
            'tunnel_status': 'connected' if connections else 'disconnected',
            'edge_connection_count': len(connections),
        }
        # edge_connection_count 是**連線**數（一個 cloudflared 預設 4 條），不是機器
        # 數——兩者差一個級距，混用就會把正常的一台看成四台。機器數由 binding
        # mixin 數 distinct client_id（#1766）。
        vals.update(self._tunnel_binding_vals_from_connections(connections))
        if connections:
            conn = connections[0]
            vals['connector_id'] = conn.get('connector_id', '')
            vals['connector_type'] = conn.get('type', '')
            # cloudflared client version (key varies by operator/CF payload)
            vals['connector_version'] = (
                conn.get('client_version') or conn.get('version') or '')
            if conn.get('opened_at'):
                vals['tunnel_uptime'] = conn['opened_at']
        else:
            vals['connector_version'] = ''
        return vals

    def _live_tunnel_status(self):
        """詳情頁用：即時向 operator 問 tunnel 狀態，**只讀、不寫 DB**。

        為什麼需要：``action_refresh_status`` 只在 ``refresh_status`` action 被
        呼叫時才寫回，而 SA 詳情頁從來沒呼叫它、也沒有排程 ⇒ 概覽頁的
        「Connector 已連線／Edge 4」會停在上一次寫入的值。2026-09-29 正式站實測：
        tunnel 已 0 connector 數小時，概覽頁仍顯示 09-25 的「已連線、Edge 4」，
        與 #1234 的 route 狀態欄（Connector offline）互相矛盾。

        只讀是刻意的：寫回要過 SA 的 write ir.rule（只發給 owner/admin），
        詳情頁卻是所有成員都能看。拿不到（operator 未設定／逾時／錯誤）就回
        ``{}``，呼叫端沿用 DB 裡的值——那是退化成原本的行為，不是新的錯誤。
        """
        self.ensure_one()
        if not self.tunnel_id:
            return {}
        client = get_paas_operator_client(self.env)
        if not client:
            return {}
        try:
            vals = self._tunnel_vals_from_status(
                client.get_tunnel_status(self.tunnel_id))
        except PaaSOperatorError as e:
            _logger.info("[SA] live tunnel status unavailable sa=%s: %s",
                         self.id, e.message)
            return {}
        if not vals.get('edge_connection_count'):
            # 斷線時「Connector Opened」不該還掛著上一次連上的時間。
            vals['tunnel_uptime'] = ''
        vals['tunnel_status_live'] = True
        return vals

    def action_delete(self):
        """User-initiated delete: full Cloudflare teardown then unlink.

        🔴 #794 覆核 HIGH：這裡**自己也要一層** savepoint ＋ 補償，不能只靠
        `unlink()` 那一層。理由是**受害者落在哪一次呼叫**：

        tunnel 刪不掉那一格（正是本腿的觸發條件）的順序是——本方法的 pre-call
        把 Access app 全部拆掉、清掉 `access_app_id`、tunnel 刪除失敗；接著
        `unlink()` 內的第二次 `_cleanup_cloudflare()` **已經沒有東西可拆**
        （id 都被 pre-call 清掉了）⇒ `unlink()` 自己的累加器是**空的**，它的
        補償什麼都不會做，而真正的受害者是 pre-call 拆掉的那幾條。

        兩層 savepoint 各自帶自己的累加器是對的（`Savepoint.__exit__` 例外時先
        `ROLLBACK TO SAVEPOINT` 才讓例外外拋）：內層先回捲、內層補償拿空清單
        no-op、例外外拋到這裡 ⇒ 外層回捲（把 pre-call 的寫入也撤掉）⇒ 外層補償
        這時才寫得進去。**刻意不把累加器經 context 傳下去**——那是可偽造的裸
        context key 那條路（#536／#783），而這裡不需要它。
        """
        self.ensure_one()
        torn_down = []
        Reap = self.env[_CF_REAP_MODEL]
        try:
            with self.env.cr.savepoint(flush=False):
                if not self._try_lock_lifecycle_rows():
                    raise UserError(_(
                        'Another Security Access lifecycle operation is in progress. '
                        'Please retry when it finishes.'
                    ))
                self._reject_unresolved_tunnel_outcome()
                self.write({'state': 'deleting'})
                self._cleanup_cloudflare(torn_down=torn_down)
                self.unlink()
        except Exception:
            Reap._compensate_fail_open_after_rollback(
                torn_down, _logger,
                '[SA] security access action_delete (sa=%s)' % (self.ids,))
            raise

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
        if not self._try_lock_lifecycle_rows():
            raise UserError(_(
                'Another Security Access lifecycle operation is in progress. '
                'Please retry when it finishes.'
            ))
        self._reject_unresolved_tunnel_outcome()
        # #322: 比照 CloudService（#318）— super().unlink() 前捕獲 in_progress
        # 訂閱，刪除成功後結算（取消＋按比例退點）。共用 _settle_on_entity_delete。
        subs_to_settle = self.sudo().mapped('subscription_id').filtered(
            lambda s: s.state == 'in_progress'
        )
        # 🔴 #794 覆核 HIGH：**第五腿**，而且比 route 那腿更直接可達（單筆刪整個
        # SA 即可，不需要 multi-id RPC）。`_cleanup_cloudflare()` 內部先把每條
        # protected route 的 CF Access app **真的拆掉**並清掉 `access_app_id`，
        # 再試著刪 tunnel；tunnel 刪不掉時它**自己不 raise**、正常返回，而下面
        # `if record.tunnel_id: raise` 才把整筆交易丟掉 ⇒ 已經在 CF 上被拆掉的
        # 那幾條 route 復活，帶著 `is_protected=True` 與死的 `access_app_id`。
        #
        # ⚠ 「`_cleanup_cloudflare` 不 raise」為真，但它**不是**這條腿安全的理由
        # ——造成回捲的 raise 在**這裡**，而且發生在它 partial success 之後。
        #
        # savepoint 是本次新增的，理由與 CS／SmartHome 兩腿相同且同樣是功能性的：
        # 補償跑在獨立連線上，外層對那幾列的 UPDATE 若沒回捲就會撞 PG 寫寫衝突
        # 等到 lock_timeout。先回捲、再補償。
        torn_down = []
        Reap = self.env[_CF_REAP_MODEL]
        try:
            with self.env.cr.savepoint():
                for record in self:
                    record._cleanup_cloudflare(torn_down=torn_down)
                    if record.tunnel_id:
                        raise UserError(
                            _("Cloudflare cleanup failed; the tunnel is still "
                              "live and billed. The record was kept so the "
                              "deletion can be retried. Please try again "
                              "shortly.")
                        )
                # 軸2: routes are freely deletable (no primary guard), so
                # cascade-delete them directly as part of tearing down the
                # whole SA.
                self.route_ids.unlink()
                res = super().unlink()
                for sub in subs_to_settle:
                    sub._settle_on_entity_delete(
                        reason='Security Access deleted')
        except Exception:
            Reap._compensate_fail_open_after_rollback(
                torn_down, _logger,
                '[SA] security access unlink (sa=%s)' % (self.ids,))
            raise
        return res

    # ------------------------------------------------------------------ #
    #  Route sync (declarative)                                          #
    # ------------------------------------------------------------------ #

    def _build_route_payload(self, override_service_url=None,
                             park_protected=False):
        """Publish protected origins only after an Access app is established.

        Provision/resume can temporarily park all protected routes while their
        desired Access state is being restored. Suspension overrides every route.
        """
        self.ensure_one()
        payload = []
        for route in self.route_ids.sorted(key=lambda r: r.id):
            blocked = route.access_publish_pending or (route.is_protected and (
                park_protected or not route.access_app_id
                or route.access_protection_lost))
            service_url = (override_service_url or
                           ('http_status:503' if blocked else route.service_url))
            payload.append({
                'subdomain': route.subdomain_prefix,
                'service_url': service_url,
                'http_host_header': (
                    route.http_host_header and not override_service_url
                    and not blocked
                ),
            })
        return payload

    def _sync_routes_to_operator(self, override_service_url=None,
                                 park_protected=False):
        """Push the full desired route list to the operator (idempotent)."""
        self.ensure_one()
        if not self.tunnel_id:
            return
        client = self._get_operator_client()
        routes = self._build_route_payload(override_service_url, park_protected)
        result = client.sync_tunnel_routes(
            self.tunnel_id,
            routes=routes,
            comment=self.canonical_name,
            # #531 歸屬主張：送**鏡回來的 CF name**（`tunnel_name`），不是
            # `canonical_name`。兩者在 SA 上通常相等，但權威的那一份是 CF 自己回的
            # 那個值（operator 的 create_tunnel 取 `result.get("name", name)`），而
            # operator 也是拿 CF 現在的 name 來比 —— 兩側都用 CF 那一份，命名規則
            # 怎麼改（env token 注入、hash 換種子）都不會讓這道檢查誤判舊列。
            tunnel_name=self.tunnel_name,
        )
        require_tunnel_routes_receipt(
            result, self.tunnel_id, routes, self._get_paas_domain())
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

    def _access_recovery_tunnel_is_live(self):
        """Return whether the exact tunnel can currently serve a recovery.

        A successful route/access write receipt only proves that Cloudflare
        accepted desired configuration.  It does not prove a connector is
        still attached: Cloudflare can retain old connection inventory briefly
        after a connector dies.  Pending-origin recovery therefore requires a
        fresh status read with a serving tunnel state and at least one current
        connection.  Anything malformed or unavailable stays fail-closed.
        """
        self.ensure_one()
        if not self.tunnel_id:
            return False
        try:
            result = self._get_operator_client().get_tunnel_status(
                self.tunnel_id)
        except PaaSOperatorError as error:
            _logger.warning(
                '[SA] Access recovery liveness unavailable sa=%s: %s',
                self.id, error.message)
            return False
        if type(result) is not dict:
            return False
        connections = result.get('connections')
        return bool(
            result.get('tunnel_id') == self.tunnel_id and
            result.get('name') == self.tunnel_name and
            not result.get('deleted_at') and
            result.get('status') in ('healthy', 'degraded') and
            type(connections) is list and
            any(type(connection) is dict and
                type(connection.get('connector_id')) is str and
                bool(connection['connector_id'].strip())
                for connection in connections)
        )

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
        # Release origins only after EVERY route's Access update succeeded.
        try:
            self._sync_routes_to_operator()
        except (PaaSOperatorError, AccessPublicationError):
            for route in self.route_ids:
                _internal_identity(route).access_publish_pending = True
            raise

    def _teardown_route_access(self, app_id, policy_ids, app_name, hostname,
                               anchor_model=None, anchor_id=None,
                               torn_down=None):
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
            payload = {
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
            }
            result = client.sync_access_app(payload)
            require_access_sync_receipt(result, payload)
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
        # 🔴 #794 二輪覆核 [HIGH]：**「CF 已經拆成功」這個事實要在這裡就記下來，
        # 不能等到 `return True`**。下面的落帳（`_settle_teardown_after_commit`）
        # 明文 **re-raise `OperationalError`**（那是 #791 的刻意設計：讓例外一路
        # 傳到 Odoo 的 `retrying()` 觸發整支 request 重播，**不要為了本格把它拿
        # 掉**）⇒ 落帳一撞到並發例外，本方法就**不會** `return True` ⇒ 呼叫端的
        # `if ok:` 不執行 ⇒ 補償清單是空的 ⇒ 補償當場空轉，而 CF 上的 app 已經
        # 真的沒了、列正要回捲復活。那個結局與「補償根本沒被接上」在列狀態上
        # **完全不可區分**。
        #
        # ⚠ 要改的是「**記錄副作用已發生**」的時機，不是例外傳播行為。回傳值
        # 語意**不動**：它仍然表示「這一趟整體成不成功」（落帳失敗仍然回不了
        # True，見 `test_G3_return_value_semantics_unchanged_on_ledger_failure`）。
        if torn_down is not None and anchor_model and anchor_id:
            torn_down.append((anchor_model, anchor_id))
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
        # #1052：判準走 mixin 的 `_is_halted()` seam——cron 已經用它決定要不要呼叫
        # 本方法（subscription_lifecycle_cron.py），mixin 預設就是 `state == 'suspended'`，
        # 零行為變化。內聯 literal 會讓將來的 override（#482/#497 那種「state 被改寫
        # 但仍在停權中」）只在 cron 生效、在這裡靜默 no-op。
        if not self._is_halted():
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
            self._sync_routes_to_operator(override_service_url='http_status:503')
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
        if not self._try_lock_lifecycle_rows():
            _logger.info(
                '[SA] _do_cleanup skipped sa=%s; lifecycle row is busy', self.id,
            )
            return
        if self.sudo().provision_outcome_unknown:
            _logger.warning(
                '[SA] _do_cleanup skipped sa=%s; tunnel creation outcome needs exact reconciliation',
                self.id,
            )
            return
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
                'error_message': _(
                    "Cloudflare cleanup failed; %(resource)s still live. "
                    "Will retry on the next cron pass.",
                    resource=(_('tunnel') if self.tunnel_id
                              else _('Access app'))),
            })
            _logger.warning(
                "[SA] _do_cleanup incomplete sa=%s tunnel=%s lingering_access=%s "
                "(state=error)",
                self.id, self.tunnel_id, lingering_access.ids,
            )
            return
        self.write({'state': 'deleting'})

    def _cleanup_cloudflare(self, torn_down=None):
        """Idempotent CF teardown. delete_tunnel cascades: operator removes
        every DNS record pointing at the tunnel, then deletes the tunnel
        (token invalidated).

        #794 覆核 HIGH：``torn_down`` 是**累加器**（``[(model, id), …]``），本方法
        真的對 CF 拆成功、並因此清掉 ``access_app_id`` 的 route 錨點會 append
        進去。形狀與 ``SmartHome._teardown_routes_access`` 逐字相同（out-parameter
        而不是改回傳型別：本方法有三個呼叫端，改契約會連帶動到兩個與本修法無關的）。

        ⚠ **為什麼呼叫端需要它**：本方法自己**不 raise**（tunnel 刪不掉時只留
        ``tunnel_id`` 並 log），但它的兩個呼叫端 ``unlink()`` / ``action_delete()``
        會在它**partial success 之後**才 raise（``if record.tunnel_id: raise``）
        ⇒ 整筆交易回捲 ⇒ 這裡已經真的在 CF 上被拆掉的那幾條 route 復活，帶著
        ``is_protected=True`` 與死掉的 ``access_app_id``。**「本方法不 raise」
        不等於「這條腿不會 fail-open」**——真正造成回捲的 raise 在呼叫鏈上一層。
        （`_do_cleanup` 那個呼叫端不 raise，寫入照常 commit，所以它不傳累加器。）
        """
        self.ensure_one()
        # Remote app deletion cannot be rolled back with the DB transaction.
        # If parking fails, stop before removing any authentication boundary.
        if self.tunnel_id:
            self._sync_routes_to_operator(override_service_url='http_status:503')
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
            # 🔴 第五腿複用 SA route 的 `_teardown_route_access`，所以同型：
            # `torn_down` 交給它在 operator 回 2xx 的當下就記（見那支方法內的
            # [HIGH] 註解）——**不能**放在下面的 `if ok:` 裡，落帳撞到
            # `OperationalError` 時 `ok` 拿不到 True。
            ok = self._teardown_route_access(
                app_id=app_id,
                policy_ids=pol_ids,
                app_name=route.subdomain_prefix,
                hostname=route.hostname,
                anchor_model=route._name,
                anchor_id=route.id,
                torn_down=torn_down,
            )
            if ok:
                _internal_identity(route).access_app_id = False
                # #798：包 recordset 不是包 record——迭代帶 context 的
                # recordset 得到的 singleton 沿用同一個 env，包一次就夠。
                for pol in _internal_policy_cf_write(route.access_policy_ids):
                    if pol.cf_policy_id:
                        pol.cf_policy_id = False
        if not self.tunnel_id:
            return
        try:
            client = self._get_operator_client()
            # #531：`tunnel_name` 在這裡一定還在 —— 本方法成功後只清 `tunnel_id`
            # （下一行的 `_internal_identity` write），`tunnel_name` 是刻意留著的。
            client.delete_tunnel(self.tunnel_id, tunnel_name=self.tunnel_name)
            _logger.info("[SA] tunnel deleted sa=%s tunnel=%s",
                         self.id, self.tunnel_id)
            # #833：tunnel_status 一起收掉。這是平台**唯一百分之百確定「這條
            # tunnel 已經不存在」的時刻**（是我們自己刪的），而且是最後一個機會——
            # `action_refresh_status()` 的第一行是 `if not self.tunnel_id: return`，
            # 下一行清掉錨點之後，那個欄位就再也沒有任何路徑能更新它，會被凍結在
            # 刪除前的最後一個值（通常是 connected 的綠燈）。
            # ⚠ 只在 delete 成功之後寫：失敗時 tunnel 還活在 CF 上，標成 deleted
            # 會把「待重試」謊報成「已刪除」，比原本的 bug 更糟。
            _internal_identity(self).write({
                'tunnel_id': False,
                'tunnel_status': 'deleted',
            })
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
            # #1766：綁在哪一台 HA、以及有幾個 cloudflared 接著。注意
            # `edge_connection_count` 是連線數（一台預設 4 條），`connector_count`
            # 才是機器數——UI 不要把前者當機器數顯示。
            'binding_state': self.binding_state,
            'bound_instance_label': self.bound_instance_label or '',
            'bound_at': self.bound_at.isoformat() if self.bound_at else '',
            'connector_count': self.connector_count,
            'binding_conflict': self.binding_conflict,
            'error_message': self.error_message or '',
            'deployed_at': self.deployed_at.isoformat() if self.deployed_at else '',
            'created_at': self.create_date.isoformat() if self.create_date else '',
            # P2.5: Access config is per-route now. Each route's to_dict()
            # carries its own is_protected / login methods / session / policies
            # / fail-open signal. The SA layer no longer exposes service-wide
            # Access fields.
            'routes': [r.to_dict() for r in self.route_ids],
            # #1234：讓前端在按下「Add Route」之前就知道額度（原本只有撞到才知道）。
            'route_quota': self._route_quota(),
            'subscription': self._get_subscription_info(),
        }

    def _route_quota(self):
        """方案允許的 route 數；沒有訂閱或方案（admin／legacy）回 ``None``＝不限。

        口徑與 ``_check_route_quota`` / controller ``_create_route`` 相同（同一個
        ``sa_max_routes``），只是這裡只讀、不擋。
        """
        self.ensure_one()
        sub = self.sudo().subscription_id
        if not sub or not sub.sh_subscription_plan_id:
            return None
        return sub.sh_subscription_plan_id.sa_max_routes

    def _route_gate_denial(self, plan):
        """換方案的 route 數前置合規 gate（#1261）：合規回 ``None``，超額回**已組好的拒絕內容**。

        ## 為什麼需要

        2026-09-29 production 瀏覽器 E2E 實撞：SA 在「3 組」方案（``sa_max_routes=3``）
        下開了 3 條 route，降級到「1 組」（``sa_max_routes=1``）**成功** ⇒ 記錄永久停在
        「3 / 1 routes」。route 的配額守衛（``_check_route_quota`` 是 ``@api.constrains
        ('security_access_id')``）只在**寫 route** 時跑，換方案不寫 route ⇒ 守衛永遠不醒；
        UI 只剩一行超額提示，沒有任何東西逼租戶收斂。#773 的席位 gate 只管 Access 席數，
        route 數這一格從來沒人擋。

        ## 形狀刻意鏡像 #773 ``_seat_gate_denial``

        - **只回報、⛔ 不刪任何 route**——與 #773 Eugene 裁決（留言 10716）同一原則：
          降級由租戶自己刪到合規，系統只偵測。刪 route 會連帶拆 DNS／Access app，
          絕不能由換方案順手做。
        - **``plan`` 是呼叫端給的目標方案**，⛔ 不走 ``_route_quota()``——那讀的是錨點
          **當下**（舊）方案的 cap，降級時拿舊 cap 比對會永遠放行（#773 同一個陷阱）。
        - **不判「是不是降級」**：判準是「現有 route 數 ≤ 目標 cap 才放行」，升級天然通過。
        - 回傳 ``{'error', 'current_routes', 'target_routes'}``，controller 原樣轉成
          ``data``，與 seat gate 的 ``current_seats`` / ``target_seats`` 同構。

        ## 語意：``sa_max_routes == 0`` ＝**一條都不准**，⛔ 不是 unlimited

        與欄位 help（``sh_subscription_plan_sa_quota.py``：「0 = none allowed」）及
        ``_check_route_quota`` 的 ``count > quota`` 同一個比較子。⚠ 相鄰的
        ``sa_max_access_policies`` / ``sa_max_emails_per_policy`` 的 0 **是** unlimited
        （#743），兩種語意刻意相反（見 ``security_access_access_policy.py`` 的說明）
        ⛔ 不要在這裡補 ``if not quota: return None`` 順手「對齊」。
        「不限」只存在於**沒有訂閱／方案**的 admin／legacy SA（``_route_quota()`` 回
        ``None``），而換方案一定有目標 plan ⇒ 本 gate 沒有 unlimited 分支。

        ## 計數用 sudo

        席位尺同理（``_seat_usage_for_anchor``）：route 數是**平台成本**，不是「這位使用者
        看得到幾條」。池以 ``self`` 的 o2m 收斂 ⇒ sudo 只放寬可見度、不會把別的 SA 算進來。
        失效方向是 fail-closed：萬一哪天 route 的 ir.rule 收窄（#1054 守的那條外部性質），
        sudo 版只會**多算**（擋得比守衛嚴），不會少算讓降級溜過去。

        :param plan: 目標 ``sh.subscription.plan``
        :return: ``None``，或 ``{'error': str, 'current_routes': int, 'target_routes': int}``
        """
        self.ensure_one()
        used = len(self.sudo().route_ids)
        allowed = plan.sa_max_routes
        if used <= allowed:
            return None
        if allowed:
            message = _(
                "This plan allows %(allowed)s route(s), but this Security Access "
                "currently has %(used)s. Delete routes until at most "
                "%(allowed)s remain, then change the plan."
            ) % {'allowed': allowed, 'used': used}
        else:
            # 0 條的目標態走一句讀得順的文案，不要落到「allows 0 route(s)」（#773 同理）。
            message = _(
                "This plan allows no routes, but this Security Access currently "
                "has %(used)s. Delete every route before changing to this plan."
            ) % {'used': used}
        return {'error': message,
                'current_routes': used,
                'target_routes': allowed}

    def _probe_route_health(self, force=False):
        """#1234：從公開網路探每條 route，回 ``{route_id: result}``。

        結果語意見 ``services/route_probe.py``。探測失敗永不 raise。
        """
        self.ensure_one()
        routes = self.route_ids
        by_host = route_probe.probe_routes(
            [(r.hostname, r.is_protected) for r in routes], force=force)
        return {r.id: by_host.get(r.hostname) for r in routes}

    def _route_traffic(self, window_hours=24):
        """#1234：各 route 的請求數／最後請求時間，資料來自 operator 的 Cloudflare
        analytics。回 ``{available, reason, window_hours, routes: {route_id: ...}}``。

        永不 raise：流量是輔助資訊，拿不到時前端顯示原因即可。``reason``：
        ``permission``（operator 的 CF token 缺 Zone Analytics Read）／
        ``operator_outdated``（operator 還沒有這個端點）／``disabled``／``error``。
        """
        self.ensure_one()
        routes = self.route_ids
        if not routes:
            return {'available': True, 'reason': '', 'window_hours': window_hours,
                    'routes': {}}
        client = get_paas_operator_client(self.env)
        if not client:
            return {'available': False, 'reason': 'disabled', 'routes': {}}
        try:
            res = client.get_hostname_traffic(routes.mapped('hostname'), window_hours)
        except PaaSOperatorError as e:
            reason = 'operator_outdated' if getattr(e, 'status_code', None) == 404 else 'error'
            _logger.info("route traffic unavailable for SA %s: %s (%s)", self.id, reason, e)
            return {'available': False, 'reason': reason, 'routes': {}}
        hosts = res.get('hosts') or {}
        return {
            'available': bool(res.get('available')),
            'reason': res.get('reason') or '',
            'window_hours': res.get('window_hours', window_hours),
            'routes': {r.id: hosts.get(r.hostname) for r in routes},
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
    # #1261：help 原本寫得像「不勾就不會帶 Host」。實際語意是 cloudflared 的
    # `originRequest.httpHostHeader`（operator `cloudflare.py` 只在勾選時寫入）：
    # **強制**把 Host 設成公開網址。沒設時 cloudflared 本來就原樣轉送訪客的 Host，
    # 常見情況下也是同一個公開網址（2026-09-29 正式站實測勾／不勾 origin 收到的
    # Host 相同）。只改說明文字，行為與預設值不變。
    http_host_header = fields.Boolean(
        string='Send HTTP Host Header',
        default=True,
        help='Force the HTTP Host header sent to the origin to the public '
             'hostname (cloudflared httpHostHeader). Without it cloudflared '
             'still forwards the visitor\'s original Host, which is normally '
             'the same public hostname.',
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
    access_publish_pending = fields.Boolean(
        string='Access Publication Pending', default=False, readonly=True,
        help='Access settings are saved, but the origin publication result is unconfirmed. Retry.')
    access_token_revoke_pending = fields.Boolean(
        string='Access Token Revocation Pending', default=False, readonly=True,
        help='Authorization changed for an existing Access app. The origin stays '
             'closed until the provider acknowledged the token revocation, the '
             'configured hold elapsed and the Access state was re-confirmed.')
    access_token_revoke_accepted_at = fields.Datetime(
        string='Access Token Revocation Accepted At', copy=False, readonly=True,
        help='When the provider acknowledged the current token revocation. The '
             'hold before re-publication is measured from this instant.')
    access_protection_lost = fields.Boolean(
        string='Protection Not In Effect', copy=False, default=False,
        help='True = the Cloudflare Access application for this route has been '
             'torn down while the row itself is still here (the delete '
             'transaction rolled back, but the Cloudflare half cannot be). '
             'is_protected is then only an **intent**: nothing on Cloudflare is '
             'forcing a login, and the hostname is still served through the '
             'tunnel. Cleared by the next successful Access sync. Deliberately '
             'kept apart from is_protected — lowering is_protected would mean '
             'the platform changing a setting the tenant can see (a '
             'product-visible change), which is not decided yet (#794).',
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
                raise ValidationError(_("%s: %r") % (err, route.subdomain_prefix or ''))

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
            # #1054：這個 `len(o2m)` 跑在寫入者自己的 env 上，而
            # `One2many.read` 走 `search_fetch`（`odoo/fields.py`）——它的
            # docstring 逐字說會套 domain **and access rules** ⇒ 讀出來的長度
            # 是 rule-filtered 的長度，機制與 #896 那條 `search_count` 相同。
            # 上一行 `sa.sudo().subscription_id` 有 sudo、這一行沒有，是刻意
            # 留著的：2026-09-06 量測（run 2674）結論是**結構上不可達**——
            # 讀不到 `sa` 的人在這一行就 AccessError（fail-closed），讀得到的
            # 人必是該 workspace 的成員，而 route 的 ir.rule 判歸屬走同一份
            # 成員關係 ⇒ 他看得到整池、計數正確。Eugene 裁決 (2)：不加 sudo，
            # 改用 regression guard 守住那個**外部性質**。
            # 🔴 安全性不在這一行，在 `src/security/ir_rules.xml`：一旦有人給
            # 非 owner/admin 開 route 的 create/write 腿、或收窄 member 的 read
            # 腿，這裡就會少算而**症狀與「方案上限很大」完全不可區分**。守線＝
            # `src/tests/test_quota_len_o2m_reach_1054.py`（改這附近先讀它）。
            count = len(sa.route_ids)
            if count > quota:
                raise ValidationError(
                    _("Route quota exceeded: plan '%s' allows %d "
                      "route(s).") % (sub.sh_subscription_plan_id.name, quota)
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
        # #1196：rename 從「一律禁止」改成「必須同時搬 CF 那一側」。
        #
        # 這一根**不是身分錨點**（見檔頭 `_IDENTITY_ANCHOR_FIELDS` 的 NOT anchors
        # 註記）——它是使用者選的值，先前擋住它的理由是**正確性**不是授權：
        # hostname / DNS / ingress 全都從 prefix 推導，改了值卻沒搬 CF 就會靜默
        # 分岔。所以解法不是放行，是「放行 + 保證搬」：底下的 sync 觸發清單納入
        # `subdomain_prefix`，DNS 由宣告式全量同步收斂（舊 hostname 不在清單裡
        # 就會被 operator 刪掉，與 unlink 走同一條路），Access app 則靠
        # `existing_app_id` + 新 `primary_hostname` 就地改名。
        #
        # **刻意用原地改名，不是 delete + create**：route 上掛著
        # `access_policy_ids` 與 `access_app_id`，砍掉重建會把整組 Access 設定
        # 一起丟掉，而且 route id 會換——參照它的歷史與外部引用全斷。
        #
        # `sa_internal_prefix_write` 這把鑰匙**已退場**（registry 標 removed）：
        # 它存在的理由是「改名不可行、但 data-repair 需要一條後門」，而改名成為
        # 正式功能之後那個前提就沒了。「只改值不碰 CF」的需求由 `sa_skip_route_sync`
        # 承接——那把鑰匙的語意正是如此，且它不會順帶讓人繞過格式驗證。
        # ⛔ 不要為了重新擋改名而把它加回來：真要擋，該擋在有 CF 同步保證的那一層。
        if 'subdomain_prefix' in vals:
            new_prefix = vals['subdomain_prefix']
            for route in self:
                if route.subdomain_prefix == new_prefix:
                    continue
                if route.is_protected:
                    raise UserError(_(
                        "Protected routes cannot be renamed. Create a new route "
                        "with a blocked origin, enable its protection, then "
                        "replace the old route."))
                err = self._subdomain_prefix_error(new_prefix)
                if err:
                    raise UserError(_("%s: %r") % (err, new_prefix or ''))
        # #1071：錨點搬移的**歸屬**守衛，對稱 policy 側 #848 與 rule 側 #941。
        # 位置在任何狀態變更之前——新錨點的授權要在 `super().write()` 之前成立。
        #
        # 排在上面兩道**之後**是刻意的，順序決定混合 payload 拒的是哪一類：
        # `_reject_identity_anchor_writes`（`UserError`）與 prefix 不可變
        # （`UserError`）守的是「值不可變」、對 superuser 也擋；本函式是授權層、
        # su 豁免。與 policy／rule 側「投影欄／identity 守衛先、授權守衛後」同序
        # （#941 的 E4 把那個順序斷言成契約），本檔由守線測試 E4 對應釘住。
        _assert_route_anchor_move_authorised(self, vals)
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
        unprotecting = self.browse()
        if (not _is_sync_control(self.env, _CTX_SKIP_ROUTE_SYNC)
                and vals.get('is_protected') is False):
            unprotecting = self.filtered(
                lambda route: route.is_protected and route.access_app_id)
        with self.env.cr.savepoint():
            # Park every affected tunnel before any route changes intent or
            # any remote Access app can be deleted.  pending keeps the normal
            # post-write route push blocked until the exact disabled receipt.
            for sa in unprotecting.mapped('security_access_id'):
                sa._sync_routes_to_operator(park_protected=True)
            if unprotecting:
                _internal_identity(unprotecting).access_publish_pending = True
            result = super().write(vals)
            # #1196：`subdomain_prefix` 一併觸發——改名的整個「搬 CF」動作就靠
            # 這一次宣告式全量推送：新 hostname 進 ingress、舊 hostname 因為不在
            # 清單裡被 operator 刪掉 DNS（與 unlink 同一條收斂路徑），新 DNS 的
            # record id 由回傳值寫回 `dns_record_id`（`_sync_routes_to_operator`
            # 用**新的** prefix 去對，所以會自己蓋掉舊 id）。
            #
            # 資料修復用的 migration 開 `sa_skip_route_sync` 就能只改值不碰 CF；
            # 那條路徑本來就要自己負責顯式 re-sync。
            if (not _is_sync_control(self.env, _CTX_SKIP_ROUTE_SYNC)
                    and any(k in vals for k in (
                        'service_url', 'http_host_header', 'subdomain_prefix',
                        'is_protected'))):
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
            #
            # #1196：`subdomain_prefix` 也在列——一條 protected route 的 Access
            # app 是**用 hostname 定義的**（payload 的 primary_hostname /
            # protected_hostnames / app_name 全從 prefix 來）。改名不重推的話，
            # CF 上那顆 app 仍然守著**舊** hostname：新網址變成無人看守的公開
            # 入口，而 UI 讀 `is_protected` 還是顯示「已保護」——與 #794 同一類
            # 的靜默 fail-open。payload 帶 `existing_app_id`，所以這是就地改名
            # 不是新建一顆。
            if (not _is_sync_control(self.env, _CTX_SKIP_ROUTE_SYNC)
                    and any(k in vals for k in (
                        'is_protected', 'login_google', 'login_email_otp',
                        'session_duration', 'subdomain_prefix'))):
                # A route held by a token revocation is never released by a
                # tenant edit; only its own hold may re-publish it.
                deferred_release = self.filtered(
                    lambda route: route.access_publish_pending
                    and not route.access_token_revoke_pending)
                for route in self:
                    route._sync_access_to_operator(
                        _DEFER_ACCESS_RELEASE if deferred_release else None)
                # Publish once per gateway only after every changed route's
                # Access receipt has validated.  Clear the group's pending
                # bits only for the publication attempt; restore on failure.
                from ..services.paas_operator import PaaSOperatorError
                for sa in deferred_release.mapped('security_access_id'):
                    routes = deferred_release.filtered(
                        lambda route: route.security_access_id == sa)
                    if not sa._access_recovery_tunnel_is_live():
                        _logger.error(
                            '[SA] Access batch recovery remains pending routes=%s: '
                            'tunnel has no live connector', routes.ids)
                        continue
                    _internal_identity(routes).access_publish_pending = False
                    try:
                        sa._sync_routes_to_operator()
                    except (PaaSOperatorError, AccessPublicationError) as error:
                        _internal_identity(routes).access_publish_pending = True
                        _logger.error(
                            "[SA] Access batch publication pending routes=%s: %s",
                            routes.ids, error)
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
        #
        # 🔴 #794：**這個 savepoint 本身就是 fail-open 的來源**。它回捲得掉 DB
        # 那半，回捲不掉「已經對 Cloudflare 發出的 Access 刪除」——所以先被拆掉
        # 的那幾條會以「列復活、`is_protected=True`、`access_app_id` 指向一顆 CF
        # 上已不存在的 app」收場，而 UI 讀的正是 `is_protected` ⇒ 顯示已保護、
        # 實際免登入即可存取。四條偵測管道逐一實測都照不到（#794 PROBE-1～7）。
        #
        # 處置：`torn_down` 記下「這一趟真的拆成功」的錨點；走到 `except` 時
        # （savepoint 已回捲、例外尚未外拋的那一格）交給
        # `cf_access_policy_reap._compensate_fail_open_after_rollback` 用**獨立
        # 交易**把列上的死 id 清掉、把 `access_protection_lost` 打開、再 best-
        # effort 重推一次 sync。為什麼載體非得是獨立交易不可（postrollback 與
        # `_settle_*_after_commit` 都活不過來），見那支 docstring。
        #
        # ⚠ 只放「拆成功」那幾條：拆失敗那條的列與它的 `access_app_id` 是**真的
        # 重試錨點**（[[two-stage-remote-provision-idempotency]]），動它會踩掉
        # `test_protected_route_unlink_teardown_failure_rolls_back_keeps_row`
        # 鎖住的既有契約。
        torn_down = []
        Reap = self.env[_CF_REAP_MODEL]
        try:
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
                # #794：pass 1 **改成 short-circuit**。舊寫法是 eager 的 list
                # comprehension，同批第一條失敗之後後面每一條 pass-1 route 仍會
                # 被真的送去 CF 拆掉才一起回捲——而回捲不掉的正是那些 CF 刪除，
                # 所以單次事故的受害者是「整批」而不是「失敗那條之前已經成功的
                # 那幾條」。代價：錯誤訊息只列得出第一條失敗的 route（既有測試
                # 沒有任何一條斷言它會列出全部，已逐檔查過）。
                def _push(route_id, info):
                    # `info['sa']` 一定還在：`security_access_id` 是 required ＋
                    # ondelete='cascade'，而唯一會刪 SA 的 Python 路徑
                    # （`SecurityAccess.unlink`）是先呼叫 `route_ids.unlink()` 才
                    # `super().unlink()`；PG 層的 cascade 則整支 Python 都不會跑到
                    # 這裡。（舊版在這裡有一道 `if not sa.exists(): continue` 的防禦
                    # 碼，覆核判定為死碼——會誤導讀者的防禦碼本身就是陷阱，故移除，
                    # 理由留在這裡。）
                    # `torn_down` 由 `_teardown_route_access` 在 operator 回
                    # 2xx 當下就 append（見該方法的 [HIGH] 註解）；呼叫端
                    # **不再**依賴回傳值來記這件事。
                    return info['sa']._teardown_route_access(
                        app_id=info['app_id'],
                        policy_ids=info['policy_ids'],
                        app_name=info['app_name'],
                        hostname=info['hostname'],
                        anchor_model=self._name,
                        anchor_id=route_id,
                        torn_down=torn_down,
                    )

                # pass 1：拆失敗 ⇒ 回滾保住列（列是真的重試錨點）。**第一條失敗
                # 就停**（#794）。
                failed = None
                for route_id, info in teardown_by_route.items():
                    if not info['retry_anchor_is_row']:
                        continue
                    if _push(route_id, info):
                        continue
                    failed = info['app_name'] or info['app_id']
                    break
                if failed:
                    raise UserError(
                        _("Cloudflare Access teardown failed for route(s): %s. The "
                          "route was kept so the deletion can be retried; its Access "
                          "app is still live in Cloudflare.") % (failed,)
                    )
                # pass 2：拆失敗 ⇒ 落帳照常刪除（保住列救不了它，沒有 sync 會再跑）
                for route_id, info in teardown_by_route.items():
                    if info['retry_anchor_is_row']:
                        continue
                    if _push(route_id, info):
                        continue
                    Reap._record_teardown(
                        self._name, route_id, info['app_id'], info['policy_ids'])
            # D1: deleting the last route drops the SA back to
            # 'active_no_route'。
            #
            # ⚠ #794 二輪覆核 [LOW]：這一行**收進 `try` 裡**（位置仍在 savepoint
            # 之外，與 develop 相同，所以既有語意不變）。理由是它落在 savepoint
            # 已釋放、而 CF 那半早已變更之後——留在 `try` 外面的話，它自己拋例外
            # 時整筆交易照樣被框架丟棄，而本腿的補償**收不到**，就成了同一個缺口
            # 的第二個入口。
            affected.exists()._recompute_route_state()
        except Exception:
            # 補償永遠不 raise、也不吞掉原本的錯誤（見 helper 的契約）。
            Reap._compensate_fail_open_after_rollback(
                torn_down, _logger,
                '[SA] route unlink (sa=%s)' % (affected.ids,))
            raise
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
            # Send the revocation until the provider acknowledged it; the
            # hold that follows must not re-revoke on every later sync.
            'revoke_tokens': bool(
                self.access_app_id and self.access_token_revoke_pending
                and not self.access_token_revoke_accepted_at),
        }

    def _mark_access_token_revoke_pending(self):
        existing = self.filtered('access_app_id')
        if existing:
            # A new authorization change starts a new revocation generation:
            # forget any earlier acknowledgement so the hold restarts.
            _internal_identity(existing).write({
                'access_token_revoke_pending': True,
                'access_publish_pending': True,
                'access_token_revoke_accepted_at': False,
            })

    def _access_revoke_gate(self, result):
        """Settle this sync's view of the token-revocation hold.

        Returns ``none`` | ``held`` | ``releasable``.  The first receipt that
        shows the provider's acknowledgement starts the hold and wakes the
        resync cron for when it ends.  Clearing the markers is left to the
        caller and happens only after the origin was actually re-published.
        """
        self.ensure_one()
        if not self.access_token_revoke_pending:
            return 'none'
        if not self.access_app_id:
            # The app was torn down (protection turned off or lost): its tokens
            # are moot, so there is nothing left to revoke or to wait for.
            return 'releasable'
        now = fields.Datetime.now()
        accepted_at = self.access_token_revoke_accepted_at
        hold = access_revoke_hold_seconds(self.env)
        if not accepted_at:
            if access_revoke_provider_accepted(result):
                accepted_at = now
                _internal_identity(self).access_token_revoke_accepted_at = accepted_at
                schedule_access_resync(
                    self.env, accepted_at + timedelta(seconds=hold + 5))
            elif not access_revoke_operator_reports(result):
                _logger.error(
                    '[ACCESS] %s %s: the operator did not report token_revocation, so '
                    'the revocation cannot be acknowledged and the origin stays closed. '
                    'The operator predates the revocation contract; deploy it before '
                    'this module.', self._name, self.id)
            elif result.get('token_revocation') == 'failed':
                _logger.error(
                    '[ACCESS] %s %s: the provider did not accept the token '
                    'revocation (%s); the origin stays closed and it is retried.',
                    self._name, self.id,
                    result.get('token_revocation_error') or 'no detail')
        state = access_revoke_state(True, accepted_at, now, hold)
        return 'releasable' if state == 'releasable' else 'held'

    def _access_revoke_finish(self):
        """Close the revocation generation after a confirmed re-publication."""
        self.ensure_one()
        if self.access_token_revoke_pending or self.access_token_revoke_accepted_at:
            _internal_identity(self).write({
                'access_token_revoke_pending': False,
                'access_token_revoke_accepted_at': False,
            })

    def _sync_access_to_operator(self, release_control=None):
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
                'Security Access route %s (%s, sa=%s)' % (
                    self.id, self.subdomain_prefix, sa.id if sa else None),
                skip)
            return False
        # Direct compensation/cron repair must park a missing/lost Access app
        # before attempting reconstruction. Lifecycle force callers parked the
        # full tunnel first and release it only after all routes succeed.
        if (self.is_protected and
                (not self.access_app_id or self.access_protection_lost or self.access_publish_pending or self.access_token_revoke_pending) and
                not _is_sync_control(self.env, _CTX_FORCE_ACCESS_SYNC)):
            sa._sync_routes_to_operator()
        recovering_pending = self.access_publish_pending
        needs_release = recovering_pending or (self.is_protected and (not self.access_app_id or self.access_protection_lost))
        client = sa._get_operator_client()
        payload = self._build_access_payload()
        try:
            result = client.sync_access_app(payload)
            require_access_sync_receipt(result, payload)
        except (PaaSOperatorError, AccessProtectionError,
                AccessPublicationError) as error:
            if not self.access_token_revoke_pending:
                raise
            # The origin was closed on purpose and the revocation is saved.
            # Raising would roll the markers back while the edge stays parked,
            # leaving a closed origin with no record for the cron to recover.
            _internal_identity(self).access_publish_pending = True
            _logger.error(
                '[SA] Access sync failed while a token revocation is open '
                'route=%s: %s; the origin stays closed and the resync cron '
                'retries', self.id, error)
            return False
        if self.is_protected and not result.get('app_id'):
            raise AccessProtectionError(_(
                "Access protection was not established. Origin publication "
                "is not confirmed; fix the login configuration and retry."))
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
        # #794：推成功了 ⇒ CF 上的狀態就是這一列宣稱的狀態，fail-open 旗標的
        # 前提（app 被拆掉但列還在）已經不成立。`enabled=False` 那一趟同樣清——
        # 那時列自己也不再宣稱保護。**旗標由 sync 結果驅動**，與
        # `access_unresolved_idp_types` 同一個形狀。
        if self.access_protection_lost:
            self.access_protection_lost = False
        # map returned policy ids back onto policy rows in (sequence,id) order
        returned = result.get('policy_ids') or []
        # #798：包 recordset 不是包 record（同一個 env 沿用給每個 singleton）。
        # ⚠ 下面是**兩個不同的迴圈**（對映回存／torn-down 清值），各自要包一次——
        # 「一個方法包一次就好」是最自然的誤讀，漏掉第二個的症狀是 teardown 清不掉。
        policies = _internal_policy_cf_write(self.access_policy_ids).sorted(
            key=lambda x: (x.sequence, x.id))
        for pol, cf_id in zip(policies, returned):
            if pol.cf_policy_id != cf_id:
                pol.cf_policy_id = cf_id
        if not result.get('app_id'):
            # torn down → clear stale cf ids on this route's policies
            for pol in _internal_policy_cf_write(self.access_policy_ids):
                if pol.cf_policy_id:
                    pol.cf_policy_id = False
        # Direct edits/cron can release this route now. Lifecycle convergence
        # uses the private force sentinel and releases all routes only once every
        # Access update succeeded, so a later sibling failure stays fail-closed.
        defer_release = release_control is _DEFER_ACCESS_RELEASE
        # Cloudflare's 2xx only *accepts* a token revocation; it is not proof
        # that an old cookie is denied.  Keep the origin closed through the
        # hold that starts at the provider's acknowledgement, then re-publish
        # (below) only after this sync re-confirmed the Access state.
        revoke_gate = self._access_revoke_gate(result)
        revoke_pending = revoke_gate == 'held'
        _internal_identity(self).access_publish_pending = bool(
            revoke_pending or (defer_release and needs_release))
        if revoke_pending:
            return False
        if defer_release:
            return True
        if needs_release and not _is_sync_control(self.env, _CTX_FORCE_ACCESS_SYNC):
            if recovering_pending and not sa._access_recovery_tunnel_is_live():
                _internal_identity(self).access_publish_pending = True
                _logger.error(
                    '[SA] Access recovery remains pending route=%s: '
                    'tunnel has no live connector', self.id)
                return False
            try:
                sa._sync_routes_to_operator()
            except (PaaSOperatorError, AccessPublicationError) as error:
                _internal_identity(self).access_publish_pending = True
                _logger.error("[SA] Access publication pending route=%s: %s", self.id, error)
                return False
        if revoke_gate == 'releasable':
            self._access_revoke_finish()
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
        return (None if (self.is_protected or self.access_app_id or self.access_publish_pending)
                else 'the route is unprotected and never had an Access app')

    def _access_mark_protection_lost(self):
        """#794：把「這一列的 CF Access app 已經被拆掉、而列本身還在」寫成列上
        的事實。**只由 `cf_access_policy_reap._compensate_fail_open_after_
        rollback` 在獨立交易裡呼叫**（三腿同名，那支 helper 唯一的 per-leg 縫）。

        做三件事，缺一不可：

        1. 清掉 `access_app_id`。它現在指向一顆 CF 上**已經不存在**的 app。
           ⚠ 這不違反 [[two-stage-remote-provision-idempotency]]「每條失敗路徑
           都保住 stored id」——那條保的是「遠端資源還活著、id 是重試錨點」的
           情形；這裡 operator 已經回 2xx 說它拆掉了，留著只會讓下一次 sync 拿
           死 id 去 `PUT /apps/<dead>` 吃 404。清掉之後 payload 的
           `existing_app_id` 是 None，operator 走建立分支 ⇒ 下一次 sync 自癒。
        2. 同理清掉這條 route 底下 policy 的 `cf_policy_id`（同一趟 teardown 一
           起送出去、一起被 CF 刪掉的）。
        3. 打開 `access_protection_lost`。**只清 id 不會關掉安全窗口**：平台沒有
           週期性 Access re-sync，只有 provision / resume / 直接寫
           `is_protected`／`login_*`／`session_duration` 三種事件會推。在那之前
           UI 仍然只讀得到 `is_protected` 這個意圖欄位。這個旗標就是讓「保護沒
           有生效」說得出口的載體，`to_dict()` 有吐。

        刻意**不動** `is_protected`：那是租戶設定的意圖，平台自動降級是產品可見
        變更，issue 自己標為待拍板（#794）。
        """
        self.ensure_one()
        _internal_identity(self).access_app_id = False
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
            # #794 fail-open 訊號：宣稱受保護、但 CF 上的 app 已經被拆掉。
            # 必須吐——不吐的話 UI 只讀得到 is_protected 這個**意圖**欄位，
            # 「以為受保護、實際完全開放」就沒有任何載體說得出口。
            'access_protection_lost': self.access_protection_lost,
            'access_publish_pending': self.access_publish_pending,
            'access_configuration_ready': access_configuration_ready(self),
            'access_policies': [p.to_dict() for p in self.access_policy_ids],
        }
