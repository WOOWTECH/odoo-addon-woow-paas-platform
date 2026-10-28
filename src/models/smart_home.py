from ..services.access_state import AccessProtectionError, AccessPublicationError, require_tunnel_config_receipt
import logging
import re
import time
import uuid
from contextlib import contextmanager, nullcontext

import psycopg2.errors

from odoo import SUPERUSER_ID, _, api, fields, models
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

# ------------------------------------------------------------------------- #
#  RACE-01：ingress 放寬圍籬（advisory lock ＋ 已提交狀態重讀）                #
# ------------------------------------------------------------------------- #
#
# 缺陷的形狀（RACE-01，真雙 PG REPEATABLE READ 交易實測）：
#
#   R（`access_resync_cron._resync_one` → `smart_home_route.
#      _sync_access_to_operator`）在自己的快照裡算出 `blocked=False`，然後進入
#      operator 外呼；外呼期間
#   M（`_do_suspend`）把 ingress 打成 `http_status:503`、寫 `state='suspended'`
#      並**提交**；
#   R 回來之後照著**開頭那個快照**發布 live target ⇒ 外部實際生效的是 live，
#   而 DB 已提交的是 suspended。停權在帳上成立、在現實中被抹掉。
#
# 兩筆交易都不碰同一列（R 只讀 smart_home、M 寫它），REPEATABLE READ 是快照隔離、
# 讀不加鎖也沒有 SSI ⇒ **兩邊都正常提交**，PG 不會回 40001。所以修補不能靠
# 序列化失敗，也不該靠「把其中一方擋掉」：
#
#   🔴 **拒絕 M（停權）不是這個缺陷的修法。** 在 smart_home 列上取
#   `FOR UPDATE NOWAIT` 會讓停權方 fail-closed（`LockNotAvailable` → UserError），
#   停權被推遲到下一輪 cron——而被延後的那一方正是收緊安全的那一方，且原案要求的
#   「兩方都提交且 primary 維持隔離」一次都沒有被證明過。
#
# 本圍籬改成只約束**唯一危險的方向**：
#
#   * **收緊**（target ＝ `http_status:503`）永遠安全、冪等、不需要排序——它是
#     fail-closed 的方向，不可能把別人的隔離解掉。
#   * **放寬**（target ＝ live）是唯一能抹掉別人已提交隔離的動作 ⇒ 它必須
#     (a) 與生命週期寫入者互斥，且 (b) 以**已提交**資料而不是自己的快照裁決。
#
# (a) 用 **transaction advisory lock**（不是列鎖）：advisory lock 不受 MVCC 版本
# 管制，取它不會回 40001，所以兩方都還是能提交。
#
# 🔴 **鎖要取在哪一條 cursor 上，就是它會被握多久**——xact advisory lock 沒有提早
# 釋放的手段，savepoint 回捲也放不掉（實測），只有那筆交易 commit／rollback 才會。
# 所以兩方刻意用**不同壽命**的臨界區：
#
#   * **收緊方**（`_do_suspend`）取在**主 cursor**：503 外呼、`state` 寫入與 commit
#     必須一起落在臨界區內，少一樣都會留下窗口。
#   * **放寬方**（`_sync_access_ingress`）取在 `_ingress_fence()` 開的**獨立短交易**
#     上，離開 `with` 就釋放。臨界區只需要涵蓋「重讀已提交狀態 → 發布」；跟著呼叫端
#     整筆交易的話，`access_resync_cron` 一輪（最多 200 列／腿、全程單一交易、逐列
#     只包 savepoint）會把它推過的每一台的圍籬一路握到整輪結束，期間 `_do_suspend`
#     只能輪詢到逾時 ⇒ 停權被推遲到下一個每日 cron。那就是 .251 候選那個 fail-closed
#     形狀換到收緊方身上。⚠ 「在方法裡晚一點才取鎖」擋不住這件事：縮的是**哪些路徑**
#     會取，不是**取了之後握多久**。
#
# (b) 用**獨立 cursor 重讀已提交狀態**：主交易是 REPEATABLE READ，快照在第一個查詢
# 就定了，之後別的交易 commit 的列**讀不到**（同一個機制在
# `smart_home_access_revision._record_confirmed_state` 的 docstring 裡逐字寫過）。
# 重讀只下 `SELECT`、不取任何列鎖，所以不會與主交易互等。重讀就跑在 (a) 那條持鎖的
# 獨立 cursor 上（一條連線兼兩用），因為**重讀與發布之間不得有窗口**。
#
# 判準刻意只重讀**家層級的生命週期**（`_is_halted()` / `_is_subscription_active()`，
# 沿用既有裁決方法、不複寫邏輯）——route 層的 pending 旗標由呼叫端自己這一筆交易
# 擁有（R 這一輪才剛清掉 `access_protection_lost`），拿已提交值去蓋會往反方向誤判。
#
#: advisory lock 的 classid（兩個 int 的鎖空間與 Odoo core 單一 bigint 的
#: `pg_try_advisory_xact_lock(%s)`（`ir_cron`）不同空間，不可能相撞）。
_INGRESS_FENCE_LOCK_CLASSID = 1465319425
#: 放寬發布等這把鎖的上限。取不到＝另一筆交易正在發布同一台 ⇒ fail-closed
#: （`AccessPublicationError`，呼叫端既有處置是標 `access_publish_pending` 重試）。
_INGRESS_FENCE_LOCK_WAIT_S = 5.0
_INGRESS_FENCE_LOCK_POLL_S = 0.05
#: 已提交狀態重讀的等鎖上限（重讀不取列鎖，這是防 hang 的安全參數；同
#: `smart_home_access_revision._LEDGER_LOCK_TIMEOUT` 的論證）。
_INGRESS_FENCE_READ_LOCK_TIMEOUT = '3s'


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
#     ⚠ 上面那句「只能重指自己那條 tunnel」的前提是「stored `subdomain` 只進
#     `configure_tunnel`（只換 ingress、不碰 DNS）」。開通重試
#     （`_converge_existing_tunnel`）把它送進 `sync_tunnel_routes`，而 operator 的
#     `reconcile_tunnel_dns` 會替清單上**每一個**名字在平台共用 zone 上建 CNAME
#     ⇒ 前提在那條路上不成立。那條路因此不信任 stored 值，送出前先過
#     `_platform_subdomain_error`（平台命名形狀＋本 workspace 的 ws_hash 段），
#     見該函式。
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
                _("Field '%s' is immutable after creation.") % field
            )


def _platform_subdomain_error(subdomain, ws_hash):
    """stored `subdomain` 是不是**平台自己產生的那種**名字；不是就回錯誤字串，是回 None。

    為什麼需要（開通重試的 HIGH）：`subdomain` 對租戶可寫——ACL
    `access_smart_home_user` 給 `base.group_user` write=1、
    `smart_home_rule_owner_admin_manage` 讓 owner/admin 寫自己那一列，`write()`
    只擋 `_IDENTITY_ANCHOR_FIELDS`，欄位本身也沒有任何格式 constraint。修前 stored
    值只會進 `configure_tunnel`（只換 ingress），所以那是「只能重指自己」的次級
    問題；`_converge_existing_tunnel` 把它送進 `sync_tunnel_routes` 之後，operator
    會替它在**平台共用 zone** 上建 proxied CNAME（`reconcile_tunnel_dns` 對清單上
    缺 record 的名字一律 `create_dns_record_for_tunnel`；#824 只擋「重指到仍活著的
    tunnel」，沒有 record 的名字直接建）。修前實際可達：owner 寫
    `{'subdomain': 'login', 'state': 'error'}` 再按 Retry setup ⇒ `login.<zone>`
    （或 `*`、多 label 的名字）指到一條由租戶自己跑 cloudflared 的 tunnel，拿到
    任意未註冊的平台 hostname 與上層網域的 cookie。

    判準刻意是「平台命名形狀」而不只是「合法的單一 DNS label」：單一 label 仍然
    擋不住 `login`／`admin`／`paas-cs-<別人的 ws_hash>-…`。要求
    `paas-sm-<本 workspace 的 ws_hash>-<8 hex>`（`naming.make_smarthome_subdomain`
    產出的形狀）之後，租戶能送出的名字只剩**自己 workspace 命名空間**裡的 SH
    名字——`ws_hash` 是伺服器產生、DB `UNIQUE`（#677），別人的命名空間與 CS／SA
    的 `paas-cs-`／`paas-sa-` 前綴都進不來。

    * 不拿 `_generate_subdomain()` 重算來逐字比對：`name` 可改，重算值會跟著變，
      而既有網址不可變（docstring 見 `_converge_existing_tunnel` 的 subdomain 段）。
    * 公開 subdomain 依 DP2 不帶 env token；`stg-` 段仍放行，只為不在形狀上
      誤殺早期的 stg 列——它依然被釘在同一個 ws_hash 命名空間裡，放寬不跨租戶。
    * 既有列（#677 以前產生的）的 ws_hash 段 = 回填的 `md5(slug)[:8]`，與
      stored `ws_hash` 逐字相同（18.0.1.0.131 migration Step 3），所以合法舊列
      過得去；過不去的列一律 fail-closed（拒絕重試、請刪除重建），不猜、不重算。
    """
    if not isinstance(ws_hash, str) or not re.fullmatch(r'[0-9a-f]{8}', ws_hash):
        return 'workspace has no valid ws_hash'
    pattern = r'paas-(?:stg-)?sm-%s-[0-9a-f]{8}' % re.escape(ws_hash)
    if not isinstance(subdomain, str) or not re.fullmatch(pattern, subdomain):
        return 'stored subdomain %r is not a platform-issued name' % (subdomain,)
    return None


#: 已有 tunnel 時仍允許「重試開通」的 state（見 `action_provision` 的 guard 段）。
#: 刻意是白名單：新加的 state 預設**不能**重推 ingress，要有人想過才加進來——
#: 例如 `suspended` 若誤入這份清單，重試會把 503 的 ingress 推回 localhost（繞過停權）。
_RETRYABLE_WITH_TUNNEL_STATES = ('pending', 'error')


class SmartHome(models.Model):
    _name = 'woow_paas_platform.smart_home'
    _inherit = [
        'woow_paas_platform.subscription_lifecycle_mixin',
        # #1766：一台 HA 一條 tunnel（claim／release／第二台偵測）。
        'woow_paas_platform.tunnel.binding.mixin',
    ]
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
                'Smart Home instances can only be created by the platform '
                'provisioning flow (%(model)s). Please use the Smart Home '
                'ordering flow instead of writing to this model directly.',
                model=self._name))
        _strip_identity_anchors(self.env, vals_list)
        records = super().create(vals_list)
        # 落地值的 `subscription_id`（`ir.default`／context default 注入）也要過
        # 綁定判準——見 mixin 的 `_guard_tenant_lifecycle_create`。
        records._guard_tenant_lifecycle_create()
        return records

    def write(self, vals):
        """#524：身分錨點的 write 側 —— 見模組頂端 `_IDENTITY_ANCHOR_FIELDS`。

        另加租戶直打 ORM 的生命週期守衛（停權逃逸、`subscription_id` 改綁／
        清空）：owner 把 `subscription_id` 清空再改 state 為 `error`，就能經
        `call_kw` 的 `action_provision` → `_converge_existing_tunnel` 把 ingress
        推回 localhost（那裡的訂閱檢查對「沒有訂閱」放行），而 cron 從此摸不到
        這一台。理由與豁免見 `subscription_lifecycle_mixin` 模組頂端。
        """
        if 'subdomain' in vals:
            for home in self:
                if (vals['subdomain'] != home.subdomain and
                        any(route.is_protected for route in
                            home.sudo().route_ids.filtered('is_primary'))):
                    raise UserError(_(
                        "Protected Smart Home hostnames cannot be changed. "
                        "Create and verify a replacement before deleting this one."))
        _reject_identity_anchor_writes(self.env, vals)
        self._guard_tenant_lifecycle_write(vals)
        return super().write(vals)

    def _get_operator_client(self):
        """Get PaaS Operator client instance."""
        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(
                _("PaaS Operator is not configured. "
                  "Please set Operator URL and API Key in Settings → Woow PaaS.")
            )
        return client

    def _naming_ws_hash(self):
        """Workspace 的 **stored** namespace 身分碼，Cloudflare 命名的 hash 種子。

        #677 之前這裡回的是 `workspace.slug`，未撞名時 slug 由租戶提供的 `name`
        逐字決定，namespace 是它的 `md5[:8]`（32-bit 截斷）⇒ 租戶可以讓自己的
        子網域落在別人的 ws_hash 段上。現在回 stored `ws_hash`（伺服器產生、DB
        `UNIQUE`）；#1516 起 slug 撞名會加隨機後綴，仍不參與 namespace 推導。

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

    def _ingress_fence_lock(self, cr):
        """在 ``cr`` 上取本台 SH 的 ingress 放寬圍籬鎖（transaction advisory lock）。

        取不到就在上限內重試，逾時 raise ``AccessPublicationError``。

        🔴 **鎖的壽命＝``cr`` 那一筆交易的壽命，不是這個呼叫的壽命**。xact advisory
        lock 沒有提早釋放的手段：savepoint 回捲**不會**放掉它（實測
        `B_still_blocked_after_A_savepoint_rollback=true`），只有 commit／rollback 會。
        所以「要鎖多久」完全由呼叫端挑哪一條 cursor 決定，不是由「在方法裡晚一點才取」
        決定：
        * **收緊方**（:meth:`_do_suspend`）必須用**主 cursor**——503 外呼、`state` 寫入
          與 commit 三件事要一起落在臨界區內。
        * **放寬方**（:meth:`_sync_access_ingress`）用 :meth:`_ingress_fence` 開的
          **獨立短交易**：臨界區只需要涵蓋「重讀已提交狀態 → 發布 live」，不需要涵蓋
          呼叫端剩下的交易。用主 cursor 的話，resync cron（一輪最多 200 列、全程單一
          交易、逐列只包 savepoint）會把每一台的圍籬一路握到整輪結束，期間任何
          `_do_suspend` 都只能輪詢到逾時＝停權被推遲一整個生命週期 cron 週期。

        🔴 **為什麼是 advisory lock 而不是列鎖**：兩筆交易都是 REPEATABLE READ，
        在 smart_home 列上取 `FOR UPDATE`／`FOR SHARE` 時，若另一筆交易已經改過
        那一列並提交，PG 直接回 ``40001``（serialization failure）＝其中一方的交易
        被打掉；`NOWAIT` 則是回 ``LockNotAvailable`` ＝其中一方 fail-closed。
        兩種都達不到「兩方都提交、且 primary 維持隔離」。advisory lock 不受 MVCC
        版本管制，純粹排序臨界區，兩邊的交易都還能正常提交。

        交易層（`_xact_`）而不是 session 層：commit／rollback 自動釋放，不會把鎖
        漏在連線池裡的連線上。同一筆交易重複取是安全的（PG 計數，無需 unlock）。

        ⚠ 等鎖用 `pg_try_…` 輪詢而不是會阻塞的 `pg_advisory_xact_lock`：阻塞版受
        `lock_timeout` 影響（呼叫端可能設過，也可能沒設），逾時行為會隨環境漂移；
        輪詢版的上限寫在這裡，與呼叫端的 session 設定無關。
        """
        self.ensure_one()
        deadline = time.monotonic() + _INGRESS_FENCE_LOCK_WAIT_S
        while True:
            cr.execute(
                'SELECT pg_try_advisory_xact_lock(%s, %s)',
                (_INGRESS_FENCE_LOCK_CLASSID, self.id))
            if cr.fetchone()[0]:
                return
            if time.monotonic() >= deadline:
                _logger.warning(
                    "[SH] ingress fence lock busy sh=%s after %ss — refusing to "
                    "publish a live origin without a valid fence",
                    self.id, _INGRESS_FENCE_LOCK_WAIT_S)
                raise AccessPublicationError(_(
                    'Another lifecycle operation is publishing this Smart '
                    'Home origin right now. Retry this operation.'))
            time.sleep(_INGRESS_FENCE_LOCK_POLL_S)

    def _ingress_fence_held_by_main_cursor(self):
        """主交易自己是不是已經持有本台的圍籬鎖。

        PG 對**同一筆交易**重複取同一把 advisory lock 是放行的（實測
        `A_reentrant=true`），所以舊版（放寬方也在主 cursor 上取）天生容許
        「同一筆交易先收緊、之後再走到放寬發布」。:meth:`_ingress_fence` 改成在
        **另一條連線**上取同一把鎖之後，那個形狀會變成跟自己互斥 ⇒ 必然輪詢到逾時。
        這裡把那個語意補回來：自己已經持有＝臨界區已經成立，不必也不該再取一次。

        判準是 `pg_locks` ＋ ``pid = pg_backend_pid()``（實測持有者看到 1、非持有者
        看到 0）。``objsubid`` 刻意不入條件（兩個 int 的鎖是 2、單一 bigint 的是 1），
        ``classid``／``objid`` 已經把鎖空間分開了。
        """
        self.ensure_one()
        self.env.cr.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND classid = %s AND objid = %s AND pid = pg_backend_pid() "
            "AND granted",
            (_INGRESS_FENCE_LOCK_CLASSID, self.id))
        return bool(self.env.cr.fetchone()[0])

    @contextmanager
    def _ingress_fence(self):
        """放寬發布的圍籬臨界區；``yield`` 出重讀已提交狀態要用的 cursor。

        臨界區只涵蓋「重讀已提交狀態 → 發布 ingress」這一段，離開 ``with`` 就釋放，
        **不**跟著呼叫端剩下的交易。做法是把鎖取在一條**獨立的短交易**上：
        xact advisory lock 只在它自己那筆交易結束時釋放，所以換一條 cursor 就等於換
        一個鎖的壽命（實測 `A_acquires_after_C_scope_closed=true`）。

        🔴 **為什麼不能取在主 cursor 上**（舊版的形狀）：xact lock 沒有提早釋放的手段
        ——savepoint 回捲放不掉它。於是 `access_resync_cron._cron_resync_access_
        protection`（一輪最多 200 列／腿、全程**單一交易**、逐列只包 savepoint，見該檔）
        會把它推過的**每一台**的圍籬一路握到整輪結束；連那些 savepoint 被回捲掉的列
        也一樣握著。期間 `subscription_lifecycle_cron` 對同一台呼
        :meth:`_do_suspend` 只能輪詢到逾時 ⇒ 停權被推遲到**下一個每日 cron**。那正是
        本圍籬聲稱要避開的 fail-closed 形狀（`.251` 列鎖候選），只是換到收緊方身上。
        「在方法裡晚一點才取鎖」擋不住這件事：縮的是**哪些路徑**會取，不是**取了之後
        握多久**。

        同一條 cursor 同時當鎖的持有者與重讀者是刻意的：重讀本來就需要一個獨立快照
        （主交易是 REPEATABLE READ），所以這裡用掉的連線數與舊版相同（1 條），只是
        它的壽命從「重讀完就關」延長到「發布完才關」——而那正是臨界區該有的長度。

        ⚠ 連線被多握住一次 operator 外呼的時間。`db_maxconn` 緊的部署下，並發的放寬
        發布數會是額外連線數的上界（每個 worker 最多 1 條）。
        """
        self.ensure_one()
        if self._ingress_fence_held_by_main_cursor():
            # 主交易已持有（例如同一筆交易剛跑過 `_do_suspend`）⇒ 已互斥。
            yield None
            return
        with self.env.registry.cursor() as fence_cr:
            fence_cr.execute('SET LOCAL lock_timeout = %s',
                             (_INGRESS_FENCE_READ_LOCK_TIMEOUT,))
            self._ingress_fence_lock(fence_cr)
            yield fence_cr

    def _ingress_committed_halt_reason(self, allow_halted=False, cr=None):
        """以**已提交**資料回答「這台現在該不該被擋住」；不該擋回 ``None``。

        在獨立 cursor（＝獨立交易、新快照）裡讀，因為主交易是 REPEATABLE READ：
        快照在交易第一個查詢就定了，之後別的交易提交的 state／訂閱**讀不到**，而
        RACE-01 的危險恰恰是「別人在我外呼期間提交了停權」。

        只下 `SELECT`、不取任何列鎖 ⇒ 不會與主交易互等（主交易若正持有這一列的
        寫鎖，MVCC 讀者也不會被阻塞）。判準沿用既有的 `_is_halted()` /
        `_is_subscription_active()` 兩個裁決方法，**不**在這裡複寫條件——複寫就是
        第二個真相來源，而兩者分歧時沒有任何訊號。

        ⚠ 刻意**不**重讀 route 層的 pending 旗標：那些由呼叫端自己這一筆未提交的
        交易擁有（R 這一輪才剛清掉 `access_protection_lost`），拿已提交值會往反
        方向誤判成「要擋」。家層級生命週期才是別人的權責。

        🔴 **「已提交快照裡沒有這一列」有兩個完全不同的成因，必須分開**：

        a. **本交易（含子交易）自己還沒提交的 INSERT**——provision 路徑在同一筆交易
           裡建好 home 就發布，`TransactionCase` 測試也是這個形狀。列還沒提交 ⇒
           不可能有任何**已提交**的隔離存在 ⇒ 沒有東西需要被保護 ⇒ **不擋**。
        b. **已提交的 DELETE**（別的交易刪掉並提交，而本交易的 REPEATABLE READ 快照
           比那次刪除舊，所以本交易還看得到它）。home 正在消失 ⇒ **擋**。

        兩個方向都踩過：
        * 「absent 就擋」是實測的偽陽性——AC-1011 系列 `access_publish_pending`
          契約共 31 個斷言整組反轉（證據 `adjacent-green-attempt01-31-regressions.log`）。
        * 「absent 就放行」會把 (b) 一起放掉，等於沒有圍籬。

        判準是 **xmin 那筆交易現在的 commit status**：本交易的快照看得到這一列、
        fresh cursor 看不到、而且 ``xmin`` 的狀態是 **in progress** ⇒ 成因 (a)。
        推論依據：另一筆**還在進行中**的交易所插入的列，本交易的快照本來就看不到；
        既然看得到又還沒提交，那筆交易只能是本交易自己（top 或任一子交易）。
        狀態是 ``committed``／``aborted``／問不出來 ⇒ 一律當成 (b) 並 park（fail-closed）。

        🔴 **為什麼不比 xid 相等**：Odoo 幾乎所有寫入都包在 savepoint（子交易）裡，
        列的 ``xmin`` 是**子交易 xid**，而 ``pg_current_xact_id_if_assigned()`` 依
        PostgreSQL 文件在子交易中回的是 **top-level xid** ⇒ 等式恆不成立。實測
        （真 PG 15.19）：``xmin=41131``、top xid ``41130``、兩種轉型下的等式都是
        ``false``，而 ``pg_xact_status`` 對那個子交易 xid 回 ``in progress``；對照
        組（已提交的列被別的交易刪掉並提交）回 ``committed``。證據
        `pg-probe.log` / `pg-probe2.log`。

        🔴 **也不用 `pg_visible_in_snapshot`**：文件明示它對子交易 xid 不正確。

        ⚠ 成因 (a) 只在 **fresh cursor 讀不到**時才走得到。列若早已提交、本交易只是
        UPDATE 過它，fresh cursor 看到的是**舊的已提交版本** ⇒ 走上面正常的重讀分支，
        不會因為當前版本的 ``xmin`` 是自己的就跳過圍籬。
        """
        self.ensure_one()
        # ``cr`` 是 :meth:`_ingress_fence` 已經開好、已經持有圍籬鎖的那一條（重讀
        # 必須在鎖內，否則重讀與發布之間又有窗口）。沒帶就自己開一條（既有呼叫端與
        # 直接單測走這一格），語意相同、只是沒有圍籬。
        own = cr is None
        with (self.env.registry.cursor() if own else nullcontext(cr)) as read_cr:
            if own:
                read_cr.execute('SET LOCAL lock_timeout = %s',
                                (_INGRESS_FENCE_READ_LOCK_TIMEOUT,))
            committed = api.Environment(read_cr, SUPERUSER_ID, {})[
                self._name].browse(self.id)
            if committed.exists():
                if committed._is_halted() and not allow_halted:
                    return 'committed state=%r is halted' % committed.state
                if not committed._is_subscription_active():
                    return 'the committed subscription is not active'
                return None
        status = self._row_xmin_commit_status()
        if status == 'in progress':
            return None
        return ('the smart home row is absent from the committed snapshot and '
                'its creating transaction is %s' % (status or 'unknown'))

    def _row_xmin_commit_status(self):
        """本交易快照裡這一列 ``xmin`` 那筆交易的 commit status；問不出來回 ``None``。

        回 ``'in progress'`` / ``'committed'`` / ``'aborted'`` / ``None``。語意與用途
        見 :meth:`_ingress_committed_halt_reason`，這裡只講兩個機制細節。

        **epoch 安全的 xid32 → xid8 還原**：``xmin`` 是 32-bit ``xid``（會回捲），
        ``pg_xact_status`` 吃的是 64-bit ``xid8``。直接 ``xmin::text::xid8`` 只在
        epoch 0 正確。這裡改用 PostgreSQL 自己比較 xid 的同一套模數算術：取快照的
        ``pg_snapshot_xmax()``（``xid8``、不回捲）當基準 ``c8``，算
        ``dist = (xmin - low32(c8)) mod 2^32``；``dist < 2^31`` 表示 ``xmin`` 在 ``c8``
        之後（子交易正是這一格）⇒ ``c8 + dist``，否則 ⇒ ``c8 - (2^32 - dist)``。
        實測與 epoch 0 的裸轉型結果一致（``dist=1``、兩者都還原出 ``41139``、狀態同為
        ``in progress``），而跨 epoch 時裸轉型會錯、這個形式不會。

        **包在 savepoint 裡**：``pg_xact_status`` 對已被 clog 截斷的古老 xid 會
        raise，而在主交易上 raise 會把整筆交易打成 aborted。savepoint ＋
        ``except psycopg2.Error`` 讓它退化成「問不出來」（呼叫端據此 park），不會把
        呼叫端的交易連帶弄死。

        ⚠ 權限：``pg_xact_status`` 的 ``proacl`` 預設是 NULL（＝EXECUTE 給 PUBLIC）；
        實測 PG 15.19 上一個**非** superuser 的 role 呼叫得動。正式環境是 PG 17.11，
        本批無法進正式 pod 實測（自動審查拒絕），所以部署前要確認那個 role 也呼叫得動
        ——問不出來只會 fail-closed（park），不會把隔離放掉，但會讓 provision 的放寬
        發布被延後一輪。
        """
        self.ensure_one()
        query = (
            'WITH c AS (SELECT pg_snapshot_xmax(pg_current_snapshot())'
            '                  ::text::numeric AS c8), '
            '     r AS (SELECT t.xmin::text::numeric AS x '
            '             FROM %s t WHERE t.id = %%s), '
            '     d AS (SELECT c8, x, '
            # 這四個百分號是刻意的：`% self._table` 把 `%%%%` 收成 `%%`，psycopg2
            # 再把 `%%` 收成 `%`，所以送到 PG 的才是 SQL 的模數運算子。寫成 `%%`
            # 會讓 psycopg2 把它當成參數佔位符（實測 IndexError）。
            '                  ((x - (c8 %%%% 4294967296) + 4294967296) '
            '                   %%%% 4294967296) AS dist FROM c, r) '
            'SELECT pg_xact_status(trunc(CASE WHEN dist < 2147483648 '
            '                                 THEN c8 + dist '
            '                                 ELSE c8 - (4294967296 - dist) '
            '                            END)::bigint::text::xid8)::text '
            'FROM d' % self._table)
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(query, (self.id,))
                row = self.env.cr.fetchone()
        except psycopg2.Error as error:
            _logger.warning(
                "[SH] could not read the xmin commit status for sh=%s: %s — "
                "treating it as unknown (the ingress fence will park)",
                self.id, error)
            return None
        return row[0] if row else None

    def _sync_access_ingress(self, park=False, service_url=None,
                             allow_halted=False):
        """Keep the current single primary hostname blocked until protection exists.

        Smart Home does not yet support non-primary tunnel ingress. Use the
        authoritative primary route, including when record rules hide the row.
        API acknowledgement alone does not prove connector propagation.

        RACE-01：**放寬**（算出 live target）必須整段待在 ingress 圍籬裡——取
        advisory lock 與生命週期寫入者互斥、以已提交狀態重判一次、然後才發布，
        三件事在同一個臨界區內。**收緊**（503）不過圍籬：它是 fail-closed 的方向，
        冪等且不可能抹掉別人的隔離。理由與機制全文見本檔開頭 §RACE-01。
        """
        self.ensure_one()
        if not self.tunnel_id:
            return
        primary = self.sudo().route_ids.filtered('is_primary')
        blocked = (park or (self._is_halted() and not allow_halted)
                   or not self._is_subscription_active()) or any(
            route.access_publish_pending or
            (route.is_protected and (not route.access_app_id or
                                    route.access_protection_lost))
            for route in primary)
        if blocked:
            # 收緊不過圍籬，也不佔鎖——否則 resync cron 的長交易會把每一台的圍籬
            # 握到整輪結束（見 :meth:`_ingress_fence`）。
            self._publish_ingress(True, service_url=service_url)
            return
        with self._ingress_fence() as fence_cr:
            halted = self._ingress_committed_halt_reason(
                allow_halted=allow_halted, cr=fence_cr)
            if halted:
                # 已提交的生命週期說這台該被擋住 ⇒ 發布 503 而不是 live。這與
                # 停權方剛剛推出去的 target 相同（冪等），所以不是「再推一次」而是
                # 「不要把它推回去」。出聲是刻意的：這一行代表真的有一次延遲發布
                # 被攔下來，與「這台本來就停權」在輸出上要分得開。
                _logger.warning(
                    "[SH] ingress fence kept sh=%s parked instead of "
                    "publishing a live origin: %s", self.id, halted)
            # 發布**在圍籬內**：重讀與發布之間不得有窗口，否則收緊方可以剛好在
            # 這兩步之間提交，而本次發布照舊把 live 推出去。
            self._publish_ingress(bool(halted), service_url=service_url)

    def _publish_ingress(self, blocked, service_url=None):
        """把這台的 ingress target 推給 operator——本腿**唯一**的 ingress 發布處。

        拆出來只為了讓收緊（圍籬外）與放寬（圍籬內）共用同一份發布碼：兩份平行的
        發布碼會漂，而漂掉的那一份讀起來與權威版本一模一樣。
        """
        self.ensure_one()
        target = ('http_status:503' if blocked else
                  service_url or self.original_service_url or
                  f"http://localhost:{self.ha_port}")
        hostname = f"{self.subdomain}.{self._get_paas_domain()}"
        result = self._get_operator_client().configure_tunnel(
            self.tunnel_id, hostname=hostname,
            service_url=target, tunnel_name=self.tunnel_name)
        require_tunnel_config_receipt(result, hostname, target)

    def _sync_all_access_then_publish(self, service_url=None,
                                      allow_halted=False):
        """Lifecycle caller has parked ingress; publish only after every sync."""
        self.ensure_one()
        for route in self.sudo().route_ids:
            _sm_forced_access_sync(route)._sync_access_to_operator()
        try:
            self._sync_access_ingress(
                service_url=service_url, allow_halted=allow_halted)
        except (PaaSOperatorError, AccessPublicationError):
            for route in self.sudo().route_ids:
                _internal_sm_route_identity(route).access_publish_pending = True
            raise

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
        if self.tunnel_id:
            # Stop before deleting Access if the protected origin cannot park.
            self._sync_access_ingress(park=True)
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

    def _try_lock_lifecycle_rows(self):
        """RACE-09: NOWAIT row lock before any external lifecycle side effect.

        ORM writes stay buffered while an external request is in flight, so
        ``state='provisioning'`` alone does not serialize a concurrent delete.
        """
        ids = tuple(sorted(set(self.ids)))
        if not ids:
            return True
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    'SELECT id FROM %s WHERE id IN %%s '
                    'ORDER BY id FOR UPDATE NOWAIT' % self._table, [ids])
                locked = {row[0] for row in self.env.cr.fetchall()}
        except (psycopg2.errors.LockNotAvailable,
                psycopg2.errors.SerializationFailure):
            return False
        if locked != set(ids):
            raise UserError(_('One or more Smart Home records no longer exist.'))
        fields_ = ['state', 'error_message', 'tunnel_id', 'tunnel_name']
        self.flush_recordset(fields_)
        self.invalidate_recordset(fields_)
        return True

    def _lock_lifecycle_or_raise(self):
        if not self._try_lock_lifecycle_rows():
            raise UserError(_(
                'Another Smart Home lifecycle operation is in progress. '
                'Please retry when it finishes.'))

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

        **guard 分支從 fail-loud 改成收斂（Smart Home 開通重試）**：這道 guard 原本在
        ``tunnel_id`` 已存在時一律 ``raise``，理由是「operator 只有 ``create_tunnel`` 與
        routes sync 兩處會建 DNS record，前者會建**新** tunnel，後者需要 SmartHome 尚未
        建模的 route payload」。後半句的前提其實不需要 route 模型就成立：SmartHome 的
        desired state 恰好**一條** hostname，而它的每一格都已經在 DB 上——``subdomain``
        與 tunnel 同一次 write 落地（``dns_failed`` 分支也一樣），``service_url`` 是
        ``http://localhost:<ha_port>``（與 ``_do_resume`` 同一個來源）。所以
        ``sync_tunnel_routes`` 帶一條 route 就能把 ingress ＋ DNS 宣告式推回去，與 SA
        guard 分支（``_sync_routes_to_operator``）同一個形狀。修前的實際後果是：租戶在
        ``error`` 卡住之後**沒有任何自助出路**（詳情頁沒有重試鈕、後端重打只會被拒），
        只能刪掉重建或等人工補 DNS。

        ⇒ 現在 guard 分三格（判準仍是 stored ``tunnel_id``，不是 ``state``）：
        - 沒有 ``tunnel_id`` ⇒ 原本的完整開通（``create_tunnel``）。
        - 有 ``tunnel_id`` 且 ``state`` ∈ ``_RETRYABLE_WITH_TUNNEL_STATES`` ⇒
          ``_converge_existing_tunnel()``：**絕不**再呼叫 ``create_tunnel``，只重推既有
          tunnel 的 ingress／DNS、補 primary route，成功落 ``active``，失敗維持
          ``error`` ＋ ``error_message``（tunnel 錨點原封不動，下一次重試照樣走這格）。
          訂閱不在 ``in_progress`` 時這格也拒絕（理由見該方法：``_do_suspend`` 失敗會
          留下「error ＋ 訂閱已停」）。
        - 有 ``tunnel_id`` 但其他 state（``active``／``provisioning``／``suspended``／
          ``deleting``）⇒ 維持 fail-loud。``active`` 沒有東西要修；``provisioning`` 是
          另一個請求正在跑（雙擊／兩個分頁），兩邊同時推 ingress 沒有意義；
          ``suspended`` 的 ingress 刻意是 ``http_status:503``，在這裡推回
          ``localhost`` 等於繞過停權——那條路歸 ``_do_resume``。

        順帶：``/api/smart-homes`` 的 ``action='provision'`` **沒有 state 閘**（只驗
        OWNER/ADMIN ＋ 訂閱），所以對一個已經 ``active`` 的 Smart Home 再打一次
        provision，修前同樣會建第二條 tunnel 並蓋掉 ``tunnel_id``。同一道 guard 一併關掉。
        """
        self.ensure_one()
        self._lock_lifecycle_or_raise()

        # stored-id guard —— 必須在任何 write 之前（含 state='provisioning'），
        # 否則被拒的重試會把記錄推離原本的 state 卻什麼也沒做成。
        if self.tunnel_id:
            if self.state in _RETRYABLE_WITH_TUNNEL_STATES:
                # 已有 tunnel ⇒ 收斂，不重建（見 docstring「guard 分支」段）。
                return self._converge_existing_tunnel()
            raise UserError(
                _("This Smart Home already has a Cloudflare tunnel (%(tunnel)s) "
                  "and is in state '%(state)s', so it cannot be provisioned "
                  "again. Re-provisioning would create a second, separately "
                  "billed tunnel and leave this one orphaned with no record "
                  "pointing at it. Setup can only be retried while the Smart "
                  "Home is pending or in error.",
                  tunnel=self.tunnel_id, state=self.state)
            )

        client = self._get_operator_client()

        # 首次開通一定會寫 `tunnel_token`（`groups='base.group_system'`）。非平台
        # 身分（`/web/dataset/call_kw` 直打本 public method，不經 controller 的
        # sudo）在那次寫入必然 AccessError——但那時 `create_tunnel` 已經在
        # Cloudflare 開好一條 tunnel，而這一支沒有 SA 那樣的 #822 補償：例外外拋、
        # 交易回捲、DB 沒有任何東西指向它 ⇒ 每打一次就多一條在計費的孤兒。
        # 這裡用 Odoo 自己的欄位遮罩判準把同一個拒絕**提前到任何外部副作用之前**
        # （su 照舊放行，controller 的平台身分路徑與後台按鈕不受影響）。
        self.check_field_access_rights('write', ['tunnel_token'])

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
                service_url="http_status:503",
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
                # 人看得懂、修得動的訊息，而不是一句籠統的失敗；「修得動」的那條
                # 路現在是詳情頁的 Retry setup ⇒ 再打一次 action_provision 會走
                # `_converge_existing_tunnel()` 在**同一條** tunnel 上補 DNS。
                anchored.write(dict(
                    tunnel_vals,
                    state='error',
                    error_message=_(
                        "Tunnel created but its DNS record could not be "
                        "created, so %(hostname)s will not resolve. The "
                        "tunnel (%(tunnel_id)s) is kept so it is not "
                        "orphaned — use Retry setup to re-create the DNS "
                        "record on the same tunnel, or delete and re-create "
                        "this Smart Home.",
                        hostname=hostname,
                        tunnel_id=result.get('tunnel_id'),
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
                state='provisioning',
                deployed_at=fields.Datetime.now(),
            ))
            # #768：Access 錨點載體。subdomain 必須先落地（route 的 hostname 由
            # 它 compute），所以放在 write 之後。冪等，重跑不會長出第二條。
            self._ensure_primary_route()
            self._sync_all_access_then_publish(
                service_url=f"http://localhost:{self.ha_port}")
            self.write({'state': 'active', 'error_message': False})

            _logger.info(
                "Smart Home '%s' provisioned successfully. Tunnel ID: %s",
                self.name, result.get('tunnel_id'),
            )

        except PaaSOperatorConnectionError as e:
            self.write({
                'state': 'error',
                'error_message': _(
                    "Cannot connect to PaaS Operator: %(detail)s",
                    detail=e.message),
            })
            _logger.error("Smart Home provision connection error: %s", e.message)
        except PaaSOperatorError as e:
            self.write({
                'state': 'error',
                'error_message': _(
                    "Tunnel creation failed: %(detail)s", detail=e.message),
            })
            _logger.error("Smart Home provision error: %s", e.message)

        except AccessProtectionError as e:
            self.write({'state': 'error', 'error_message': str(e)})
            _logger.error("Smart Home protection configuration error: %s", e)

    def _converge_existing_tunnel(self):
        """重試開通、而 tunnel 已經存在：在**同一條** tunnel 上把 ingress／DNS 推回去。

        只由 `action_provision` 的 guard 分支呼叫（state 已驗過在
        `_RETRYABLE_WITH_TUNNEL_STATES` 內）。形狀抄 SA 的 retry 分支
        （`SecurityAccess.action_provision` 的 `if self.tunnel_id:`）：先
        `provisioning`、推宣告式 route sync、補 Access、收斂回 `active`；任何失敗都
        落 `error` ＋ `error_message`，**tunnel 錨點一格都不動**——下一次重試照樣走
        這條路，不會變成「再開一條」。

        為什麼是 `sync_tunnel_routes` 而不是 `configure_tunnel`：後者只換 ingress、
        **不碰 DNS**（operator `PUT /tunnels/{id}/config`），而 `dns_failed` 留下的
        缺口正好是 DNS；前者是 ingress 覆寫 ＋ `reconcile_tunnel_dns`（缺的補建、
        指向本 tunnel 但不在清單上的刪掉）。payload 刻意逐格重現 `create_tunnel`
        當初烙進去的那條 ingress：同一個 hostname、同一個
        `http://localhost:<ha_port>`、`http_host_header=False`（`create_tunnel` 走的
        `configure_tunnel` 不帶 `originRequest`）⇒ 對一條本來就好的 ingress 是
        no-op，只有 DNS 缺的那一半會真的變動。

        ⚠ operator 端 hostname 是 `f"{subdomain}.{cloudflare_domain}"`，平台這側的
        `tunnel_route` 用 `paas_domain`。兩者不一致時 `create_tunnel` 的 DNS 分支本來
        就建在 operator 的 domain 上（它從 hostname 抽 subdomain），所以這裡沒有引入
        新前提——與 `create_tunnel` 同一個「兩邊 domain 相同」的部署假設。

        身分錨點：本方法**不寫** `tunnel_id`／`tunnel_name`／`tunnel_token` 任何一格
        （只寫 `state`／`error_message`／`deployed_at`），所以刻意**不**開
        `_internal_identity` 閘——逃生口的表面積只留在真正存 CF id 的那幾次寫入。

        ⛔ stored `subdomain` **不可信**：它對租戶可寫，而本方法會把它變成平台共用
        zone 上的 DNS record（`create_tunnel` 的首次開通用的是伺服器現算的名字，
        沒有這個問題）⇒ 送出前一律過 `_platform_subdomain_error`。`state` 同樣對
        租戶可寫（可以自己把一台 `active` 改成 `error` 來走到這裡）；名字驗過之後，
        這樣做最多只是把**自己**那條已驗證的 hostname 再推一次（宣告式，對好的
        ingress 是 no-op），停權則另有下面的訂閱檢查守著，所以不另外鎖 `state`。

        #531：`tunnel_name` 是歸屬主張（operator 拿它比對 CF 上的實際名字，對不上就
        拒）。`comment` 是 DNS record 上只寫不讀的 traceability stamp，這裡剛好給同一
        個值（SH 的 tunnel 名本身帶 env token，正是反查需要的那個字串），但兩個參數
        的語意仍是分開的，見 `PaaSOperatorClient.sync_tunnel_routes` 的註解。
        """
        self.ensure_one()
        # 前置檢查全部在任何 write 之前：被拒的呼叫不得把記錄推離原本的 state。
        client = self._get_operator_client()
        if not self.subdomain:
            # tunnel 與 subdomain 是同一次 write 落地的（含 dns_failed 分支），走到
            # 這裡代表資料被外力改過——沒有 hostname 就沒有 desired state 可推，
            # 猜一個（例如重新 `_generate_subdomain()`）等於替租戶換網址。
            raise UserError(
                _("This Smart Home has a Cloudflare tunnel (%(tunnel)s) but no "
                  "stored hostname, so its setup cannot be retried "
                  "automatically. Delete this Smart Home (which tears the "
                  "tunnel down) and create it again.",
                  tunnel=self.tunnel_id)
            )

        naming_error = _platform_subdomain_error(
            self.subdomain, self._naming_ws_hash())
        if naming_error:
            # stored `subdomain` 對租戶可寫，而這條路會把它變成平台共用 zone 上的
            # DNS record ⇒ 送出前先驗是平台自己發的名字（理由見
            # `_platform_subdomain_error`）。fail-closed：不猜、不重算（重算＝替
            # 租戶換網址），也不寫任何欄位——被拒的呼叫不得把記錄推離原本的 state。
            _logger.warning(
                "[SH] provision retry refused sh=%s tunnel=%s: %s",
                self.id, self.tunnel_id, naming_error)
            raise UserError(
                _("This Smart Home's stored hostname (%(subdomain)s) is not one "
                  "the platform issued, so its setup cannot be retried "
                  "automatically. Delete this Smart Home (which tears the "
                  "tunnel down) and create it again.",
                  subdomain=self.subdomain)
            )

        if not self._is_subscription_active():
            # 這條路會把 ingress 推回 `http://localhost:<ha_port>`。`_do_suspend` 推
            # 503 失敗時會把記錄落 `error` 而**不是** `suspended`（見該方法），所以
            # 「error ＋ 訂閱已停」是真的會出現的組合——放行就等於租戶按一下重試鈕
            # 繞過停權。controller 的 `_check_operation_allowed` 擋得到大部分，但它
            # 對 admin 一律放行、對「非 in_progress 但不在寬限期判準內」的訂閱也放行，
            # 所以 model 自己再守一次（無訂閱＝legacy／admin 建的記錄，照舊允許）。
            # 復權的正路是 `_do_resume`。
            raise UserError(
                _("This Smart Home's subscription is not active, so its setup "
                  "cannot be retried. Restore the subscription first.")
            )

        self.write({'state': 'provisioning', 'error_message': False})
        hostname = f"{self.subdomain}.{self._get_paas_domain()}"
        try:
            client.sync_tunnel_routes(
                self.tunnel_id,
                routes=[{
                    'subdomain': self.subdomain,
                    'service_url': 'http_status:503',
                    'http_host_header': False,
                }],
                comment=self.tunnel_name or None,
                tunnel_name=self.tunnel_name,  # #531 歸屬主張
            )
            # #768：`dns_failed` 分支本來就會建 primary route，但冪等補一次不花錢，
            # 而且涵蓋「那一次 `_ensure_primary_route()` 本身沒成功」的舊列。
            self._ensure_primary_route()
            # 與 `_do_resume` 同一個收斂點：state 此刻是 `provisioning`，route 的
            # liveness gate 會擋掉非 forced 的推送 ⇒ 要帶 forced 旗標。未保護且沒有
            # app id 的 route 是 no-op（`_access_sync_skip_reason`）。
            self._sync_all_access_then_publish(
                service_url=f"http://localhost:{self.ha_port}")
            vals = {'state': 'active', 'error_message': False}
            if not self.deployed_at:
                vals['deployed_at'] = fields.Datetime.now()
            self.write(vals)
            _logger.info(
                "[SH] provision retry converged sh=%s tunnel=%s hostname=%s",
                self.id, self.tunnel_id, hostname,
            )
        except PaaSOperatorConnectionError as e:
            self.write({
                'state': 'error',
                'error_message': _(
                    "Cannot connect to PaaS Operator: %(detail)s",
                    detail=e.message),
            })
            _logger.error(
                "[SH] provision retry connection error sh=%s: %s",
                self.id, e.message,
            )
        except PaaSOperatorError as e:
            self.write({
                'state': 'error',
                'error_message': _(
                    "Retrying setup for %(hostname)s failed: %(detail)s. The "
                    "tunnel (%(tunnel_id)s) is kept; you can retry again.",
                    hostname=hostname, detail=e.message,
                    tunnel_id=self.tunnel_id),
            })
            _logger.error(
                "[SH] provision retry error sh=%s tunnel=%s: %s",
                self.id, self.tunnel_id, e.message,
            )
        except AccessError:
            # 權限拒絕必須傳到呼叫端，不得降級成看起來無害的 state='error'
            # （two-stage-remote-provision-idempotency §守則 2；與 SA retry 分支對稱）。
            raise
        except Exception as e:
            # 非 operator 類失敗（route 建立的 ValidationError、DB 錯誤…）同樣要
            # fail-loud 落 `error`：controller 的 `except Exception` 會把例外收成
            # 200，若不在這裡落帳，記錄會停在 `provisioning`，而詳情頁的重試鈕只在
            # pending／error 出現 ⇒ 租戶又回到沒有出路的狀態。
            self.write({
                'state': 'error',
                'error_message': _(
                    "Retrying setup failed: %(error)s", error=e),
            })
            _logger.exception(
                "[SH] provision retry unexpected error sh=%s", self.id,
            )

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

        ## 檢查用呼叫者身分

        判準必須是**呼叫者**有沒有權限（`self.check_access`，非 sudo）。形狀取自本
        repo 既有的前置檢查前例 `cloud_service._assert_reverse_binding_authorized()`
        （#698）。#740 原本還有「sudo 枚舉 route → 呼叫者身分驗 route」那一半，
        #1294 拿掉，理由見下一節。

        ## 檢查範圍：只有本 model 這一張（#1294 起）

        推導方式（不要照抄結論，改 `unlink()` 時重推一次）：讀 `unlink()` 本體，找出
        每一個以**呼叫者身分**走到 Odoo `check_access('unlink')` 的位置。#1294 之後只剩
        本 model 的 `super().unlink()` 一處——route 那次顯式刪除
        （`_sm_route_cascade_unlink(record.sudo().route_ids).unlink()`）改走平台身分。

        **不含** `woow_paas_platform.smart_home_route`（#1294 改動，原本有）：#740 當時
        route 是以呼叫者身分刪的，所以要先驗；但 route 對 `base.group_user` 的 ACL 是
        `unlink=0`（本階段沒有 route CRUD，`test_sh_access_acl_768.py` R7 釘住），
        只要以呼叫者身分刪 route，租戶 owner/admin 就永遠刪不掉自己的 SH。改成：
        「能不能刪這台 SH」由下面那一行驗完，route 只是它的附屬列，隨 SH 以平台身分
        一起走——route 的單獨刪除入口因此維持關閉，不必為了刪 SH 放寬 route ACL。
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
        只是把豁免寫在看得見的地方。
        🔴 連帶：本守衛在 `TransactionCase` 的預設 env（su）底下是 no-op ⇒ 用預設
        env 寫的「守住了」測試**測不到它**
        （`docs/decisions/patterns/su-env-exempt-guard-false-green.md` 的形狀）。
        租戶那一側的回歸測試因此一律走真 HTTP，見
        `tests/test_sh_unlink_authz_preflight_740.py`。

        ⚠ 授權姿態（#1294）：#740 落地時本方法**不改變**授權姿態，租戶刪除仍然
        失敗、它只保證「失敗得乾淨」（零外部呼叫、零持久欄位變更）。#1294 起
        `access_smart_home_user` 改成 `unlink=1`，租戶 owner/admin 經
        `smart_home_rule_owner_admin_manage` 刪得掉自己工作區的 SH；同工作區的
        user/guest 與跨工作區的身分仍在這裡被拒（那條 rule 是唯一 `perm_unlink=True`
        的 rule），而且仍然「失敗得乾淨」。
        """
        if self.env.su:
            return
        # 唯一一張以呼叫者身分消耗 unlink 權的表（推導見 docstring「檢查範圍」）。
        # ⚠ 不要把 route 的檢查加回來：route ACL 對 `base.group_user` 刻意是
        # `unlink=0`，加回來 ⇒ 租戶 owner/admin 又刪不掉自己的 SH（#1294 的原狀）。
        self.check_access('unlink')

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
        self._lock_lifecycle_or_raise()
        if self.tunnel_id:
            self.write({'state': 'deleting'})
        self.unlink()

    def _cleanup_tunnel(self):
        """冪等刪除 tunnel，將遠端刪除與本地 bookkeeping 分成兩個失敗域。

        遠端未確認成功時保留錨點供 retry；成功後的記帳失敗必須 fail-loud，
        不能讓呼叫端從殘留 tunnel_id 推論 Cloudflare 上仍有活著的 tunnel。
        """
        from psycopg2 import OperationalError

        self.ensure_one()
        if not self.tunnel_id:
            return
        tunnel_id = self.tunnel_id
        try:
            client = self._get_operator_client()
            # #531：claim 取自本列鏡回來的 CF name。⚠ 三個錨點是在 delete **成功
            # 之後**才一起清（見下方註解），所以呼叫當下 `tunnel_name` 一定還在。
            client.delete_tunnel(tunnel_id, tunnel_name=self.tunnel_name)
        except Exception as e:
            # 連線／timeout／operator 拒絕或設定錯誤：尚無成功回執，保持舊重試語意。
            _logger.warning(
                "[SH] tunnel cleanup failed sh=%s: %s — kept for retry",
                self.id, e,
            )
            return

        # #859：提交點已跨過；之後任何 DB 回捲都不會讓 Cloudflare tunnel 復活。
        _logger.info("[SH] tunnel deleted sh=%s tunnel=%s", self.id, tunnel_id)
        try:
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
            # #833：tunnel_status 一起收掉。這是平台**唯一百分之百確定「這條
            # tunnel 已經不存在」的時刻**（是我們自己刪的），而且是最後一個機會——
            # `action_refresh_status()` 的第一行是 `if not self.tunnel_id: return`，
            # 下一行清掉錨點之後，那個欄位就再也沒有任何路徑能更新它，會被凍結在
            # 刪除前的最後一個值（通常是 connected 的綠燈）。
            # ⚠ 只在 delete 成功之後寫：失敗時 tunnel 還活在 CF 上，標成 deleted
            # 會把「待重試」謊報成「已刪除」，比原本的 bug 更糟。
            #
            # #1296：這筆是平台自己的帳，維持 sudo ＋ private sentinel 的窄寫入。
            # sudo 打不開身分錨點守衛（無 su 豁免），開閘的仍是 `_internal_identity`；
            # 授權由呼叫鏈上的 `_assert_unlink_authorized()` 負責，本方法是私有方法。
            # #859：錨點與狀態同一個 flushing savepoint；離開時 flush 觸發的
            # immediate constraint 失敗在本段分類，DEFERRED constraint 到 COMMIT
            # 才驗，不由 RELEASE SAVEPOINT 觸發。回捲僅撤回本地記帳，不補整筆 rollback。
            with self.env.cr.savepoint():
                _internal_identity(self.sudo()).write(dict(
                    {field: False for field in _IDENTITY_ANCHOR_FIELDS},
                    tunnel_status='deleted',
                ))
        except OperationalError:
            # #518：回捲成功時保留原型別／物件給 Odoo retrying；真斷線若使
            # savepoint 回捲另拋例外，原物件可能被蓋過。不查可能已 aborted 的 DB。
            _logger.exception(
                "[SH] tunnel deleted in Cloudflare sh=%s tunnel=%s; local identity "
                "anchors could not be cleared (database operational failure)",
                self.id, tunnel_id,
            )
            raise
        except AccessError as e:
            # 記帳權限失敗不是呼叫者無刪除權；安全文案交代既成事實，不回顯 ACL 細節。
            _logger.exception(
                "[SH] tunnel deleted in Cloudflare sh=%s tunnel=%s; local identity "
                "anchors could not be cleared (bookkeeping permission failure)",
                self.id, tunnel_id,
            )
            raise UserError(_(
                "The tunnel was deleted in Cloudflare, but its local identity anchors "
                "could not be cleared due to a bookkeeping permission error. "
                "Contact an administrator to repair the local record."
            )) from e
        except Exception as e:
            # 其他 ORM／SQL／程式錯誤同樣 fail-loud，原例外保留在 log 與 chain，
            # 不把技術細節交給租戶，也不落回呼叫端的 still-live guard。
            _logger.exception(
                "[SH] tunnel deleted in Cloudflare sh=%s tunnel=%s; local identity "
                "anchors could not be cleared (bookkeeping failure)",
                self.id, tunnel_id,
            )
            raise UserError(_(
                "The tunnel was deleted in Cloudflare, but its local identity anchors "
                "could not be cleared due to a bookkeeping error. "
                "Contact an administrator to repair the local record."
            )) from e

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
                            _("Cloudflare Access teardown failed for route(s): %s. The "
                              "Smart Home was kept so the deletion can be retried; its "
                              "Access app is still live in Cloudflare.")
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
                    #
                    # #1294：route 以**平台身分**刪（`sudo()`）。授權判準是「呼叫者
                    # 能不能刪這台 SH」，已由第一行的 `_assert_unlink_authorized()`
                    # 驗完；route 對 `base.group_user` 的 ACL 刻意是 `unlink=0`（沒有
                    # route CRUD），用呼叫者身分刪 ⇒ 租戶 owner/admin 永遠刪不掉自己的
                    # SH。枚舉同樣用 sudo 的集合，與 `_teardown_routes_access()` 一致
                    # （不讓 record rule 決定哪些 route 走得到上面那段 FK 處理）。
                    _sm_route_cascade_unlink(record.sudo().route_ids).unlink()
                    if record.tunnel_id:
                        record._cleanup_tunnel()
                    if record.tunnel_id:
                        raise UserError(
                            _("Cloudflare cleanup failed; the tunnel is still live and "
                              "billed. The record was kept so the deletion can be "
                              "retried. Please try again shortly.")
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
        """Refresh tunnel status from the PaaS Operator.

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
        if not self.tunnel_id:
            return

        client = self._get_operator_client()

        try:
            result = client.get_tunnel_status(self.tunnel_id)
            vals = self._tunnel_vals_from_status(result)
            # 翻旗標的那一刻記一行（不是每次輪詢都記）——#1766 的現場是「平台顯示
            # 一切正常」，所以第二台接上必須在 log 裡留痕。
            self._tunnel_binding_log_conflict(vals)
            self.write(vals)
        except PaaSOperatorError as e:
            self.write({
                'tunnel_status': 'error',
                'error_message': _(
                    "Status refresh failed: %(detail)s", detail=e.message),
            })
            _logger.warning(
                "Failed to refresh tunnel status for Smart Home '%s': %s",
                self.name, e.message,
            )

    @api.model
    def _tunnel_vals_from_status(self, result):
        """operator ``GET /api/tunnels/{id}`` 的回應 → tunnel 相關欄位值。

        ``action_refresh_status``（寫回 DB）與 ``_live_tunnel_status``（只讀疊在
        詳情 API 回應上）共用這一份判讀，兩邊才不會各自演化出不同的「已連線」
        定義——SA 在 #1256 抽的是同一刀（``SecurityAccess._tunnel_vals_from_status``）。
        ``deleted_at`` 的語意見 ``action_refresh_status`` docstring（#833）。

        ⚠ 與 SA 版**刻意不合併**：SH 沒有 ``edge_connection_count`` /
        ``connector_version`` 欄位，而且抽出來必須與原本內嵌的判讀逐格相同
        （例如 disconnected 時 SH 從來不清 connector_id）——行為改變不該藏在
        一次重構裡。
        """
        connections = result.get('connections', [])
        if result.get('deleted_at'):
            # Cloudflare 上已經沒有這條 tunnel 了 ⇒ 「等連線恢復」不會有結果，
            # 要重建。連 connector 記帳一起清掉：留著上一輪的數字會變成
            # 「Deleted ＋ Edge Connections: 4」這種看起來很正常的混合世代。
            return {
                'tunnel_status': 'deleted',
                'connector_id': '',
                'connector_type': '',
                'connector_count': 0,
                'binding_conflict': False,
            }
        vals = {
            'tunnel_status': 'connected' if connections else 'disconnected',
        }
        if connections:
            conn = connections[0]
            vals['connector_id'] = conn.get('connector_id', '')
            vals['connector_type'] = conn.get('type', '')
            if conn.get('opened_at'):
                vals['tunnel_uptime'] = conn['opened_at']
        # ⛔ 上面只看 connections[0]（顯示用的那一個 connector），所以它**看不出**
        # 有兩台機器接著同一條 tunnel——那正是 #1766 的隱形成因。機器數要數
        # distinct client_id，由 binding mixin 負責（#1766）。
        vals.update(self._tunnel_binding_vals_from_connections(connections))
        return vals

    def _live_tunnel_status(self):
        """詳情頁／HA status 端點用：即時向 operator 問 tunnel 狀態，**只讀、不寫 DB**。

        為什麼需要：``action_refresh_status`` 只在 ``refresh_status`` action
        （owner/admin）被呼叫時才寫回，詳情頁載入時從不呼叫、也沒有排程 ⇒
        「Tunnel Information」停在上一次寫入的值——與 SA 在 #1256 修掉的是同一個
        形狀（SA 概覽頁 tunnel 已 0 connector 數小時仍顯示「已連線」）。

        只讀是刻意的：寫回要過 smart_home 的 write ir.rule（#524 起只發給
        owner/admin），詳情頁卻是所有成員都能看；HA machine API 的 status 端點
        （``controllers/ha_api.py``）也改走這裡，才不會讓只過 OAuth scope 的呼叫
        端觸發 DB 寫入。

        絕不拋例外：拿不到（沒有 tunnel／operator 未設定／逾時／錯誤／回應形狀
        不對）就回 ``{}``，呼叫端沿用 DB 裡的值——那是退化成原本的行為，不是新的
        錯誤，詳情頁不能因為 operator 抖一下整頁失敗。
        """
        self.ensure_one()
        if not self.tunnel_id:
            return {}
        client = get_paas_operator_client(self.env)
        if not client:
            return {}
        try:
            result = client.get_tunnel_status(self.tunnel_id)
        except PaaSOperatorError as e:
            _logger.info("[SH] live tunnel status unavailable sh=%s: %s",
                         self.id, e.message)
            return {}
        if not isinstance(result, dict):
            # operator 回了非物件（空 body 以外的怪形狀）⇒ 判讀不了，當作拿不到。
            _logger.info("[SH] live tunnel status unexpected payload sh=%s: %r",
                         self.id, type(result).__name__)
            return {}
        vals = self._tunnel_vals_from_status(result)
        if vals.get('tunnel_status') != 'connected':
            # 斷線／已刪除時「Connector Opened」不該還掛著上一次連上的時間。
            # （SH 沒有 edge_connection_count，改以判讀出的 tunnel_status 為準。）
            vals['tunnel_uptime'] = ''
        vals['tunnel_status_live'] = True
        return vals

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
            # #1766：綁在哪一台 HA、以及現在有幾個 cloudflared 接著。
            # `binding_conflict=True` ＝ 兩台共用一條 tunnel，對外網址會連到先回應
            # 的那一台（UI 要把它當錯誤顯示，不是裝飾）。
            'binding_state': self.binding_state,
            'bound_instance_label': self.bound_instance_label or '',
            'bound_at': self.bound_at.isoformat() if self.bound_at else '',
            'connector_count': self.connector_count,
            'binding_conflict': self.binding_conflict,
            'ha_port': self.ha_port,
            'subdomain': self.subdomain or '',
            'error_message': self.error_message or '',
            'deployed_at': self.deployed_at.isoformat() if self.deployed_at else '',
            'created_at': self.create_date.isoformat() if self.create_date else '',
            'subscription': self._get_subscription_info(),
            'mcp': self._get_mcp_integration(),
            # 自動撤權 v1：只增不減的不透明版本（撤權帳本最大 id，沒有就 0）。HA 經
            # `GET /api/smarthome/homes/<id>/status` 讀到它改變就重設 connector；
            # 數值本身沒有意義，只能比較。帳本租戶零 ACL ⇒ sudo。
            'access_revision': self.env[
                'woow_paas_platform.smart_home_access_revision'
            ].sudo()._revision_for_home(self.id),
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

        # RACE-01：取 ingress 圍籬鎖，讓「503 外呼 ＋ 寫 suspended ＋ commit」這一段
        # 與**放寬**發布互斥。沒有它，`_sync_access_ingress` 的已提交重讀可能剛好
        # 在本交易提交之前完成（讀到 active），接著本交易提交停權、對方才把 live
        # 推出去 ⇒ 圍籬被跨過。本方法本身只收緊，不需要重讀已提交狀態。
        #
        # 刻意放在 `try` **之外**：取不到鎖是「這一輪不要停權、下一輪重試」，不是
        # 「停權失敗」——寫進 `state='error'` 會把一次等鎖變成要人工處理的狀態，而
        # 生命週期 cron 每一筆都包在 savepoint ＋ `except Exception` 裡（
        # `subscription_lifecycle_cron._cron_enforce_subscription_lifecycle`），
        # raise 只影響這一筆、明天以新快照重判。
        # 主 cursor：這一把必須握到本交易 commit（503 外呼、state 寫入與 commit 要
        # 一起落在臨界區內），所以收緊方刻意**不**走 :meth:`_ingress_fence` 的獨立
        # 短交易。
        self._ingress_fence_lock(self.env.cr)

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
        # #1052：判準走 mixin 的 `_is_halted()` seam——cron 已經用它決定要不要呼叫
        # 本方法（subscription_lifecycle_cron.py），mixin 預設就是 `state == 'suspended'`，
        # 零行為變化。內聯 literal 會讓將來的 override（#482/#497 那種「state 被改寫
        # 但仍在停權中」）只在 cron 生效、在這裡靜默 no-op。
        if not self._is_halted():
            return

        service_url = self.original_service_url or f"http://localhost:{self.ha_port}"
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
                self._sync_all_access_then_publish(
                    service_url=service_url, allow_halted=True)
                _logger.info(
                    "[SH] _do_resume tunnel restored sh=%s tunnel=%s",
                    self.id, self.tunnel_id,
                )
            except Exception as e:
                _logger.warning(
                    "[SH] _do_resume tunnel failed sh=%s: %s",
                    self.id, e,
                )
                self.write({'state': 'error', 'error_message': str(e)})
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
                error_message = _(
                    "Cloudflare Access teardown failed for route(s): "
                    "%(routes)s. Will retry on the next cron pass.",
                    routes=', '.join(str(f) for f in failed_access),
                )
            else:
                error_message = _(
                    "Cloudflare Access teardown reported success but "
                    "route(s) %(routes)s still hold Access object id(s); the "
                    "CF Access app and its reusable policies are still live. "
                    "Will retry on the next cron pass.",
                    routes=', '.join(
                        str(rid) for rid in lingering_access.ids),
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
                'error_message': _(
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
