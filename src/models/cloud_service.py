import contextlib
import copy
import json
import logging
import math
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta

import pytz
from psycopg2 import OperationalError
from psycopg2 import errors as pg_errors

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL, mute_logger
from odoo.tools.sql import column_exists

from ..services import dns_probe
from ..services.naming import make_cs_access_canonical_name
from ..services.woow_odoo_readiness import (
    CONTRACT_MANAGED,
    CONTRACT_MISMATCH,
    chart_mismatch_detail,
    woow_odoo_contract,
    woow_odoo_ready,
)
from .cloud_app_template import SPECS_UNPARSEABLE, SupplyGateError
from .cloud_service_init_attempt import (
    InitOutcome,
    INIT_ATTEMPT_WORST_SECONDS,
    INIT_LEASE_SECONDS,
    LEGACY_LEASE_SECONDS,
    OUTCOME_FAILED,
    OUTCOME_LEGACY,
    OUTCOME_NOT_EXECUTED,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    OUTCOME_UNKNOWN,
    classify_init_result,
    mask_init_error,
)
# #798：policy 列上 `cf_policy_id` 的唯一寫入豁免。sentinel／opener 住在 SA 那支，
# 因為 `security_access` 在 `models/__init__.py` 早於本檔載入、且它不 import 任何
# sibling model ⇒ 這個方向沒有循環；反方向（把 opener 放 policy 那支）才會有。
from .security_access import _internal_policy_cf_write
from .workspace_access import ROLE_ADMIN

_logger = logging.getLogger(__name__)

# #1159：每次進入這些狀態都開始等待新的真實 operator status；platform_monitor
# 共用此集合，list/get 只回報已存的 episode，不自行建立。
OBSERVATION_TRANSITION_STATES = ('deploying', 'upgrading', 'initializing', 'deleting')

#: #528 的待回收 CF Access policy 帳本（三腿共用）。
_CF_REAP_MODEL = 'woow_paas_platform.cf_access_policy_reap'

#: #1052：租戶勾選的 addon 在 Odoo pod 內的落點。`_build_odoo_addon_helm_values` 的 initContainer
#: 把 addon 複製到這裡、再用 sed 把它 append 進 odoo.conf 的 addons_path。兩段 sed 打的 conf
#: 路徑字面不同：initContainer 用 PVC 路徑 `/bitnami/odoo/conf/odoo.conf`（run.sh 之前 /opt 那條
#: 還不存在，見 docs/tracking/landings/prod-odoo-paas-not-installed.md），post-init script 與
#: operator 側 `extra/paas-operator/src/services/odoo_scripts/update_module_list.py`
#: （`ODOO_CONF_PATH`）用 `/opt/bitnami/odoo/conf/odoo.conf`；operator 讀到的 addons_path 就是
#: 那次 append 的結果。兩個 sed 的冪等判準是 `grep -q "selected-addons"`，所以路徑必須含
#: `selected-addons` 這段（守衛：test_helm_values_addon_builder_1052）。
ODOO_SELECTED_ADDONS_PATH = '/opt/bitnami/odoo/selected-addons'

#: #1130：woow-odoo chart 的 Service port。Service 名＝release 名，80 與 8072 都 targetPort
#: nginx sidecar 的 8080（chart values.yaml 檔頭〈固定契約〉）。`_build_expose_config` 明帶它，
#: 不交給 operator 的「第一個非 headless Service」自動偵測（同 ns 還有 `<release>-postgresql`）。
WOOW_ODOO_SERVICE_PORT = 80

#: #1130：woow-odoo 初始管理員密碼的 sensitive 路徑（charts#81 R2-Q2 定名 `conf.adminPassword`，
#: 不改名）。值由平台鑄造、存 `cloud_service_secret.admin_password`，經 operator 的 sensitive
#: 通道送入；chart 只在首次 `-i base` 套用（#1174：redeploy／reconfigure 重送同值是 no-op）。
#: 不得落 `helm_values`（`_strip_mcp_sensitive_values`）；詳情 API 的第二道遮罩見
#: `PaasController._sanitize_helm_values`。
WOOW_ODOO_ADMIN_PASSWORD_PATH = ('conf', 'adminPassword')

#: #1158：K8s storage quantity（只收二進位單位，平台與 chart 一律寫 Gi）→ GiB。
_STORAGE_QUANTITY_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*(Mi|Gi|Ti)\s*$')
_STORAGE_UNIT_GIB = {'Mi': 1 / 1024, 'Gi': 1, 'Ti': 1024}


def _storage_quantity_gib(size):
    """``'30Gi'`` → ``30.0``；看不懂（含十進位單位、純數字）回 ``None``。"""
    match = _STORAGE_QUANTITY_RE.match(str(size))
    if not match:
        return None
    return float(match.group(1)) * _STORAGE_UNIT_GIB[match.group(2)]


#: #1130：Bitnami odoo 時代 seed 寫進每台服務 helm_values 的頂層鍵（woow-odoo tenant schema
#: 全部拒收）。`_woow_odoo_legacy_release_error` 用它辨識 #1128 範圍的存量服務。
BITNAMI_ODOO_VALUE_KEYS = frozenset({
    'odooEmail', 'odooPassword', 'odooSkipInstall', 'postgresql', 'customPostInitScripts',
})

#: #1130：woow-odoo 管理員 login（＝租戶填的 email，`conf.adminEmail`）的格式檢查，
#: create／reconfigure 共用（#1160）。只擋明顯不是 email 的值；254＝RFC 5321 路徑上限。
#: #1348：operator 開通前儲存配額檢查的錯誤碼（``extra/paas-operator/src/services/
#: storage_quota.py`` 的同名常數；改名兩邊一起改）。
STORAGE_QUOTA_ERROR_CODE = 'workspace_storage_quota_exhausted'
#: #1415：operator 開通前叢集儲存（Longhorn）排程額度檢查的錯誤碼（``extra/paas-operator/
#: src/services/storage_capacity.py`` 的 ``CAPACITY_ERROR_CODE``；改名兩邊一起改）。
CLUSTER_STORAGE_CAPACITY_ERROR_CODE = 'cluster_storage_capacity_exhausted'
#: #1431：從未進過 running 的服務刪除時，孤兒 PVC／DataVolume 只保留這麼多天（operator
#: ``DELETE /api/releases/...?grace_period_days=``；只能縮短預設、不能延長）。沒有租戶資料
#: 要保護，不必佔 Longhorn 排程額度 90 天（#1415 事件）。
NEVER_RUNNING_ORPHAN_GRACE_DAYS = 1
#: #1424：operator「明確拒絕開通、沒有建立服務」的錯誤碼。收到時若服務從未 running 過，
#: 平台自動取消它的訂閱（不扣款），租戶刪除服務後再重新開通即可。
PROVISION_REJECTION_ERROR_CODES = (
    STORAGE_QUOTA_ERROR_CODE, CLUSTER_STORAGE_CAPACITY_ERROR_CODE,
)

WOOW_ODOO_ADMIN_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
#: #1335：保留給文件／測試、永遠收不到信的網域（RFC 2606 §3 的 example.com／.net／.org
#: 與其子網域，RFC 2606 §2／RFC 6761 的保留 TLD）。舊版 seed 預填 user@example.com，
#: 租戶沒改就成了收不到密碼重設信的 Odoo 管理員。只在 create 擋（見
#: ``_woow_odoo_admin_email_error`` 的 ``reject_reserved``）。
RESERVED_EMAIL_DOMAINS = frozenset({'example.com', 'example.net', 'example.org'})
RESERVED_EMAIL_TLDS = frozenset({'example', 'invalid', 'test', 'localhost'})

#: 租戶自設的 admin 帳號（template.admin_username_helm_path 有值的模板）。與 chart
#: 端 `open-design.basicAuthUsername` 同一條規則：帳號被原樣寫進 htpasswd
#: （`user:{PLAIN}pw`，nginx 以第一個 ':' 切分）與 ttyd 的 `-c user:pw`，含 ':' 或空白
#: 會把帳密切成另一組。平台先擋，給租戶看得懂的訊息；chart 端 fail render 是第二道。
TENANT_ADMIN_USERNAME_RE = re.compile(r'^[A-Za-z0-9._@-]{1,64}$')
#: 租戶自設的 admin 密碼：12–128 個可見 ASCII（不含空白）。下限對齊平台鑄造密碼的
#: 強度級距；排除空白與控制字元是因為密碼會進 htpasswd 單行與 ttyd argv，前後空白
#: 在不同元件會被不同地 trim，換行會長出第二行 htpasswd。
TENANT_ADMIN_PASSWORD_RE = re.compile(r'^[\x21-\x7e]{12,128}$')


# ------------------------------------------------------------------------- #
#  #519：cloud_service 的建立閘（private sentinel）                           #
# ------------------------------------------------------------------------- #
#
# 不變式：**沒有任何 RPC 呼叫端**（裸 ORM／XML-RPC／JSON-RPC／`call_kw`，不論
# 身分、不論送什麼 vals 或 context）能讓一筆 cloud_service 落地。只有明示開閘
# 的平台伺服器端 in-process 碼建得出來。閘門在 `create()` 的第一行。
#
# 為什麼守線只能長在這裡（#519 的裁決，見
# `docs/decisions/519-cloud-service-provision-gate.md`）：ACL／`ir.rule` 那層
# 對本案結構性無效——owner 資格是**可自我滿足**的（任何 `base.group_user` 帳號
# 免費自建 workspace → `workspace.py` 用 `.sudo()` 自動補一列 role=owner 的
# access → 滿足 #676 的 `cloud_service_rule_owner_admin_manage`）。而「自建
# workspace 即為 owner」是**產品設計**、不是缺陷，所以不動那一層。
#
# 判準**只讀** context 裡那個值的**物件身分**（`is` 比對），刻意不讀：
#   * `vals`（raw 或 `_add_missing_default_values` 解析後都不讀）——租戶對
#     `ir.default` 有完整 CRUD（Odoo 18 base `ir.model.access.csv` 的
#     `access_ir_default_group_user` 是 1,1,1,1，且 `ir.default.set()` 是
#     `@api.model`、不查權限）⇒ `default_<欄位>` 有**持久化**載體，任何讀 vals
#     的守衛都會被繞過。不讀 vals ⇒ 那條陷阱在本形狀下結構上不存在。
#   * `env.su`——`TransactionCase` 預設就是 su env，讀它會在每個測試裡靜默停用
#     守衛，正是測試本該證明它有效的地方
#     （`docs/decisions/patterns/su-env-exempt-guard-false-green.md`）。
#   * `env.user` / `env.uid`——`controllers/ha_paas.py` 的 `auth='none'` 路徑下
#     `env.user` 是空 recordset，讀它會誤判。
#   * workspace role / `has_group()`——讀了就把上面那條產品設計變成守衛的旁路。
#
# 為什麼把手的值必須是這個**私有物件**、不能是 `True`／字串／任何 JSON 值：
# `/web/dataset/call_kw` 把呼叫端送來的 context **整包**換進 env
# （`odoo/api.py::call_kw`：`context = kwargs.pop('context', None) or {}` →
# `recs.with_context(context)`，位置參數＝取代不是 merge），所以任何 JSON
# 表達得出的判準，RPC 呼叫端在 payload 裡多加一個 key 就打開了。這不是推論：
# #522 在 18.0 上實測過同一件事——一個只有 `base.group_user` 的租戶送
# `context={'sa_internal_identity_write': true}` 就改寫了 `canonical_name`，
# 而且新值真的 commit 進 Postgres。JSON 載不了物件身分，`is` 比對才擋得住。
#
# 本把手與 `models/security_access.py` 的 `_INTERNAL_IDENTITY_WRITE`
# （`:64-102`）同構——那是本 repo 已被 #522 實測過的同一套機制，這裡刻意逐字
# 沿用它的形狀，不另發明。
class _InternalProvision:
    """平台伺服器端開通碼的 marker type —— 見上方註解。"""

    __slots__ = ()

    def __repr__(self):
        return '<internal provision>'


_INTERNAL_PROVISION = _InternalProvision()

#: 唯一的逃生口 context key。key 本身不是判準（呼叫端當然抄得到這個字串），
#: 判準是它的**值身分**，見 `_is_internal_provision`。
_INTERNAL_PROVISION_CTX_KEY = 'woow_internal_provision'


def _internal_provision(records):
    """回傳「唯一被允許建立 cloud_service」的那個 context 底下的 ``records``。

    比照 `security_access.py` 的 `_internal_identity(records)`：開閘掛在**會讓
    記錄落地的那一次呼叫**上，不掛在 model handle 的綁定處——唯讀 handle 一律
    不帶這個 context，逃生口的表面積就只有真正的建立點。

    ``with_context(**kwargs)`` 是 merge，所以 `.sudo()` / `.with_user()` /
    後續 `with_context(foo=...)` 都保留這個把手。
    """
    return records.with_context(
        **{_INTERNAL_PROVISION_CTX_KEY: _INTERNAL_PROVISION})


def _is_internal_provision(env):
    """判準：**只**比對 context 裡那個值的物件身分（`is`），不讀 vals／su／user。"""
    return (env.context.get(_INTERNAL_PROVISION_CTX_KEY)
            is _INTERNAL_PROVISION)


# ------------------------------------------------------------------------- #
#  #744：CS Access 的身分錨點（private sentinel，形狀抄 #522/#524）           #
# ------------------------------------------------------------------------- #
#
# 身分錨點＝「這一列綁的是**哪一個** Cloudflare 物件」的欄位。`access_app_id`
# 決定了之後的 sync／teardown 會去動帳號裡的哪一個 Access application——租戶
# 把它改成別人的 app id，平台就會拿受害者的 Access 設定去覆寫或刪除，而這一列
# 仍然帶著**自己** workspace 的 ir.rule 祝福（rule 擋的是「動哪一筆」，擋不到
# 「在自己那一筆裡填別人的 id」）。
#
# 為什麼另立一套而不共用上面的 `_INTERNAL_PROVISION`：兩者的語意不同——那個是
# 「這一次建立來自平台開通流程」，本組是「這一次寫入是伺服器在回存 Cloudflare
# 剛發回來的 id」。共用一把手，等於讓開通路徑順便獲得改寫 Access 錨點的能力
# （`security_access.py` 把 `sa_internal_prefix_write` 與
# `sa_internal_identity_write` 分開，理由同）。
#
# 值必須是**私有物件**、判準必須是 `is`：`/web/dataset/call_kw` 把呼叫端的
# context 整包換進 env，任何 JSON 表達得出的判準（True／1／"true"）在 RPC
# payload 多一個 key 就打開了——#522 在 18.0 上實測過並且值真的 commit 進
# Postgres。見 docs/decisions/patterns/su-env-exempt-guard-false-green.md。
_CS_ACCESS_IDENTITY_ANCHOR_FIELDS = (
    'access_app_id',    # WHICH CF Access application this service configures
)

# #745：改到這幾個欄位就要把整份 desired state 重推給 operator。
#
# 只列**租戶設定得了**的那幾個：`access_app_id` / `access_unresolved_idp_types`
# 是 sync 自己的回存值，列進來會讓每一次回存再觸發一次 sync（無窮遞迴）。
_CS_ACCESS_SYNC_FIELDS = (
    'is_protected', 'login_google', 'login_email_otp', 'session_duration',
)


class _InternalCsAccessIdentityWrite:
    """CS Access 身分錨點回存的 marker type —— 見上方註解。"""

    __slots__ = ()

    def __repr__(self):
        return '<internal cs access identity write>'


_INTERNAL_CS_ACCESS_IDENTITY_WRITE = _InternalCsAccessIdentityWrite()

#: key 本身不是判準（呼叫端當然抄得到這個字串），判準是它的**值身分**。
_CS_ACCESS_IDENTITY_CTX_KEY = 'woow_cs_access_identity_write'


def _internal_cs_access_identity(records):
    """回傳「唯一被允許寫 CS Access 身分錨點」的那個 context 底下的 ``records``。"""
    return records.with_context(
        **{_CS_ACCESS_IDENTITY_CTX_KEY: _INTERNAL_CS_ACCESS_IDENTITY_WRITE})


def _is_internal_cs_access_identity_write(env):
    return (env.context.get(_CS_ACCESS_IDENTITY_CTX_KEY)
            is _INTERNAL_CS_ACCESS_IDENTITY_WRITE)


def _strip_cs_access_identity_anchors(env, vals_list):
    """create() 側：把身分錨點**壓成 False**，不是 pop。

    `super().create()` 不經過 `write()`，只守 write 會留下 create-time squat：
    一列剛落地就綁在受害者的 Access app 上，之後任何 sync／teardown 都會動到
    對方的資源。對**所有人**生效（含 superuser），sentinel 是唯一豁免。
    選擇「壓值」而不是「raise」：`copy()` 與一般資料載入照樣要能跑，而一份
    不帶錨點的複本正是複本該有的樣子。

    🔴 為什麼是**壓成 False** 而不是 `vals.pop(...)`：`pop` 只清得掉呼叫端
    寫在 vals 裡的那一份，而 `default_<欄位>` 是在 `super().create()` 內部的
    `_add_missing_default_values` 才展開的——租戶對 `ir.default` 有完整 CRUD
    （Odoo 18 base ACL 的 `access_ir_default_group_user` 是 1,1,1,1），`call_kw`
    也直接把呼叫端 context 換進 env ⇒ 送 `context={'default_access_app_id':
    '<victim>'}` 就能繞過整道 pop，而且守衛不會有任何訊號（#519／#705 是本
    repo 已經踩過的同一個形狀）。vals 裡的明確值優先於 default，所以壓成
    False 是「連 default 那條路一起關掉」的最小寫法；`access_app_id` 本來就
    沒有 default 且 `copy=False`，壓 False 與剝掉在語意上等價。
    """
    if _is_internal_cs_access_identity_write(env):
        return
    for vals in vals_list:
        for field in _CS_ACCESS_IDENTITY_ANCHOR_FIELDS:
            vals[field] = False


def _reject_cs_access_identity_anchor_writes(env, vals):
    """write() 側：拒絕搬動錨點，且要吵。對所有人生效（含 superuser）。"""
    if _is_internal_cs_access_identity_write(env):
        return
    for field in _CS_ACCESS_IDENTITY_ANCHOR_FIELDS:
        if field in vals:
            raise UserError(
                _("Field '%s' is immutable after creation.") % field
            )


# ------------------------------------------------------------------------- #
#  #1257：init 執行管控欄位只有平台寫得進去                                   #
# ------------------------------------------------------------------------- #
#
# ACL 給 `base.group_user` 完整 RWCU、`cloud_service_rule_owner_admin_manage` 給
# workspace owner／admin 寫入 ⇒ 沒有這道閘時，租戶可以 `POST /web/dataset/call_kw`
# 直接寫這些欄位：寫 `init_redeploy_requested` 讓 cron 以 superuser 跑 `_redeploy()`
# （繞過 #1185 的 group gate 與停權／欠費檢查），寫 `init_exit='failed'` 讓 helm 失敗
# 的 error 也通過 #1159 Q30 的 reconfigure 放行，寫計數／lease 則能重置失敗額度。
# 平台自己的寫入一律走 sudo（controller 的 `sys_service`、`_open_init_generation`、
# cron），所以判準是 `env.is_system()`（含 su 與 base.group_system）——這些欄位有合法
# 的改值路徑，屬 `su-env-exempt-guard-false-green` 說「su＋has_group 雙閘放行是對的」
# 那一邊。
_INIT_CONTROL_FIELDS = frozenset({
    'init_pending', 'init_generation', 'init_retries', 'init_unknown_count',
    'init_attempt_seq', 'init_attempt_id', 'init_lease_until', 'init_wait_since',
    'init_wait_spent_seconds', 'init_wait_started_at', 'init_retry_eligible_at',
    'init_queue_turn', 'init_redeploy_requested', 'init_exit', 'init_error',
    'init_ready_observed', 'init_ready_since', 'init_capacity_wait_since',
    'n8n_owner_alerted_at',
})


def _reject_init_control_writes(env, vals):
    """write() 側：非平台身分不得寫 init 管控欄位（#1257 r1 M1）。"""
    if env.is_system():
        return
    touched = sorted(_INIT_CONTROL_FIELDS.intersection(vals))
    if touched:
        raise AccessError(_(
            'Post-deploy initialization fields are managed by the platform: '
            '%(fields)s', fields=', '.join(touched)))


# observed_at 是 operator 觀測證據，episode 是平台命令建立的等待邊界。
# 兩者都不是呼叫者輸入，否則可以隱藏 stale poll 或偽造新觀測。
_OBSERVATION_CONTROL_FIELDS = frozenset({
    'observed_at', 'observation_episode_started_at',
})


def _reject_observation_control_writes(env, vals):
    """write() 側：觀測證據與等待 episode 只能由平台寫入。"""
    if env.is_system():
        return
    touched = sorted(_OBSERVATION_CONTROL_FIELDS.intersection(vals))
    if touched:
        raise AccessError(_(
            'Service observation fields are managed by the platform: %(fields)s',
            fields=', '.join(touched)))


# #1416：「這筆 error 是部署逾時寫的」標記。背景 cron 以 superuser 依它重查、就緒就把
# 服務推回 running／initializing——租戶若能自己寫它，就能讓 init 額度出口、刪除失敗等
# 其他來源的 error 被 cron 推回去（同 `_INIT_CONTROL_FIELDS` 的理由，判準也同樣是
# `env.is_system()`：平台自己的寫入全走 cron／sudo）。
_TIMEOUT_RECOVERY_CONTROL_FIELDS = frozenset({'transition_timeout_at'})


def _reject_timeout_recovery_control_writes(env, vals):
    """write() 側：逾時自癒標記只能由平台寫入（#1416）。"""
    if env.is_system():
        return
    touched = sorted(_TIMEOUT_RECOVERY_CONTROL_FIELDS.intersection(vals))
    if touched:
        raise AccessError(_(
            'Deployment timeout recovery fields are managed by the platform: %(fields)s',
            fields=', '.join(touched)))


# #1552：「這筆 error 是背景 upgrade（修改配置／換方案）沒有確認套用寫的」標記。有值時
# 租戶可以直接重新送出修改配置（``_update_service`` 的守衛放行）；其他來源的 error（安裝
# 失敗、刪除失敗、部署逾時……）照舊擋。租戶若能自己寫它，就能讓刪除失敗等 error 被
# reconfigure 拉起來（同 `_TIMEOUT_RECOVERY_CONTROL_FIELDS` 的理由與判準）。
_UPGRADE_FAILURE_CONTROL_FIELDS = frozenset({'upgrade_failed_at'})


def _reject_upgrade_failure_control_writes(env, vals):
    """write() 側：升級失敗標記只能由平台寫入（#1552）。"""
    if env.is_system():
        return
    touched = sorted(_UPGRADE_FAILURE_CONTROL_FIELDS.intersection(vals))
    if touched:
        raise AccessError(_(
            'Upgrade failure fields are managed by the platform: %(fields)s',
            fields=', '.join(touched)))


# #1431／#1424：服務「曾經正常運作過」的事實。平台依它決定刪除後孤兒 PVC 保留多久
# （從未 running ⇒ 1 天）與要不要扣款（從未 running 的 error 服務暫停扣款）——租戶若能自己
# 寫它，就能把用過的服務偽裝成沒用過（縮短資料保留、逃避計費）。判準同上：`env.is_system()`。
# `provision_rejected_code`（#1424）同理：它讓 lifecycle cron 不去停權、讓已取消訂閱的服務
# 仍可刪除——租戶自己寫得進去就能讓欠費服務逃過停權。
_SERVICE_HISTORY_FIELDS = frozenset({'first_running_at', 'provision_rejected_code'})


def _reject_service_history_writes(env, vals):
    """write() 側：服務歷程事實只能由平台寫入（#1431）。"""
    if env.is_system():
        return
    touched = sorted(_SERVICE_HISTORY_FIELDS.intersection(vals))
    if touched:
        raise AccessError(_(
            'Service history fields are managed by the platform: %(fields)s',
            fields=', '.join(touched)))


# #1525：chart 版本帳本。reconfigure／換方案／改帳密都以 `helm_chart_version` 釘版
# （#552 的 fallback：服務自己的版本優先）——租戶若能自己寫它，就能經
# `/web/dataset/call_kw` 先把帳本改成 registry 裡的任何一版、再按一次「儲存設定」，
# 等於繞過 controller 對 `version` 參數的檢查，把服務降回有洞的舊 chart。
# ACL 給 `base.group_user` 完整寫入、`cloud_service_rule_owner_admin_manage` 給
# owner／admin 寫入，而 owner 資格可自我滿足（自建 workspace），所以只能在這裡擋。
# 平台自己的寫入全部走 `_record_chart_version`（sudo）或 cron／背景執行緒
# （superuser）；判準同上：`env.is_system()`。
_CHART_VERSION_CONTROL_FIELDS = frozenset({'helm_chart_version'})


def _reject_chart_version_writes(env, vals):
    """write() 側：chart 版本帳本只能由平台寫入（#1525）。"""
    if env.is_system():
        return
    touched = sorted(_CHART_VERSION_CONTROL_FIELDS.intersection(vals))
    if touched:
        raise AccessError(_(
            'The chart version of a service is managed by the platform: %(fields)s',
            fields=', '.join(touched)))


# ------------------------------------------------------------------------- #
#  #787：CS Access sync 的抑制鑰匙（private sentinel，形狀抄 #536）           #
# ------------------------------------------------------------------------- #
#
# `cs_skip_access_sync` 抑制 `write()` 之後對 operator 的 Access 宣告式推送
# （#745 接上的那一段）。原本的判定只看 truthy ⇒ `/web/dataset/call_kw` 把呼叫端
# context 整包換進 env，租戶送 `context={"cs_skip_access_sync": true}` 就能讓
# `is_protected` 這類欄位**落地進 Postgres 而 Cloudflare 完全不知情**。方向是
# fail-open：租戶（與平台自己）看到一個開著的保護開關，服務其實完全公開。
#
# 這條路徑的可達性在 #787 是**實測過**的，不是推論：租戶自建 workspace 當下就是
# owner，而 `cloud_service_rule_owner_admin_manage` 給 owner／admin 完整寫入 ⇒
# 真 RPC 打得進 `CloudService.write()`。ACL 那一半沒有任何東西擋得到它——這把閘
# 的判準從頭到尾都不是「你是誰」，而是「context 裡那個值是什麼」。
#
# 形狀刻意逐字沿用 `models/security_access.py` 的 `_INTERNAL_SYNC_CONTROL`
# （`:160`）＋`_is_sync_control`（`:188`）——那是本 repo 已被 #522／#536 實測過
# 的同一套機制，這裡不另發明。
#
# ⚠ **key 名沒有改，改的是值**：`with_context(cs_skip_access_sync=False)` 的
# 「不抑制」語意原封不動，而 `=True` 從此不再開閘。後者是 fail-safe 的方向（多推
# 一次冪等的全量狀態，而不是漏掉一次），但它是**無聲**的，所以下面的判定函式在
# 看到「truthy 但不是 sentinel」時會 WARNING —— 那既是偽造的訊號，也是「還有
# in-process 的碼照舊寫法傳 True」的訊號。
class _InternalCsSyncControl:
    """CS Access sync-control 逃生門的 marker type —— 見上方註解。"""

    __slots__ = ()

    def __repr__(self):
        return '<internal cs sync control>'


_INTERNAL_CS_SYNC_CONTROL = _InternalCsSyncControl()

#: key 本身不是判準（呼叫端當然抄得到這個字串），判準是它的**值身分**。
_CTX_CS_SKIP_ACCESS_SYNC = 'cs_skip_access_sync'


def _cs_skip_access_sync(records):
    """開閘：這一次 write 只搬 DB 狀態，不對 operator 做 Access 推送。

    今天生產碼裡一個呼叫端都沒有（這把鑰匙只有測試佈景在用），helper 仍然要
    存在：它是 `with_context(cs_skip_access_sync=True)` 這種**可偽造寫法**的唯一
    替代品。少了它，下一個真的需要抑制推送的人只會照舊寫 `=True`，然後在一個
    只會印 WARNING 的無聲失效裡 debug。
    """
    return records.with_context(
        **{_CTX_CS_SKIP_ACCESS_SYNC: _INTERNAL_CS_SYNC_CONTROL})


def _is_cs_sync_control(env, key):
    """這個 sync-control 開關是不是**伺服器端程式碼**設的（而非呼叫端偽造的）。

    只有 sentinel 物件算數。JSON 表達不出物件同一性，所以租戶送
    ``context={"cs_skip_access_sync": true}`` 進 ``call_kw`` 時走的是下面那條
    WARNING —— 守衛不開閘，這一次寫入照常把 Access 狀態推給 operator。
    """
    value = env.context.get(key)
    if value is _INTERNAL_CS_SYNC_CONTROL:
        return True
    if value:
        _logger.warning(
            "Ignoring non-sentinel %r in context (value=%r, uid=%s): this "
            "hatch only opens for in-process callers using the module's "
            "helper — see _INTERNAL_CS_SYNC_CONTROL (#787). Either a forged "
            "RPC context, or in-process code still passing True.",
            key, value, env.uid)
    return False


class SubscriptionBindingError(AccessError):
    """#519 提案 B：這一次 `subscription_id` 的綁定不合法。

    獨立型別的用途同 `ProvisionGateError`／`SupplyGateError`——讓測試斷言例外
    **型別**而非文案片段，而且要能和「被 ACL 擋下」（同樣是 `AccessError`）分開。
    """


class ProvisionGateError(AccessError):
    """#519：這一筆 cloud_service 不是從平台的開通流程來的。

    獨立型別的用途與 `SupplyGateError` 相同——**讓測試斷言例外型別而非文案
    片段**。這裡尤其必要：基底 `AccessError` 同時也是 ACL／`ir.rule` 拒絕會丟
    的型別，只斷言 `AccessError` 分不出「被本閘擋下」與「被 ACL 擋下」，那正是
    這道守線最容易假綠的地方（現有的 ACL 測試本來就在斷言 `AccessError`）。
    """


# #336（Wave A）：計數「活躍實例」時排除的刪除終態。CloudService 刪除走真 unlink
# （record 離場），故 state 值集內唯一「即將釋放、不應佔用配額」的過渡態
# 為 'deleting'（Task 0 校驗：值集無 deleted／terminated）。
TERMINAL_STATES = ('deleting',)

# provision「每帳號限一」的 advisory-lock namespace（比照 project_project 的
# _REACH_LOCK_NS reach-lock；任意固定 int、全 module 唯一即可）。'HAPA' = 0x48415041。
_HA_PROVISION_LOCK_NS = 0x48415041

# #501/#518 全域實例上限的**衝突列** UPDATE。
#
# ⚠ 不能只用 advisory lock。Odoo 的 cursor 是 **REPEATABLE READ**
# （`odoo/sql_db.py` 的 `set_isolation_level(ISOLATION_LEVEL_REPEATABLE_READ)`，
# 該 class docstring 明文選用），所以「取鎖之後重新計數」在同一個 tx 內**拿不到新
# snapshot**：輸家取得鎖後看到的仍是取鎖前的舊資料，照樣放行。實測（#518）：cap=1、
# 兩並發，兩邊取鎖後計數都是 1，最終落 2 列——鎖確實序列化了執行，但在它唯一存在的
# 理由上零貢獻。#345 的 docstring 寫「READ COMMITTED」是與事實相反的前提。
#
# 正解是讓並發的第二個 tx 撞上 **write-write 衝突**：兩者都 UPDATE 同一列時，
# PostgreSQL 在 RR 下對後到者回 `could not serialize access due to concurrent
# update`（SQLSTATE 40001），Odoo 的 `retrying()` 會以**新 tx／新 snapshot** 重試
# （最多 5 次），重算後的計數才是對的。UPDATE 的 row lock 同時提供互斥，所以
# advisory lock 在這裡是冗餘的，已移除——少一個機制少一份誤解。
#
# 衝突列選 template row：全域上限的語意範圍就是 template，粒度剛好，也不會誤傷
# 無關交易。`max_per_partner` 的語意範圍是 (template, partner)，不能共用這一列
# （會把它的併發粒度粗化），其修法另見 #518。
_GLOBAL_CAP_CONFLICT_SQL = (
    "UPDATE woow_paas_platform_cloud_app_template "
    "SET write_date = (clock_timestamp() AT TIME ZONE 'UTC') WHERE id = %s"
)

# ------------------------------------------------------------------------- #
#  #489：轉移狀態的 age timeout（F-423-1 Step 2）                             #
# ------------------------------------------------------------------------- #
#
# `deploying` / `upgrading` 在此之前**沒有任何時間上界**：cron 每分鐘、前台每
# 5 秒都只是「再問一次 operator」，沒有一方會說「問太久了，這條不會好了」。
# 永遠不會就緒的服務因此無限期停在轉移狀態，而它與「正在正常部署」在 Odoo 側
# 外觀完全相同（#456 兩筆、#423 W5a 的 VM RBAC 缺權皆屬此類）。
#
# ⚠ 門檻**寧鬆勿緊**，因為誤殺不可自癒：兩層 poll 都只在 deploying /
# upgrading / initializing 才呼叫，一旦寫進 `error` 就再也沒有任何路徑會把它
# poll 回 `running`——即使叢集裡的服務在門檻之後其實就緒了。誤殺＝本來會好的
# 服務被打成 error、租戶得重新下單；晚報＝維運晚 N 分鐘知道。兩者不對稱。
# （#1416 起逾時造成的 error 由 `_cron_recover_timed_out_services` 在時間窗內低頻
# 重查、就緒就推回來——那是補救，不是放寬門檻的理由：誤殺期間租戶照樣看到錯誤。）
#
# 預設值的實證依據（見 PR 論證）：
#   * 容器型 30 分 —— 本 repo 沒有容器型的分段計時實測，取 #489 建議值 15 分
#     的兩倍餘裕。這段時間涵蓋 image pull（跨 registry 的大 image）＋ PVC
#     provision ＋ pod 排程。
#   * VM 型 120 分 —— #423 W6 stg e2e 第二輪實測「下單→running 98s」
#     （registry/OCI 路徑，import 58s）；design §5.2 對 http source 的估計是
#     30–60 分（**二手估計，未實測**）。對未實測的最慢合法路徑取兩倍餘裕。
#     CDI 磁碟 import 是這裡最大的變數，故 VM 必須有自己的一檔。
#
# 兩者皆可用 ir.config_parameter 覆寫——正確值依叢集規格／網路而異，prod 與
# stg 未必相同，不該要改 code 才能調。
DEPLOY_TIMEOUT_MINUTES_DEFAULT = 30
VM_DEPLOY_TIMEOUT_MINUTES_DEFAULT = 120

_DEPLOY_TIMEOUT_PARAM = 'woow_paas_platform.deploy_timeout_minutes'
_VM_DEPLOY_TIMEOUT_PARAM = 'woow_paas_platform.vm_deploy_timeout_minutes'

# ------------------------------------------------------------------------- #
#  #648 (b)：Public URL 的 DNS 落地閘                                         #
# ------------------------------------------------------------------------- #
#
# CS 翻 `running` 早於 Cloudflare DNS record 真的可解析，租戶看到綠燈、點進去拿
# `ERR_NAME_NOT_RESOLVED`。閘加在 `_is_release_ready`（#423 W5a 的單一事實源）。
#
# 🔴 **窗口綁 `_transition_anchor()`，不自己另計時**：#489 的 age timeout 從
# `max(deployed_at, last_upgraded_at)` 起算，而 `deployed_at` 在 helm install 一
# 回來就寫（`_execute_deploy`）。閘若從「第一次探測失敗」起算，「pods 很慢才
# ready ＋ DNS 未落地」會把 age 推過 `deploy_timeout_minutes` ⇒ 服務被寫成
# `error`，而 `error` 不在 `_TIMEOUT_GOVERNED_STATES` 裡、**沒有任何 poll 分支
# 救得回來**。窗口綁同一個錨之後可證明：閘擋住時 age 必定小於寬限期，而寬限期
# 被 clamp 在 timeout 以下 ⇒ 閘永遠不可能造成 `error`。
#
# 語意也對：29 分鐘後才 ready 的服務，問題本來就不是 DNS 傳播。
DNS_GRACE_SECONDS_DEFAULT = 90
# 夾住上界時留給 age timeout 的餘裕（秒）——不能剛好等於 timeout，那樣兩邊會在
# 同一輪 poll 撞在一起。
_DNS_GRACE_SAFETY_MARGIN_SECONDS = 60

_DNS_GRACE_PARAM = 'woow_paas_platform.dns_grace_seconds'
# prod kill switch：新守線誤判時要有一個開關能立刻停損（沙盒方便性是附帶的）。
# 預設開；只有明確設成假值才關。
_DNS_GATE_ENABLED_PARAM = 'woow_paas_platform.dns_gate_enabled'
_FALSEY_PARAM_VALUES = ('0', 'false', 'no', 'off')

# age timeout 管轄的狀態。**`initializing` 刻意不在內**，但它**不再是「不計時」**
# （#1257 Q3 明文取代 #489 的原註解）：post-deploy init 有自己的一組上界，分工是
# 「時間只管等待、次數只管結果」——
#   * 等待（not_executed：pod 還沒 ready、init 請求確定沒送達 operator）不耗額度，
#     受獨立的等待時限約束（poll 拿不到 status 時只有 release 已不存在的 404 會
#     計時；operator 整體連不上是平台故障，不計時）：錨點 `init_wait_since`（進入 initializing 與上次實際執行的 attempt
#     結束，兩者取較晚者），時限 `_INIT_WAIT_TIMEOUT_PARAM`。
#   * 確定失敗每一代 `INIT_FAILED_LIMIT` 次；結果未知（unknown／partial）每一代另計
#     `_INIT_UNKNOWN_LIMIT_PARAM` 次。
# 兩套機制仍不夾擊同一個狀態：age timeout 只看 deploying／upgrading。
_TIMEOUT_GOVERNED_STATES = ('deploying', 'upgrading')

# ------------------------------------------------------------------------- #
#  #1257：post-deploy init 的執行管控（欠 init、attempt、lease、CAS）          #
# ------------------------------------------------------------------------- #
#
# 設計權威：docs/decisions/refactor/post-deploy-init-execution-model.md。
# 每一代（Q24）確定失敗 5 次即轉 error——沿用 #489 以前的 `init_retries` 上界。
INIT_FAILED_LIMIT = 5
# #1534：n8n init 的確定失敗裡，屬於「平台認不得現在的 owner」的步驟。``owner_identity`` 是新
# operator（owner 已存在、平台沒確認過、Email 對不上）；``login`` 是舊 operator 或首次建立時
# 的登入失敗。用完額度時給專屬說明（``_init_failed_message``）。
_N8N_OWNER_FAILURE_STEPS = ('owner_identity', 'login')


def _n8n_owner_login_rejected(outcome):
    """舊 operator 用平台存的 owner 密碼登入被 n8n 拒絕（401；MFA 也是 401＋code 998）。"""
    return outcome.error_step == 'login' and 'HTTP 401' in (outcome.message or '')
# Q8／Q37：unknown 與 partial 共用的額度（第 2 次即轉 error）；可用 param 覆寫。
INIT_UNKNOWN_LIMIT_DEFAULT = 2
_INIT_UNKNOWN_LIMIT_PARAM = 'woow_paas_platform.init_unknown_limit'
# Q10／Q35／Q37：等待時限，對齊 #489 容器型的 30 分；可用 param 覆寫。
INIT_WAIT_TIMEOUT_MINUTES_DEFAULT = 30
_INIT_WAIT_TIMEOUT_PARAM = 'woow_paas_platform.init_wait_timeout_minutes'
# D2-R1：operator 明確回報 not_executed 後的冷卻；常數，不另開 config_parameter。
INIT_NOT_EXECUTED_COOLDOWN_SECONDS = 60
# Q25：停權前若在這些狀態，resume 時依欠 init 標記導回 initializing。
_INIT_TRANSITIONAL_STATES = ('deploying', 'upgrading', 'initializing')
# Q2：claim 撞到別人的 row lock 時不要等（等到的也只是「沒拿到」）。
_INIT_CLAIM_LOCK_TIMEOUT = '5s'
# #1159 Q33：init cron 每 tick 的 wall 預算。claim 條件是「已用時間＋該類最壞 attempt
# （`INIT_ATTEMPT_WORST_SECONDS`）≤ B」；1100 ＋ sleep 70 ＋ 同趟其他 job 預留 600 仍 ≤
# `limit_time_real_cron=1800`（Q34）。成功路徑實測約 5s，一 tick 通常能做多筆。
INIT_CRON_BUDGET_SECONDS = 1100
# Q33 的軟上限：一 tick 最多送出幾次 init；有積壓就自我 `_trigger()` 開新的一趟。
INIT_CRON_SOFT_CAP = 10
_INIT_CRON_XMLID = 'woow_paas_platform.ir_cron_run_post_deploy_inits'
# init cron 的 wall 時鐘（模組層級，測試可替換）。
_init_cron_clock = time.monotonic

# 逾時訊息的分型提示：把 `_is_release_ready` 的 detail 對應到「維運該往哪查」。
# 目的是讓人不必進叢集就知道方向——detail 說「卡在哪」，hint 說「去查什麼」。
#
# 以 detail 的子字串比對（順序即優先序）。detail 字串日後若改動，
# `TestTimeoutDiagnosis` 會紅——這個耦合是刻意用測試守住的，而不是靠註解。
# 未命中時不附 hint（fail-soft：訊息仍有前綴與 detail，只是少了方向）。
#
# #1417：每筆是 ``(needle, hint key, 原因分型)``。
#   * hint 文字在 `CloudService._timeout_hint_text`——要以**服務建立者的語言**翻譯
#     （cron 的 env 語言不是租戶的），模組常數做不到，所以這裡只放 key。
#   * 原因分型決定給租戶看的那一句話（`_timeout_tenant_summary`）。
#   * `waiting=`／`event=` 是 `_describe_unready_pod` 依 operator 回報（PodInfo
#     `waiting`／`last_warning`）寫進 detail 的標記，排在通用的
#     `pods not started`／`pods not ready` 之前：有具體原因就不要給籠統的方向。
#     同一則 detail 有多顆 Pod 時，表上越前面的原因越根本（掛不上磁碟 > 拉不到
#     image > 反覆重啟 > readiness）。
TIMEOUT_CAUSE_STORAGE = 'storage'      # 磁碟掛載／Pod 排程（含儲存額度）
TIMEOUT_CAUSE_IMAGE = 'image'          # 拉不到 image
TIMEOUT_CAUSE_CRASH = 'crash_loop'     # 容器反覆重啟
TIMEOUT_CAUSE_READINESS = 'readiness'  # 起來了但沒回報 ready
TIMEOUT_CAUSE_SUSPENDED = 'suspended'  # 停權中（#482）
TIMEOUT_CAUSE_OTHER = 'other'

_TIMEOUT_HINTS = (
    # #1251 Q45：woow-odoo 判定專屬的 hint。刻意排在最前面（至少要在 'pods not ready'
    # 之前）：先比中先用，別讓通用 hint 把方向帶偏。
    # detail 字串的撞名規則見 docs/decisions/patterns/woow-odoo-readiness-identity.md
    ('chart mismatch', 'chart_mismatch', TIMEOUT_CAUSE_OTHER),
    ('operator lacks pod identity', 'operator_lacks_identity', TIMEOUT_CAUSE_OTHER),
    # #1251 review S2（使用者裁定，超出 Q45 字面的兩筆）：woow-odoo 最常卡住的兩種
    # 情形。前者只擋 server 的 FailedCreate 不產 Pod，'no pods found' 蓋不到；後者若落到
    # 'pods not ready' 會被帶去看 container log，方向錯。
    ('required component missing', 'required_component_missing', TIMEOUT_CAUSE_OTHER),
    ('required pods not running', 'required_pods_not_running', TIMEOUT_CAUSE_OTHER),
    # #1417：operator 報上來的具體原因（只有通用判定 `_is_container_ready` 會寫這些標記）。
    ('event=FailedAttachVolume', 'volume_attach', TIMEOUT_CAUSE_STORAGE),
    ('event=FailedMount', 'volume_mount', TIMEOUT_CAUSE_STORAGE),
    ('event=FailedScheduling', 'scheduling', TIMEOUT_CAUSE_STORAGE),
    ('waiting=ErrImagePull', 'image_pull', TIMEOUT_CAUSE_IMAGE),
    ('waiting=ImagePullBackOff', 'image_pull', TIMEOUT_CAUSE_IMAGE),
    ('waiting=InvalidImageName', 'image_pull', TIMEOUT_CAUSE_IMAGE),
    ('waiting=ErrImageNeverPull', 'image_pull', TIMEOUT_CAUSE_IMAGE),
    ('waiting=CrashLoopBackOff', 'crash_loop', TIMEOUT_CAUSE_CRASH),
    ('waiting=CreateContainerConfigError', 'container_config', TIMEOUT_CAUSE_OTHER),
    ('waiting=CreateContainerError', 'container_config', TIMEOUT_CAUSE_OTHER),
    ('waiting=RunContainerError', 'container_config', TIMEOUT_CAUSE_OTHER),
    ('event=Unhealthy', 'pods_not_ready', TIMEOUT_CAUSE_READINESS),
    ('pod lookup failed', 'pod_lookup_failed', TIMEOUT_CAUSE_OTHER),
    ('no pods found', 'no_pods_found', TIMEOUT_CAUSE_OTHER),
    ('no active pods', 'no_active_pods', TIMEOUT_CAUSE_OTHER),
    # #1417：Pod 根本沒跑起來（Pending）與「Running 但未 ready」分開——10-04 事件的 Pod
    # 從頭到尾停在 Init，卻被下一筆說成 Running、叫人去看 container log。
    ('pods not started', 'pods_not_started', TIMEOUT_CAUSE_OTHER),
    ('pods not ready', 'pods_not_ready', TIMEOUT_CAUSE_READINESS),
    ('include_vm', 'vm_status_missing', TIMEOUT_CAUSE_OTHER),
    ('VMI lookup failed', 'vmi_lookup_failed', TIMEOUT_CAUSE_OTHER),
    ('no VMI yet', 'no_vmi_yet', TIMEOUT_CAUSE_OTHER),
    ('VMI not ready', 'vmi_not_ready', TIMEOUT_CAUSE_OTHER),
    ('helm release status is', 'helm_status_stuck', TIMEOUT_CAUSE_OTHER),
)

# 停權中的服務被 redeploy 後會卡在 `deploying`（#482）：VM 被**正確地** Halted
# 著，所以永遠不會 ready。此時上面那些 hint 全是誤導（叫人去查 pod / quota，
# 而真因是停權），故停權時**取代**而非附加（hint key ``suspended``）。

# #1416：逾時轉 error 的服務繼續由背景 cron 低頻重查的時間窗（小時，從逾時那一刻算）。
# 缺值、非正整數一律退回預設——不停用自癒（同 `_transition_timeout_minutes` 的 fail-safe）。
TIMEOUT_RECOVERY_WINDOW_HOURS_DEFAULT = 7 * 24
_TIMEOUT_RECOVERY_WINDOW_PARAM = 'woow_paas_platform.timeout_recovery_window_hours'

# #497 一次性收斂的判別式：停權旗標仍在，但 state 已被 #493 合併前的舊碼汙染成
# 這些「對租戶不可用」的狀態之一。`initializing` 在內是因為容器型被 upgrade 喚醒後
# poll 會把它從 `deploying` 推進去（poll 不看 `_is_halted`）。
#
# **`running` 刻意不在內**：那批同樣帶 stale 旗標，但誤動的代價是把正在跑的服務關掉
# ——交給 lifecycle cron 分支 2 分流（訂閱有效 → resume 清旗標且零停機）。
_STUCK_SUSPENDED_STATES_497 = ('deploying', 'upgrading', 'initializing', 'error')

# 殘留記號的固定前綴：state 已收斂但 workload 沒能確認停機時寫進 `error_message`。
# 停權服務照跑就是計費繞過，所以這個窗口必須能被 prod rollout 的 checklist 一條
# search 盤出來，而不是散在 log 裡靠人翻。文字改動會讓 #497 的測試紅——它是對維運的
# 契約，不該靜默漂移。
_UNCONFIRMED_WORKLOAD_MARKER_497 = '#497 已收斂但無法確認 workload 已停機'

# #424①／#672：把服務自身的對外根位址交給「需要知道自己公開位址」的 chart 的
# value path 對照表。**這張表是「這個 chart 有沒有對外 dashboard」的唯一事實源**，
# 被兩件事讀：
#
#   (a) 部署時注入 —— `_inject_dashboard_public_url`（→ `HERMES_DASHBOARD_PUBLIC_URL`）
#   (b) 一次性憑證交付 —— `_dashboard_public_url` → controller `_gateway_credentials`
#
# 兩邊讀同一張表，「交給租戶的 URL」與「注入給 agent 的 URL」才**必然**是同一個。
# 各自維護一份的話，漂移那天兩邊都不會報錯：agent 的 OAuth 回呼照舊正確，只有租戶
# 手上那條 MCP 連線是死的（#672 的形狀——而它一路無聲，直到租戶回報整個管理面壞掉）。
#
# 為什麼判準是 value path，而不是像 gateway 那樣讀 seed（`gateway.externalPath`）：
# dashboard 的對外形態是 **root URL、沒有子路徑**（chart 0.1.3 移除 webui container、
# nginx `/` 直連 dashboard，`/dashboard/` 是硬 404），所以 seed 裡**沒有**可推導的
# 路徑值可讀——0.1.3 正是把 `dashboard.externalPath` 整個拿掉的那一版。能表達「這個
# chart 有對外 dashboard」的事實只剩「平台會不會告訴它自己的公開位址」，也就是這張表。
#
# ⚠ 這**不是**新增 slug allowlist（#530 review 否決過的那個做法）：allowlist 是為了
# 某個判斷另立第二份名單、與 chart 的事實各自演化；這裡的表是 chart schema 知識
# （值要寫進哪個鍵）本來就只有這一份、且早於本次改動就存在，判斷是從它**推導**出來
# 的。本次改動沒有新增任何名單，也沒有借用 `template.has_admin_gui`（那是
# admin_password 的閘門，has_admin_gui 模板裡只有 hermes-agent 有 dashboard 對外形態）。
_DASHBOARD_PUBLIC_URL_VALUE_PATHS = {
    'hermes-agent': [('config', 'dashboardPublicUrl')],
}


# #1334：只在**首次安裝**決定、之後永遠沿用的 storageClass 注入點。
#
# PVC 的 ``spec.storageClassName`` 建立後不可變。n8n 的 8gears chart 2.0.1 讀的是
# ``main.persistence.storageClass``（不是頂層 ``persistence``），以前注入錯路徑 ⇒
# 存量 n8n 的 PVC 都落在叢集預設 SC ``longhorn``（Retain，刪服務後 PV 變 Released）。
# 若改成每次部署都注入正確路徑，存量服務下一次 helm upgrade 會把 storageClassName
# 從「未指定」改成 longhorn-delete，API server 拒絕 ⇒ upgrade 失敗、服務轉 error。
#
# 所以這張表的語意是「首裝時寫進 ``helm_values``」（``_pin_install_storage_class``）：
# 新服務從此每次 upgrade 都帶同一個值；存量服務的 helm_values 沒有這個鍵，render
# 結果與以前一模一樣，PVC 不受影響。
_INSTALL_PINNED_STORAGE_CLASS_PATHS = {
    'n8n': [('main', 'persistence', 'storageClass')],
}


# #1532：方案規格（cs_*）要寫進 chart **真正讀的** values 路徑。
#
# 自家 chart（woow-paas-charts 全系列）與 Bitnami 讀頂層 ``resources``／
# ``persistence.size``，是預設。8gears n8n chart 2.0.1 沒有頂層 ``resources``／
# ``persistence``：Pod 讀 ``main.resources``（templates/deployment.yaml）、PVC 讀
# ``main.persistence.size``（templates/pvc.yaml）。以前 overlay 一律寫頂層 ⇒ chart 完全
# 沒讀到，所有 n8n 方案都是範本預設（100m/256Mi～1/1Gi、PVC 2Gi），畫面卻顯示 10～40 GB。
#
# 鍵是範本 slug（與 ``_INSTALL_PINNED_STORAGE_CLASS_PATHS`` 同一套）；換 chart 時這裡要
# 跟著改。``storage`` 的最後一段是「大小」鍵，前面是它所在的 dict。
_PLAN_SPEC_DEFAULT_PATHS = {
    'resources': ('resources',),
    'storage': ('persistence', 'size'),
}
_PLAN_SPEC_VALUE_PATHS = {
    'n8n': {
        'resources': ('main', 'resources'),
        'storage': ('main', 'persistence', 'size'),
    },
}


def _plan_spec_paths(template):
    """``template`` 的方案規格寫入路徑（#1532）；沒列的範本用頂層預設。"""
    return _PLAN_SPEC_VALUE_PATHS.get(template.slug, _PLAN_SPEC_DEFAULT_PATHS)


def _values_dict_at(values, path):
    """回傳 ``values`` 在 ``path`` 的 dict，沿路缺的或不是 dict 的層都換成新的 ``{}``。"""
    d = values
    for key in path:
        if not isinstance(d.get(key), dict):
            d[key] = {}
        d = d[key]
    return d


def _value_at(values, path):
    """``values`` 在 ``path`` 的值；任一層缺或不是 dict 回 ``None``（純讀）。"""
    d = values
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


#: 舊 overlay（#1532 以前）在**不讀頂層**的 chart 上留下的殘值：頂層 ``resources`` 只有
#: requests／limits 的 cpu／memory、頂層 ``persistence`` 只有 size。形狀完全符合才移除，
#: 避免誤刪別人有意寫的值。
_LEGACY_TOP_LEVEL_RESOURCE_KEYS = {'requests', 'limits'}
_LEGACY_TOP_LEVEL_QUANTITY_KEYS = {'cpu', 'memory'}


def _drop_legacy_top_level_plan_spec(values):
    """移除舊 overlay 寫在頂層、chart 根本不讀的方案規格殘值（#1532）。就地修改。"""
    res = values.get('resources')
    if (isinstance(res, dict) and set(res) <= _LEGACY_TOP_LEVEL_RESOURCE_KEYS
            and all(isinstance(v, dict) and set(v) <= _LEGACY_TOP_LEVEL_QUANTITY_KEYS
                    for v in res.values())):
        values.pop('resources')
    persistence = values.get('persistence')
    if isinstance(persistence, dict) and set(persistence) <= {'size'}:
        values.pop('persistence')


# #1334 附帶：n8n 要知道自己的公開網址，否則 webhook 節點顯示的網址、OAuth
# 憑證的 callback、邀請信連結都會是 ``http://localhost:5678``。8gears chart 的
# ``main.config`` 每個鍵直接變成容器 env（``toEnvVars``）。
_N8N_PUBLIC_URL_CONFIG_KEYS = ('WEBHOOK_URL', 'N8N_EDITOR_BASE_URL')

# #1533：n8n 2.x 的 Python Code 節點要另外部署外部 task runner（n8nio/runners），範本沒有
# ⇒ 執行一律失敗（「Python 3 is missing from this system」）。在決定要不要加 runner 之前，
# 明確關掉 Python：錯誤訊息直接說「本服務不允許 Python」，JavaScript 不受影響。部署時注入
# （``_inject_n8n_runtime_defaults``）而不是寫在範本預設：存量服務的 helm_values 是開通時凍結
# 的，寫在範本只會影響新服務。服務自己的 helm_values 有明確值就不覆寫（將來加 runner 時，
# 個別服務可改成 'true'）。
_N8N_RUNTIME_DEFAULT_CONFIG = {'N8N_PYTHON_ENABLED': 'false'}


# 範本的「平台政策鍵」：每次部署都以範本 ``helm_default_values`` 的值為準，不吃服務
# 自己 ``helm_values`` 快照裡的值、也不讓 chart 預設補位。
#
# 為什麼需要：服務的 ``helm_values`` 是開通當下的快照，Redeploy 只換 image
# （#1408）。chart 升版新增的鍵若預設會**開啟**某個行為，而平台決定不開，新開通的服務
# 快照裡有範本寫的 false，既有服務的快照卻沒有這個鍵 ⇒ Redeploy 時 chart 預設 true
# 生效，同一個商品兩種行為。
#
# ha-full（chart home-assistant-full 0.1.6，使用者 2026-10-07 決定）：
#   - ``haProxy.ipBanEnabled``：#1446 不做（登入失敗不封 IP）；
#   - ``seed.unlockCoreConfig``：#1447 不做（不把核心設定搬進 .storage/core.config）。
#
# ⚠ 只列平台決定、租戶不能設的鍵（ha-full 的 helm_value_specs 為空）。刻意**不**收
# ``persistence.size``：PVC 只能長大，範本調大後套到既有服務就是一次沒人決定過的線上
# 擴容（既有服務維持自己快照裡的大小）。範本沒有該鍵時不動（no-op，不刪服務的值）。
_TEMPLATE_POLICY_VALUE_PATHS = {
    'ha-full': [('haProxy', 'ipBanEnabled'), ('seed', 'unlockCoreConfig')],
}
_MISSING = object()


# #1408：範本 ``helm_default_values`` 裡的 image 參照（含 ``repository`` 與 ``tag``
# 或 ``digest`` 的 dict）是**商品版本**的一部分，與 chart 版本同進同退（#336）。
# 開通時它們被 deep-merge 進服務自己的 ``helm_values`` 後就凍結了；管理員 Redeploy
# 會把 chart 升到範本釘的版本，image 卻永遠停在開通當時 ⇒ 新 chart 配舊 image。
# Redeploy 時以範本為準覆蓋這幾個鍵（租戶可設定的 spec 鍵除外）。
_IMAGE_REF_KEYS = ('registry', 'repository', 'tag', 'digest')
# omnigent 的 runner.image 是字串（chart 規定），#1408 的 image 刷新刷不到它，見
# ``_woow_migrate_omnigent_runner_image_017``。
_OMNIGENT_HOST_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/omnigent-host'


def _iter_image_refs(values, path=()):
    """Yield ``(path tuple, ref dict)`` for every image reference in a values tree."""
    if not isinstance(values, dict):
        return
    if isinstance(values.get('repository'), str) and (
            values.get('tag') or values.get('digest')):
        yield path, values
    for key, child in values.items():
        if isinstance(child, dict):
            yield from _iter_image_refs(child, path + (key,))


def _refresh_image_refs(service_values, template_values, tenant_keys=()):
    """Copy the template's image refs over the service's values.

    Returns ``(new_values, changes)``; ``changes`` is a list of
    ``(dotted path, old, new)``. Only ``_IMAGE_REF_KEYS`` are touched, only at paths
    where the template has an image ref, and never a key a tenant may set
    (``tenant_keys``: dotted helm_value_specs keys — the key itself or any ancestor).
    A ref key the service carries but the template no longer does (e.g. a stale
    ``digest`` after the template moved to ``tag@sha256``) is removed, since the chart
    would otherwise keep rendering it.
    """
    new_values = copy.deepcopy(service_values) if isinstance(service_values, dict) else {}
    tenant = set(tenant_keys or ())
    changes = []

    def tenant_owned(dotted):
        parts = dotted.split('.')
        return any('.'.join(parts[:i]) in tenant for i in range(1, len(parts) + 1))

    for path, ref in _iter_image_refs(template_values):
        target = new_values
        for key in path:
            if not isinstance(target.get(key), dict):
                target[key] = {}
            target = target[key]
        for key in _IMAGE_REF_KEYS:
            dotted = '.'.join(path + (key,))
            if tenant_owned(dotted):
                continue
            if key in ref:
                if target.get(key) != ref[key]:
                    changes.append((dotted, target.get(key), ref[key]))
                    target[key] = ref[key]
            elif key in target:
                changes.append((dotted, target[key], None))
                del target[key]
    return new_values, changes


def _split_image(image):
    """``registry/repo:tag@digest`` → ``(repo without registry host, tag, digest)``."""
    image = (image or '').strip()
    digest = None
    if '@' in image:
        image, digest = image.split('@', 1)
    tag = None
    slash = image.rfind('/')
    colon = image.rfind(':')
    if colon > slash:
        image, tag = image[:colon], image[colon + 1:]
    parts = image.split('/')
    if len(parts) > 1 and ('.' in parts[0] or ':' in parts[0] or parts[0] == 'localhost'):
        parts = parts[1:]
    if len(parts) == 2 and parts[0] == 'library':
        parts = parts[1:]
    return '/'.join(parts), tag, digest


def _expected_image(ref):
    """An image-ref dict → ``(repo, tag, digest)`` the running container should show."""
    repository = ref.get('repository') or ''
    if ref.get('registry'):
        repository = '%s/%s' % (ref['registry'].rstrip('/'), repository)
    tag = ref.get('tag') or ''
    digest = ref.get('digest') or None
    if '@' in tag:
        tag, digest = tag.split('@', 1)
    repo, _tag, _digest = _split_image(repository)
    return repo, (tag or None), digest


def _image_mismatches(values, pod_images):
    """Image refs in ``values`` whose repository runs with a different tag/digest.

    A ref whose repository does not appear in any running container is skipped
    (that component is disabled or runs elsewhere) — the check only catches
    "the right container, the wrong version", which is what a stale
    ``helm_values`` produces. Returns a list of ``(expected str, running list)``.
    """
    running = [_split_image(image) for image in pod_images or [] if image]
    mismatches = []
    for _path, ref in _iter_image_refs(values):
        repo, tag, digest = _expected_image(ref)
        same_repo = [r for r in running if r[0] == repo]
        if not same_repo:
            continue
        if digest:
            ok = any(r[2] == digest for r in same_repo)
        else:
            ok = any(r[1] == tag for r in same_repo)
        if not ok:
            expected = repo + (':' + tag if tag else '') + ('@' + digest if digest else '')
            shown = sorted({
                r[0] + (':' + r[1] if r[1] else '') + ('@' + r[2] if r[2] else '')
                for r in same_repo
            })
            mismatches.append((expected, shown))
    return mismatches


# ── 同工作區服務網址注入（只帶網址，絕不帶 token／密碼） ─────────────────────────
# 消費端 chart 學不到「同工作區有沒有某個服務、release 叫什麼」，由平台在組裝第 7 步
# （``_assemble_deploy_values``）把「同工作區、running、最早建立」的提供者叢集內網址
# （Nextcloud 例外：公開 https 網址，見 ``_NEXTCLOUD_BASE_URL_VALUE_PATHS``）
# 寫進消費端的 value path（和 ``_inject_allowed_origins`` 同一 family）。共同規則
# （#1340 Omnigent 起，PLAN §10.2 泛化）：
#
# - 只注入**網址**：金鑰（preauth key、LiteLLM virtual key、omnigent login）一律由租戶
#   在提供者後台建好、自己貼進消費端；平台絕不跨服務傳秘密。
# - **執行期設定**：消費端 chart 只把它 render 進不入 checksum 的 runtime ConfigMap
#   （或 env from ConfigMap）、**不在** pod template 裡 ⇒ 網址出現、改變或消失都**不會**
#   讓 pod 重啟，所以每條部署路徑（含 reset 密碼那條 ``helm upgrade --atomic --timeout
#   15s``）都可以照常重算它，不會重演 F-461-2（pod template 變動 → 15 秒等不到新 pod
#   → 回退斷線）。各 chart 的 render-contract 測試鎖住這一點。
# - 工作區沒有 running 的提供者 ⇒ 不注入（不寫鍵，chart 預設補位）。
# - helm_values 裡已經有非空值（管理員／seed，或白名單退回 allow-all 的 template 由
#   租戶經 API 明確指定）⇒ 尊重它、不覆寫。注入結果只存在於組裝出的 values，從不寫回
#   ``helm_values``。
# - 沒有 fan-out：提供者晚開時，消費端要等自己下一次部署、儲存設定或 reset 才補上。
# - 新增 value path 一律登記 ``scripts/audit/audit_411_reset_values.py``
#   ``_INJECTOR_VALUE_PATHS``（``test_audit_411_classifier`` 以 AST 對帳本檔所有
#   ``*_VALUE_PATHS`` 表）。

# Omnigent BYO host：code-server／pi-agent／open-design 在主容器裡內建 omnigent host
# （woow-paas-charts ``shared/omnigent-host`` 共用套件），標準 values 區塊
# ``omnigentHost.serverUrl``。chart 把它放進 ``<name>-runtime`` ConfigMap（key
# ``server-url``），以目錄掛進容器，kubelet 約一分鐘內就地換檔，supervisor 與
# ``omnigent-host-status`` 每次用到時重讀（套件 README「Environment contract」）。
# supervisor 拿它印 ``omnigent login <url>`` 提示，並在租戶還沒登入過時當備援；host
# 實際連的永遠是租戶最後一次 ``omnigent login`` 的那個 server（WOOW 預設用 Omnigent
# 的 admin 帳號登入三台 host）。
_OMNIGENT_SERVER_URL_VALUE_PATHS = {
    'code-server': [('omnigentHost', 'serverUrl')],
    'pi-agent': [('omnigentHost', 'serverUrl')],
    'open-design': [('omnigentHost', 'serverUrl')],
}
# VPN 節點（woow-paas-charts charts/tailscale ≥ 0.1.2）：``tailscale.controlPlane=workspace``
# 時登入這個網址（同工作區 Headscale 的控制埠 8080）。chart 把它放進
# ``<fullname>-runtime`` ConfigMap，tailscaled 啟動時以 env ``TS_LOGIN_SERVER`` 讀入；
# 其他模式不用它。preauth key 仍是租戶在 Headplane 建好貼進 ``tailscale.authKey``。
# 舊 chart（0.1.1）不認得這個鍵，多寫無害。
_WORKSPACE_HEADSCALE_URL_VALUE_PATHS = {
    'tailscale': [('tailscale', 'workspaceHeadscaleUrl')],
}
# 同工作區 LiteLLM（AI 模型閘道）：四個 agent／IDE chart 的 ``llmGateway.baseUrl``
# （進非 checksum 的 ConfigMap，env ``LITELLM_BASE_URL``；hermes-agent／open-design 只在
# 這個值有設時對同 ns ``app.kubernetes.io/name=litellm``:4000 開 egress 洞），以及
# mcp-litellm（mcp-server chart，cloud_app_mcp_litellm）的 ``config.nonSensitive.connection.base_url``
# （app 第一次啟動 seed-if-absent，只對新開通生效）。virtual key／master key 一律由租戶
# 在 LiteLLM 後台建立後自己貼。網址後綴見 ``_LITELLM_BASE_URL_SUFFIXES``。
_LITELLM_BASE_URL_VALUE_PATHS = {
    'pi-agent': [('llmGateway', 'baseUrl')],
    'code-server': [('llmGateway', 'baseUrl')],
    'open-design': [('llmGateway', 'baseUrl')],
    'hermes-agent': [('llmGateway', 'baseUrl')],
    'mcp-litellm': [('config', 'nonSensitive', 'connection', 'base_url')],
}
# LiteLLM 的 OpenAI 相容 API 在 ``/v1``：``llmGateway.baseUrl`` 是 OpenAI SDK 慣例的
# base_url ⇒ 帶 ``/v1``；mcp-litellm 的 ``connection.base_url`` 是 LiteLLM 管理 API 的根
# （app 自己接 ``/model/info``、``/key/generate`` 等路徑）⇒ **不帶**。
_LITELLM_BASE_URL_SUFFIXES = {
    'pi-agent': '/v1',
    'code-server': '/v1',
    'open-design': '/v1',
    'hermes-agent': '/v1',
    'mcp-litellm': '',
}
# 同工作區 Nextcloud：mcp-nextcloud（mcp-server chart，cloud_app_mcp_nextcloud）的
# ``config.nonSensitive.connection.base_url``（app 第一次啟動 seed-if-absent，只對新開通生效）。
# ⚠ 唯一注入**公開** https 網址的提供者（``_WORKSPACE_PROVIDERS`` 的 ``public_url``）：
# Nextcloud 以 trusted_domains 比對 Host、以 overwritehost／overwrite.cli.url 產生網址，
# 叢集內網址會被當成不信任的網域拒絕，WebDAV 回傳的 href 也是公開網址。值＝提供者的
# ``_public_base_url()``，與 Nextcloud chart 的 ``nextcloud.publicUrl``（``_inject_allowed_origins``）
# 同一個字串。app password 一律由租戶在 Nextcloud 建立後自己貼，平台絕不供給。
_NEXTCLOUD_BASE_URL_VALUE_PATHS = {
    'mcp-nextcloud': [('config', 'nonSensitive', 'connection', 'base_url')],
}

# 提供者（被連的服務）：template slug → 怎麼鏡射它的入口 Service。
#   label：log 用的名稱
#   chart：chart 名（chart 的 fullname helper 用它判斷 release 名是否已含 chart 名）
#   port_path／default_port：決定網址 port 的 helm values 路徑與 chart values.yaml 預設
#   public_url：True ＝ 改給提供者的公開 https 根位址（``_public_base_url()``），不組叢集內
#     網址（此時 port_path／default_port 不用；目前只有 Nextcloud，理由見
#     ``_NEXTCLOUD_BASE_URL_VALUE_PATHS``）
# 每一列對齊 woow-paas-charts ``charts/<chart>/templates/_helpers.tpl``（``<chart>.fullname``）
# 與 ``values.yaml``（omnigent：``service.port`` 8000；headscale：入口 Service 的第二個埠
# ``headscale.port`` 8080——第一個埠 ``service.port`` 3000 是 Headplane 網頁；litellm：
# ``service.port`` 4000）。seed lockstep 由 ``test_workspace_service_url_injection`` 守線。
_OMNIGENT_TEMPLATE_SLUG = 'omnigent'
_OMNIGENT_CHART_NAME = 'omnigent'
_OMNIGENT_DEFAULT_SERVICE_PORT = 8000
_HEADSCALE_TEMPLATE_SLUG = 'headscale'
_LITELLM_TEMPLATE_SLUG = 'litellm'
_NEXTCLOUD_TEMPLATE_SLUG = 'nextcloud'
_WORKSPACE_PROVIDERS = {
    _OMNIGENT_TEMPLATE_SLUG: {
        'label': 'Omnigent', 'chart': _OMNIGENT_CHART_NAME,
        'port_path': ('service', 'port'), 'default_port': _OMNIGENT_DEFAULT_SERVICE_PORT,
    },
    _HEADSCALE_TEMPLATE_SLUG: {
        'label': 'Headscale', 'chart': 'headscale',
        'port_path': ('headscale', 'port'), 'default_port': 8080,
    },
    _LITELLM_TEMPLATE_SLUG: {
        'label': 'LiteLLM', 'chart': 'litellm',
        'port_path': ('service', 'port'), 'default_port': 4000,
    },
    _NEXTCLOUD_TEMPLATE_SLUG: {
        'label': 'Nextcloud', 'chart': 'nextcloud', 'public_url': True,
    },
}
# 組裝第 7 步依序跑的注入器：(消費端 value path 表, 提供者 slug, 每個消費端的網址後綴)。
# 一個 chart 可以同時是好幾個注入器的消費端（例 code-server：Omnigent＋LiteLLM）。
_WORKSPACE_URL_INJECTORS = (
    (_OMNIGENT_SERVER_URL_VALUE_PATHS, _OMNIGENT_TEMPLATE_SLUG, {}),
    (_WORKSPACE_HEADSCALE_URL_VALUE_PATHS, _HEADSCALE_TEMPLATE_SLUG, {}),
    (_LITELLM_BASE_URL_VALUE_PATHS, _LITELLM_TEMPLATE_SLUG, _LITELLM_BASE_URL_SUFFIXES),
    (_NEXTCLOUD_BASE_URL_VALUE_PATHS, _NEXTCLOUD_TEMPLATE_SLUG, {}),
)

# open-design 的 ``networkPolicy.omnigentEgress`` 只對 Omnigent pod 的這個 port 開洞
# （NetworkPolicy 比對的是 pod 的 containerPort＝Service 的 targetPort，不是
# service.port）。Omnigent 的 targetPort 被改掉時，OD 會連不到注入的網址。
_OMNIGENT_EGRESS_PINNED_TARGET_PORT = 8000
# 同一條 OD egress 規則的 pod selector（woow-paas-charts charts/open-design
# ``open-design.omnigentEgress``）：預設 ``app.kubernetes.io/name=omnigent``＋
# ``component=server``；注入的網址主機是 ``<X>-omnigent[.<ns>…]`` 時再釘
# ``app.kubernetes.io/instance=<X>``（X＝主機名去掉 ``-omnigent``）。Omnigent chart
# 的 pod 標籤是 name＝``nameOverride``（預設 chart 名）、instance＝release 名，所以
# 只有「Service 名＝``<release>-omnigent``、沒有 nameOverride」時兩邊才對得上。
_OMNIGENT_EGRESS_SELECTOR_NAME = 'omnigent'
_OMNIGENT_EGRESS_HOST_SUFFIX = '-omnigent'
# hermes-agent／open-design 的 ``networkPolicy.litellmEgress``：pod selector
# ``app.kubernetes.io/name=litellm``＋``component=app``、只開 pod port 4000（charts/litellm
# 的 pod name 標籤＝``nameOverride``（預設 chart 名）、containerPort＝``service.targetPort``）。
# 對不上時這兩個 chart 連不到注入的網址（pi-agent／code-server 的 egress 不受影響）。
_LITELLM_EGRESS_SELECTOR_NAME = 'litellm'
_LITELLM_EGRESS_PINNED_TARGET_PORT = 4000
# k8s DNS label（RFC 1123）：release 名、namespace、Service 名都必須符合，
# 否則不組網址（防止奇怪的 helm_values 把任意字串拼進 URL）。
_DNS_LABEL_RE = re.compile(r'^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$')


def _unflatten_dotpath_keys(flat_dict):
    """Convert flat dot-path keys to nested dict.

    Example: {"a.b.c": 1} → {"a": {"b": {"c": 1}}}
    """
    result = {}
    for key, value in flat_dict.items():
        parts = key.split('.')
        if len(parts) == 1:
            result[key] = value
            continue
        d = result
        for part in parts[:-1]:
            if part not in d or not isinstance(d[part], dict):
                d[part] = {}
            d = d[part]
        d[parts[-1]] = value
    return result


def _deep_merge(base, override):
    """Deep merge override into base. Override values win."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# #1417：逾時 detail 裡每類最多列幾顆 Pod、事件訊息最多留幾個字（error_message 會上畫面）。
_UNREADY_PODS_LISTED = 3
_UNREADY_EVENT_TEXT_LIMIT = 200


def _describe_unready_pod(pod):
    """detail 用的未就緒 Pod 摘要（#1417）：``name[phase ready waiting=… event=…]``。

    ``waiting=<reason>``／``event=<reason>`` 是 `_TIMEOUT_HINTS` 比對的標記，來源是
    operator 的 ``PodInfo.waiting``／``last_warning``；舊 operator 沒有這兩個 key ⇒ 只剩
    名字、phase、ready（照舊可用，只是沒有具體原因）。
    """
    parts = [pod.get('phase') or 'Unknown', pod.get('ready') or '?']
    waiting = pod.get('waiting') or {}
    if waiting.get('reason'):
        kind = 'init container' if waiting.get('init_container') else 'container'
        parts.append('waiting=%s(%s %s)' % (
            waiting['reason'], kind, waiting.get('container') or '?'))
    event = pod.get('last_warning') or {}
    if event.get('reason'):
        message = ' '.join(str(event.get('message') or '').split())
        if len(message) > _UNREADY_EVENT_TEXT_LIMIT:
            message = message[:_UNREADY_EVENT_TEXT_LIMIT - 1] + '…'
        parts.append('event=%s: %s' % (event['reason'], message) if message
                     else 'event=%s' % event['reason'])
    return '%s[%s]' % (pod.get('name') or '?', ' '.join(parts))


def _describe_unready_pods(label, pods):
    """``label: a[…], b[…] (+N more)``；沒有 Pod 回 None。"""
    if not pods:
        return None
    listed = ', '.join(map(_describe_unready_pod, pods[:_UNREADY_PODS_LISTED]))
    extra = len(pods) - _UNREADY_PODS_LISTED
    return '%s: %s%s' % (label, listed, ' (+%d more)' % extra if extra > 0 else '')


class CloudService(models.Model):
    _name = 'woow_paas_platform.cloud_service'
    _inherit = ['woow_paas_platform.subscription_lifecycle_mixin']
    _description = 'Cloud Service Instance'
    _order = 'create_date desc'

    _sql_constraints = [
        (
            'unique_subdomain',
            'UNIQUE(subdomain)',
            'Subdomain must be unique across all services.',
        ),
        (
            'unique_reference_id',
            'UNIQUE(reference_id)',
            'Reference ID must be unique.',
        ),
    ]

    # Relationships
    workspace_id = fields.Many2one(
        comodel_name='woow_paas_platform.workspace',
        string='Workspace',
        required=True,
        ondelete='cascade',
        help='Workspace this service belongs to',
    )
    template_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_app_template',
        string='Application Template',
        required=True,
        ondelete='restrict',
        help='The application template used to create this service',
    )
    provision_partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Provision Owner (One Per Account)',
        index=True,
        help='Filled in only for platform auto-provisioned "one per account" '
             'services (such as HA PaaS), so the partial unique index can '
             'guarantee at most one per partner. Left empty for ordinary '
             'services (#336).',
    )

    # Identity
    name = fields.Char(
        string='Service Name',
        required=True,
        help='User-defined name for this service instance',
    )
    reference_id = fields.Char(
        string='Reference ID',
        index=True,
        readonly=True,
        default=lambda self: str(uuid.uuid4()),
        help='Immutable unique identifier for this service',
    )
    deployment_id = fields.Char(
        string='Deployment ID',
        help='External deployment system identifier',
    )

    # State
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('deploying', 'Deploying'),
            ('initializing', 'Initializing'),
            ('running', 'Running'),
            ('error', 'Error'),
            ('upgrading', 'Upgrading'),
            ('suspended', 'Suspended'),
            ('deleting', 'Deleting'),
        ],
        string='State',
        default='pending',
        help='Current state of the service deployment',
    )
    error_message = fields.Text(
        string='Error Message',
        help='Error details if deployment failed',
    )
    # #1416：目前這筆 error 是 #489 的部署逾時寫的——以及何時寫的。逾時只代表「等太久」，
    # 不代表失敗；有值時背景 cron（`_cron_recover_timed_out_services`）會在時間窗內繼續
    # 重查，release 就緒就照正常路徑推進。它描述的是「現在這則 error_message 的來源」：
    # 改寫 error_message 或離開 error（停權除外）都會清掉它（`write()`），其他來源的
    # error 因此永遠不會被這條自癒碰到。只有平台寫得進去（`_reject_timeout_recovery_control_writes`）。
    transition_timeout_at = fields.Datetime(
        string='Deployment Timed Out At',
        copy=False,
        readonly=True,
        help='Set when the deployment age timeout turned this service into an error. '
             'While set, a background job keeps checking the release for a limited '
             'time and resumes the service as soon as it becomes ready.',
    )
    # #1552：目前這筆 error 是背景 upgrade（修改配置／換方案）失敗、新設定**沒有確認套用**
    # 寫的——以及何時寫的。有值＝租戶可以直接重新送出修改配置（出口），其他來源的 error
    # 不受影響。與 ``transition_timeout_at`` 同一套生命週期：改寫 error_message 或離開
    # error（停權除外）都會清掉它（`write()`）；只有平台寫得進去。
    upgrade_failed_at = fields.Datetime(
        string='Upgrade Failed At',
        copy=False,
        readonly=True,
        help='Set when a configuration change could not be confirmed as applied (the '
             'deployment service was unreachable, busy or failed). While set, the tenant '
             'can submit the configuration again from the error state.',
    )
    # #1431：服務第一次進 running 的時間。由 `write()`／`create()` 在 state 第一次變成
    # running 時統一寫入（不逐路徑 stamp，pitfall reaped-since-single-path-stamp-gc-immunity），
    # 之後不再改、也不會被清——`init_ready_*` 那類「本段 initializing」的觀測會被清掉，不能
    # 拿來回答「曾經能用過嗎」。空值＝從未正常運作過：刪除時孤兒 PVC 只留 1 天（#1431）。
    # 18.0.1.0.209 以前的服務由 migration 保守回填（見 `_woow_migrate_first_running_at_1431`）。
    first_running_at = fields.Datetime(
        string='First Running At',
        copy=False,
        readonly=True,
        help='When the service reached the running state for the first time. Empty means '
             'it never worked: its leftover disks are kept for only 1 day after deletion. '
             'Services created before version 18.0.1.0.209 were backfilled conservatively.',
    )
    # #1424：operator 明確拒絕開通（#1348 工作區儲存配額、#1415 叢集儲存額度，回 409）、
    # 服務也從沒 running 過時，平台自動取消它的訂閱，並在這裡記下拒絕的錯誤碼。有值的服務：
    # lifecycle cron 不對它停權（叢集上沒有可停的東西，打 operator 只會 404、把說明蓋掉）；
    # 訂閱雖已取消，租戶仍可刪除它（`_check_operation_allowed`）。只有平台寫得進去。
    provision_rejected_code = fields.Char(
        string='Provisioning Rejected',
        copy=False,
        readonly=True,
        help='Error code with which the deployment service refused to create this service '
             '(workspace storage quota or cluster storage capacity). The subscription of a '
             'service that never ran was cancelled automatically at that moment, so it is '
             'never charged; the service can still be deleted.',
    )

    # VM 型服務的開通進度（#423 W5a，design §7.4）。
    #
    # 為何需要：VM 首次開通要把一整顆磁碟映像 import 進 PVC，耗時遠長於容器
    # 型的 pull image。沒有進度時服務只是停在 `deploying`，租戶（與維運）
    # 看不出是「正在跑」還是「卡住了」。CDI 原生提供百分比，poll 時順手帶回。
    #
    # 這**不是**狀態機的一部分：它純供顯示，就緒判定完全不看它
    # （DataVolume 查詢失敗只會讓進度消失，不該擋住一台已 Ready 的 VM）。
    import_progress = fields.Char(
        string='Import Progress',
        help='Import progress of the VM disk image (e.g. "42.31%"); display '
             'only, it takes no part in the readiness decision',
    )

    # Post-deploy initialization tracking
    # #1257：語意收窄為「本代確定失敗次數」（Q3／Q24）；unknown 另計在
    # `init_unknown_count`，not_executed 不計。
    init_retries = fields.Integer(
        string='Init Failures (this generation)',
        default=0,
        copy=False,
        help='Definite post-deploy init failures in the current init generation; '
             'the service turns to error at INIT_FAILED_LIMIT',
    )
    init_error = fields.Text(
        string='Init Error',
        copy=False,
        help='Last (masked) error from post-deploy initialization',
    )
    # #1257：**不再**決定要不要 init（那是 `init_pending` 的事）。保留欄位是因為
    # migration Q26(a) 要讀它回填，以及「addon 變更留下的待辦」仍是有用的顯示資訊。
    addon_update_pending = fields.Boolean(
        string='Addon Update Pending',
        default=False,
        copy=False,
        help='Whether addon selection changed during last upgrade and needs post-deploy init',
    )
    # ---- #1257 執行管控（ADR post-deploy-init-execution-model）----
    # Q9：持久的「欠 init」標記。需要 init 與否**只**看它；每次需要 init 算一代
    # （`_open_init_generation` 是唯一開代入口），失敗額度按代計算。停權、resume、
    # 不改 addon 的 reconfigure 都不清它，只有 init 成功會清。
    init_pending = fields.Boolean(
        string='Init Owed',
        default=False,
        copy=False,
        help='The service owes a post-deploy init (#1257 Q9); cleared only by a '
             'successful init',
    )
    init_generation = fields.Integer(
        string='Init Generation',
        default=0,
        copy=False,
        help='Incremented every time a new init generation opens (#1257 Q24)',
    )
    init_unknown_count = fields.Integer(
        string='Init Unknown Results (this generation)',
        default=0,
        copy=False,
        help='unknown + partial results in the current generation (#1257 Q8)',
    )
    init_attempt_seq = fields.Integer(
        string='Init Attempt Counter',
        default=0,
        copy=False,
        help='Per-service attempt ordinal, bumped by the claim itself',
    )
    init_attempt_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_service_init_attempt',
        string='Current Init Attempt',
        ondelete='set null',
        copy=False,
        groups='base.group_system',
        help='The attempt holding (or last holding) the lease',
    )
    # Q2：lease 一律以 DB 時鐘判斷（`_init_db_now`），存 UTC naive。
    init_lease_until = fields.Datetime(
        string='Init Lease Until',
        copy=False,
        help='While in the future an init request may be in flight; reconfigure, '
             'rollback and redeploy are refused (#1257 Q9／Q23／Q27)',
    )
    # Q10：等待時限的錨點（UTC）。
    init_wait_since = fields.Datetime(
        string='Init Waiting Since',
        copy=False,
        help='Later of "entered initializing" and "last actually executed attempt '
             'finished"; the readiness wait timeout counts from here (#1257 Q10)',
    )
    # D2-R：operator readiness 的獨立累計時計。只計 not_executed 的 request wall time
    # 與其後完整 60 秒冷卻；eligible 之後未獲 claim 的純容量排隊停止計時。
    init_wait_spent_seconds = fields.Float(
        string='Init Operator Wait Spent Seconds',
        default=0,
        copy=False,
        help='Accumulated operator not_executed wait budget; never backfilled from history.',
    )
    init_wait_started_at = fields.Datetime(
        string='Init Operator Wait Started At',
        copy=False,
        help='Start of the active not_executed cooldown interval (UTC).',
    )
    init_retry_eligible_at = fields.Datetime(
        string='Init Retry Eligible At',
        copy=False,
        help='The full not_executed cooldown deadline; capacity queueing after it is free.',
    )
    init_queue_turn = fields.Datetime(
        string='Init Queue Turn',
        copy=False,
        help='Same-init-type fair queue position; successful claims move themselves to the tail.',
    )
    init_capacity_wait_since = fields.Datetime(
        string='Init Capacity Waiting Since',
        copy=False,
        readonly=True,
        help='Start of continuously observed eligible-unclaimed capacity waiting. '
             'Never backfilled from first-ready or queue order.',
    )
    # Q27：redeploy 撞到在途 lease 時登記的意圖；背景看到就停止自動重送，lease
    # 結束後先執行 redeploy。
    init_redeploy_requested = fields.Boolean(
        string='Redeploy Requested',
        default=False,
        copy=False,
        groups='base.group_system',
    )
    # Q30：init 從哪個出口轉成 error。只有額度出口（failed／unknown）放行
    # reconfigure；等待逾時與 helm 失敗一樣照擋。
    init_exit = fields.Selection(
        selection=[
            ('failed', 'Definite failures exhausted'),
            ('unknown', 'Unknown results exhausted'),
            ('wait_timeout', 'Readiness wait timed out'),
            # #1159 D1：等待被 fast-fail 訊號短路（Pod 反覆重啟且未就緒）。與
            # wait_timeout 同類——pods 起不來不是 init 的額度出口，reconfigure 照擋。
            ('fast_fail', 'Readiness wait short-circuited (crash loop)'),
        ],
        string='Init Exit',
        copy=False,
    )
    # ---- #1159 執行面（背景觀測；list/get 只讀這些值，不自己探）----
    # Q1／Q40：背景 poll 最近一次成功向 operator 取得 status 的時間（UTC）。list/get
    # 只回這個「最近觀測」，前端據此在轉移中停太久時顯示「最後更新 X 前」；監控看全平台
    # 最舊的一筆（Q38）。
    observed_at = fields.Datetime(
        string='Last Observed',
        copy=False,
        readonly=True,
        help='Last time the background poll got a status from the operator '
             '(#1159 Q1); list/get only report this, they never poll',
    )
    observation_episode_started_at = fields.Datetime(
        string='Observation Episode Started At',
        copy=False,
        readonly=True,
        help='Platform timestamp for entry into the current transitional state. '
             'It is a wait boundary, never an operator observation.',
    )
    # Q4／Q33：短 poll 觀測到 initializing 的 pods ready ＝ 排進 init cron 的佇列。
    # init cron 只 claim 這些服務；not ready 時清掉（等待期間由時限約束）。
    init_ready_observed = fields.Boolean(
        string='Init Ready Observed',
        default=False,
        copy=False,
        help='The last background poll saw the pods ready while initializing: the '
             'service is queued for the init cron (#1159 Q1／Q4)',
    )
    # Q33／Q28b：本次 initializing 第一次觀測到 ready 的時間（UTC）。init cron 依它
    # 排「等待最久者」；n8n owner 是否在 5 分內被平台確認也從這裡量（不是 owner 建立時間）。
    init_ready_since = fields.Datetime(
        string='Init Ready Since',
        copy=False,
        help='First time the pods were seen ready during this initializing stretch '
             '(#1159 Q33 queue order, Q28b n8n owner confirmation window)',
    )
    # Q28b：本 episode 已發出「平台尚未確認 n8n owner」告警的時間；每 episode 一行，
    # 開新一代或重新進 initializing 時與 init_ready_since 一起清掉。只有平台寫得進去。
    n8n_owner_alerted_at = fields.Datetime(
        string='n8n Owner Alerted At',
        copy=False,
        readonly=True,
        help='When the platform monitor reported that the n8n owner was not confirmed '
             'within the window of this initializing episode (#1159 Q28b)',
    )

    # Network
    # 對外位址（chart 的 ngrok sidecar 廣告的那一個：EMQX 的 tcp://、Headscale 的
    # https://）。由背景 poll 經 `_release_facts` 回寫——**唯讀帳本欄位**，不是設定：
    # 租戶要改位址是去改 helm values 的 ngrok 設定，不是改這裡。
    # 空值是正常狀態（商品沒這個能力、租戶沒開、agent 還沒連上、sidecar 撤回）。
    # 18.0.1.0.205 以前叫 mqtt_public_url；改名後的值搬移見 `init()`。
    public_tunnel_url = fields.Char(
        string='Public Tunnel Address',
        readonly=True,
        copy=False,
        help='The tcp://host:port or https://host the chart\'s ngrok sidecar currently '
             'advertises, read back from its container log by the background poll. '
             'Empty when the product has no such address or the agent has not '
             'connected.',
    )
    # #1559：沒有對外位址時，ngrok sidecar log 說得出的原因（authtoken 錯、沒填、網域被占用、
    # Headscale 要重啟才會改用 ngrok 網址……），已依服務建立者的語言組好的一句話。與
    # public_tunnel_url 同一個咽喉回寫（`_release_facts`），有位址時一律清掉。operator 只回
    # 封閉集合的原因碼，這裡不會出現任何原始 log。
    public_tunnel_error = fields.Char(
        string='Public Tunnel Error',
        readonly=True,
        copy=False,
        help='Why the ngrok sidecar has no public address, when its log says so '
             '(wrong or missing authtoken, domain already in use, restart needed).',
    )
    subdomain = fields.Char(
        string='Subdomain',
        help='Subdomain for accessing this service (combined with PaaS domain, e.g., myapp.woowtech.io)',
    )
    custom_domain = fields.Char(
        string='Custom Domain',
        help='Custom domain configured by user',
    )
    public_url_dns_unverified = fields.Boolean(
        string='Public URL Not Verified',
        default=False,
        copy=False,
        help=(
            "The service is ready, but the DNS record for its Public URL never "
            "resolved within the grace period, so it was let through **with "
            "reservations** (#648 (b)). Meant for a soft hint in the front "
            "end.\n\n"
            "⚠ Deliberately NOT written into `error_message`: the "
            "`t-if=\"service.error_message\"` in `ServiceDetailPage.xml` **does "
            "not check state**, so writing it there would make a healthy "
            "service raise a red failure banner — while `ServiceCard.xml` "
            "additionally checks `state === 'error'`, so the two already behave "
            "differently.\n\n"
            "⚠ `running` is in no status-poll domain (the status cron picks up "
            "deploying/upgrading/initializing/deleting), so the flag is cleared "
            "by the dedicated background re-probe cron "
            "`_cron_reprobe_public_url_dns` (every 15 minutes, #1159 Q16/D4; "
            "see `_refresh_public_url_dns_flag`). Service list/get only read it."
        ),
    )
    internal_port = fields.Integer(
        string='Internal Port',
        help='Internal port where the service is listening',
    )

    # ------------------------------------------------------------------ #
    #  #744 CS Access Control：整組鏡射 SA route 的 access 欄位            #
    #  （設計拍板 10，見 docs/plans/2026-Q3/                              #
    #   2026-08-15-cs-access-control-design.md）                          #
    # ------------------------------------------------------------------ #
    #
    # 語意逐欄對齊 `security_access.py` 的 `SecurityAccessRoute`——**刻意鏡射
    # 而不是共用**：兩者的載體不同（route 掛 SA 專屬 tunnel，CS 走平台共用
    # tunnel），但送到 operator 的 Access payload 是同一份 hostname 級的宣告，
    # 所以欄位集必須一致，否則 T3 的 payload builder 會被迫在兩邊各寫一套。
    #
    # ⚠ 這些欄位刻意**沒有** `groups=`。本 model 已經有一批
    # `groups='base.group_system'` 的欄位（helm_values / deploy_params /
    # mcp_auth_token…），照抄看起來很自然，但 #739 的教訓正是反過來的：欄位級
    # `groups=` 會讓**平台自己**走租戶身分的寫入路徑吃 AccessError，而
    # superuser TransactionCase 看不見（欄位級 groups 在 su env 不生效）⇒
    # 「從來沒成功過」可以整整潛伏到 stg 實測才浮出來。`access_app_id` 要的是
    # 「不可被租戶搬動」，那是身分錨點守衛（見模組頂端
    # `_CS_ACCESS_IDENTITY_ANCHOR_FIELDS`）的職責，它對所有人生效且在 su env
    # 下照樣 fire——用對工具，不要用會假綠的那個。
    is_protected = fields.Boolean(
        string='Access Protected',
        default=False,
        help='When enabled, the public subdomain of this service is protected '
             'by Cloudflare Access: every visitor must authenticate through '
             'Access first. The granularity is the whole subdomain '
             '(decision 5).',
    )
    session_duration = fields.Selection(
        selection=[
            ('30m', '30 minutes'), ('1h', '1 hour'), ('8h', '8 hours'),
            ('24h', '24 hours'), ('168h', '7 days'),
        ],
        string='Access Session Duration',
        default='24h',
        help='How long users stay authenticated before re-login (this service).',
    )
    login_google = fields.Boolean(string='Access Login: Google', default=False)
    login_email_otp = fields.Boolean(
        string='Access Login: Email OTP', default=False)
    access_app_id = fields.Char(
        string='CF Access App ID', copy=False,
        help='Cloudflare Access Application id for THIS service (written back '
             'on sync). Identity anchor: only the server-side sentinel path can '
             'write it.',
    )
    access_policy_ids = fields.One2many(
        comodel_name='woow_paas_platform.security_access_access_policy',
        inverse_name='cloud_service_id',
        string='Access Policies',
        help='Access policies for this service (the policy model is shared with '
             'SA routes; the anchor is an exclusive arc, see #744).',
    )
    access_unresolved_idp_types = fields.Char(
        string='Unresolved Login Methods', copy=False,
        help='Requested login methods (e.g. google) with no matching Cloudflare '
             'account IdP at the last sync of THIS service. Non-empty means the '
             'login restriction **silently widened** — if the resolved set '
             'emptied, the Access app falls back to ALL available IdPs. '
             'Surfaced deliberately, not dropped.',
    )
    access_protection_lost = fields.Boolean(
        string='Protection Not In Effect', copy=False, default=False,
        help='True = the Cloudflare Access application for this service has '
             'been torn down while the record itself is still here (the delete '
             'transaction rolled back; the Cloudflare half cannot be rolled '
             'back). is_protected is then only an **intent**: nothing on CF is '
             'forcing a login and the subdomain is reachable as usual. Cleared '
             'by the next successful Access sync. Deliberately kept apart from '
             'is_protected — lowering is_protected would mean the platform '
             'changing a setting the tenant can see (a product-visible change), '
             'which is not decided yet (#794).',
    )

    # Helm Release Info
    helm_release_name = fields.Char(
        string='Helm Release Name',
        help='Name of the Helm release in Kubernetes',
    )
    helm_namespace = fields.Char(
        string='Helm Namespace',
        help='Kubernetes namespace where the service is deployed',
    )
    helm_values = fields.Text(
        string='Helm Values',
        groups='base.group_system',
        help='JSON object with Helm values used for deployment',
    )
    helm_revision = fields.Integer(
        string='Helm Revision',
        default=1,
        help='Current Helm release revision number',
    )
    helm_chart_version = fields.Char(
        string='Chart Version',
        help='Version of the Helm chart currently deployed',
    )
    last_release_sync_at = fields.Datetime(
        string='Last Release Sync At',
        help=(
            "When the release facts (chart version / revision) were last "
            "fetched from the operator. Mind the semantics: this is the last "
            "time we **asked**, not the last time the **value changed** — what "
            "a tenant needs to know is how fresh the version number in front of "
            "them is, not when it last changed.\n\n"
            "⚠ This field is stamped once a day by the #627 (i) daily "
            "reconciliation cron, **at the cost of `write_date` now changing "
            "every day**: before that, `write_date` stayed at the last time the "
            "platform itself touched this service, which could be read as "
            "\"how long this record has gone untouched\". The #552 / #627 "
            "investigations used that signal twice (to establish that a given "
            "helm upgrade was not initiated by Odoo, and to prove that an "
            "upgrade never wrote the facts back). **That diagnostic ability is "
            "gone as of this version** — to tell when the platform last acted "
            "on a service, read `last_upgraded_at` / `deployed_at`, no longer "
            "`write_date`."
        ),
    )

    # Resources
    allocated_vcpu = fields.Integer(
        string='Allocated vCPU',
        help='Number of vCPU cores allocated to this service',
    )
    allocated_ram_gb = fields.Float(
        string='Allocated RAM (GB)',
        help='RAM in GB allocated to this service',
    )
    allocated_storage_gb = fields.Integer(
        string='Allocated Storage (GB)',
        help='Storage in GB allocated to this service',
    )

    # Support Project — Phase A M:N reverse (shared project_cloud_service_rel)
    project_ids = fields.Many2many(
        comodel_name='project.project',
        relation='project_cloud_service_rel',
        column1='cloud_service_id',
        column2='project_id',
        string='Support Projects',
    )

    # MCP Sidecar
    mcp_auth_token = fields.Char(
        string='MCP Auth Token',
        readonly=True,
        groups='base.group_system',
        help='Authentication token for the MCP sidecar, auto-generated during deployment',
    )
    n8n_api_key = fields.Char(
        string='N8N API Key',
        readonly=True,
        groups='base.group_system',
        help='API key for n8n instance, generated during post-deploy init',
    )
    n8n_owner_email = fields.Char(
        string='N8N Owner Email',
        readonly=True,
        groups='base.group_system',
    )
    n8n_owner_password = fields.Char(
        string='N8N Owner Password',
        readonly=True,
        groups='base.group_system',
    )
    # Timestamps
    deployed_at = fields.Datetime(
        string='Deployed At',
        help='Timestamp when service was first deployed',
    )
    last_upgraded_at = fields.Datetime(
        string='Last Upgraded At',
        help='Timestamp of the most recent upgrade',
    )
    # #1408：Redeploy 之後「實際跑的」是否等於範本預期——helm upgrade 成功不代表
    # workload 換到了新 image（pi-agent 就是 chart 升了、image 沒動）。pending 在
    # Redeploy 成功時設起，服務下一次就緒時由 poll 核對並清掉；結果寫進 message。
    redeploy_verify_pending = fields.Boolean(
        string='Redeploy Verification Pending',
        default=False,
        copy=False,
        groups='base.group_system',
    )
    redeploy_check_message = fields.Text(
        string='Redeploy Check',
        copy=False,
        readonly=True,
        groups='base.group_system',
        help='Result of comparing the running workload with the product template '
             'after the last redeploy (chart version and images). Empty when they match.',
    )

    # Odoo Addons
    odoo_addon_ids = fields.Many2many(
        comodel_name='woow_paas_platform.cloud_odoo_addon',
        relation='cloud_service_odoo_addon_rel',
        column1='service_id',
        column2='addon_id',
        string='Odoo Addons',
        help='Odoo addons selected for this service',
    )

    # Deploy metadata (JSON blob consumed by _execute_deploy cron)
    deploy_params = fields.Text(
        string='Deploy Params',
        groups='base.group_system',
        help='JSON parameters for pending deployment (expose_config, mcp_api_key, etc.)',
    )
    # #1599：付款入帳／訂閱回到 in_progress 的交易只寫這個旗標並 `_trigger()` 付款後
    # 復權的 cron，commit 之後才由 cron 呼叫 operator（見 subscription_lifecycle_cron）。
    # `groups='base.group_system'`：只有平台身分寫得進去（同 `deploy_params`）——
    # 這是「請盡快復權」的佇列，不是租戶的設定。
    resume_requested = fields.Boolean(
        string='Resume Requested',
        copy=False,
        groups='base.group_system',
        help='Set by the platform when a payment or a renewal makes the subscription of '
             'this suspended service active again. A background job resumes the service '
             'right after the payment is committed, then clears the flag.',
    )

    def init(self):
        """建 partial unique index：同一 provision_partner_id 至多一列（#336 每帳號
        限一的 DB backstop）。比照 skill.py init 的 partial-unique-index 慣例——Odoo
        原生 hook，每次 install / -u 自動跑、冪等（IF NOT EXISTS），涵蓋新裝與升級
        兩條路徑，免 migration + post_init 三件套。不加 state 條件——排除 deleting
        會放行 delete/create race（並發第二筆 INSERT commit 撞 IntegrityError →
        provision controller catch → 回既有）。"""
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                woow_paas_cloud_service_provision_partner_uniq
            ON woow_paas_platform_cloud_service (provision_partner_id)
            WHERE provision_partner_id IS NOT NULL
        """)
        # #519 提案 B：一筆訂閱至多對應一筆 cloud_service。
        #
        # 為什麼是 DB 索引而不是 Python COUNT：本檔 `create()` 的 docstring 已經
        # 記過——Odoo 的 cursor 是 **REPEATABLE READ**，取 advisory lock **不會**
        # 刷新 snapshot ⇒ check-after-act 在並發下無效（那正是 `max_per_partner`
        # 至今仍可被繞過的原因，#518）。而「一筆訂閱至多對應一筆 service」正好就是
        # partial unique index 表達得出的「至多一列」，比照上面 provision_partner_id
        # 的既有先例。
        #
        # ⚠ 既有重複列會讓這一句直接失敗（psycopg2 UniqueViolation）。那個失敗訊息
        # 讀不出「是哪幾筆」，所以偵測與可讀的錯誤由
        # `migrations/18.0.1.0.139/pre-migrate.py` 先做掉；這裡只負責建索引，並且
        # 對 fresh install 與 `-u` 兩條路徑都冪等（`IF NOT EXISTS`）。
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                woow_paas_cloud_service_subscription_uniq
            ON woow_paas_platform_cloud_service (subscription_id)
            WHERE subscription_id IS NOT NULL
        """)
        self._carry_over_legacy_tunnel_url()

    def _carry_over_legacy_tunnel_url(self):
        """P1：帳本欄位改名 mqtt_public_url → public_tunnel_url，把舊值搬過來。

        為什麼在 ``init()`` 而不是 migration：migration 要 bump manifest 版號（規則 B），
        會和並行的發版 PR 搶號；``init()`` 每次 install／``-u`` 都跑，而且跑在
        ``_auto_init`` 建好新欄位之後、``ir.model.data._process_end`` 清掉已移除欄位
        （連同舊 column）之前——正好是舊值還在、新欄位已在的那一刻。

        不搬的後果：EMQX 服務的對外位址在升級後消失，直到每日對帳 cron 重新探到
        （最多一天），租戶看到「還沒取得位址」。

        冪等：搬完把舊 column 清成 NULL。舊 column 若沒被 Odoo 刪掉，下次 ``-u`` 也
        不會把一個已經撤回（public_tunnel_url 清空）的舊位址又搬回來。column 不存在
        （全新安裝、或已被刪）就什麼都不做。
        """
        cr = self.env.cr
        if not column_exists(cr, self._table, 'mqtt_public_url'):
            return
        cr.execute(SQL(
            """
            UPDATE %(table)s
               SET public_tunnel_url = mqtt_public_url
             WHERE public_tunnel_url IS NULL
               AND mqtt_public_url IS NOT NULL
               AND mqtt_public_url <> ''
            """,
            table=SQL.identifier(self._table),
        ))
        moved = cr.rowcount
        cr.execute(SQL(
            "UPDATE %(table)s SET mqtt_public_url = NULL WHERE mqtt_public_url IS NOT NULL",
            table=SQL.identifier(self._table),
        ))
        if moved:
            _logger.info(
                'P1: carried %s public tunnel address(es) over from mqtt_public_url',
                moved,
            )

    # ------------------------------------------------------------------ #
    #  Actions                                                            #
    # ------------------------------------------------------------------ #

    def action_redeploy(self):
        """後台 button／RPC 的 redeploy 入口：只放行 ``base.group_system``。

        #1185：本方法原本就是 public（後台兩顆 button 的 ``name``），而
        ``cloud_service`` 對 ``base.group_user`` 的 ACL 全開、``ir.rule``
        ``cloud_service_rule_owner_admin_manage`` 又給 workspace owner/admin
        write ⇒ 租戶只要知道 service id，``POST /web/dataset/call_kw
        {"method": "action_redeploy"}`` 就能繞過 ``/woow`` controller 的
        ``_check_operation_allowed``（停權／欠費）直接觸發不可逆的 chart 跳版。
        與 #357 C05（``_reset_admin_password``）同一種攻擊形狀，解法同款：
        實作搬進 private 的 ``_redeploy``（``get_public_method`` 擋 RPC），
        這裡只留一層 group gate 給後台 button。

        gate 用 ``env.is_system()``（含 sudo 與 uid 1）而不是 sentinel：這不是
        `su-env-exempt-guard-false-green` 那種「沒有任何合法改值路徑」的不變量，
        redeploy 本來就是 ops 的合法動作，只是要 gated——屬該 doc 說「`env.su`
        ＋ `has_group` 雙閘放行是對的」那一邊。deny 分支的測試要 ``with_user``
        退出 su 才打得到（``test_mcp_admin_password_authz.py``）。
        """
        self.ensure_one()
        if not self.env.is_system():
            raise AccessError(
                _("Only platform administrators can redeploy a service.")
            )
        return self._redeploy()

    def _redeploy_require_pinned_version(self):
        """#552 層 4：沒釘 chart 版就不准 redeploy（upgrade 分支）。

        redeploy 帶 `template.helm_chart_version` 做 helm upgrade，空值 ⇒ 不帶
        `--version` ⇒ **升到 registry 當下的 latest**，而 `reset_values=True` 又讓 values
        以新版 chart 的預設重新渲染——一次誤按就是不可逆的跳版。

        ⚠ 用 `UserError` 而不是靜默略過：直接按下的 redeploy 是**同步**的，例外會回到
        後台 UI。#1257 起 lease 在途時會改成「登記意圖、由 cron 執行」（Q27），所以
        `_redeploy` 登記意圖之前也先呼叫本檢查，讓 admin 當場看到拒絕理由；cron 執行時
        若仍被拒，原因寫進 `error_message`（`_cron_run_requested_redeploys`）。

        刻意**不**退回 `self.helm_chart_version`：redeploy 的語意就是「重新套用商品當前
        釘的版本」（允許夾帶升版），改讀服務自己的版本會把它悄悄變成另一個功能。要放行
        就去把 template 的版本填上。
        """
        template = self.template_id
        if not template.helm_chart_version:
            raise UserError(
                _('Cannot redeploy: the product "%(product)s" has no '
                  'chart version pinned. Deploying now would pull whatever '
                  'is latest in the registry, which is an irreversible '
                  'version jump. Set the version in the product settings '
                  'and try again.', product=template.name)
            )

    def _redeploy(self):
        """Re-deploy the service, preserving existing data.

        If the Helm release already exists, performs a helm upgrade
        (data-safe — PVCs are preserved). If no release exists yet
        (e.g. initial deploy failed), resets to pending for a fresh install.

        ⛔ 不得改回 public：唯一合法入口是 ``action_redeploy`` 的 group gate
        （#1185；守線測試 ``test_mcp_admin_password_authz.py``）。
        """
        self.ensure_one()
        from ..services.paas_operator import (
            get_paas_operator_client,
            PaaSOperatorError,
        )

        # #1130：Bitnami 存量（template 已換 woow-odoo）先擋，不登記意圖、不碰 operator。
        legacy_error = self._woow_odoo_legacy_release_error()
        if legacy_error:
            raise UserError(legacy_error)

        # #1257 Q27（修訂 Q14）：lease 在途時擋下並**登記意圖**，而不是只提示到期
        # 時間——否則 lease 一到期背景馬上重送、再拿一個 lease，admin 的出口可能一直
        # 被搶走。背景看到意圖就停止自動重送，lease 結束後 cron 先執行 redeploy。
        # 用回傳 notification 而不是 raise：raise 會回捲交易，意圖就登記不進去。
        # v1 不做強制撤銷。
        if self._init_lease_active():
            # 能當場判定的拒絕理由先在這裡擋（admin 看得到），不要排進背景之後才靜默
            # 失敗（Crosswind r1 S3）：lease 在途 ⇒ release 已存在 ⇒ 走 upgrade 分支，
            # 需要 operator 與 #552 的釘版。
            if not get_paas_operator_client(self.env):
                raise UserError(_("PaaS Operator not configured. Contact administrator."))
            self._redeploy_require_pinned_version()
            self.write({'init_redeploy_requested': True})
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'type': 'warning',
                    'sticky': True,
                    'title': _('Redeploy queued'),
                    'message': _(
                        'A post-deploy initialization of this service may still be '
                        'running (lease until %(until)s UTC). The redeploy has been '
                        'registered and will run automatically as soon as it ends.',
                        until=fields.Datetime.to_string(self.init_lease_until)),
                },
            }

        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(_("PaaS Operator not configured. Contact administrator."))

        # Check whether a Helm release already exists
        release_exists = False
        if self.helm_release_name and self.helm_namespace:
            try:
                client.get_release(
                    namespace=self.helm_namespace,
                    release_name=self.helm_release_name,
                )
                release_exists = True
            except PaaSOperatorError:
                release_exists = False

        if release_exists:
            # Upgrade in-place — preserves PVCs and data.
            # #411: 走全量 values 組裝 helper（與 fresh install 同一條管線）＋
            # reset_values 語意（chart values.yaml 為 base，全量 values 疊上）。
            # 既存 release 升級不再以 reuse_values 沿用舊 coalesced values，
            # chart 升版新增的預設能自然補位。見 docs/decisions/patterns/
            # helm-reset-values-unified-pipeline.md。
            template = self.template_id
            # #552 層 4：沒釘版就拒絕（理由見 helper）。這條路只有 `action_redeploy`
            # 的 group_system gate（#1185；在那之前連租戶都能經 `call_kw` 打到這裡）。
            self._redeploy_require_pinned_version()
            # #1408：範本改釘的 image 寫回服務自己的 helm_values（之後租戶儲存設定
            # 也沿用同一組）。upgrade 失敗時 UserError 讓整筆交易回捲，這次寫入一起撤銷。
            image_changes = self._refresh_template_images()
            values = self._assemble_deploy_values()['values']
            # #1130：woow-odoo 的 conf.adminPassword 與 MCP 家族平行（各自的 helper）。
            sensitive_values = self._build_deploy_sensitive_values()
            try:
                release_info = client.upgrade_release(
                    namespace=self.helm_namespace,
                    release_name=self.helm_release_name,
                    chart=template.helm_chart_name,
                    version=template.helm_chart_version,
                    values=values,
                    sensitive_values=sensitive_values or None,
                    reset_values=True,
                )
            except PaaSOperatorError as e:
                # #1552：503 operator_busy 的 detail 是 dict（error_code＋中文 message）。
                detail = e.detail.get('message') if isinstance(e.detail, dict) else e.detail
                raise UserError(
                    _("Upgrade failed: %(reason)s", reason=detail or e.message)
                ) from e

            # #482: 停權中的服務不得被 upgrade 拉出停權。寫成 'deploying' 會讓
            # _do_resume 的 guard 擋掉復權（租戶付款也回不來），而 poll 又永遠
            # 等不到一台本該停著的服務就緒 —— 兩頭卡死。必須在 write 之前判定，
            # 因為 write 本身會改掉 state 這個判斷來源。
            halted = self._is_halted()

            # Set to 'deploying' — cron + frontend polling will handle
            # the rest (pod readiness check → init → running)
            #
            # #489：`last_upgraded_at` 是這條路徑的時間錨。這裡寫的 state 是
            # `deploying`（首裝路徑的狀態）但做的是 helm **upgrade**——不記錨的
            # 話，age timeout 會退回 `deployed_at`（首裝時間），一台跑了三個月
            # 的服務按下重新部署就會在第一輪 poll 被判逾時。
            self.write({
                'state': 'suspended' if halted else 'deploying',
                'error_message': False,
                'init_redeploy_requested': False,
                'helm_revision': release_info.get('revision', self.helm_revision),
                # #552: redeploy runs a real helm upgrade carrying
                # `template.helm_chart_version` (possibly empty ⇒ registry latest),
                # so it can move the deployed chart version. Record what the
                # operator resolved; `or self.helm_chart_version` keeps the old
                # value when talking to an operator that does not report it yet.
                # Per-service field only — see `_execute_deploy` for why the
                # template record must never be written back to.
                'helm_chart_version': (
                    release_info.get('chart_version') or self.helm_chart_version
                ),
                'last_upgraded_at': fields.Datetime.now(),
                # #1408：就緒後由 poll 核對 image；chart 版本當場就能核對。
                'redeploy_verify_pending': True,
                'redeploy_check_message': self._redeploy_chart_check_message(
                    release_info.get('chart_version')),
            })
            if image_changes:
                _logger.info(
                    "Redeploy of service id=%s refreshed template images: %s",
                    self.id, image_changes,
                )
            # #1257 Q24：人工 redeploy 開新的一代（計數歸零、欠 init）。
            self._open_init_generation()
            # #482: upgrade 用 chart 預設把 replicas 渲染回去了，收回停機。
            # 放在 write 之後 —— 這個 helper 失敗時要寫 error_message，順序反過來
            # 會被上面那行 'error_message': False 清掉。
            self._rescale_halted_after_upgrade(client)

            # Trigger cron so state transitions without frontend polling.
            # 停權服務不觸發：poll 掃 deploying/upgrading/initializing，把停權
            # 服務餵進去只會讓它被判定成「還沒就緒」而空轉。
            if not halted:
                cron = self.env.ref(
                    'woow_paas_platform.ir_cron_check_deploying_services',
                    raise_if_not_found=False,
                )
                if cron:
                    cron._trigger()
        else:
            # No release yet — do a full fresh deploy via cron
            deploy_params = {}
            template = self.template_id
            if template.ingress_enabled and self.subdomain:
                deploy_params['expose_config'] = {
                    'enabled': True,
                    'subdomain': self.subdomain,
                }

            self.write({
                'state': 'pending',
                'error_message': False,
                'init_redeploy_requested': False,
                'deploy_params': json.dumps(deploy_params) if deploy_params else False,
            })
            self._open_init_generation()

            cron = self.env.ref(
                'woow_paas_platform.ir_cron_deploy_cloud_services',
                raise_if_not_found=False,
            )
            if cron:
                cron._trigger()

        return True

    # #1432 — 租戶「重新啟動」
    # ------------------------------------------------------------------ #

    def _restart_workloads(self):
        """#1432：rollout restart 這台服務的所有 Deployment／StatefulSet。

        R8：所有範本都沒有租戶可用的重新啟動入口；HA 網頁卡死（tcpSocket liveness
        偵測不到）時只能找客服，其他服務只能靠「用原密碼重存帳密」之類的繞路。

        - **private**（``_`` 開頭）：授權（workspace owner／admin、訂閱閘）只在
          controller，``get_public_method`` 擋 RPC 直呼（同 #357 C05、#1185）。
        - 只接受 ``running``：``upgrading`` 中再重啟會與進行中的 helm／rollout 互踩；
          ``error`` 的成因（helm 失敗、init 額度用完）重啟救不了，出口是修改配置或
          回復版本；``suspended`` 是停權（訂閱閘已擋，這裡是第二道）。
        - VM 型（HAOS）不支援：KubeVirt 沒有 rollout 語意，硬關機重開不是同一種操作。
        - 列鎖序列化：與 reset 同一招，第二個請求等第一個提交後會看到 ``upgrading``。
        - 成功後轉 ``upgrading``＋``last_upgraded_at``（#489 的時間錨），poll 在 Pod
          就緒後轉回 ``running``；前端同時鎖住其他操作。
        """
        self.ensure_one()
        from ..services.paas_operator import (
            get_paas_operator_client,
            PaaSOperatorError,
        )

        if self._needs_vm_status():
            raise UserError(_("This type of service cannot be restarted from the platform."))

        self.env.cr.execute(
            "SELECT id FROM woow_paas_platform_cloud_service WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()
        if self.state == 'upgrading':
            raise UserError(
                _("Service is finishing an update (usually under a minute). "
                  "Please wait and try again.")
            )
        if self.state != 'running':
            raise UserError(_("Only a running service can be restarted."))
        if not (self.helm_release_name and self.helm_namespace):
            raise UserError(
                _("Service has no helm release yet — deploy first before restarting it.")
            )
        if self._init_lease_active():
            raise UserError(
                _("An initialization request for this service may still be running. "
                  "Please try again in a few minutes.")
            )

        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(_("PaaS Operator not configured. Contact administrator."))
        try:
            client.restart_release(
                namespace=self.helm_namespace,
                release_name=self.helm_release_name,
            )
        except PaaSOperatorError as e:
            _logger.error(
                "restart failed for service %s (id=%s): %s",
                self.name, self.id, e.detail or e.message,
            )
            raise UserError(_("Restart failed. Check service status and retry.")) from e

        self.write({
            'state': 'upgrading',
            'error_message': False,
            'last_upgraded_at': fields.Datetime.now(),
        })
        return True

    # #357 — admin_password 重置
    # ------------------------------------------------------------------ #

    def _reset_admin_password(self, new_password=None, new_username=None,
                              rotate_gateway_key=True):
        """#357 外科式 reset mcp admin password（保留 GUI 自填 connection/tools）。

        ``rotate_gateway_key``（#1557）：有 gateway 的 chart 要不要一併換發 Gateway API
        Key。預設 True＝#530 的 Reset 語意（一次換兩把）；租戶自設帳密時由 controller
        依租戶的選擇傳入，False＝保留目前那一把（明確送回 chart，不靠 chart 的回讀）。
        服務還沒有 key（legacy）時照樣鑄一把——否則叢集與 DB 會各自一把。

        ``new_password`` / ``new_username``（租戶自設憑證）：只開放給
        ``template.admin_credentials_settable`` 的模板——``admin_username_helm_path``
        有值（帳密是 chart Secret 管的一組 basic-auth，例 open-design 的 nginx
        auth-proxy 與 ttyd），或 ``admin_password_settable``（只有密碼、沒有帳號，
        例 code-server；此時只收 ``new_password``）。兩者都不給
        ＝原本的 Reset（平台鑄造新密碼）。給了密碼就用租戶的值、不鑄造；給了帳號就
        寫進 values 的那個路徑。兩者走**同一條** helm upgrade ＋ rollout restart，
        提交點與記帳語意完全不變（見下方 C11′ 與 #525 B3 註解）——帳號與密碼是同一
        個事實（叢集生效的登入憑證），所以一起在「憑證落庫」那段 savepoint 落 DB。

        密碼只短暫存在記憶體與 ``cloud_service_secret``；不寫回 ``helm_values``。
        先用 current user 的存取規則驗證 service，再 sudo 讀寫該 service 的 secret。

        #357 C05：**必須維持 ``_`` 開頭（private）**。本方法尾端回傳明文新密碼，
        而授權（workspace role=admin）只存在 controller 層。命名為 ``action_*``
        時它是 RPC 可達的：任何 workspace 成員（含 role=user/guest——cloud_service
        的 ACL 對 group_user 全開、ir.rule 只驗「是該 workspace 成員」）可繞過
        controller 的 role gate 直接 ``POST /web/dataset/call_kw``，RPC 回傳值
        即明文新密碼，同時把真 admin 的密碼換掉（提權 + DoS）。這是「controller
        filter 擋不住 ORM 直呼」的實例，見 docs/decisions/patterns/
        paas-workspace-member-acl-tightening.md §Controller filter 不夠。

        Odoo 18 由 ``get_public_method``（odoo/service/model.py:43
        ``if name.startswith('_') ... raise AccessError``）擋下 ``_`` 開頭方法的
        遠端呼叫，**兩條** RPC 路徑均已覆蓋：JSON-RPC 走
        odoo/addons/web/controllers/dataset.py:34（call_kw 與 call_button）、
        XML-RPC 走 odoo/service/model.py:71（execute_cr）。
        ⚠️ 勿照 17.0 經驗判斷：17.0 用的是 ``check_method_name``，該函式在 18.0
        雖仍定義於 models.py:155 但**已無任何呼叫點＝遺留死碼**（本 repo 是 18.0，
        於 18.0 容器內 grep 實證）。

        改回 public 命名（或加上會被 RPC 觸及的 wrapper）即重新打開該洞，
        ``TestResetAdminPasswordAuthorization`` 會紅。
        """
        self.ensure_one()
        from ..services.paas_operator import (
            get_paas_operator_client,
            PaaSOperatorError,
            PaaSOperatorTimeoutError,
        )

        # app_managed（omnigent）：app 只在第一次開機讀平台給的密碼，之後密碼活在 app
        # 自己的資料庫。照 helm_secret 走 reset 會 helm upgrade 成功、密碼卻沒變
        # （假裝成功），所以在碰 operator 之前就拒絕。controller 的能力 gate 讀同一個
        # 欄位，這裡是第二道防線（本 method 還有別的 caller 時也守得住）。
        # 只擋「有供給、但不能重設」：has_admin_gui=False 的授權一直由 controller
        # 負責（model 端既有行為不變，縱深測試直接呼叫本 method 驗 state 守衛）。
        # #1370 後續：omnigent 現在 admin_reset_supported=True，但那是 omnigent_exec
        # （_reset_app_admin_password）的能力；helm 這條對 app_managed 仍然是假裝成功，
        # 所以載體本身也要擋。
        if self.template_id.has_admin_gui and (
                not self.template_id.admin_reset_supported
                or self.template_id.admin_password_carrier == 'app_managed'):
            raise UserError(
                _("This service manages its own admin password. "
                  "Change it inside the app.")
            )

        # Hold a row lock across the operator saga.  A second reset waits for
        # the first request to commit, then observes state='upgrading' below
        # instead of interleaving Secret/config rollback with a newer reset.
        #
        # #357 C11: this serialises the success path only. On a client timeout
        # the lock is released while the operator's saga keeps running — it is a
        # sync handler, so the client hanging up cancels nothing — and state
        # stays 'running' because a timeout stores nothing. The retry message
        # below therefore asks the tenant to wait rather than press again at
        # once, which would race the in-flight saga.
        self.env.cr.execute(
            "SELECT id FROM woow_paas_platform_cloud_service WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()
        if self.state == 'upgrading':
            # #357 C11: a successful reset stamps 'upgrading', so this is the
            # message a tenant hits when they reset, lose the password, and
            # immediately try again. The generic text below reads as breakage;
            # _cron_check_deploying_services polls every minute and flips it
            # back to 'running', so say that instead.
            raise UserError(
                _("Service is finishing an update (usually under a minute). "
                  "Please wait and try again.")
            )
        if self.state != 'running':
            raise UserError(
                _("Service must be running before resetting its admin password.")
            )

        if not (self.helm_release_name and self.helm_namespace):
            raise UserError(
                _("Service has no helm release yet — deploy first before resetting password.")
            )

        # #357 C03：不檢查既有 secret。reset 是「重新生成並下發」而非「讀舊密碼」，
        # 舊 secret 缺席（legacy 服務建立時平台尚未供給 admin_password）不是錯誤——
        # 這正是租戶要 reset 的情境。_set_admin_password_secret 會 create-or-write。

        username_path = self.template_id.admin_username_helm_path
        # 能力事實源＝template.admin_credentials_settable（帳號路徑，或只有密碼的
        # admin_password_settable）；改帳號另外要有帳號路徑。
        if ((new_password is not None or new_username is not None)
                and not self.template_id.admin_credentials_settable):
            raise UserError(
                _("This service's admin credentials can only be reset, not set.")
            )
        if new_username is not None and not username_path:
            raise UserError(
                _("This service has no username; only its password can be set.")
            )
        if new_username is not None:
            self._validate_tenant_admin_username(new_username)
        if new_password is not None:
            self._validate_tenant_admin_password(new_password)

        # C20: the single generation point — never inline token_urlsafe here.
        # A tenant-supplied password is used as-is (validated above).
        if new_password is None:
            new_password = self._generate_admin_password()
        # #530：Reset 一次換發兩把憑證。它是租戶唯一的換發入口，也是**存量服務取得
        # gateway key 的唯一路徑**（刻意不做從叢集回讀的 backfill——那會違反「憑證
        # 權威來源在平台端」的不變式，且要新增 operator 端點）。只換一把的話 UI 那句
        # 「Reset 會一併換掉 Gateway API Key」就是謊話。
        # 沒有 gateway 的 chart 傳 None：`_set_admin_password_secret` 的補鑄同樣受
        # `_chart_has_gateway` 閘門管，這裡硬生一把會繞過那道閘門（鑄造／供給／交付
        # 三個閘門必須同源，否則就是 review finding 3 那條隱性契約再長回來）。
        new_gateway_api_key = None
        if self._chart_has_gateway():
            # #1557：租戶只想改後台登入密碼時保留目前的 key（LiteLLM 的 master key，
            # 所有管理腳本與整合都用它）。保留也要**明確送回**同一把：不送的話 chart
            # 會退到「查既有 Secret」那段優先序，結果取決於 chart 實作，不是平台決定的。
            existing_secret = self._get_secret_record()
            existing_key = existing_secret.gateway_api_key if existing_secret else False
            if rotate_gateway_key or not existing_key:
                new_gateway_api_key = self._generate_admin_password()
            else:
                new_gateway_api_key = existing_key
        # #411: values 走全量組裝 helper（與 deploy/redeploy/reconfigure 同一條
        # 管線）。reset_values 語意讓 chart 自身預設補位，不再以 --reuse-values
        # 沿用舊 coalesced values（同一 bug class，見 #411）。chart 版本見下方
        # `pinned_chart_version`：reset **不**升版。
        # #1158：不疊方案規格——那會改 pod template，15 秒等不到新 pod（F-461-2）。
        values = self._assemble_deploy_values(apply_plan=False)['values']
        if new_username is not None:
            # 只改送給 operator 的這份；helm_values 等 2xx 之後才落庫（2xx 前失敗
            # 叢集仍是舊帳號，DB 也必須還是舊帳號）。
            self._set_values_path(values, username_path, new_username)
        operator_sensitive_values = self._build_mcp_sensitive_values(
            new_password, gateway_api_key=new_gateway_api_key,
        )

        # F-461-2（2026-09-29 修）：reset／改帳密**釘住這台服務現在跑的 chart 版本**，
        # 不夾帶升版——與 reconfigure（controllers/paas.py `_update_service` 的
        # `resolved_version`）同一條 fallback：service 已知版本優先，legacy 服務沒有
        # 紀錄才退到 template 的 pin（#552：送出去的 version 不得為空）。
        #
        # 為什麼不能夾帶：operator 的 reset 是 `helm upgrade --atomic --timeout 15s`
        # （隱含 --wait）。升 chart 會改 pod template ⇒ helm 要等新 pod ready，而
        # pod 實際要 30–70 秒 ⇒ 必定逾時回退；Recreate 的 Deployment 在回退時還會
        # 斷線約 1 分鐘。原本只加 log，租戶按「重試」只會重複斷線、永不收斂
        # （2026-09-29 prod 實測）。chart 升版屬於 deploy／redeploy 的職責，那兩條
        # 路的等待預算才蓋得住 pod 重建。
        pinned_chart_version = (
            self.helm_chart_version or self.template_id.helm_chart_version
        )
        target_chart_version = self.template_id.helm_chart_version
        # 兩邊都要有值才算「落後」：helm_chart_version 為空代表平台**不知道**線上是
        # 哪一版（legacy 服務）。落後本身不擋 reset（釘版後照樣收斂），只留給維運
        # 一條線索：這台服務要靠 redeploy 才會升上 template 的新 chart。
        if (target_chart_version and self.helm_chart_version
                and target_chart_version != self.helm_chart_version):
            _logger.info(
                "reset-admin-password for service %s (id=%s) keeps chart %s "
                "(template pins %s; redeploy to upgrade)",
                self.name, self.id, self.helm_chart_version, target_chart_version,
            )

        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(_("PaaS Operator not configured. Contact administrator."))

        try:
            # #552: the response is no longer discarded — it carries the chart
            # version helm resolved for this upgrade (both operator branches,
            # config_json and helm_secret, report it).
            reset_result = client.reset_release_config(
                namespace=self.helm_namespace,
                release_name=self.helm_release_name,
                chart=self.template_id.helm_chart_name,
                version=pinned_chart_version,
                values=values,
                sensitive_values=operator_sensitive_values or None,
                reset_values=True,
                # #449：告訴 operator 這個 chart 把密碼放在哪。**供給面兩者相同**
                # （都是 config.sensitive.admin_password，上一行的 sensitive_values
                # 不因載體而異）；差別只在 operator 要不要 exec patch
                # /data/config.json。hermes-agent / open-design 沒有那個檔，走預設
                # 值必 500（#449）。判別器＝template 欄位，不得在此自建 slug／chart
                # allowlist——與 has_admin_gui 同一紀律（#357 C01）。
                admin_password_carrier=self.template_id.admin_password_carrier,
            )
        except PaaSOperatorTimeoutError as e:
            # #461 C11′ (commit-on-secret) — this branch survived the async
            # rewrite, but its *reason* changed. Read this before touching it.
            #
            # OLD C11: the DB may only hold a password that really took effect,
            # because the operator waited for the rollout and rolled the Secret
            # BACK when it failed. Storing on an ambiguous outcome could therefore
            # record a password the cluster had already discarded.
            #
            # C11′: the operator no longer waits for the rollout, so there is no
            # rollback after 2xx either. The commit point moved to "the operator
            # answered 2xx" ⇒ the new password IS the authoritative value in the
            # chart Secret / config.json and nothing reverts it. Everything that
            # can still fail happens BEFORE that answer and is compensated (helm
            # --atomic rolls itself back; a failed config.json patch restores the
            # backup) ⇒ on any error the cluster still holds the OLD password ⇒
            # not storing is still exactly right.
            #
            # What this branch now means: the budget ladder makes the platform's
            # 75s strictly larger than the operator's own 73s hard cap (#1552: 65s + 8s queue)
            # (RESET_HARD_CAP_SECONDS), so a client timeout is no longer the
            # expected outcome of a slow-but-healthy reset — it means the operator
            # blew its own ceiling, or the 2xx was lost in flight. The cluster may
            # then hold a password we never stored, so "Reset failed" would still
            # be a lie the tenant acts on.
            #
            # Recovery is unchanged and does not need the old password: state stays
            # 'running' (nothing was written), so the tenant can press Reset again;
            # it mints a fresh password and re-commits. Deliberately no pending-
            # password column — storing an unconfirmed credential buys only this
            # narrow window and costs a second live secret plus a mid-request
            # commit that would break this handler's row lock (design §4.4).
            _logger.error(
                "reset-admin-password timed out for service %s (id=%s): %s — "
                "operator saga may still be in flight; new password NOT stored",
                self.name, self.id, e.detail or e.message,
            )
            raise UserError(
                _("Reset status unknown — the request timed out before the service "
                  "confirmed it. The reset may still have taken effect, so the "
                  "password was not saved. Wait a few minutes, then press Reset "
                  "again to issue a new password.")
            ) from e
        except PaaSOperatorError as e:
            _logger.error(
                "reset-admin-password failed for service %s (id=%s): %s",
                self.name, self.id, e.detail or e.message,
            )
            raise UserError(_("Reset failed. Check service status and retry.")) from e

        # ================= 提交點（C11′ commit-on-secret）之後 =================
        #
        # #525 B3：從這裡開始，「新密碼已生效」已經是**既成事實**——operator 回了
        # 2xx，新憑證就是叢集的權威值，而且不會再被回退（helm --atomic 自己回捲、
        # config.json patch 失敗會還原備份，能失敗的全在 2xx 之前）。
        #
        # 因此提交點之後的每一步都只是**記帳**（落庫、stamp state、停權回收），
        # 不是「reset 有沒有成功」的判準。讓記帳的例外往外拋 ⇒ controller 回
        # `success: False` ⇒ 告訴租戶「沒換」。那句話是假的，代價也不對稱：
        # 明文只顯示這一次、平台刻意沒有回讀端點（憑證一律一次性），租戶因此
        # **永久拿不到**唯一能登入 admin GUI 的那把密碼，還會拿已失效的舊密碼
        # 反覆重試。所以這兩段的例外一律不外拋（`OperationalError` 除外，見下）。
        #
        # 🔴 這裡**刻意不補** `cr.rollback()`。#525 把本處列為 B 級（外觀與 #520
        # 同構、因果相反）正是這個理由：回捲會把已落庫的新密碼退回舊值，而叢集
        # 已經是新值 ⇒ 從「一致但租戶不知情」變成「DB 與叢集不一致」，管理員連
        # admin GUI 都登不進去，之後的 redeploy 還會拿 DB 的舊值去覆寫叢集。
        #
        # 用 `cr.savepoint()` 而非裸 try：Python 層例外會在 ORM cache 留下待
        # flush 的髒值，不清掉的話它會在本 request 稍後（甚至 controller 之外的
        # commit）再炸一次 → 變成 500 並回捲整筆交易，正好是我們要避免的形態。
        # `_FlushingSavepoint.rollback` 會先 `cr.clear()` 再 ROLLBACK TO SAVEPOINT。
        #
        # 兩段分開包，是因為**回捲單位不同**：憑證落庫必須在記帳失敗時存活下來
        # （它是與叢集一致的那個事實）；記帳則整組成立或整組不成立。
        try:
            with self.env.cr.savepoint():
                # #530：兩把憑證一起落庫。2xx 代表兩者都已是叢集權威值。
                self._set_admin_password_secret(
                    new_password, gateway_api_key=new_gateway_api_key,
                )
                if new_username is not None:
                    # 帳號與密碼同屬「叢集已生效的登入憑證」這個事實：同一個回捲
                    # 單位。不落庫的話下一次 redeploy 會用舊帳號覆寫叢集。
                    helm_values = self._helm_values_dict()
                    self._set_values_path(helm_values, username_path, new_username)
                    self._apply_helm_values(helm_values)
        except OperationalError:
            # #518：並發／連線類 DB 例外（SerializationFailure 40001 等）必須傳
            # 出去讓 Odoo 的 `retrying()` 看見；此刻交易已 aborted，吞掉只會讓
            # 後續 flush 再炸。重播整個 handler 會重跑一次 reset（另鑄一把新密碼
            # 重新下發），結果仍然是一致的。
            raise
        except Exception:
            _logger.exception(
                "[#525 B3] reset-admin-password could NOT store the new "
                "credentials for service %s (id=%s) although the operator "
                "already applied them — cluster and DB now disagree; the live "
                "password is being returned to the tenant so they can still "
                "sign in, but a later redeploy would re-supply the old value",
                self.name, self.id,
            )

        # `last_upgraded_at` 同 action_redeploy：reset 走的是同一條 helm
        # upgrade 管線，不記錨就會讓 #489 的 age timeout 拿首裝時間當起點。
        #
        # #482 縱深：這條路對 suspended 服務**不可達**（本方法開頭就要求
        # state == 'running'），但 reset 同樣是一次 reset_values 重渲染。二階
        # 情境是服務先被 reconfigure 汙染（suspended → upgrading → poll →
        # running）而 pre_suspend_state 仍在：此時 _is_halted() 仍為真＝它其實
        # 還在停權中，reset 完必須把它收回停機，而不是留下一台跑著的欠費服務。
        try:
            with self.env.cr.savepoint():
                halted = self._is_halted()
                self.write({
                    'state': 'suspended' if halted else 'upgrading',
                    'error_message': False,
                    'last_upgraded_at': fields.Datetime.now(),
                })
                # #552: reset sends `pinned_chart_version` (the service's own
                # chart; it no longer rides a template upgrade — F-461-2), but
                # it is still a helm upgrade, so it stays one of the four places
                # the ledger is refreshed from what helm actually resolved.
                # `or self.helm_chart_version` =
                # backward compatibility with an operator whose reset response
                # has no `chart_version` yet.
                # Per-service field only — never `template.helm_chart_version`.
                #
                # #525 B3：帳本更新與 state stamp 同屬「記帳」，一起進 savepoint。
                # 它失敗時整組回捲（帳本維持舊值＝與失敗前一致），但**不會**
                # 波及上一段已落庫的憑證，也不會翻轉「密碼已生效」的回應。
                # #1525：帳本走平台身分（租戶身分的 write 會被擋）——`self` 在這條路上
                # 是租戶身分（`_service_admin_credentials` 的 internal user）。
                self._record_chart_version(
                    (reset_result or {}).get('chart_version')
                    or self.helm_chart_version
                )
                self._rescale_halted_after_upgrade(client)
        except OperationalError:
            raise
        except Exception:
            # 記帳整組回捲 ⇒ state 留在 'running'。代價是前端少一輪 upgrading
            # 輪詢（服務仍會自己收斂），遠小於讓租戶永久拿不到已生效的密碼。
            _logger.exception(
                "[#525 B3] reset-admin-password bookkeeping failed for service "
                "%s (id=%s) AFTER the new credentials took effect — the reset "
                "itself stands and the password is being returned; platform-side "
                "state/last_upgraded_at/helm_chart_version may be stale",
                self.name, self.id,
            )

        return new_password

    # #1370：operator 腳本回報的原因碼 → 租戶看得懂、能行動的訊息。這些都是「交易已
    # rollback、密碼未變」的確定失敗（error_kind=failed／not_executed）。
    def _app_admin_reset_refusal_message(self, code, kind=None):
        # Omnigent 的 admin_inactive 意思是「帳號還在、但已被拿掉管理員角色」（Members 裡
        # 降級），不是 Odoo 的封存——共用 Odoo 文案會叫租戶去「Odoo 內」重新啟用。
        if kind == 'omnigent_exec' and code == 'admin_inactive':
            return _(
                "The administrator account of this service no longer has administrator "
                "rights, so the password was not changed. Ask another administrator in "
                "Omnigent to make it an administrator again, then reset again; if there "
                "is none, please open a support ticket.")
        messages = {
            'admin_missing': _(
                "The administrator account could not be found in this service, so the "
                "password was not changed. Please contact support."),
            # Odoo：封存可由租戶自己的其他管理員解（#1186 驗收文案）；Omnigent 見上方。
            'admin_inactive': _(
                "The administrator account of this service has been archived, so the "
                "password was not changed. Ask another administrator in Odoo to "
                "reactivate it, then reset again; if there is none, please open a "
                "support ticket."),
            'owner_ambiguous': _(
                "This service has more than one owner account, so the password was not "
                "changed. Please contact support."),
        }
        return messages.get(code) or _(
            "Reset failed and the password was not changed. Check the service status "
            "and try again.")

    def _reset_app_admin_password(self):
        """#1370／#1186：重設 app 自管的管理員密碼（Odoo base.user_admin／n8n owner／
        Omnigent admin）。

        與 ``_reset_admin_password``（helm Secret／config.json）是兩條不同的契約：這兩個
        app 的密碼權威在 app 自己的 DB，operator exec 一支腳本直接改，**不 helm
        upgrade、不重啟、不改 state**。Odoo 同交易清 TOTP；n8n 同交易關 MFA。

        Returns:
            ``(new_password, login)``：明文新密碼（一次性交付）與實際被重設的登入帳號。

        #357 C05：**必須維持 ``_`` 開頭（private）**——回傳明文，而授權（workspace
        role=admin）只存在 controller 層；理由與 ``_reset_admin_password`` docstring 相同。

        提交點＝operator 回 ``success: true``（DB 已 commit，新密碼即權威值）。之後的
        落庫只是記帳，失敗也不翻轉「已生效」（#525 B3 同一紀律）：
          - Odoo：``cloud_service_secret.admin_password``（redeploy 會重送 conf.adminPassword，
            chart 只在首次 -i base 套用，重送新值是 no-op，但保持平台與 app 一致）。
          - n8n：``n8n_owner_password``／``n8n_owner_email``——平台記住的 owner 帳密。#1534 起
            post-deploy init 只在建立 owner 那一次用密碼登入；owner 已存在時不登入，平台沒確認
            過 owner 時才比對 Email。租戶在 n8n 介面自己改過 owner 密碼／email 的漂移一併由這裡
            對齊（平台記的與實際一致）。
        """
        self.ensure_one()
        from ..services.paas_operator import (
            get_paas_operator_client,
            PaaSOperatorAPIError,
            PaaSOperatorError,
            PaaSOperatorTimeoutError,
        )

        kind = self.template_id.app_admin_reset_kind or 'none'
        if kind not in ('odoo_exec', 'n8n_exec', 'omnigent_exec'):
            raise UserError(_("This service does not support resetting its admin password."))

        # 同 _reset_admin_password：列鎖序列化同一台服務的並發 Reset。
        self.env.cr.execute(
            "SELECT id FROM woow_paas_platform_cloud_service WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()
        if self.state == 'upgrading':
            raise UserError(
                _("Service is finishing an update (usually under a minute). "
                  "Please wait and try again.")
            )
        if self.state != 'running':
            raise UserError(
                _("Service must be running before resetting its admin password.")
            )
        if not (self.helm_release_name and self.helm_namespace):
            raise UserError(
                _("Service has no helm release yet — deploy first before resetting password.")
            )
        if kind == 'odoo_exec':
            # Bitnami 時代的存量服務：conf 路徑與 chart 契約都不同，exec 腳本不適用。
            legacy_error = self._woow_odoo_legacy_release_error()
            if legacy_error:
                raise UserError(legacy_error)

        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(_("PaaS Operator not configured. Contact administrator."))

        new_password = self._generate_admin_password()
        reset = {
            'odoo_exec': client.reset_odoo_admin_password,
            'n8n_exec': client.reset_n8n_owner_password,
            'omnigent_exec': client.reset_omnigent_admin_password,
        }[kind]
        unknown_message = _(
            "Reset status unknown — the request timed out before the service confirmed "
            "it. The password may already have changed, so it was not saved. Wait a "
            "minute, then press Reset again to issue a new password.")
        try:
            result = reset(
                namespace=self.helm_namespace,
                release_name=self.helm_release_name,
                new_password=new_password,
            )
        except PaaSOperatorTimeoutError as e:
            _logger.error(
                "app admin password reset timed out for service %s (id=%s): %s — "
                "new password NOT stored", self.name, self.id, e.detail or e.message,
            )
            raise UserError(unknown_message) from e
        except PaaSOperatorAPIError as e:
            # 404＝operator 還沒有這個端點（平台先於 operator 上線）：什麼都沒碰。
            _logger.error(
                "app admin password reset rejected by operator for service %s (id=%s): "
                "HTTP %s %s", self.name, self.id, e.status_code, e.detail or e.message,
            )
            if e.status_code == 404:
                raise UserError(_(
                    "Resetting this service's admin password is not available yet. "
                    "Please try again later.")) from e
            if e.status_code and e.status_code < 500:
                # 其他 4xx（驗證／認證）：請求在 operator 入口就被拒，腳本沒跑。
                raise UserError(_("Reset failed. Check service status and retry.")) from e
            # 5xx 帶 detail.error_kind=unknown：腳本可能已經跑完，不能宣稱密碼未變。
            raise UserError(unknown_message) from e
        except PaaSOperatorError as e:
            _logger.error(
                "app admin password reset failed for service %s (id=%s): %s",
                self.name, self.id, e.detail or e.message,
            )
            raise UserError(_("Reset failed. Check service status and retry.")) from e

        if not (result or {}).get('success'):
            error_kind = (result or {}).get('error_kind')
            _logger.error(
                "app admin password reset not applied for service %s (id=%s): "
                "kind=%s code=%s step=%s error=%s", self.name, self.id, error_kind,
                (result or {}).get('error_code'), (result or {}).get('error_step'),
                (result or {}).get('error'),
            )
            if error_kind == 'unknown' or not error_kind:
                raise UserError(unknown_message)
            raise UserError(self._app_admin_reset_refusal_message(
                result.get('error_code'), kind))

        login = result.get('login') or None
        # ================= 提交點之後：只剩記帳（#525 B3） =================
        try:
            with self.env.cr.savepoint():
                if kind in ('odoo_exec', 'omnigent_exec'):
                    # omnigent：chart Secret 的 admin_password 只在首次開機用；存回憑證記錄
                    # 讓平台與 app 一致（redeploy 重送也是 no-op）。
                    self._set_admin_password_secret(new_password)
                else:
                    vals = {'n8n_owner_password': new_password}
                    if login:
                        vals['n8n_owner_email'] = login
                    self.sudo().write(vals)
        except OperationalError:
            raise
        except Exception:
            _logger.exception(
                "[#1370] app admin password reset could NOT store the new password for "
                "service %s (id=%s) although the app already applied it — returning it "
                "to the tenant anyway", self.name, self.id,
            )
        _logger.info(
            "[#1370] app admin password reset for service %s (id=%s, kind=%s, "
            "second factor cleared=%s)", self.name, self.id, kind,
            bool(result.get('second_factor_cleared')),
        )
        return new_password, login

    # ------------------------------------------------------------------ #
    #  Cron: process pending deployments                                  #
    # ------------------------------------------------------------------ #

    @api.model
    def _cron_process_pending_deploys(self):
        """Called by ir.cron to deploy services still in 'pending' state.

        Processes one service per cron run to let Odoo manage the transaction.
        If more pending services remain, re-triggers the cron for immediate pickup.
        """
        service = self.search([('state', '=', 'pending')], limit=1)
        if not service:
            return

        try:
            service._execute_deploy()
        except Exception:
            _logger.exception(
                "Deploy failed for service %s (id=%s)",
                service.name, service.id,
            )
            service.write({
                'state': 'error',
                'error_message': _(
                    'Unexpected error during deployment. '
                    'Check server logs.'),
            })

        # If more pending services exist, re-trigger for next run
        remaining = self.search_count([('state', '=', 'pending')])
        if remaining:
            self.env.ref('woow_paas_platform.ir_cron_deploy_cloud_services')._trigger()

    def _execute_deploy(self):
        """Execute the actual deployment for a single pending service."""
        self.ensure_one()
        from ..services.paas_operator import (
            get_paas_operator_client,
            PaaSOperatorError,
            PaaSOperatorConnectionError,
        )

        _logger.info("Deploying service %s (id=%s)", self.name, self.id)
        self.write({'state': 'deploying'})

        client = get_paas_operator_client(self.env)
        if not client:
            self.write({
                'state': 'error',
                'error_message': _(
                    'PaaS Operator not configured. '
                    'Contact administrator.'),
            })
            return

        template = self.template_id
        helm_namespace = self.helm_namespace
        helm_release_name = self.helm_release_name

        # #1334：首裝才決定的 storageClass 先寫進 helm_values，之後每次 upgrade
        # 都經 `_assemble_deploy_values` 帶同一個值（PVC storageClassName 不可變）。
        self._pin_install_storage_class()

        # #411: values 組裝走單一真相源 helper（與 redeploy / reconfigure /
        # reset admin password 共用同一條全量管線）。expose_config 亦由 helper
        # 重算，取代 create 時存在 deploy_params 的等價值（收斂雙寫）。
        assembled = self._assemble_deploy_values()
        merged_values = assembled['values']
        expose_config = assembled['expose_config']

        # sensitive_values 與 mcp_api_key 屬本 install 路徑專屬（非部署 values），
        # 另外組。mcp_api_key 首裝 patch sidecar 用，仍自 deploy_params 取。
        # #1130：woow-odoo 的 conf.adminPassword 與 MCP 家族平行（各自的 helper）。
        sensitive_values = self._build_deploy_sensitive_values()
        params = json.loads(self.deploy_params) if self.deploy_params else {}
        mcp_api_key = params.get('mcp_api_key')

        try:
            # #552 層 4 —— **backstop，不是主閘門**。主閘門在可購買性裁決點
            # （`cloud_app_template._reject_reason_for_new_instance`），擋在收錢之前。
            #
            # 🔴 **這不是過渡期保險，是永久涵蓋——不要當成清得掉的舊碼。**
            # 主閘門掛在 `CloudService.create()` 上，所以它只看得到**新建**的列。
            # 有一條當前就存在、可重複觸發的路徑會讓一台服務走進 `_execute_deploy`
            # 而**完全不經過 create**：
            #
            #     controllers/ha_paas.py 的 provision 重試 —— 既有服務處於 `error`
            #     時，它 `existing.sudo().write({'state': 'pending'})` 再觸發 deploy
            #     cron。**是 write 不是 create** ⇒ `_enforce_supply_gate` 不會跑，
            #     cron 隨後直接呼叫本方法。這條路上**只有這道 backstop 接得住**。
            #
            # （另一種同樣接不到主閘門的列：閘門上線前就已經躺在 `pending` 的服務。
            # 那個確實是過渡期的，但它**不是**本 backstop 存在的唯一理由。）
            #
            # 而 install 不帶 `--version` 就是 registry latest。
            #
            # 🔴 **位置在 `try` 之內是必要條件，不是風格**。放在 try 外面時，例外
            # 會直接穿過本方法，落進 `_cron_process_pending_deploys` 的泛用
            # `except Exception` —— 訊息被換成「Unexpected error during
            # deployment. Check server logs.」，租戶與第一線都無從排錯（#520 症狀
            # 4 同型）。實測踩過一次：`state` 確實變成 error，但 `error_message`
            # 完全不提版本，看起來就像基礎設施故障。
            #
            # ⚠ 例外型別同理**必須**是 `PaaSOperatorError`：底下那個 handler 才會
            # 把 detail 寫進 `error_message`。兩個條件（位置＋型別）少一個都會讓
            # 這道 backstop 退化成「擋下來了，但沒人知道為什麼」。
            if not template.helm_chart_version:
                raise PaaSOperatorError(
                    'chart version not pinned',
                    detail=_(
                        'The product "%(product)s" has no deployment version '
                        'pinned. Provisioning was aborted to avoid deploying '
                        'an unknown chart version. Pin the version in the seed '
                        'and cut a new release (#552).',
                        product=template.name,
                    ),
                )

            # Create namespace (409 = already exists, OK)
            cpu_limit, memory_limit, storage_limit = self._compute_ns_quota(
                self.workspace_id,
            )
            eph_request, eph_limit = self._compute_ns_ephemeral_quota(self.workspace_id)
            try:
                client.create_namespace(
                    namespace=helm_namespace,
                    cpu_limit=cpu_limit,
                    memory_limit=memory_limit,
                    storage_limit=storage_limit,
                    workspace_id=self.workspace_id.id or None,
                    ephemeral_storage_request=eph_request,
                    ephemeral_storage_limit=eph_limit,
                )
            except PaaSOperatorError as e:
                if e.status_code != 409:
                    self.write({
                        'state': 'error',
                        'error_message': _(
                            'Failed to create namespace: %(detail)s',
                            detail=e.detail or e.message),
                    })
                    return

            # Install Helm release
            _logger.info(
                "Helm install: %s in %s (chart=%s, expose=%s)",
                helm_release_name, helm_namespace, template.helm_chart_name, expose_config,
            )
            release_info = client.install_release(
                namespace=helm_namespace,
                release_name=helm_release_name,
                chart=template.helm_chart_name,
                repo_url=template.helm_repo_url,
                version=template.helm_chart_version,
                values=merged_values,
                sensitive_values=sensitive_values or None,
                create_namespace=True,
                expose=expose_config,
            )

            # Patch MCP sidecar if enabled
            if template.mcp_enabled and template.mcp_sidecar_image:
                mcp_auth_token = str(uuid.uuid4())
                sidecar_config = self._build_mcp_sidecar_config(
                    template, mcp_auth_token, mcp_api_key,
                    helm_release_name=helm_release_name,
                )
                try:
                    client.patch_sidecar(
                        namespace=helm_namespace,
                        release_name=helm_release_name,
                        sidecar_config=sidecar_config,
                    )
                    self.write({'mcp_auth_token': mcp_auth_token})
                    _logger.info("MCP sidecar patched for %s", self.name)
                except PaaSOperatorError as e:
                    _logger.warning("Failed to patch MCP sidecar: %s", e)

            # Helm install done — update release info and clean up deploy params
            self.write({
                'helm_revision': release_info.get('revision', 1),
                # #552: record the chart version helm ACTUALLY resolved. With an
                # empty `template.helm_chart_version` no `--version` is sent and
                # helm picks the registry's latest — the ledger used to keep the
                # (empty) template value forever, so the tenant's service-detail
                # page showed a version nobody had verified.
                #
                # ⚠ Written to **cloud_service** (per-service), never to
                # `template.helm_chart_version`: the seed is `noupdate=0` and the
                # healer at the tail of `data/cloud_app_templates.xml` clears
                # `ir.model.data.noupdate` on every `-u`, so a hand-written
                # template value is silently reverted by the next release (same
                # discipline as `cloud_app_template.py` `_DEFAULT_GLOBAL_INSTANCE_CAPS`).
                #
                # `or self.helm_chart_version` keeps backward compatibility with
                # an operator that predates the field: unknown ⇒ keep, never blank.
                'helm_chart_version': (
                    release_info.get('chart_version') or self.helm_chart_version
                ),
                'deployed_at': fields.Datetime.now(),
                'deploy_params': False,
            })
            _logger.info("Service %s (id=%s) helm install complete", self.name, self.id)

            # #648 (a)：operator 對 expose 的回報要讀齊。帳本先落地（上面那個
            # write）再判，因為 helm release 已經真的存在——不論下面判成功或失敗，
            # revision / chart_version / deployed_at 都是事實。
            if self._expose_failed(release_info):
                return

        except PaaSOperatorConnectionError as e:
            _logger.error("Operator connection error: %s", e)
            self.write({
                'state': 'error',
                'error_message': _(
                    'Unable to connect to deployment service. '
                    'Please try again later.'),
            })

        except PaaSOperatorError as e:
            _logger.error("Operator error: %s", e)
            # #1424：operator 明確拒絕開通 ⇒ 先取消訂閱（不扣款），訊息才說得準。
            rejection = self._cancel_subscription_on_provision_rejection(e.detail)
            quota_message = (
                self._storage_quota_error_message(e.detail, rejection=rejection)
                # #1415：叢集儲存排程額度不足（operator 在 helm install 前擋下）。
                or self._cluster_storage_capacity_error_message(e.detail, rejection=rejection)
            )
            self.write({
                'state': 'error',
                'error_message': quota_message or _(
                    'Deployment failed: %(detail)s',
                    detail=e.detail or e.message),
            })
            self._alert_cluster_storage_capacity_rejection(e.detail)

    def _storage_quota_error_message(self, detail, rejection=None):
        """#1348：operator 回「工作區儲存配額已滿」時，給租戶看得懂的訊息；其他錯誤回 ``None``。

        ``rejection``（#1424）：``_cancel_subscription_on_provision_rejection`` 的結果。訂閱真的
        被自動取消時（``cancelled``），訊息改成說明「訂閱已取消、不會扣款，請刪除這個服務」；
        沒有訂閱可取消時照舊。

        operator（``extra/paas-operator/src/services/storage_quota.py``）在 helm install
        前檢查 namespace ResourceQuota 的 PVC 數量／requests.storage，用滿時回 409、
        ``detail.error_code == STORAGE_QUOTA_ERROR_CODE``，附用量與保留中的孤兒 PVC
        （#317：刪除服務後 PVC 保留到保留期滿，仍計入配額）。原本租戶只看到
        「Helm installation failed. Check operator logs」，無從自助排除。

        ``error_message`` 由 cron 寫入、畫面直接顯示原文，cron 的 env 語言不是租戶的
        ⇒ 以服務建立者的語言（沒有就用目前 env）組字串，``env._`` 依該 env 的 lang 翻譯。
        """
        self.ensure_one()
        if not isinstance(detail, dict) or detail.get('error_code') != STORAGE_QUOTA_ERROR_CODE:
            return None
        lang = self.sudo().create_uid.lang or self.env.lang
        env = self.with_context(lang=lang).env
        usage = detail.get('usage') or {}
        exhausted = detail.get('exhausted') or []
        pvc = usage.get('persistentvolumeclaims') or {}
        storage = usage.get('requests.storage') or {}
        if 'persistentvolumeclaims' in exhausted and pvc.get('hard'):
            parts = [env._(
                'Workspace storage quota is full: %(used)s of %(hard)s storage volumes are in use.',
                used=pvc.get('used', '?'), hard=pvc['hard'])]
        elif 'requests.storage' in exhausted and storage.get('hard'):
            parts = [env._(
                'Workspace storage quota is full: %(used)s of %(hard)s storage capacity is in use.',
                used=storage.get('used', '?'), hard=storage['hard'])]
        else:
            parts = [env._('Workspace storage quota is full.')]
        orphans = detail.get('orphaned_pvc_count') or 0
        if orphans:
            parts.append(env._(
                '%(count)s of them hold data of deleted services that is still kept for its '
                'retention period (%(size)s).',
                count=orphans, size=detail.get('orphaned_storage') or '?'))
            release_epoch = detail.get('next_release_epoch')
            if isinstance(release_epoch, int) and release_epoch > 0:
                tz = pytz.timezone(self.sudo().create_uid.tz or 'Asia/Taipei')
                when = datetime.fromtimestamp(release_epoch, tz).strftime('%Y-%m-%d')
                parts.append(env._('The earliest of them is released on %(date)s.', date=when))
        if rejection and rejection.get('cancelled'):
            # #1424：訂閱已自動取消；下一步是刪掉這個服務，再到有空間的地方重開。
            parts.append(self._provision_rejection_billing_note(env, rejection))
            parts.append(env._(
                'Please delete this service, then contact support to release the storage '
                'early, or deploy it again in another workspace.'))
        else:
            parts.append(env._(
                'Contact support to release it early, or deploy this service in another '
                'workspace.'))
        return ' '.join(parts)

    def _cluster_storage_capacity_error_message(self, detail, rejection=None):
        """#1415：operator 回「叢集儲存排程額度不足」時給租戶的訊息；其他錯誤回 ``None``。

        operator（``extra/paas-operator/src/services/storage_capacity.py``）在 helm install
        前用同一份 chart／values 渲染出這次要建的磁碟，照 Longhorn 的排程規則模擬放置；
        確定排不下時回 409、``detail.error_code == CLUSTER_STORAGE_CAPACITY_ERROR_CODE``，
        **什麼都沒建**。原本這種情況 PVC 建了排不上，pod 卡 Init 30 分鐘後逾時（#1415）。

        訊息寫給租戶：這是平台的儲存空間問題、不是他的設定錯；沒有建立任何資源；系統管理員
        已收到通知（``_alert_cluster_storage_capacity_rejection``）。租戶能做的只有刪除後
        稍後重開、或聯絡客服。訂閱的說法對照實際行為：#1424 起從未 running 過的服務在這裡
        已被自動取消訂閱（``rejection``）——說「已取消、不會扣款」；沒有自動取消的（曾經 running
        過、平台代管的服務）維持舊說法：刪除服務會取消訂閱（``_settle_on_entity_delete``），
        試用期內不扣款，已扣的點數依未使用天數退回。語言同 #1348：以服務建立者的語言組字串。
        """
        self.ensure_one()
        if (not isinstance(detail, dict)
                or detail.get('error_code') != CLUSTER_STORAGE_CAPACITY_ERROR_CODE):
            return None
        lang = self.sudo().create_uid.lang or self.env.lang
        env = self.with_context(lang=lang).env
        # operator 的 "80Gi"／"77.2Gi" → 租戶看得懂的 "80 GiB"。
        sizes = [
            str(v['size'])[:-2] + ' GiB' if str(v['size']).endswith('Gi') else str(v['size'])
            for v in detail.get('unschedulable') or []
            if isinstance(v, dict) and v.get('size')
        ]
        if sizes:
            parts = [env._(
                'The platform is temporarily short of storage space and cannot create the '
                'disk this service needs (%(size)s).', size=', '.join(sizes))]
        else:
            parts = [env._(
                'The platform is temporarily short of storage space and cannot create the '
                'disks this service needs.')]
        parts.append(env._(
            'Nothing was created for this deployment, and the platform administrators have '
            'been notified.'))
        cancelled = bool(rejection and rejection.get('cancelled'))
        if cancelled:
            parts.append(self._provision_rejection_billing_note(env, rejection))
        parts.append(env._(
            'Please delete this service and deploy it again later, or contact support.'))
        if not cancelled and self.sudo().subscription_id:
            parts.append(env._(
                'Deleting it also cancels its subscription: nothing is charged during the '
                'trial, and points already paid are refunded for the unused days.'))
        return ' '.join(parts)

    @staticmethod
    def _provision_rejection_billing_note(env, rejection):
        """#1424：訂閱已自動取消時，給租戶的計費說明（以呼叫端組好的建立者語言 env）。"""
        if rejection.get('credited'):
            return env._(
                'Its subscription has been cancelled automatically, so it will not be charged '
                'again; the points already paid have been refunded to your wallet for the '
                'unused days.')
        return env._(
            'Its subscription has been cancelled automatically, so it will not be charged.')

    def _is_tenant_managed(self):
        """#1424：這台服務的生命週期是不是租戶自己在 /woow 管的。

        平台代開的「每帳號一台」服務（``provision_partner_id``，例如 HA PaaS）與隱藏實例的
        商品（``template_id.conceal_instances``）不是：租戶在 /woow 看不到、也刪不了它們，
        HA App 還會在 error 時原地重試開通（error → pending）。對它們自動取消訂閱或寄「請刪除
        這個服務」的提醒都沒有意義——取消後重試成功的服務反而會被 lifecycle cron 停權。
        """
        self.ensure_one()
        svc = self.sudo()
        return not svc.provision_partner_id and not svc.template_id.conceal_instances

    def _cancel_subscription_on_provision_rejection(self, detail):
        """#1424：operator 明確拒絕開通時，取消這台服務的訂閱（不扣款）；不適用回 ``None``。

        只在三個條件同時成立時動手：

        1. 錯誤碼是 ``PROVISION_REJECTION_ERROR_CODES``（#1348 工作區儲存配額、#1415 叢集儲存
           額度，operator 回 409）——operator 確定開不起來、服務沒有建立；
        2. 服務從未 running 過（``first_running_at`` 空）——曾經能用的服務不在這裡取消；
        3. 租戶自己管的服務（``_is_tenant_managed``）。

        取消走 ``_settle_on_entity_delete``（與刪除服務同一個結算點）：試用期內什麼都不扣、
        也沒有東西可退；已付費的方案依未使用天數退回錢包（剛下單就被拒＝整期退回）。同時記下
        ``provision_rejected_code``，讓 lifecycle cron 不去停權、讓租戶仍能刪除這台服務。

        取消與記錄在同一個 savepoint：任一步失敗就整組不生效（訂閱照舊、回 ``None``，訊息
        退回舊說法），開通失敗本身照常寫 error——這裡只是記帳，不能讓它把 error 的落地
        一起拖垮。``OperationalError`` 一律外拋（post-commit-bookkeeping-rollback-unit 規則一）。

        回傳 ``{'code', 'cancelled', 'credited'}``；``cancelled`` 為 False 表示沒有可取消的
        進行中訂閱（沒有訂閱、或早已不是 in_progress），仍會記下拒絕碼。
        """
        self.ensure_one()
        code = detail.get('error_code') if isinstance(detail, dict) else None
        if code not in PROVISION_REJECTION_ERROR_CODES:
            return None
        if self.first_running_at or not self._is_tenant_managed():
            return None
        sub = self.sudo().subscription_id
        try:
            with self.env.cr.savepoint():
                credited = 0.0
                cancelled = False
                if sub and sub.state == 'in_progress':
                    credited = sub._settle_on_entity_delete(
                        reason='Provisioning rejected (%s)' % code)
                    cancelled = True
                self.sudo().write({'provision_rejected_code': code})
        except OperationalError:
            raise
        except Exception:
            _logger.exception(
                "[#1424] could not cancel the subscription of service %s (id=%s) after the "
                "operator refused provisioning (%s); the subscription is left untouched",
                self.name, self.id, code)
            return None
        _logger.info(
            "[#1424] operator refused provisioning service %s (id=%s, %s): subscription %s "
            "cancelled=%s credited=%s", self.name, self.id, code,
            sub.id if sub else None, cancelled, credited)
        return {'code': code, 'cancelled': cancelled, 'credited': credited}

    def _provision_rejected_nothing_created(self):
        """#1424：operator 拒絕開通後一直停在 error、從沒跑起來的服務（沒有可停權的東西）。"""
        self.ensure_one()
        return bool(
            self.provision_rejected_code and not self.first_running_at
            and self.state == 'error')

    def _check_operation_allowed(self, operation='write'):
        """#1424：訂閱因為開通被拒而自動取消的服務，租戶仍要能刪除它。

        mixin 的訂閱閘對已取消的訂閱一律拒絕（「訂閱已到期、服務已停權，請續約」）——對
        這種服務，錯誤訊息正叫租戶「請刪除這個服務」，擋下刪除就是死路。只放行
        ``operation='delete'``（controller 的刪除分支傳這個值），更新／rollback 照舊擋。
        """
        self.ensure_one()
        if operation == 'delete' and self._provision_rejected_nothing_created():
            return
        return super()._check_operation_allowed(operation=operation)

    def _alert_storage_expansion_rejection(self, plan, detail):
        """#1513：換方案在扣款前被 operator 升級預檢擋下（磁碟擴不了）時告警給系統管理員。

        租戶身分呼叫（換方案端點）⇒ 告警寫入要提權（activity 給 base.group_system）。
        """
        self.ensure_one()
        self.env['woow_paas_platform.platform_monitor'].sudo()._report_storage_expansion_rejection(
            self.sudo(), plan.sudo(), detail)

    def _alert_cluster_storage_capacity_rejection(self, detail):
        """#1415：開通被叢集儲存額度擋下時，告警給系統管理員（一台一行，同 summary 未處理前追加）。"""
        self.ensure_one()
        if (not isinstance(detail, dict)
                or detail.get('error_code') != CLUSTER_STORAGE_CAPACITY_ERROR_CODE):
            return
        self.env['woow_paas_platform.platform_monitor']._report_storage_capacity_rejection(
            self, detail)

    # ------------------------------------------------------------------ #
    #  #411 — 全量 values 組裝（單一真相源）                              #
    # ------------------------------------------------------------------ #

    def _assemble_deploy_values(self, apply_plan=True, values=None, plan=None):
        """組裝部署用的全量「非敏感」helm values ＋ expose_config（單一真相源）。

        Fresh install 與既存 release 升級**共用這一條**組裝管線，讓 helm 在
        ``reset_values`` 語意下（chart 自身 values.yaml 為 base）每次都把平台
        管理的所有值補齊 —— chart 升版新增的預設能自然 coalesce 生效，終結
        ``reuse_values`` bug class（既存 release 升級路徑靜默漏掉 chart 新預設；
        見 #411 / #413 與 docs/decisions/patterns/helm-reset-values-unified-pipeline.md）。

        組裝順序（後者疊在前者上）：

        1. ``svc.helm_values`` —— template 預設（``template.helm_default_values``）
           ＋ user config ＋ MCP api key ＋ plan overlay，於 service create 時已
           merge 定案（controllers/paas.py ``_deploy_service``）。此處先剝掉
           ``config.sensitive`` 子樹（``_strip_mcp_sensitive_values``），再把
           helm_value_specs 帶 ``format`` 的租戶文字欄位轉成 chart 形態
           （``template._apply_spec_formats``，例 code-server git.tokenHosts）。
           接著以範本值蓋上「平台政策鍵」（``_TEMPLATE_POLICY_VALUE_PATHS``；
           ``_inject_template_policy_keys``）。
        2. storage_class injector（PVC 生命週期；n8n 例外，首裝時已寫進
           helm_values，見 ``_pin_install_storage_class``／#1334）
        3. image_pull_secrets injector（私有 registry）
        4. allowed_origins injector（open-design 瀏覽器 Origin 白名單，#380）
        5. dashboard_public_url injector（hermes-agent MCP OAuth 回呼，#424①）
           ＋ n8n 公開網址（main.config.WEBHOOK_URL／N8N_EDITOR_BASE_URL，#1334 附帶）
           ＋ n8n 固定執行設定（main.config.N8N_PYTHON_ENABLED=false，#1533；與公開網址無關，
           沒有網址也注入）
        6. apiserver_cidrs injector（open-design／hermes-agent console egress，#404）
        7. workspace_service_url injectors（同工作區 running 提供者的叢集內網址，只有網址；
           chart 放進不入 checksum 的 runtime ConfigMap，不會讓 pod 重啟；已有明確值就
           不覆寫）：Omnigent → code-server／pi-agent／open-design 的
           ``omnigentHost.serverUrl``；Headscale → tailscale 的
           ``tailscale.workspaceHeadscaleUrl``；LiteLLM → pi-agent／code-server／
           open-design／hermes-agent 的 ``llmGateway.baseUrl``（帶 /v1）與 mcp-litellm 的
           ``config.nonSensitive.connection.base_url``（不帶）；Nextcloud → mcp-nextcloud 的
           ``config.nonSensitive.connection.base_url``（唯一給**公開** https 網址的提供者）。見
           ``_WORKSPACE_URL_INJECTORS``。
        8. odoo addon selection（initContainer + addons_path）

        #1158：步驟 1 之後再疊**目前訂閱方案**的 cs_* 規格（``_apply_plan_spec_overlay``
        既有服務模式：儲存只擴不縮）——方案是規格的真相源，換方案的背景 upgrade 若失敗，
        之後任何一次 deploy／redeploy／reconfigure 都收斂到目前方案。換方案當下已把同一份
        結果寫回 ``helm_values``，所以正常情況這一步是 no-op（prod 2026-10-06 盤點 44 台
        0 差異）。``apply_plan=False`` 只給 reset 密碼那條（``--atomic`` 15 秒預算）：方案
        規格改的是 pod template，夾帶進 reset 必定等不到新 pod 而回退（F-461-2）。

        #1513：``values``／``plan`` 給換方案在**扣款前**預演用——以「換方案後要存的
        values」與「新方案」組出真正 upgrade 會送的全量 values（送 operator 預檢），
        此時 DB 裡還是舊 values、舊訂閱。兩者都不給＝現行行為（讀 stored values、目前方案）。

        **純唯讀、不寫 DB、不呼叫 operator** —— 盤點腳本可安全 dry-run 呼叫，
        取 ``['values']`` 去比對 ``helm get values <rel>``（stored、非敏感）。
        ``sensitive_values``（admin_password）不在此組：它是敏感值、且 stored
        release values 本就不含它，由各呼叫點自組（reset 路徑帶新密碼）。

        Returns:
            dict: {
                'values': 全量非敏感 helm values（dict）,
                'expose_config': ingress expose dict 或 None（Cloudflare tunnel；
                                僅 fresh install 用，upgrade endpoint 無 expose
                                參數，升級路徑忽略此鍵）,
            }
        """
        self.ensure_one()
        template = self.template_id
        # #559：helm_values 有 groups=，屬性直讀會 AccessError。走唯一入口。
        values = self._strip_mcp_sensitive_values(
            values if values is not None else self._helm_values_dict()) or {}
        # helm_value_specs 帶 ``format`` 的租戶文字欄位 → chart 形態（例 code-server
        # 的 git.tokenHosts「github.com, git.example.com」→ https URL list）。
        # helm_values 保留租戶原文；不合法的值丟掉、chart 預設補位。
        values = template._apply_spec_formats(values)
        if apply_plan:
            plan = plan or self._current_plan()
            if plan:
                values = self._apply_plan_spec_overlay(
                    values, template, plan, existing=True)
        # 範本的平台政策鍵（ha-full 的 ipBanEnabled／unlockCoreConfig）以範本為準：
        # 既有服務的快照沒有這些鍵，不補就會在 Redeploy 時吃到 chart 預設。
        values = self._inject_template_policy_keys(values, template)

        # Inject StorageClass for PVC lifecycle management
        storage_class = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.storage_class_name', 'longhorn-delete'
        )
        if storage_class:
            values = self._inject_storage_class(
                values, template.slug, storage_class,
            )

        # Inject imagePullSecrets for private registry access
        registry_secret = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.registry_secret_name', 'paas-registry'
        )
        if registry_secret:
            values = self._inject_image_pull_secrets(
                values, registry_secret,
                # #1130：woow-odoo schema 只收 `[{name: ...}]`（字串清單在 install 當下
                # 直接被 schema 擋）；Bitnami 系 chart 兩種都收，維持原本的字串形。
                as_objects=template._is_woow_odoo_chart(),
            )

        # Inject the service's own public origin for charts that gate on the
        # browser Origin header (open-design daemon 403s a public Origin unless
        # whitelisted verbatim; the chart never learns the tenant host — the
        # platform exposes services via a Cloudflare tunnel, not the chart's
        # ingress). No-op for other templates. See #380.
        public_base_url = self._public_base_url()
        if public_base_url:
            values = self._inject_allowed_origins(
                values, template.slug, public_base_url,
            )
            # Inject the service's own public URL for charts whose app must hand
            # that URL to a third party (hermes-agent dashboard is the OAuth
            # redirect_uri target for external MCP servers). No-op elsewhere.
            # See #424①.
            values = self._inject_dashboard_public_url(
                values, template.slug, public_base_url,
            )
            # n8n 的 webhook／OAuth callback／編輯器連結要用公開網址（#1334 附帶）。
            values = self._inject_n8n_public_urls(
                values, template.slug, public_base_url,
            )

        # n8n 的平台固定執行設定（#1533：關掉沒有 runner 的 Python Code 節點）。
        values = self._inject_n8n_runtime_defaults(values, template.slug)

        # Inject the kube-apiserver egress allowlist for charts whose pods run
        # kubectl from under a tenant NetworkPolicy (open-design console).
        # No-op for other templates. See #404.
        apiserver_cidrs = self._get_apiserver_cidrs()
        if apiserver_cidrs:
            values = self._inject_apiserver_cidrs(
                values, template.slug, apiserver_cidrs,
            )

        # Inject same-workspace service URLs (Omnigent → omnigent host,
        # Headscale → tailscale control plane, LiteLLM → LLM gateway base URL,
        # Nextcloud → mcp-nextcloud connection.base_url — the PUBLIC https URL).
        # URL only — never a token / key. Runtime config only: the charts put
        # it in a ConfigMap outside the pod checksum, so a new/changed/removed
        # URL never restarts the pod (safe on the 15 s reset path too). An
        # explicit non-empty value in helm_values wins. No-op when the
        # workspace has no running provider or the chart has no such path.
        values = self._inject_workspace_service_urls(values, template.slug)

        # #423 W5a：VM 型的 runStrategy 必須與服務狀態同源。
        #
        # 為什麼一定要寫進 values：suspend/resume 走的是 operator 的外科式
        # `kubectl patch vm`（不動 helm release），所以 live 物件與 helm 的
        # stored manifest 會漂移。下一次 helm upgrade 若 render 出 chart 預設
        # 的 `Always`，就會**把停權中（欠費）的 VM 喚醒**——沉默失效，且會演
        # 變成計費爭議。把當前狀態餵進 values 讓兩條路徑同源。
        # 守線測試：src/tests/test_haos_suspend_resume_runstrategy.py
        if template.workload_type == 'vm':
            values.setdefault('vm', {})['runStrategy'] = (
                'Halted' if self._is_halted() else 'Always'
            )

        if template._is_woow_odoo_chart():
            # #1130：woow-odoo chart 自己擁有 addon initContainer（每次開機從 image
            # `/all-addons` 複製到 emptyDir），平台只給名稱清單——下面那段 Bitnami 形的
            # initContainers／extraVolumes／customPostInitScripts 在 tenant schema
            # （additionalProperties: false）會讓 install 當場失敗。每次組裝都從
            # `odoo_addon_ids` 重算**完整**清單（含空 list），取消勾選不必另外清 values。
            # 語意與舊 builder 相同：只有 third_party／custom 需要複製（其餘本來就在
            # image 的 addons_path 內）；勾選不自動 -i、移除不代為 uninstall。
            # 完整的 addon P1（UI 深連結、builder 清理、文件）在 #1187。
            values['addons'] = {'selected': self._woow_odoo_selected_addons()}
            # 身分值進 backup manifest、還原時比對（woow-odoo `identity.*`；#1213 消費）。
            # 用 DB id：與服務列一對一、跨 redeploy 不變；namespace／release 另有比對。
            values['identity'] = {
                'cloudServiceId': str(self.id),
                'workspaceId': str(self.workspace_id.id),
            }
        # Inject Odoo addon selection (initContainer + addons_path) — Bitnami 形
        elif self.odoo_addon_ids and template.slug == 'odoo':
            image_vals = values.get('image', {})
            registry = image_vals.get('registry', 'docker.io')
            repo = image_vals.get('repository', '')
            tag = image_vals.get('tag', 'latest')
            image_ref = f"{registry}/{repo}:{tag}"
            addon_values = self._build_odoo_addon_helm_values(image_ref)
            # Deep-merge addon values into the assembled values
            for key, val in addon_values.items():
                if key in values and isinstance(values[key], dict) and isinstance(val, dict):
                    values[key].update(val)
                else:
                    values[key] = val

        return {
            'values': values,
            'expose_config': self._build_expose_config(),
        }

    def _build_expose_config(self):
        """Cloudflare tunnel ingress expose 設定（fresh install 用）。

        Template 開 ingress 且 service 有 subdomain 才 expose；與 service create
        時 (controllers/paas.py ``_deploy_service``) 及 action_redeploy fresh 分支
        的算法一致，收斂為單一真相源。upgrade endpoint 無 expose 參數 —— ingress
        於首裝建立後升級不重設（Cloudflare tunnel 已在），故升級路徑不用此值。
        """
        self.ensure_one()
        if self.template_id.ingress_enabled and self.subdomain:
            config = {'enabled': True, 'subdomain': self.subdomain}
            if self.template_id._is_woow_odoo_chart():
                # #1130：明帶 nginx Service（＝release 名）＋80。operator 的自動偵測挑
                # 「第一個非 headless Service」（extra/paas-operator/src/api/releases.py），
                # woow-odoo 同 ns 還有 `<release>-postgresql`，挑錯就是一條指向 DB 的
                # 公開 route（#1141 裁決 4 連帶必做）。
                config['service_name'] = self.helm_release_name
                config['service_port'] = WOOW_ODOO_SERVICE_PORT
            return config
        return None

    def _woow_odoo_legacy_release_error(self):
        """#1130 過渡守門：template 已換成 woow-odoo，但這台服務存的 values 還是 Bitnami 形。

        這類服務（2026-10：prod svc-f11b1cbb、stg svc-42487178）的 release 是 Bitnami odoo；
        拿舊 values 對新 chart 做 helm upgrade 一定被 tenant schema 擋（叢集不動），但
        reconfigure 會先把狀態寫成 upgrading、背景失敗後翻成 error——租戶按一次「儲存設定」
        或改 addon，跑得好好的服務在平台上就變成 error。這裡在任何寫入之前擋下，回可讀
        訊息、狀態不動。判準是存的 values 帶著舊 seed 的 Bitnami 頂層鍵
        （``BITNAMI_ODOO_VALUE_KEYS``，woow-odoo schema 全部拒收），不是 chart 名比對——
        正式的「chart 換版拒絕 in-place upgrade」守門與實例處置屬 #1128，那邊落地後本守門
        可移除。回 ``None`` 表示放行。
        """
        self.ensure_one()
        if not self.template_id._is_woow_odoo_chart():
            return None
        if not BITNAMI_ODOO_VALUE_KEYS & set(self._helm_values_dict() or {}):
            return None
        return _(
            'This Odoo service still runs on the previous Odoo deployment and cannot be '
            'changed or redeployed in place. Please contact support to migrate it.')

    @staticmethod
    def _is_reserved_email_domain(email):
        """#1335：email 的網域是否屬於永遠收不到信的保留網域（大小寫、結尾點不計）。"""
        domain = email.rsplit('@', 1)[-1].strip().rstrip('.').lower()
        if domain.rsplit('.', 1)[-1] in RESERVED_EMAIL_TLDS:
            return True
        return any(domain == d or domain.endswith('.' + d) for d in RESERVED_EMAIL_DOMAINS)

    @api.model
    def _woow_odoo_admin_email_error(self, template, values, reject_reserved=False):
        """#1130：woow-odoo 的 `conf.adminEmail` 檢查；回錯誤訊息，合格回 ``None``。

        檢查的是**合併後**的 values（template 預設＋租戶輸入），create 與 reconfigure
        同一個判準（#1160）。非 woow-odoo template 一律 ``None``。

        ``reject_reserved``（#1335）：另擋 example.com 這類保留網域。只有 create 開
        ——reconfigure 若也擋，舊預設 user@example.com 的存量服務改任何設定都會被
        連坐卡住，而 bootstrap 的 set-admin 只在首裝套用 login，改了也不會生效。
        """
        if not template._is_woow_odoo_chart():
            return None
        conf = values.get('conf') if isinstance(values, dict) else None
        email = conf.get('adminEmail') if isinstance(conf, dict) else None
        if (not isinstance(email, str) or len(email) > 254
                or not WOOW_ODOO_ADMIN_EMAIL_RE.match(email)):
            return _('Administrator email must be a valid email address')
        if reject_reserved and self._is_reserved_email_domain(email):
            return _('Administrator email must be a mailbox you can receive mail at '
                     '(%(domain)s is a reserved example domain, so password reset '
                     'emails would never arrive)',
                     domain=email.rsplit('@', 1)[-1])
        return None

    def _woow_odoo_selected_addons(self):
        """#1130：woow-odoo `addons.selected`——要從 image `/all-addons` 複製的 addon 名稱。

        與 Bitnami 形 builder（``_build_odoo_addon_helm_values``）同一條篩選：只有
        ``third_party``／``custom`` 需要複製。名稱在 image 內不存在時 chart 以
        ``Init:Error`` fail-loud（woow-odoo values.yaml〈addons〉）。排序固定，讓同一組
        勾選每次 render 出位元相同的 values（不白白觸發 Recreate）。
        """
        self.ensure_one()
        return sorted(
            addon.technical_name
            for addon in self.odoo_addon_ids
            if addon.source_type in ('third_party', 'custom')
        )

    def _build_odoo_addon_helm_values(self, image_ref):
        """Build Helm values for Odoo addon selection via initContainer.

        Args:
            image_ref: Full image reference (e.g., registry/repo:tag)
                       used for the initContainer (same as main container).

        Returns:
            Dict of Helm values to deep-merge, or empty dict if no addons.
        """
        if not self.odoo_addon_ids:
            return {}

        # Separate addons by source type
        copy_addons = []  # need initContainer copy from /all-addons/
        for addon in self.odoo_addon_ids:
            if addon.source_type in ('third_party', 'custom'):
                copy_addons.append(addon.technical_name)

        values = {}
        selected_addons_path = ODOO_SELECTED_ADDONS_PATH

        if copy_addons:
            addon_list = ' '.join(copy_addons)
            values['initContainers'] = [{
                'name': 'addon-selector',
                'image': image_ref,
                'imagePullPolicy': 'IfNotPresent',
                'command': ['/bin/bash', '-c'],
                'args': [
                    f'echo "[addon-selector] Copying addons..."\n'
                    f'for addon in {addon_list}; do\n'
                    f'  if [ -d "/all-addons/$addon" ]; then\n'
                    f'    cp -r "/all-addons/$addon" /mnt/selected-addons/\n'
                    f'    echo "  + $addon"\n'
                    f'  else\n'
                    f'    echo "  ! $addon not found"\n'
                    f'  fi\n'
                    f'done\n'
                    f'CONF=/bitnami/odoo/conf/odoo.conf\n'
                    f'if [ -f "$CONF" ] && ! grep -q "selected-addons" "$CONF"; then\n'
                    f'  sed -i "s|^addons_path.*|&,{selected_addons_path}|" "$CONF"\n'
                    f'  echo "[addon-selector] Updated addons_path"\n'
                    f'fi\n'
                    f'echo "[addon-selector] Done"',
                ],
                'resources': {
                    'requests': {'cpu': '100m', 'memory': '128Mi'},
                    'limits': {'cpu': '500m', 'memory': '256Mi'},
                },
                'volumeMounts': [
                    {'name': 'selected-addons', 'mountPath': '/mnt/selected-addons'},
                    {'name': 'odoo-data', 'mountPath': '/bitnami/odoo'},
                ],
            }]
            values['extraVolumes'] = [{'name': 'selected-addons', 'emptyDir': {}}]
            values['extraVolumeMounts'] = [
                {'name': 'selected-addons', 'mountPath': selected_addons_path},
            ]

        # Fallback for first boot (odoo.conf doesn't exist yet in initContainer)
        addons_path_script = (
            f'#!/bin/bash\n'
            f'CONF=/opt/bitnami/odoo/conf/odoo.conf\n'
            f'if [ -f "$CONF" ] && ! grep -q "selected-addons" "$CONF"; then\n'
            f'  sed -i "s|^addons_path.*|&,{selected_addons_path}|" "$CONF"\n'
            f'fi'
        )
        values.setdefault('customPostInitScripts', {})
        values['customPostInitScripts']['00-addons-path.sh'] = addons_path_script

        return values

    def _remove_addon_helm_values(self, values):
        """Remove only addon-selector related entries from helm values.

        Used when all addons are deselected. Precisely removes addon-specific
        items without affecting other extraVolumes/initContainers.
        """
        if 'initContainers' in values:
            values['initContainers'] = [
                c for c in values['initContainers']
                if c.get('name') != 'addon-selector'
            ]
        if 'extraVolumes' in values:
            values['extraVolumes'] = [
                v for v in values['extraVolumes']
                if v.get('name') != 'selected-addons'
            ]
        if 'extraVolumeMounts' in values:
            values['extraVolumeMounts'] = [
                m for m in values['extraVolumeMounts']
                if m.get('name') != 'selected-addons'
            ]
        if 'customPostInitScripts' in values:
            values['customPostInitScripts'].pop('00-addons-path.sh', None)

    @staticmethod
    def _inject_storage_class(values, template_slug, storage_class):
        """Inject StorageClass into helm values for PVC lifecycle management.

        Different Helm charts use different value paths for storageClass.
        This method sets the correct paths based on the template slug.

        Args:
            values: Helm values dict (will be modified in-place and returned)
            template_slug: Template identifier (e.g., 'odoo', 'postgresql')
            storage_class: StorageClass name to inject

        Returns:
            Modified values dict
        """
        # Mapping of template slug to storageClass value paths
        path_map = {
            # #1130：woow-odoo（woow-paas-charts#81）每份部署 3 顆 PVC——data、獨立
            # postgres StatefulSet、backup——三個 storageClass 空值都 fail-loud，平台三個
            # 都要注入；不再是 Bitnami 子 chart 的 postgresql.primary.persistence。
            'odoo': [
                ('persistence', 'storageClass'),
                ('postgres', 'persistence', 'storageClass'),
                ('backup', 'persistence', 'storageClass'),
            ],
            'postgresql': [
                ('primary', 'persistence', 'storageClass'),
            ],
            # #1334：n8n 不在每次部署時注入——正確路徑 main.persistence.storageClass
            # 改由 `_pin_install_storage_class` 在首裝時寫進 helm_values（PVC 的
            # storageClassName 不可變，存量服務不能被改）。空 list＝不走 fallback。
            'n8n': [],
            'redis': [
                ('master', 'persistence', 'storageClass'),
                ('replica', 'persistence', 'storageClass'),
            ],
            # MCP server cloud services (hermes/odoo/n8n) — single mcp-server
            # chart, persistence.storageClass. Explicit entries (default already
            # falls back here) to document intent and guard against default drift.
            'mcp-hermes': [
                ('persistence', 'storageClass'),
            ],
            'mcp-odoo': [
                ('persistence', 'storageClass'),
            ],
            'mcp-n8n': [
                ('persistence', 'storageClass'),
            ],
            # #423 W5a: HAOS（VM 型）。chart 的 DataVolume 用扁平
            # persistence.storageClass，與 mcp-* 同鍵名，故預設 fallback 本來
            # 就會命中——列出顯式 entry 是記錄意圖並防 default 漂移（同 mcp-*
            # 的既有做法）。
            # ⚠ chart 端此鍵 required：woow-k3s 有兩個 default StorageClass
            # （local-path 與 longhorn），留空會讓 DataVolume 落點不確定，
            # 可能掉到 node-local 的 local-path（節點故障即資料失聯）。故
            # chart 讓空值直接 render 失敗，平台這邊也絕不可傳空字串。
            'haos': [
                ('persistence', 'storageClass'),
            ],
            # #424①: hermes-agent（open-design 派生 chart）走扁平
            # persistence.storageClass —— 契約明訂這是平台注入點，per-PVC 的
            # persistence.data.storageClass 留空時由 chart 繼承扁平鍵（與 open-design
            # 同款）。與 mcp-* 同理列出顯式 entry：記錄意圖並防 default 漂移。
            # 若 Phase A Step 0 翻案要開內建 DB，這裡必須同 PR 補
            # ('postgresql', 'persistence', 'storageClass') /
            # ('redis', 'persistence', 'storageClass') —— 那兩顆 PVC 不在扁平鍵的
            # 繼承鏈上。lock-step 由 src/tests/test_hermes_agent_seed_424.py 的
            # test_db_disabled_lockstep_storage_class 守。
            'hermes-agent': [
                ('persistence', 'storageClass'),
            ],
        }

        paths = path_map.get(template_slug, [('persistence', 'storageClass')])

        for path in paths:
            d = values
            for key in path[:-1]:
                d = d.setdefault(key, {})
            d[path[-1]] = storage_class

        return values

    def _pin_install_storage_class(self):
        """首次安裝時把 storageClass 寫進 ``helm_values``（#1334）。

        只處理 ``_INSTALL_PINNED_STORAGE_CLASS_PATHS`` 裡的範本。helm_values 已有
        非空值就不動（重試安裝、或管理員明確指定時沿用原值）。之後的 upgrade 不會
        再經過這裡——值留在 helm_values，由 ``_assemble_deploy_values`` 原樣帶出，
        所以同一台服務的 storageClassName 永遠一致。
        """
        self.ensure_one()
        paths = _INSTALL_PINNED_STORAGE_CLASS_PATHS.get(self.template_id.slug)
        if not paths:
            return
        storage_class = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.storage_class_name', 'longhorn-delete'
        )
        if not storage_class:
            return
        values = self._helm_values_dict()
        changed = False
        for path in paths:
            d = values
            for key in path[:-1]:
                if not isinstance(d.get(key), dict):
                    d[key] = {}
                d = d[key]
            if not d.get(path[-1]):
                d[path[-1]] = storage_class
                changed = True
        if changed:
            self._apply_helm_values(values)

    @staticmethod
    def _inject_n8n_public_urls(values, template_slug, base_url):
        """把服務公開網址寫進 n8n 的 ``main.config``（#1334 附帶）。

        ``WEBHOOK_URL``：webhook／表單節點顯示給外部呼叫的網址、OAuth 憑證的
        callback；``N8N_EDITOR_BASE_URL``：邀請信、重設密碼信裡的連結。兩者都以
        ``/`` 結尾（n8n 文件寫法）。已有非空值（管理員明確指定，例如自訂網域）就
        不覆寫。其他範本 no-op。
        """
        if template_slug != 'n8n' or not base_url:
            return values
        url = base_url.rstrip('/') + '/'
        main = values.get('main')
        if not isinstance(main, dict):
            main = values['main'] = {}
        config = main.get('config')
        if not isinstance(config, dict):
            config = main['config'] = {}
        for key in _N8N_PUBLIC_URL_CONFIG_KEYS:
            if not str(config.get(key) or '').strip():
                config[key] = url
        return values

    @staticmethod
    def _inject_n8n_runtime_defaults(values, template_slug):
        """n8n 的平台固定執行設定（``_N8N_RUNTIME_DEFAULT_CONFIG``，#1533）寫進 ``main.config``。

        ``main.config`` 的每個鍵由 8gears chart 變成容器 env。已有非空值（服務自己的設定）
        就不覆寫。其他範本 no-op。
        """
        if template_slug != 'n8n':
            return values
        main = values.get('main')
        if not isinstance(main, dict):
            main = values['main'] = {}
        config = main.get('config')
        if not isinstance(config, dict):
            config = main['config'] = {}
        for key, value in _N8N_RUNTIME_DEFAULT_CONFIG.items():
            if not str(config.get(key) or '').strip():
                config[key] = value
        return values

    @staticmethod
    def _inject_image_pull_secrets(values, secret_name, as_objects=False):
        """Inject imagePullSecrets into helm values for private registry access.

        Uses global.imagePullSecrets which is supported by all Bitnami charts
        and propagates to all containers including initContainers.

        Args:
            values: Helm values dict (modified in-place and returned)
            secret_name: Name of the dockerconfigjson K8s Secret
            as_objects: #1130：以 ``{'name': ...}`` 物件形寫入（woow-odoo schema 只收
                這一形；字串會在 install 當下被 schema 擋）。預設 False＝原本的字串形。

        Returns:
            Modified values dict
        """
        values.setdefault('global', {})
        existing = values['global'].get('imagePullSecrets', [])
        if as_objects:
            names = {
                item.get('name') if isinstance(item, dict) else item
                for item in existing
            }
            existing = [
                item if isinstance(item, dict) else {'name': item}
                for item in existing
            ]
            if secret_name not in names:
                existing.append({'name': secret_name})
        elif secret_name not in existing:
            existing.append(secret_name)
        values['global']['imagePullSecrets'] = existing
        return values

    @api.model
    def _inject_template_policy_keys(self, values, template):
        """以範本 ``helm_default_values`` 的值蓋上平台政策鍵（就地修改並回傳）。

        鍵清單見 ``_TEMPLATE_POLICY_VALUE_PATHS``（含為什麼 ``persistence.size`` 刻意
        不在裡面）。範本沒有該鍵、或範本 JSON 壞掉時不動——壞 JSON 在開通時就擋過，
        這裡不讓它把部署整個卡住。
        """
        paths = _TEMPLATE_POLICY_VALUE_PATHS.get(template.slug)
        if not paths:
            return values
        try:
            defaults = json.loads(template.helm_default_values or '{}')
        except ValueError:
            _logger.warning(
                "Template %s has invalid helm_default_values; policy keys not applied",
                template.id)
            return values
        for path in paths:
            src = defaults
            for key in path:
                if not isinstance(src, dict) or key not in src:
                    src = _MISSING
                    break
                src = src[key]
            if src is _MISSING:
                continue
            dst = values
            for key in path[:-1]:
                if not isinstance(dst.get(key), dict):
                    dst[key] = {}
                dst = dst[key]
            dst[path[-1]] = copy.deepcopy(src)
        return values

    @staticmethod
    def _inject_allowed_origins(values, template_slug, origin):
        """Inject the service's own public origin into charts that gate on the
        browser Origin header.

        The open-design daemon rejects a PUBLIC tenant Origin unless it is
        whitelisted verbatim (its same-origin fallback only trusts
        loopback/private-LAN hosts), and a bare "*" CRASHES it — so the chart
        cannot self-default this (it never learns the tenant host: the platform
        exposes services via a Cloudflare tunnel, not the chart's ingress). The
        platform, which alone knows the host, supplies it here. No-op for charts
        without such a value path. See woow-paas-charts open-design 0.1.1 +
        issue #380.

        Args:
            values: Helm values dict (modified in-place and returned)
            template_slug: Template identifier (e.g., 'open-design')
            origin: The service's own origin, e.g. "https://<sub>.<domain>"

        Returns:
            Modified values dict
        """
        path_map = {
            'open-design': [('config', 'allowedOrigins')],
            # code-server：公開網址的 host 成為 --trusted-origins（瀏覽器經 tunnel 送來的
            # Origin 才會被接受）；chart 從 codeServer.publicUrl 取 host。
            'code-server': [('codeServer', 'publicUrl')],
            # omnigent：accounts 模式的 OMNIGENT_ACCOUNTS_BASE_URL（https ⇒ cookie 用
            # __Host- 前綴；WebSocket 也以它比對 Origin）。chart 同樣學不到 tenant host。
            'omnigent': [('server', 'baseUrl')],
            # nextcloud：Nextcloud 以 trusted_domains 比對 Host，**不在清單內的網域整站
            # 拒絕服務**（回「Access through untrusted domain」），而 chart 只知道
            # localhost（探針用）。chart 從 nextcloud.publicUrl 衍生
            # NEXTCLOUD_TRUSTED_DOMAINS／OVERWRITEHOST／OVERWRITECLIURL；少了這一條，
            # 服務開得起來但租戶一打開網址就是錯誤頁。
            'nextcloud': [('nextcloud', 'publicUrl')],
            # ha-full：寫進 HA core.config 的 external_url（chart 0.1.5 seed STEP 7，
            # #1388）。少了它 Companion App／需要公開網址的整合都拿不到位址，而 image 的
            # `homeassistant:` YAML 讓 config_source=yaml、租戶在 UI 也改不了。
            # chart 只在原值為空或仍是上次平台寫的值時才覆寫。
            'ha-full': [('seed', 'coreConfig', 'externalUrl')],
            'ha-full-apporo': [('seed', 'coreConfig', 'externalUrl')],
        }
        paths = path_map.get(template_slug)
        if not paths:
            return values
        for path in paths:
            d = values
            for key in path[:-1]:
                d = d.setdefault(key, {})
            d[path[-1]] = origin
        return values

    @staticmethod
    def _inject_dashboard_public_url(values, template_slug, base_url):
        """把服務自身的對外 URL 注入需要「知道自己公開位址」的 chart。

        #424①：hermes-agent 的 dashboard（容器內 :9119）是 MCP OAuth 授權流程的
        回呼入口——agent 要把 ``redirect_uri`` 交給外部 MCP server，該 URL 必須是
        **租戶從瀏覽器看得到的公開位址**，不是 pod 內的 loopback。chart 自己學不到
        這個值（平台以 Cloudflare tunnel 對外，不走 chart 的 ingress），與
        ``_inject_allowed_origins``（#380）同一 family，由唯一知道 host 的平台補上。

        對外形態＝**root URL**（chart 0.1.3 移除 webui container，nginx ``/``
        直連 dashboard，故 OAuth ``redirect_uri``／SPA 資產路徑／cookie Path 一律
        以 root 為基準）：
        ``https://{subdomain}.{paas_domain}``，
        由 chart 的 auth-proxy 轉給 127.0.0.1:9119。

        ⚠ 注入值**必須是 root URL**：上游直接拿這個值當 OAuth ``redirect_uri``
        的權威來源；先前 0.1.x（含 0.1.1）注入 ``/dashboard`` suffix，OAuth
        回呼路徑與 SPA fetch 才能落到 dashboard，0.1.3 移除 webui 後 suffix
        變成多餘且會把回呼打到 404 subpath。

        無此 value path 的 chart 一律 no-op。

        #672：對照表已上移為 module 層的 ``_DASHBOARD_PUBLIC_URL_VALUE_PATHS``
        ——同一張表現在也是「要不要把這個 URL 交給租戶」的判準
        （``_dashboard_public_url``）。放在方法內部的話，交付端只能自己另寫一份
        判斷，而那份會與這裡各自演化。

        Args:
            values: Helm values dict（就地修改並回傳）
            template_slug: Template identifier（例 'hermes-agent'）
            base_url: 服務自身對外根位址，例 "https://<sub>.<domain>"

        Returns:
            Modified values dict
        """
        paths = _DASHBOARD_PUBLIC_URL_VALUE_PATHS.get(template_slug)
        if not paths or not base_url:
            return values
        public_url = base_url.rstrip('/')
        for path in paths:
            d = values
            for key in path[:-1]:
                d = d.setdefault(key, {})
            d[path[-1]] = public_url
        return values

    @staticmethod
    def _parse_cidr_list(raw):
        """把逗號分隔的 CIDR 字串 parse 成 list（去空白、濾空項）。

        ir.config_parameter 只能存字串，但 chart 的 apiServerCidrs 是 CIDR
        字串陣列，故需要這層轉換。

        Args:
            raw: 逗號分隔字串，例如 "10.43.0.1/32, 192.168.10.21/32"

        Returns:
            CIDR 字串 list（輸入為空 → 空 list）
        """
        if not raw:
            return []
        return [c.strip() for c in raw.split(',') if c.strip()]

    def _get_apiserver_cidrs(self):
        """讀取 console → kube-apiserver 的 egress 白名單（cluster-specific）。

        預設 ``10.43.0.1/32`` 與 chart 預設一致（k3s 的 kubernetes Service
        ClusterIP），保持 cluster-agnostic。**woow-k3s 這類 CNI 在 kube-proxy
        DNAT «之後» 比對 egress 的叢集**，連 ClusterIP 會被 DNAT 成 apiserver
        的真實 endpoint（control-plane node IP），該 IP 不在白名單就被 deny —
        這些 node /32 屬 cluster-specific 值，由 ops 在各環境以 config
        parameter 設定，不寫死進 code / git seed。見 #404。

        Returns:
            CIDR 字串 list
        """
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.apiserver_cidrs', '10.43.0.1/32',
        )
        return self._parse_cidr_list(raw)

    @staticmethod
    def _inject_apiserver_cidrs(values, template_slug, cidrs):
        """Inject the kube-apiserver egress allowlist into charts whose pods must
        reach the apiserver from under a tenant NetworkPolicy.

        The open-design console runs ``kubectl`` (connect.sh) to locate and exec
        into the daemon pod. Its NP egress opens ONLY the CIDRs listed in
        ``networkPolicy.apiServerCidrs``; the chart defaults to the kubernetes
        Service ClusterIP alone, which is silently insufficient on clusters whose
        CNI matches egress POST-DNAT (the packet's real destination becomes a
        control-plane node IP). Those node IPs are cluster-specific, so the chart
        cannot self-default them — the platform supplies them here. No-op for
        charts without such a value path. See issue #404 + woow-paas-charts
        open-design 0.1.3 (``values.yaml`` apiServerCidrs).

        Args:
            values: Helm values dict (modified in-place and returned)
            template_slug: Template identifier (e.g., 'open-design')
            cidrs: List of CIDR strings, e.g. ['10.43.0.1/32', '192.168.10.21/32']

        Returns:
            Modified values dict
        """
        path_map = {
            'open-design': [('networkPolicy', 'apiServerCidrs')],
            # #424①: hermes-agent 的 console 與 open-design 同構（同一顆 baked
            # console image、同一支 connect.sh kubectl exec 進 daemon 容器），故
            # 同樣需要 apiserver egress 白名單，鍵名亦沿用（chart 由 open-design 派生）。
            'hermes-agent': [('networkPolicy', 'apiServerCidrs')],
            # #1441：omnigent server 以 K8s API 在本 ns 建 runner Job（Role 只限本 ns）。
            # chart ≥ 0.1.5 拿掉了「RFC 1918 全段 :6443」的 fallback，空值只剩 Service
            # ClusterIP（woow-k3s 是 DNAT 後比對，光有 ClusterIP 連不到）⇒ 平台必須注入
            # 同一份 node /32。鍵名照 chart（小寫 s：apiserverCidrs）。
            'omnigent': [('networkPolicy', 'apiserverCidrs')],
        }
        paths = path_map.get(template_slug)
        if not paths or not cidrs:
            return values
        for path in paths:
            d = values
            for key in path[:-1]:
                d = d.setdefault(key, {})
            d[path[-1]] = list(cidrs)
        return values

    # ------------------------------------------------------------------ #
    #  同工作區服務網址注入（組裝第 7 步；常數與規則見 _WORKSPACE_URL_INJECTORS） #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _explicit_value_at_paths(values, paths):
        """``paths`` 裡第一個非空白字串值（沒有回 ''）。

        注入結果只存在於組裝出的 values、從不寫回 ``helm_values``
        （``_assemble_deploy_values`` 唯讀），設定頁也沒有這些欄位（不在
        helm_value_specs）。所以非空＝管理員、seed，或（helm_value_specs 為空清單、
        白名單退回 allow-all 的 template，例 pi-agent）租戶自己經 API 明確指定，
        平台不覆寫；空字串、純空白、``None`` 視同沒設。
        """
        for path in paths or ():
            d = values
            for key in path:
                d = d.get(key) if isinstance(d, dict) else None
            if isinstance(d, str) and d.strip():
                return d
        return ''

    @staticmethod
    def _inject_value_at_paths(values, paths, value):
        """把 ``value`` 寫進 ``values`` 的每一條 ``paths``（就地修改並回傳）。

        中間層不存在就建；是 ``None`` 或被誤設成非 dict（例 ``omnigentHost: null``）
        時換成新 dict，免得 ``setdefault`` 拋 TypeError 讓整個部署失敗。
        """
        for path in paths or ():
            d = values
            for key in path[:-1]:
                nxt = d.get(key)
                if not isinstance(nxt, dict):
                    nxt = {}
                    d[key] = nxt
                d = nxt
            d[path[-1]] = value
        return values

    @staticmethod
    def _inject_workspace_service_url(values, template_slug, provider_slug, provider_url):
        """把提供者（``provider_slug``）的叢集內網址注入消費端（``template_slug``）。

        依 ``_WORKSPACE_URL_INJECTORS`` 找出這個提供者在這個 chart 的 value path，
        網址後面接上該消費端的後綴（LiteLLM 的 ``llmGateway.baseUrl`` 帶 ``/v1``）。
        沒有這條 value path 的 chart、網址為空、或已有明確（非空）值一律 no-op。

        ⚠ 只注入網址，**絕不**注入 token／密碼。

        Args:
            values: Helm values dict（就地修改並回傳）
            template_slug: 消費端 template slug（例 'tailscale'）
            provider_slug: 提供者 template slug（'omnigent'／'headscale'／'litellm'／'nextcloud'）
            provider_url: 例 "http://svc-xxxx-headscale.paas-ws-xxxx.svc.cluster.local:8080"
                （nextcloud 是公開網址，例 "https://svc-xxxx.woowtech.io"）

        Returns:
            Modified values dict
        """
        if not provider_url:
            return values
        for path_table, provider, suffixes in _WORKSPACE_URL_INJECTORS:
            if provider != provider_slug:
                continue
            paths = path_table.get(template_slug)
            if not paths or CloudService._explicit_value_at_paths(values, paths):
                continue
            values = CloudService._inject_value_at_paths(
                values, paths, provider_url + suffixes.get(template_slug, ''),
            )
        return values

    def _inject_workspace_service_urls(self, values, template_slug):
        """組裝第 7 步：依序跑每個同工作區網址注入器（見 ``_WORKSPACE_URL_INJECTORS``）。

        每個注入器：本 chart 不是它的消費端 → 跳過；helm_values 已有明確值 → 記 info、
        不覆寫（log 不帶值：管理員可能帶了 userinfo）；否則查同工作區最早建立的 running
        提供者（``_workspace_provider_url``），有網址才注入。唯讀、不寫 DB。
        """
        self.ensure_one()
        for path_table, provider_slug, _suffixes in _WORKSPACE_URL_INJECTORS:
            paths = path_table.get(template_slug)
            if not paths:
                continue
            if self._explicit_value_at_paths(values, paths):
                _logger.info(
                    "Service id=%s keeps its explicit %s; skip same-workspace "
                    "%s URL injection", self.id, '.'.join(paths[0]),
                    _WORKSPACE_PROVIDERS[provider_slug]['label'],
                )
                continue
            provider_url = self._workspace_provider_url(provider_slug)
            if provider_url:
                values = self._inject_workspace_service_url(
                    values, template_slug, provider_slug, provider_url,
                )
        return values

    @staticmethod
    def _helm_trunc63(name):
        """Helm ``trunc 63 | trimSuffix "-"``（trimSuffix 只去掉一個 '-'）。"""
        name = (name or '')[:63]
        if name.endswith('-'):
            name = name[:-1]
        return name

    def _provider_endpoint(self, provider_slug):
        """本筆（提供者服務）的入口 Service：``{service, namespace, port, release, values}``；
        組不出來回 ``None``（已記 warning）。

        鏡射 woow-paas-charts ``charts/<chart>/templates/_helpers.tpl`` 的
        ``<chart>.fullname``（入口 Service 名）：

        - ``fullnameOverride`` 有值 → 用它；
        - 否則 name＝``nameOverride`` 或 chart 名；release 名已含 name → 用 release 名，
          不然 ``<release>-<name>``；
        - 都經 ``trunc 63 | trimSuffix "-"``。

        Port＝``_WORKSPACE_PROVIDERS[slug]['port_path']``（缺省用 chart 預設）。值都讀自
        本筆的 helm_values（template 預設＋user config，create 時已 merge），所以 seed
        若覆寫了名稱或 port，這裡會跟著變。Service 名與 namespace 都必須是 DNS label、
        port 必須是 1–65535 的整數，否則不組網址（防止奇怪的 helm_values 把任意字串拼進
        URL）。
        """
        self.ensure_one()
        provider = _WORKSPACE_PROVIDERS[provider_slug]
        label = provider['label']
        release = self.helm_release_name or ''
        namespace = self.helm_namespace or ''
        if not release or not namespace:
            return None
        try:
            values = self._helm_values_dict()
        except ValueError:
            _logger.warning(
                "%s service id=%s has unparsable helm_values; "
                "skip same-workspace %s URL injection", label, self.id, label,
            )
            return None
        if not isinstance(values, dict):
            values = {}
        fullname_override = values.get('fullnameOverride')
        if isinstance(fullname_override, str) and fullname_override:
            service_name = self._helm_trunc63(fullname_override)
        else:
            name_override = values.get('nameOverride')
            name = (
                name_override
                if isinstance(name_override, str) and name_override
                else provider['chart']
            )
            if name in release:
                service_name = self._helm_trunc63(release)
            else:
                service_name = self._helm_trunc63('%s-%s' % (release, name))
        port = provider['default_port']
        node = values
        for key in provider['port_path']:
            node = node.get(key) if isinstance(node, dict) else None
        if node is not None:
            port = node
        if isinstance(port, bool) or not isinstance(port, int) \
                or not 0 < port < 65536:
            _logger.warning(
                "%s service id=%s has invalid %s %r; skip same-workspace %s URL "
                "injection", label, self.id, '.'.join(provider['port_path']), port,
                label,
            )
            return None
        if not (_DNS_LABEL_RE.match(service_name)
                and _DNS_LABEL_RE.match(namespace)):
            _logger.warning(
                "%s service id=%s has a non-DNS-label service/namespace name; "
                "skip same-workspace %s URL injection", label, self.id, label,
            )
            return None
        return {
            'service': service_name, 'namespace': namespace, 'port': port,
            'release': release, 'values': values,
        }

    def _provider_url(self, provider_slug):
        """本筆（提供者服務）要交給同工作區消費端的網址；組不出來回 ''。

        預設是叢集內網址（``_provider_internal_url``）；``_WORKSPACE_PROVIDERS`` 標了
        ``public_url`` 的提供者（Nextcloud）改給它的公開 https 根位址
        （``_public_base_url()``，沒有 subdomain 回 ''）——與提供者自己 chart 收到的公開
        網址同一個組法，不另立網址規則。
        """
        if _WORKSPACE_PROVIDERS[provider_slug].get('public_url'):
            return self._public_base_url()
        return self._provider_internal_url(provider_slug)

    def _provider_internal_url(self, provider_slug):
        """本筆（提供者服務）在叢集內的網址；組不出來回 ''。

        網址形如 ``http://<service>.<namespace>.svc.cluster.local:<port>``（組法見
        ``_provider_endpoint``）。叢集內走 http：流量留在 pod 網路裡，但跨節點時以明文
        經 flannel／vxlan——租戶之後在消費端貼的金鑰（preauth key 只在登入時用一次、
        LiteLLM virtual key 每個請求都帶）會走這條；要避開可改用提供者的公開 https 網址
        並自行設定（已有明確值時平台不覆寫）。

        ⚠ **連不連得到由各消費端 chart 自己的 egress 規則決定，平台不保證**：租戶
        namespace 的 baseline NetworkPolicy（``paas-baseline-allow-same-ns``）只管
        **Ingress**。各提供者另有可達性檢查（對不上只記 warning、照常注入）：
        Omnigent 見 ``_omnigent_internal_url``；LiteLLM 見
        ``_warn_litellm_egress_reachability``；Headscale 不需要（tailscale chart 的
        egress 放行同 namespace 所有 pod）。
        """
        endpoint = self._provider_endpoint(provider_slug)
        if not endpoint:
            return ''
        if provider_slug == _OMNIGENT_TEMPLATE_SLUG:
            self._warn_omnigent_egress_reachability(endpoint)
        elif provider_slug == _LITELLM_TEMPLATE_SLUG:
            self._warn_litellm_egress_reachability(endpoint)
        return 'http://%s.%s.svc.cluster.local:%d' % (
            endpoint['service'], endpoint['namespace'], endpoint['port'],
        )

    def _omnigent_internal_url(self):
        """本筆（Omnigent 服務）在叢集內的網址；組不出來回 ''。

        Service 名鏡射 ``charts/omnigent`` 的 ``omnigent.fullname``、port＝``service.port``
        （chart 預設 8000），見 ``_provider_endpoint``。Omnigent 帳密在
        ``omnigent login`` 時也走這條 http；想避開可改用公開 https 網址登入，host 會照
        租戶登入的網址連。

        code-server 靠 ``networkPolicy.sameNamespaceEgress``（或 ``allowAllEgress``）、
        pi-agent 靠 ``allowAllEgress``；open-design 的 internet egress 排除 10.0.0.0/8，
        要靠 ``networkPolicy.omnigentEgress`` 開洞，而那條規則同時取決於 **port 與 pod
        selector**（``_warn_omnigent_egress_reachability``）。
        """
        return self._provider_internal_url(_OMNIGENT_TEMPLATE_SLUG)

    def _warn_omnigent_egress_reachability(self, endpoint):
        """open-design 的 ``networkPolicy.omnigentEgress`` 連不連得到這台 Omnigent。

        - port：只開 Omnigent pod 的 targetPort 8000——``service.targetPort`` 不是 8000
          時 OD 連不到；
        - selector：``app.kubernetes.io/name=omnigent``＋``component=server``，網址主機是
          ``<X>-omnigent`` 形態時再釘 ``app.kubernetes.io/instance=<X>``。Omnigent pod 的
          instance 是 release 名、name 是 ``nameOverride``（預設 ``omnigent``），所以
          release 名本來就含 ``omnigent``（例 ``my-omnigent``，Service 名照 chart 用
          release 名原樣 ⇒ OD 釘成 instance=``my``）、``fullnameOverride`` 成
          ``<X>-omnigent`` 而 X 不是 release 名、或設了 ``nameOverride`` 時，selector
          對不上 pod，OD 一樣連不到（``_omnigent_matches_od_egress_selector``）。

        兩種情形平台都照常注入（code-server／pi-agent 不受影響），只記 warning；OD 那台
        只能改用公開網址登入，或由 chart 端放寬 selector。正式站的 release 都是
        ``svc-xxxxxxxx``、沒有 override，兩條都不會觸發。
        """
        svc_values = endpoint['values'].get('service')
        target_port = (svc_values.get('targetPort')
                       if isinstance(svc_values, dict) else None)
        if target_port is not None \
                and target_port != _OMNIGENT_EGRESS_PINNED_TARGET_PORT:
            _logger.warning(
                "Omnigent service id=%s has service.targetPort %r (not %d): "
                "charts whose egress rule pins port %d (open-design "
                "networkPolicy.omnigentEgress) cannot reach the injected URL",
                self.id, target_port, _OMNIGENT_EGRESS_PINNED_TARGET_PORT,
                _OMNIGENT_EGRESS_PINNED_TARGET_PORT,
            )
        if not self._omnigent_matches_od_egress_selector(
                endpoint['release'], endpoint['service'], endpoint['values']):
            _logger.warning(
                "Omnigent service id=%s: service name / nameOverride does not "
                "match open-design networkPolicy.omnigentEgress's pod selector "
                "(app.kubernetes.io/name=%s, instance=<host minus %s>); "
                "open-design cannot reach the injected URL",
                self.id, _OMNIGENT_EGRESS_SELECTOR_NAME,
                _OMNIGENT_EGRESS_HOST_SUFFIX,
            )

    @staticmethod
    def _omnigent_matches_od_egress_selector(release, service_name, values):
        """open-design 的 omnigentEgress selector 會不會選到這個 Omnigent 的 pod。

        鏡射 charts/open-design ``open-design.omnigentEgress``（見
        ``_OMNIGENT_EGRESS_SELECTOR_NAME`` 的說明）：pod 的 name 標籤必須是
        ``omnigent``（沒有別的 ``nameOverride``）；Service 名是 ``<X>-omnigent``
        時 OD 另釘 instance=X，X 必須＝release 名（pod 的 instance 標籤）。
        """
        name_override = values.get('nameOverride') if isinstance(values, dict) else None
        if isinstance(name_override, str) and name_override \
                and name_override != _OMNIGENT_EGRESS_SELECTOR_NAME:
            return False
        if service_name.endswith(_OMNIGENT_EGRESS_HOST_SUFFIX) \
                and len(service_name) > len(_OMNIGENT_EGRESS_HOST_SUFFIX):
            pinned = service_name[:-len(_OMNIGENT_EGRESS_HOST_SUFFIX)]
            return pinned == release
        return True

    def _warn_litellm_egress_reachability(self, endpoint):
        """hermes-agent／open-design 的 ``networkPolicy.litellmEgress`` 連不連得到這台 LiteLLM。

        那條洞的 pod selector 是 ``app.kubernetes.io/name=litellm``＋``component=app``、
        只開 pod port 4000。charts/litellm 的 pod name 標籤＝``nameOverride``（預設
        ``litellm``）、containerPort＝``service.targetPort``（預設 4000）。任一對不上時
        照常注入（pi-agent／code-server 不受影響），只記 warning。正式站的 release 都
        沒有 override，不會觸發。
        """
        values = endpoint['values']
        svc_values = values.get('service')
        target_port = (svc_values.get('targetPort')
                       if isinstance(svc_values, dict) else None)
        if target_port is not None \
                and target_port != _LITELLM_EGRESS_PINNED_TARGET_PORT:
            _logger.warning(
                "LiteLLM service id=%s has service.targetPort %r (not %d): "
                "hermes-agent / open-design networkPolicy.litellmEgress (pod "
                "port %d) cannot reach the injected URL",
                self.id, target_port, _LITELLM_EGRESS_PINNED_TARGET_PORT,
                _LITELLM_EGRESS_PINNED_TARGET_PORT,
            )
        name_override = values.get('nameOverride')
        if isinstance(name_override, str) and name_override \
                and name_override != _LITELLM_EGRESS_SELECTOR_NAME:
            _logger.warning(
                "LiteLLM service id=%s has nameOverride (pod label "
                "app.kubernetes.io/name is not %s): hermes-agent / open-design "
                "networkPolicy.litellmEgress's pod selector cannot reach the "
                "injected URL", self.id, _LITELLM_EGRESS_SELECTOR_NAME,
            )

    def _workspace_provider_url(self, provider_slug):
        """同工作區 running 提供者（``provider_slug``）的網址；沒有回 ''。

        網址由 ``_provider_url`` 決定：預設叢集內網址，Nextcloud 是公開 https 網址。

        條件：同 ``workspace_id``、template slug＝``provider_slug``、``state='running'``、
        已有 release 名與 namespace；本筆有 namespace 時另要求同一 namespace（各消費端
        chart 開給提供者的 egress 都只涵蓋自己的 namespace——code-server 的
        sameNamespaceEgress、open-design 的 omnigentEgress／litellmEgress、tailscale 的
        同 namespace 規則；跨 namespace 的網址給了多半連不上）。本筆自己不算。
        公開網址的提供者（Nextcloud）沿用同一套條件（工作區與 namespace 一對一，不另立規則）；
        它的連線走公開網址（消費端 chart 的 internet egress），不靠 namespace 內的放行。

        只認 ``running``：提供者正在 upgrading／suspended／error 時這次組裝不帶網址。
        因為網址只進 runtime ConfigMap，這只會讓消費端暫時少了網址（已連上的不受影響，
        chart 端在網址消失時的行為各自定義，例 tailscale 保持原登入），下一次部署／重設
        就補回來。

        多個提供者時取**最早建立**的那個（``create_date``、``id`` 由小到大，決定性），
        並記一行 info log 列出被略過的；最早那台組不出網址（壞掉的 helm_values 等，已
        log）就照同一順序試下一個。

        唯讀、不寫 DB、不呼叫 operator（``_assemble_deploy_values`` 的契約）。

        以 sudo 查詢＝**防禦性**：注入結果只該由工作區內有哪些提供者決定，不該由觸發
        部署的是誰決定。**今天**不存在「讀得到本筆、卻讀不到同工作區提供者」的非系統
        使用者——cloud_service 的讀取 rule（``ir_rules.xml``
        ``cloud_service_rule_member_read``）對同一工作區的每一筆都一樣（成員＝全看得到），
        系統管理員有 ``cloud_service_rule_admin_full``，cron 是 superuser。sudo 守的是
        **日後**：只要哪天多一條會遮住部分服務的 record rule（例如依服務或依角色的
        可見性），未 sudo 的查詢就會讓「誰按了儲存設定」決定網址有沒有注入。
        ``test_omnigent_server_url_injection`` 以一條測試專用 rule 重現這種情形釘住它。
        讀到的只有 release 名／namespace／helm_values 裡的名稱與 port，不讀任何敏感欄位。
        """
        self.ensure_one()
        provider = _WORKSPACE_PROVIDERS.get(provider_slug)
        if not provider or not self.workspace_id:
            return ''
        domain = [
            ('workspace_id', '=', self.workspace_id.id),
            ('template_id.slug', '=', provider_slug),
            ('state', '=', 'running'),
            ('helm_release_name', '!=', False),
            ('helm_namespace', '!=', False),
        ]
        if self.id:
            domain.append(('id', '!=', self.id))
        if self.helm_namespace:
            domain.append(('helm_namespace', '=', self.helm_namespace))
        candidates = self.sudo().search(domain, order='create_date asc, id asc')
        for chosen in candidates:
            url = chosen._provider_url(provider_slug)
            if not url:
                continue
            if len(candidates) > 1:
                _logger.info(
                    "Workspace %s has %d running %s services; injecting "
                    "the oldest usable one (id=%s, release=%s) into service "
                    "id=%s, other ids=%s",
                    self.workspace_id.id, len(candidates), provider['label'],
                    chosen.id, chosen.helm_release_name, self.id,
                    (candidates - chosen).ids,
                )
            return url
        return ''

    # ---- Omnigent 的相容入口（#1340；行為不變，邏輯在上面的通用 helper） ----

    @staticmethod
    def _explicit_omnigent_server_url(values, template_slug):
        """helm_values 裡已經有的非空 ``omnigentHost.serverUrl``（沒有回 ''）。"""
        return CloudService._explicit_value_at_paths(
            values, _OMNIGENT_SERVER_URL_VALUE_PATHS.get(template_slug))

    @staticmethod
    def _inject_omnigent_server_url(values, template_slug, server_url):
        """把同工作區 Omnigent 的叢集內網址注入 ``omnigentHost.serverUrl``。

        只有網址、絕不帶 token（Omnigent 是 accounts 模式，host 的擁有者＝在終端機裡
        ``omnigent login`` 的那個帳號，平台不代登入）。無此 value path 的 chart、網址
        為空、或已有明確（非空）值一律 no-op。
        """
        return CloudService._inject_workspace_service_url(
            values, template_slug, _OMNIGENT_TEMPLATE_SLUG, server_url)

    def _workspace_omnigent_url(self):
        """同工作區 running Omnigent 的叢集內網址（只對 ``_OMNIGENT_SERVER_URL_VALUE_PATHS``
        裡的 chart；其他 chart 回 ''）。查找規則見 ``_workspace_provider_url``。"""
        self.ensure_one()
        if self.template_id.slug not in _OMNIGENT_SERVER_URL_VALUE_PATHS:
            return ''
        return self._workspace_provider_url(_OMNIGENT_TEMPLATE_SLUG)

    @api.model
    def _heal_tailscale_control_plane(self):
        """升級期 healer（data XML ``<function>``，每次 ``-u`` 都跑；冪等）。

        tailscale 範本（chart 0.1.2 起）多了 ``tailscale.controlPlane`` select，設定頁
        預設顯示「官方 Tailscale」（spec ``default: official``）。chart 0.1.2 之前開通、
        填了 ``tailscale.loginServer`` 的服務，helm_values 沒有 ``controlPlane``——chart
        端未設＝0.1.1 語意（loginServer 非空 → custom），部署行為不變；但設定頁會把它
        **顯示成「官方」**，租戶一按儲存就把 ``controlPlane=official`` 寫回去，節點改登官方
        控制伺服器（登出原本的 Headscale、變成新機器）。

        這裡把這類服務補上 ``controlPlane='custom'``（＝chart 對它們本來的解讀，部署出來的
        manifest 不變）。只動「``tailscale.loginServer`` 是非空字串、``controlPlane``
        沒設或空」的服務；loginServer 空的服務本來就是官方，不必補。helm_values 解析失敗的
        跳過（不在升級時替人修壞資料）。只寫 DB、不呼叫 operator。
        """
        services = self.sudo().search([('template_id.slug', '=', 'tailscale')])
        healed = []
        for svc in services:
            try:
                values = svc._helm_values_dict()
            except ValueError:
                continue
            node = values.get('tailscale') if isinstance(values, dict) else None
            if not isinstance(node, dict) or node.get('controlPlane'):
                continue
            login_server = node.get('loginServer')
            if not (isinstance(login_server, str) and login_server):
                continue
            node['controlPlane'] = 'custom'
            svc._apply_helm_values(values)
            healed.append(svc.id)
        if healed:
            _logger.info(
                "tailscale.controlPlane=custom backfilled for %d service(s) that "
                "set tailscale.loginServer before chart 0.1.2: ids=%s",
                len(healed), healed,
            )
        return healed

    @staticmethod
    def _generate_admin_password():
        """#357 C20：平台供給憑證的**唯一**生成點（admin password ＋ #530 gateway key）。

        ``secrets.token_urlsafe(24)`` 給 ~32 chars url-safe base64，強度足夠
        mcp-admin-core 的明文比對（auth/middleware.py 的 secrets.compare_digest），
        也**必然滿足** hermes gateway 對 api server key 的 16 字元下限（#530：低於
        下限時上游會靜默不登記 api server 平台，健康信號直接消失——無聲失效）。
        只在建立 / reset 成功的 response 回傳一次，後續 API 一律走
        ``PaasController._sanitize_helm_values`` 遮蔽。

        收斂理由：原本 controller（自己 import secrets 生一份）與 model create()
        各寫一次 ``token_urlsafe(24)``，同一條安全屬性散在兩處＝改強度時容易只改
        一邊，且「密碼唯一性」測試找不到單一標的。所有生成路徑都必須呼叫這裡
        ——#530 的 gateway key 同樣走這裡，不另開第二套隨機來源（兩套會分歧成
        兩種強度，而弱的那把不會有任何測試盯著）。
        """
        return secrets.token_urlsafe(24)

    @staticmethod
    def _validate_tenant_admin_username(username):
        """租戶自設帳號的格式檢查（規則見 ``TENANT_ADMIN_USERNAME_RE``）。"""
        if not isinstance(username, str) or not TENANT_ADMIN_USERNAME_RE.match(username):
            raise ValidationError(
                _("Username must be 1-64 characters: letters, digits, and . _ @ -")
            )

    @staticmethod
    def _validate_tenant_admin_password(password):
        """租戶自設密碼的格式檢查（規則見 ``TENANT_ADMIN_PASSWORD_RE``）。"""
        if not isinstance(password, str) or not TENANT_ADMIN_PASSWORD_RE.match(password):
            raise ValidationError(
                _("Password must be 12-128 characters, using letters, digits and "
                  "symbols only (no spaces).")
            )

    @staticmethod
    def _set_values_path(values, path, value):
        """把 ``value`` 寫進巢狀 dict 的點分式 ``path``（中間層不存在就建）。"""
        keys = path.split('.')
        node = values
        for key in keys[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                child = node[key] = {}
            node = child
        node[keys[-1]] = value

    @staticmethod
    def _get_values_path(values, path):
        """讀巢狀 dict 的點分式 ``path``；任一層缺席回 None。"""
        node = values
        for key in path.split('.'):
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node

    def _admin_username(self):
        """目前生效的 admin 帳號；模板不開放改帳號（無 admin_username_helm_path）回 None。

        讀序＝部署實際用的順序：服務自己的 helm_values（建立時已 merge template 預設、
        之後租戶改過就是新值）→ template 預設。都沒有時回 None，不自己猜 chart 預設。
        帳號不是敏感值（密碼才是），可以進 payload 給前端預填。
        """
        self.ensure_one()
        path = self.template_id.admin_username_helm_path
        if not path:
            return None
        # 本方法會被服務列表的序列化呼叫：一台服務的壞 JSON 不得拖垮整個列表，
        # 讀不到就當沒有（前端只是少一個預填值）。
        try:
            value = self._get_values_path(self._helm_values_dict(), path)
        except ValueError:
            value = None
        if not value:
            try:
                defaults = json.loads(self.template_id.helm_default_values or '{}')
            except ValueError:
                defaults = {}
            value = self._get_values_path(defaults, path)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _build_gateway_public_url(external_path, base_url):
        """把 chart 的 gateway 子路徑接到服務的公開根位址上（純字串正規化）。

        #530：hermes-agent 的 gateway（容器內 :8642）以 auth-proxy 的 ``/gateway``
        子路徑對外。租戶要把這條網址填進 MCP 的 Gateway 區，所以**由後端組**而非
        丟一個根位址讓前端或租戶自己拼——同一個坑已經在 dashboard 那區發生過
        （租戶填成服務根網址、漏掉子路徑而永遠連不上）。

        ``external_path`` 由 ``CloudAppTemplate._gateway_external_path()`` 從 seed
        讀出，**平台側不再維護 slug→路徑對照表**（見該 method 的 docstring 與 spec
        「不得新建 slug allowlist」）。本 method 因此只剩拼接與 rstrip 正規化：純
        函式、沒有能力語意——「這個 chart 有沒有 gateway」由 external_path 空不空
        表達，判準只有一處。

        Args:
            external_path: chart 對外的 gateway 子路徑（例 '/gateway'）；'' ＝沒有
            base_url: 服務自身對外根位址，例 "https://<sub>.<domain>"

        Returns:
            完整 gateway URL；任一輸入為空時回 ''。
        """
        if not external_path or not base_url:
            return ''
        return '%s%s' % (base_url.rstrip('/'), external_path)

    def _public_base_url(self):
        """服務自身的對外根位址（Cloudflare tunnel 曝光）；無 subdomain 回 ''。

        `_assemble_deploy_values` 的三個 injector 與 #530 的 gateway URL 交付都要
        這個值，收斂成一處避免 domain 組法漂移。
        """
        self.ensure_one()
        if not self.subdomain:
            return ''
        paas_domain = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io',
        )
        return 'https://%s.%s' % (self.subdomain, paas_domain)

    def _admin_page_public_url(self):
        """mcp-server 家族的管理頁網址（＝服務對外根位址）；其他模板或無 subdomain 回 ''。

        #1542：一次性憑證視窗要附「登入網址」。判準直接讀模板欄位（has_admin_gui＋
        config_json 載體＝mcp-server 家族，不建 slug 名單）：這類服務的後台只有一組密碼、
        沒有帳號，也沒有 console／dashboard，GUI 掛在根路徑 "/"。
        """
        self.ensure_one()
        template = self.template_id
        if not (template.has_admin_gui and template.admin_password_carrier == 'config_json'):
            return ''
        return self._public_base_url()

    def _chart_has_gateway(self):
        """本服務的 chart 有沒有對外 gateway ⇒ 該不該有 gateway API key。

        #530 的**鑄造／供給**閘門（`_set_admin_password_secret` 補鑄、
        `_build_mcp_sensitive_values` 送出 `auth.apiServerKey`、Reset 換發），與
        **交付**閘門 `_gateway_public_url()` 同源但條件較寬：交付另要求服務有對外
        根位址（subdomain），沒有 subdomain 的服務仍然需要 key（gateway 在叢集內
        仍在跑），只是平台沒有網址可以交給租戶。

        為什麼鑄造也要判、而不是「先鑄好、之後 chart 支援了就能用」：`has_admin_gui`
        的模板裡只有 hermes-agent 有 gateway，其餘 chart 會收到一個平台
        並不打算給它的 `auth.apiServerKey`。今天無害純粹是因為 mcp-server／
        open-design chart 沒有宣告 `auth` 區塊、也沒有 values.schema.json——是**隱性
        契約**：哪天 mcp-server 補了 `auth:`（很合理的演進），這幾顆就會無聲吃下一
        把不該給它的憑證。存量服務要補鑄有既定路徑（Reset 是唯一換發入口，本來就
        會重跑本閘門），所以「先鑄起來放」買不到任何東西。
        """
        self.ensure_one()
        return bool(self.template_id._gateway_external_path())

    def _chart_has_public_tunnel_address(self):
        """本服務的 chart 有沒有一個要回讀給租戶看的對外位址（ngrok sidecar）。

        唯一事實源是 `cloud_app_template.has_public_tunnel_address`（能力 gate 一律
        收斂成單一欄位，見該欄位註解與
        docs/decisions/patterns/capability-gate-single-source-field.md）。
        poll 的探測閘門、service payload 與前端文案都必須讀本方法／本欄位，
        **不得**任何一端自建 slug 名單。
        """
        self.ensure_one()
        return bool(self.template_id.has_public_tunnel_address)

    def _probe_public_tunnel_address(self, client):
        """問 operator 一次：ngrok agent 現在廣告的對外位址是什麼。

        回傳值的三態**刻意**與 `_release_facts` 的「只有探過才寫」對齊：

        * ``None``  ——「這一輪沒探」：商品沒這個能力，或 operator 掛了。帳本原值不動。
        * ``''``    ——「探過，現在沒有位址」：租戶關掉 ngrok、agent 還沒連上、
                      隧道斷了（sidecar 撤回）。這是**有結論**的觀測，要把帳本清掉，
                      否則租戶會一直看到一個已經連不上的舊位址（比空值更糟）。
        * 位址字串 ——「探過，就是這個」。

        operator 失敗一律降級成 ``None`` 並 log：對外位址是服務頁上的一個附帶資訊，
        讓它把整輪 poll 打斷（state 回寫、就緒判定都在同一輪）是完全不成比例的。
        """
        self.ensure_one()
        return self._probe_public_tunnel(client)[0]

    def _probe_public_tunnel(self, client):
        """#1559：同 `_probe_public_tunnel_address`，再加上「沒有位址時的原因」。

        回 ``(address, error)``：``address`` 三態同上；``error`` 只在探過而且沒有位址時才可能
        非空（已依服務建立者的語言組好），沒探時是 ``None``、有位址時是 ``''``。
        """
        self.ensure_one()
        if not self._chart_has_public_tunnel_address():
            return None, None
        try:
            payload = client.get_public_address(
                self.helm_namespace, self.helm_release_name
            )
        except Exception as e:  # noqa: BLE001 — 見 docstring：附帶資訊不得打斷 poll
            _logger.warning(
                'Could not read the advertised public address for %s: %s',
                self.name, e,
            )
            return None, None
        address = payload.get('public_url') or ''
        if address:
            return address, ''
        return '', self._public_tunnel_error_text(payload.get('error_code'))

    def _public_tunnel_error_text(self, code):
        """#1559：operator 回的原因碼 → 服務建立者語言的一句話（未知形狀一律 ''）。

        只認封閉集合（operator ``ngrok_errors``）：``NO_AUTHTOKEN``、``SERVER_URL_MISMATCH``、
        ``ERR_NGROK_<n>``。operator 的 ``error`` 文字不採用——平台自己有多語系文案，而且不把
        operator 給的任何字串直接放到租戶畫面上。
        """
        self.ensure_one()
        if not isinstance(code, str) or not code:
            return ''
        lang = self.sudo().create_uid.lang or self.env.lang
        env = self.with_context(lang=lang).env
        if code == 'NO_AUTHTOKEN':
            return env._(
                'ngrok is turned on, but no ngrok authtoken has been entered. Enter the '
                'authtoken of your own ngrok account in Configuration.')
        if code == 'SERVER_URL_MISMATCH':
            return env._(
                'ngrok is connected, but the service started before it was, so it still uses '
                'the in-cluster address. Restart the service to switch to the ngrok address.')
        if code in ('ERR_NGROK_105', 'ERR_NGROK_107'):
            return env._(
                'The ngrok authtoken is not valid (%(code)s). Copy it again from the "Your '
                'Authtoken" page of your ngrok dashboard and enter it in Configuration.',
                code=code)
        if code == 'ERR_NGROK_108':
            return env._(
                'This ngrok account already has the maximum number of agents online '
                '(ERR_NGROK_108). Stop the other ngrok agent that uses this account, or use '
                'another account.')
        if code == 'ERR_NGROK_334':
            return env._(
                'This ngrok domain is already online somewhere else (ERR_NGROK_334). A domain '
                'can be used by only one agent at a time: stop the other one or use another '
                'domain.')
        if code == 'ERR_NGROK_4018':
            return env._(
                'ngrok needs a verified account and authtoken (ERR_NGROK_4018). Verify your '
                'ngrok account and enter its authtoken in Configuration.')
        if re.fullmatch(r'ERR_NGROK_[0-9]{1,5}', code):
            return env._(
                'The ngrok agent could not connect (%(code)s, see '
                'https://ngrok.com/docs/errors/%(slug)s). Check the ngrok authtoken and '
                'domain in Configuration.',
                code=code, slug=code.lower())
        return ''

    def _gateway_public_url(self):
        """本服務的 gateway 完整網址（chart 無 gateway 或服務無 subdomain 時回 ''）。

        #530 的**交付**判準本體：controller 的 `_gateway_credentials` 用它決定要不要
        把明文憑證交給租戶，service payload 的 `has_gateway`（前端文案的 gate）也鏡射
        它。三處讀同一個 method，不各自重算——否則「畫面說會換發、回應裡卻沒有」這種
        漂移兩邊都不會報錯。
        """
        self.ensure_one()
        external_path = self.template_id._gateway_external_path()
        if not external_path:
            # 短路：絕大多數 template 沒有 gateway，先擋掉才不會為了清單裡的每一列
            # 服務都去查一次 paas_domain（_public_base_url 走 ir.config_parameter）。
            return ''
        return self._build_gateway_public_url(external_path, self._public_base_url())

    def _dashboard_public_url(self):
        """本服務 dashboard REST API 的對外網址（chart 不吃／服務無 subdomain 回 ''）。

        #672 的交付判準本體：controller 的 `_gateway_credentials` 用它決定要不要把
        這條網址交給租戶。判準兩層，與 gateway 同一種分層——
        「chart 有沒有對外 dashboard」是 template 的屬性
        （`_DASHBOARD_PUBLIC_URL_VALUE_PATHS`，同時也是注入端的事實源），
        「這台服務有沒有對外根位址」是 service 的屬性（subdomain）。兩個都成立才交得
        出東西：半條網址不是憑證，交出去只會讓租戶去 debug 一個不存在的端點。

        回傳值形態是 **root URL、不帶任何子路徑**（chart 0.1.3 移除 webui container、
        nginx `/` 直連 dashboard，`/dashboard/` 是硬 404）。這正是 #530 那個坑的鏡像：
        那次是漏了 gateway 的子路徑，這次是**多**了 dashboard 的——兩種都是一條看起來
        正常的死鏈，且都不會有任何一端報錯。正規化（`rstrip('/')`）與注入端同一行邏輯，
        所以「交付值 ≡ 注入值」是可斷言的（見
        test_dashboard_url_equals_the_url_injected_into_the_chart）。
        """
        self.ensure_one()
        if not _DASHBOARD_PUBLIC_URL_VALUE_PATHS.get(self.template_id.slug):
            # 短路：絕大多數 template 不吃這個值，先擋掉才不會為了清單裡的每一列服務
            # 都去查一次 paas_domain（_public_base_url 走 ir.config_parameter）。
            return ''
        return self._public_base_url().rstrip('/')

    def _get_secret_record(self):
        self.ensure_one()
        return self.env['woow_paas_platform.cloud_service_secret'].sudo().search(
            [('service_id', '=', self.id)],
            limit=1,
        )

    def _set_admin_password_secret(self, admin_password, gateway_api_key=None):
        """建立／更新本服務的憑證記錄（create-or-write，secret 的**唯一**寫入點）。

        #530：``gateway_api_key`` 未指定、記錄上還沒有值、**且 chart 真的有對外
        gateway**（``_chart_has_gateway``）時在這裡補鑄。收斂在這一處而不是讓每條
        呼叫路徑各自記得補——「有憑證記錄 ∧ chart 有 gateway ⟹ 兩把憑證都在」是狀態
        不變式，逐路徑 stamp 必然漏（見 pitfall
        reaped-since-single-path-stamp-gc-immunity：gate 欄位只在單一路徑 stamp，
        其他路徑到達同狀態的列就永久缺值）。Reset 顯式帶新值以強制換發（它同樣先
        判 ``_chart_has_gateway``，沒有 gateway 就傳 None 而不是硬換一把）。
        """
        self.ensure_one()
        Secret = self.env['woow_paas_platform.cloud_service_secret'].sudo()
        secret = self._get_secret_record()
        vals = {'admin_password': admin_password, 'updated_at': fields.Datetime.now()}
        if (gateway_api_key is None and not (secret and secret.gateway_api_key)
                and self._chart_has_gateway()):
            gateway_api_key = self._generate_admin_password()
        if gateway_api_key:
            vals['gateway_api_key'] = gateway_api_key
        if secret:
            secret.write(vals)
            return secret
        return Secret.create(dict(vals, service_id=self.id))

    def _build_mcp_sensitive_values(self, admin_password=None, gateway_api_key=None):
        """組出送進 chart 的敏感值（**不寫回 helm_values**，另走 operator 的分離通道）。

        #530：gateway API key 走 ``auth.apiServerKey``——chart 憑證解析優先序的第一段
        就是「顯式 values 值」，所以平台供給的 per-service key 會勝過 chart 自產。
        它有**自己的**閘門 ``_chart_has_gateway``（seed 的 gateway.externalPath），
        不與 admin_password 的 ``has_admin_gui`` 共用：has_admin_gui=True 的模板
        裡只有 hermes-agent 有 gateway，共用閘門會把這個鍵送給其餘並不吃它的
        chart（今天無害只因那些 chart 沒宣告 auth 區塊——隱性契約，見
        ``_chart_has_gateway`` 的 docstring）。兩個閘門都不是 slug allowlist。
        """
        self.ensure_one()
        if not self.template_id.has_admin_gui:
            return {}
        if admin_password is None:
            secret = self._get_secret_record()
            if not secret:
                # #357 C03（方案 A）：secret 缺席回 {}，不 raise、不亂生密碼。
                # 供給鏈接上前建立的 legacy 服務沒有 secret，redeploy 會走到這裡。
                # 回 {} 讓 helm 沿用現有 Secret：這批服務的 chart Secret 本來就是
                # config.secret.json="{}"（平台從沒供給過 admin_password）→「{} → {}」
                # 零變化；chart deployment.yaml 的 config.json 是 write-only-if-absent，
                # 既有 config.json 不動；chart 對 admin_password 無 self-generate
                # （randAlphaNum 只給 app=n8n 的 authToken）→ 不會把密碼洗成空。
                # 租戶要取得密碼走 reset 流程（唯一同時換 Secret + patch config.json 者）。
                return {}
            admin_password = secret.admin_password
            if gateway_api_key is None:
                gateway_api_key = secret.gateway_api_key
        values = {'config': {'sensitive': {'admin_password': admin_password}}}
        # #530：沒有 key 時**整個鍵不送**，而不是送空字串。chart 的第一段優先序拿到
        # 顯式空值就不會落到第二段「查既有 Secret」，等於把 chart 自產、agent 正在
        # 用的那把洗掉 → gateway 當場拒絕所有請求。存量服務走 Reset 換發。
        # `_chart_has_gateway` 是 fail-closed 的第二道：鑄造閘門已經讓沒有 gateway 的
        # chart 不會有 key，但存量列（閘門上線前鑄的）仍可能有值，供給面必須自己判，
        # 不能靠「上游應該不會給我髒資料」。
        if gateway_api_key and self._chart_has_gateway():
            values['auth'] = {'apiServerKey': gateway_api_key}
        return values

    def _rollback_preserved_value_paths(self):
        """#1433：「回復版本」時必須維持**目前值**的 helm values 路徑（平台管的憑證）。

        純 ``helm rollback`` 會把目標 revision 的 ``config.sensitive.admin_password``
        一起回復 ⇒ 租戶換掉（撤銷）的舊密碼復活、目前的密碼失效，而平台 DB 的
        ``cloud_service_secret`` 與帳號仍是新的（R8：open-design、code-server 重現）。
        回傳非空時 controller 會請 operator 改走「保留這些路徑」的回復。

        路徑與供給鏈同源（不自建 slug 名單，#357 C01 同一紀律）：

        - ``config.sensitive``：``_build_mcp_sensitive_values`` 的唯一通道，閘門
          ``template.has_admin_gui``。
        - ``auth.apiServerKey``：#530 gateway key，閘門 ``_chart_has_gateway``。
        - ``template.admin_username_helm_path``：租戶自設的帳號——帳號與密碼是同一組
          登入憑證，只保留密碼會變成「舊帳號＋新密碼」。

        woow-odoo 的 ``conf.adminPassword`` 刻意**不**列入：chart 只在首次建庫時套用，
        對既有 DB 是 no-op（見 ``_build_odoo_sensitive_values``），回復它不會讓舊密碼
        生效；列入反而會把 Odoo 的回復全部改成重新渲染，擴大這張單的影響面。

        #1526：``helm_value_specs`` 裡 ``type: password`` 的可調設定（OpenRouter／
        Anthropic 金鑰、Git token、ngrok／Tailscale auth key、n8n SMTP 密碼…）也列入，
        與管理員帳密同一個語意：**憑證不跟著回復**。否則租戶因外洩而撤換（或 #1455
        清除）的金鑰，回復到換金鑰之前的 revision 就會復活；而平台不回寫 DB 的
        ``helm_values``，下一次「修改配置」又悄悄換回新金鑰、服務再重啟一次（sbx-b
        #16 實測 open-design）。保留後 DB 與叢集的金鑰始終一致。這會讓「只有 password
        knob、沒有 admin GUI」的模板（n8n）也改走保留憑證的回復。
        """
        self.ensure_one()
        template = self.template_id
        paths = []
        if template.has_admin_gui:
            paths.append('config.sensitive')
            if self._chart_has_gateway():
                paths.append('auth.apiServerKey')
            if template.admin_username_helm_path:
                paths.append(template.admin_username_helm_path)
        for key in self._rollback_password_spec_paths():
            if key not in paths:
                paths.append(key)
        return paths

    def _rollback_password_spec_paths(self):
        """#1526：範本 ``helm_value_specs`` 裡 ``type: password`` 的 helm 路徑（不含 ``_init.*``）。

        specs 讀不懂 ⇒ 無從得知哪些鍵是憑證 ⇒ ``UserError``（fail-closed：寧可這次回復
        做不了，也不要讓撤換過的金鑰復活；與 #535 遮罩層同一判準）。
        """
        self.ensure_one()
        specs = self.template_id._parse_helm_value_specs_or_fail()
        if specs is SPECS_UNPARSEABLE:
            raise UserError(_(
                'The settings definition of this application cannot be read, so the '
                'rollback cannot keep its API keys safe. Please contact support.'))
        if not isinstance(specs, dict):
            return []
        keys = []
        for group in ('required', 'optional'):
            group_specs = specs.get(group) or []
            if not isinstance(group_specs, list):
                continue
            for spec in group_specs:
                if not isinstance(spec, dict) or spec.get('type') != 'password':
                    continue
                key = spec.get('key')
                if isinstance(key, str) and key and not key.startswith('_init.'):
                    keys.append(key)
        return keys

    def _build_odoo_sensitive_values(self):
        """#1130：woow-odoo 的初始管理員密碼 sensitive value（odoo 專屬 helper）。

        ⛔ 刻意**不經** ``has_admin_gui``／``admin_password_carrier``／
        ``_build_mcp_sensitive_values``：那條送 ``config.sensitive.admin_password``，
        woow-odoo tenant schema（``additionalProperties: false``）直接擋（#1141 ⛔ 條款）。
        也不塞進 ``_assemble_deploy_values``——那是唯讀、不含敏感值的稽核真相源。
        ``_execute_deploy``／``_redeploy``／reconfigure 三個呼叫點各自平行呼叫（#1160）。

        每次都送全量（operator 只 merge 這次呼叫傳進來的 sensitive，``reset_values``
        之下不送就變空值）。chart 只在首次 ``-i base`` 套用，既有 DB 重送同值是 no-op
        （#1174 c14185）。secret 缺席（不該發生：create 時一定鑄造）回 ``{}``——首裝時
        chart bootstrap 會以 exit 4 fail-loud，而不是平台在這裡亂生一把沒人知道的密碼。
        """
        self.ensure_one()
        if not self.template_id._is_woow_odoo_chart():
            return {}
        secret = self._get_secret_record()
        if not (secret and secret.admin_password):
            return {}
        section, key = WOOW_ODOO_ADMIN_PASSWORD_PATH
        return {section: {key: secret.admin_password}}

    def _build_deploy_sensitive_values(self):
        """三個部署呼叫點共用：MCP 家族與 woow-odoo 的 sensitive values 疊在一起。

        兩者的閘門互斥（``has_admin_gui`` 與 woow-odoo chart），鍵也不重疊
        （``config``／``auth`` 對 ``conf``），淺合併即可。
        """
        self.ensure_one()
        values = dict(self._build_mcp_sensitive_values())
        values.update(self._build_odoo_sensitive_values())
        return values

    @staticmethod
    def _strip_mcp_sensitive_values(values):
        """剝掉不得落庫（helm_values）的敏感子樹。

        ⚠ 這層是 **path-aware** 的：新增一條敏感值通道時**必須**在這裡補對應路徑，
        否則那個值會留在 ``helm_values`` 並經服務詳情 API 明文流出，而只掃既有路徑
        的測試不會紅（#530 就是這個形狀：``auth.apiServerKey`` 不在 config.sensitive
        底下）。與 ``PaasController._sanitize_helm_values`` 的遮罩層是兩道獨立防線，
        新增路徑要**兩邊都補**。
        """
        if not values or not isinstance(values, dict):
            return values
        result = json.loads(json.dumps(values))
        config = result.get('config')
        if isinstance(config, dict):
            config.pop('sensitive', None)
        # #530 gateway API key。只 pop 這一個鍵、不動整個 auth 子樹——同層還有
        # 非憑證欄位（PostgreSQL 家族 seed 的 auth.username／auth.database）。
        auth = result.get('auth')
        if isinstance(auth, dict):
            auth.pop('apiServerKey', None)
        # #1130 woow-odoo 初始管理員密碼。同樣只 pop 這一格：同層 `conf.*` 是 typed
        # odoo.conf 鍵（adminEmail、workers…），不是憑證。
        section, key = WOOW_ODOO_ADMIN_PASSWORD_PATH
        conf = result.get(section)
        if isinstance(conf, dict):
            conf.pop(key, None)
        return result

    # ------------------------------------------------------------------
    # #559：helm_values 的欄位級授權（groups='base.group_system'）之後，
    # 平台程式碼存取它的**唯二**入口。
    #
    # 為什麼欄位要有 groups=：`helm_values` 裝的是租戶親手填的憑證
    # （odooPassword／smtpPassword／auth.*ApiKey）。#535 把服務詳情 API 的遮罩
    # 改成 spec-driven，但那道遮罩在 **controller 層**；欄位本身沒有 groups= 時，
    # 租戶可以直接打 `POST /web/dataset/call_kw` 送 `read(['helm_values'])`
    # **繞過整個 controller 遮罩層**拿到明文。實測坐實（#559）：最低權限
    # （workspace role=guest）的 `base.group_user` 租戶讀得到完整明文，而同一條
    # 路徑上讀 `n8n_owner_password`（有 groups=）會被 AccessError 擋下
    # ——鑑別項證明了「有 groups= 就會被擋」在這條路徑上確實生效。
    #
    # 為什麼要這兩個 helper：groups= 擋的不只是 RPC，**連平台自己的 ORM 屬性存取
    # 也一併擋掉**（實測：`_format_service` 的 `service.helm_values` 直接拋
    # AccessError，租戶的服務詳情頁與 reconfigure 全掛）。租戶**應該**看得到自己
    # 的設定——只是要走 BFF、經 `_sanitize_helm_values` 遮罩後才出去。所以合法路徑
    # 需要提權，而提權集中在這裡、不散落成一堆 `.sudo()`。
    #
    # ⚠ 呼叫端的義務：這兩個 helper **只提升欄位級權限，不做記錄級授權**。
    # 呼叫前必須已經確認呼叫者有權碰這筆 service（controller 端是
    # `workspace.check_user_access()` ＋ ir.rule 在 browse 後的第一次讀）。
    # 不要在「還沒授權的 recordset」上呼叫它們——那才是
    # docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md 講的那種洞。
    # ------------------------------------------------------------------

    def _refresh_template_images(self):
        """#1408：把範本目前釘的 image 參照寫進服務的 helm_values；回傳變更清單。

        只在管理員 Redeploy 呼叫——租戶儲存設定與重設密碼（15 秒 atomic）不換
        image，避免 pod template 變動。範本 JSON 壞掉時不動（create 時就擋過）。
        """
        self.ensure_one()
        try:
            template_values = json.loads(self.template_id.helm_default_values or '{}')
        except ValueError:
            _logger.warning(
                "Service id=%s: template %s has invalid helm_default_values; "
                "images not refreshed", self.id, self.template_id.id)
            return []
        new_values, changes = _refresh_image_refs(
            self._helm_values_dict(), template_values,
            self.template_id._tenant_value_keys(),
        )
        if changes:
            self._apply_helm_values(new_values)
        return changes

    def _redeploy_chart_check_message(self, resolved_version):
        """Redeploy 後 chart 版本與範本不符時的提示（相符或無從得知回 False）。"""
        self.ensure_one()
        expected = self.template_id.helm_chart_version
        if resolved_version and expected and resolved_version != expected:
            return _(
                'Redeploy finished, but the release runs chart %(actual)s while the '
                'product pins %(expected)s.',
                actual=resolved_version, expected=expected)
        return False

    def _verify_redeploy_images(self, pods):
        """#1408：服務就緒時核對 Redeploy 後 workload 的 image；結果寫進 message。

        舊 operator 的 PodInfo 沒有 ``images``（None）⇒ 無從核對，只清 pending。
        收尾中的 Pod（deletionTimestamp）不算。
        """
        self.ensure_one()
        live = [p for p in pods or [] if not p.get('deletion_timestamp')]
        if not live or any(p.get('images') is None for p in live):
            self.sudo().write({'redeploy_verify_pending': False})
            return
        images = [image for p in live for image in p.get('images') or []]
        try:
            values = self._helm_values_dict()
        except ValueError:
            values = {}
        mismatches = _image_mismatches(values, images)
        vals = {'redeploy_verify_pending': False}
        if mismatches:
            detail = '; '.join(
                '%s (running: %s)' % (expected, ', '.join(shown))
                for expected, shown in mismatches)
            message = _(
                'After the redeploy the workload is not running the expected '
                'image: %(detail)s', detail=detail)
            previous = self.sudo().redeploy_check_message
            vals['redeploy_check_message'] = (
                '%s\n%s' % (previous, message) if previous else message)
            _logger.warning(
                "Service id=%s redeploy image check failed: %s", self.id, mismatches)
        self.sudo().write(vals)

    def _helm_values_dict(self):
        """讀出 helm_values 並解析成 dict（空值／未設定回 ``{}``）。

        解析失敗時**照原樣拋出**，與改動前各呼叫點的行為一致（那些點原本就是
        裸 ``json.loads``）——不在這裡吞掉，免得壞掉的 JSON 變成靜默的空設定。
        """
        self.ensure_one()
        raw = self.sudo().helm_values
        return json.loads(raw) if raw else {}

    def _apply_helm_values(self, values, extra_vals=None):
        """寫回 helm_values（會套用與 ``create()`` 相同的敏感值剝除）。

        ``extra_vals`` 走 ``self.write``、不提權；提權只落在 ``helm_values``。
        ir.rule／欄位 groups 只在呼叫者不是 superuser 時對 ``extra_vals``
        生效。呼叫者是 superuser（``env.su``）時，Odoo 本來就不套 ir.rule；
        本函式不會、也不打算改變這一點。

        已知帶 ``extra_vals`` 的生產呼叫點與身分（#699）：

        - ``src/controllers/paas.py:3730`` 的 ``_update_service``：由
          ``api_workspace_service`` 的 ``auth="user"`` 入口（同檔 :1551）、
          ``action='update'`` 的 owner/admin role gate（:1589-1590），以未
          ``sudo`` 的 ``service``（:1582）呼叫，故規則生效；守線是
          ``src/tests/test_helm_values_field_acl_559.py:189`` 的
          ``test_extra_vals_are_written_with_the_callers_identity``（#559）。
        - n8n init 成功的套用（#1257 起在 ``_init_apply_outcome``）：由
          ``_cron_check_deploying_services`` 經 ``_poll_and_update_status`` →
          ``_run_post_deploy_init`` 進入；cron record
          ``src/data/cloud_service_cron.xml:15-23`` 未設 ``user_id``，且 #699
          實測其身分為 ``SUPERUSER_ID``，故 ir.rule 不生效；租戶不能取得該
          cron 身分，因此不是攻擊面。
        - ``src/controllers/paas.py`` 的 ``api_service_change_plan``（#1158）：
          ``auth="user"``、owner/admin role gate 之後，以 ``_PaaSCapability`` 取得的
          ``service`` 呼叫（與 ``_update_service`` 同一個來源與身分）；寫的是
          ``allocated_*``／state／``last_upgraded_at``／``error_message``，與
          reconfigure 寫的欄位同類。``helm_chart_version`` 自 #1525 起不經這裡（租戶
          身分寫不進去），兩條路都改走 ``_record_chart_version``。

        新增帶 ``extra_vals`` 的呼叫點時，須回來補上這份身分清單（#699）。
        """
        self.ensure_one()
        if extra_vals:
            self.write(extra_vals)
        stripped = self._strip_mcp_sensitive_values(values)
        self.sudo().write({'helm_values': json.dumps(stripped)})

    def _record_chart_version(self, version):
        """以平台身分回寫 chart 版本帳本（#1525）。

        ``helm_chart_version`` 只有平台身分寫得進去（``_reject_chart_version_writes``），
        而 reconfigure／換方案／回復／改帳密這幾條路的 ``self`` 是租戶身分。呼叫端
        已經做完授權（role gate、訂閱閘），值是平台自己決定的（服務目前版本或範本
        pin）或 operator 回報的事實，不是租戶輸入。空值不寫（未知不得覆蓋成空）。
        """
        self.ensure_one()
        if version and version != self.helm_chart_version:
            self.sudo().write({'helm_chart_version': version})

    def _set_deploy_params(self, params):
        """以平台身分寫入 ``deploy_params``（#1317）。

        ``deploy_params`` 帶 ``groups='base.group_system'``，而 ``/woow`` 開通流程
        （``controllers/paas.py`` 的 ``_create_service``）拿的是**租戶身分**的
        recordset：Portal 會員經 ``_PaaSCapability`` 拿到 sudo recordset 所以沒事，
        但只掛 ``base.group_user`` 的 workspace owner／admin 寫這個欄位必定
        ``AccessError``，整筆開通 rollback，畫面只剩「建立服務時發生錯誤」
        （2026-10-02 正式站 uid 41 實際撞到）。

        提權是對的層次，因為值**不是租戶給的**：``expose_config`` 是伺服器算出的
        subdomain，``mcp_api_key`` 是伺服器鑄造的 uuid。比照 #739 的原則，
        ``.sudo()`` 只包這一次寫入；呼叫端的 role／訂閱／workspace 閘全部留在
        sudo 之外，本函式也不讀回任何值給呼叫端。
        """
        self.ensure_one()
        self.sudo().write({'deploy_params': json.dumps(params)})

    def _has_mcp_admin_secret(self):
        self.ensure_one()
        return bool(self.template_id.has_admin_gui and self._get_secret_record())

    def _build_mcp_sidecar_config(
        self, template, auth_token, api_key=None, helm_release_name=None,
    ):
        """Build MCP sidecar container config for PaaS Operator patch endpoint."""
        sidecar_port = template.mcp_sidecar_port or 3000

        env_vars = [
            {'name': 'MCP_MODE', 'value': 'http'},
            {'name': 'AUTH_TOKEN', 'value': auth_token},
            {'name': 'N8N_API_URL', 'value': f'http://localhost:{template.default_port}'},
            {'name': 'PORT', 'value': str(sidecar_port)},
            {'name': 'AUTH_RATE_LIMIT_MAX', 'value': '500'},
            {'name': 'AUTH_RATE_LIMIT_WINDOW', 'value': '900000'},
        ]

        if template.mcp_api_key_helm_path:
            env_name = template.mcp_api_key_helm_path.rsplit('.', 1)[-1]
            if helm_release_name:
                secret_name = f'{helm_release_name}-n8n-app-secret'
                env_vars.append({
                    'name': env_name,
                    'valueFrom': {
                        'secretKeyRef': {
                            'name': secret_name,
                            'key': env_name,
                        }
                    }
                })
            elif api_key:
                env_vars.append({'name': env_name, 'value': api_key})

        if template.mcp_sidecar_env:
            try:
                custom_env = json.loads(template.mcp_sidecar_env)
                reserved_keys = {'MCP_MODE', 'AUTH_TOKEN', 'PORT', 'N8N_API_URL'}
                if template.mcp_api_key_helm_path:
                    reserved_keys.add(template.mcp_api_key_helm_path.rsplit('.', 1)[-1])
                for key, value in custom_env.items():
                    if key not in reserved_keys:
                        env_vars.append({'name': key, 'value': str(value)})
            except (json.JSONDecodeError, TypeError):
                _logger.warning("Invalid MCP sidecar env JSON in template %s", template.name)

        return {
            'container': {
                'name': 'mcp-sidecar',
                'image': template.mcp_sidecar_image,
                'ports': [{'containerPort': sidecar_port}],
                'env': env_vars,
                'resources': {
                    'requests': {'memory': '128Mi', 'cpu': '100m'},
                    'limits': {'memory': '256Mi', 'cpu': '200m'},
                },
                'livenessProbe': {
                    'httpGet': {'path': '/health', 'port': sidecar_port},
                    'initialDelaySeconds': 15,
                    'periodSeconds': 30,
                },
                'readinessProbe': {
                    'httpGet': {'path': '/health', 'port': sidecar_port},
                    'initialDelaySeconds': 10,
                    'periodSeconds': 10,
                },
            },
        }

    # ------------------------------------------------------------------ #
    #  Cron: check deploying / upgrading / initializing services         #
    # ------------------------------------------------------------------ #

    @api.model
    def _cron_check_deploying_services(self):
        """背景短 poll：轉移狀態（含 deleting）的服務各向 operator 取一次 status。

        #1159 Q1：list/get 純讀之後，「由觀測推進、離開轉移狀態」只由這裡寫——
        transition 完成、init 分派（排進 init cron 的佇列）、deleting 404→unlink。
        它**不執行 init**：init 在獨立的 ``_cron_run_post_deploy_inits``（Q4），一筆慢
        init 不會堵住這裡的 upgrading→running。

        Q36：每台服務一個回捲單位（commit／rollback），一台失敗不影響同一 tick 的其他台。
        """
        services = self.search([
            ('state', 'in', ['deploying', 'upgrading', 'initializing', 'deleting']),
        ])
        client = None
        if services:
            from ..services.paas_operator import get_paas_operator_client

            client = get_paas_operator_client(self.env)

        for service in (services if client else self.browse()):
            service_label = (service.name, service.id)
            try:
                service._poll_and_update_status(client)
                self.env.cr.commit()
            except Exception:
                # 回捲掉的只有「這一台還沒 commit 的 poll 寫入」：poll 本身不對叢集下
                # 變更，下一輪會重算；deleting 的 teardown 有自己的 savepoint，服務留在
                # deleting、下一輪再試。不 rollback 的話交易已 aborted 時同一 tick 後面
                # 的服務全部 InFailedSqlTransaction。
                # ⚠ #1052 標記（非裁定）：這是逐筆 batch isolation，OperationalError 一併
                # 收掉，與 post-commit-bookkeeping-rollback-unit.md 規則一分歧（同
                # `_cron_reconcile_release_facts` 的站點）。
                self.env.cr.rollback()
                _logger.exception(
                    "Error checking status for service %s (id=%s)", *service_label)
        # Q38：init cron 的狀態＋全域監控項（與 init cron 互查 lastcall）。沒有轉移中的
        # 服務、或 operator 沒設定時也要跑——那正是監控要看得到的情形。
        self.env['woow_paas_platform.platform_monitor']._run_poll_cron_checks()

    # ------------------------------------------------------------------ #
    #  Cron：post-deploy init（#1159 Q4／Q33）                            #
    # ------------------------------------------------------------------ #

    @api.model
    def _init_cron_queue(self):
        """init cron 的候選，依 Q33 排序：n8n 優先（Q28a），其次等待最久者。

        n8n 優先不變；同類依 ``init_queue_turn`` 輪替。首次 ready 以
        ``init_ready_since`` 設第一輪位置；每次成功 claim 原子移到同類尾端，避免最早
        ready 的服務永久佔隊首。``init_ready_since`` 本身不改，仍是 n8n owner 監控錨點。
        """
        self.flush_model()
        template_table = self.env['woow_paas_platform.cloud_app_template']._table
        self.env.cr.execute(
            f"""SELECT s.id
                  FROM {self._table} s
                  JOIN {template_table} t ON t.id = s.template_id
                 WHERE s.state = 'initializing'
                   AND s.init_pending
                   AND s.init_ready_observed
                   AND NOT COALESCE(s.init_redeploy_requested, FALSE)
                   AND t.post_deploy_init_type IN %s
                   AND (s.init_lease_until IS NULL
                        OR s.init_lease_until <= (statement_timestamp() AT TIME ZONE 'UTC'))
                 ORDER BY (t.post_deploy_init_type = 'n8n') DESC,
                          COALESCE(s.init_queue_turn, s.init_ready_since) ASC NULLS LAST,
                          s.id""",
            (tuple(INIT_LEASE_SECONDS),),
        )
        return self.browse([row[0] for row in self.env.cr.fetchall()])

    @api.model
    def _cron_run_post_deploy_inits(self):
        """post-deploy init 的背景執行者（#1159 Q4／Q33）；獨立 ir.cron，與短 poll 不同
        job、不同交易。

        - 開頭先執行登記過的 redeploy（Q27；#1257 f：本 cron 掌管 lease 的生命週期）。
        - 依 ``_init_cron_queue`` 的順序逐台呼叫單一入口 ``_run_post_deploy_init``
          （入口自己 per-service commit）。
        - 每 tick wall 預算 B（``INIT_CRON_BUDGET_SECONDS``）：已用時間＋該類最壞 attempt
          超過 B 就停；另有軟上限 ``INIT_CRON_SOFT_CAP``。
        - 結束時回報 ``remaining=0``，有積壓就自我 ``_trigger()``。**不要**回報
          ``remaining>0``：core 會在同一趟 pass、同一個 watchdog 內連跑最多
          ``MAX_BATCH_PER_CRON_JOB`` 批、中間不 ping，最壞必被 SIGKILL；自我 trigger 則
          開新的一趟、watchdog 重新計時。
        """
        started = _init_cron_clock()
        self._cron_run_requested_redeploys()
        # D2-R：先收 operator not_executed 時計；無 client／未 Ready 也不能逃過到期。
        self._init_settle_operator_wait_timeouts()
        self.env.cr.commit()
        queue = self._init_cron_queue()
        sent = 0
        backlog = False
        if queue:
            from ..services.paas_operator import get_paas_operator_client

            client = get_paas_operator_client(self.env)
            if not client:
                _logger.warning(
                    "[#1159] init cron: operator client 未設定，%d 台服務等待 init",
                    len(queue))
                queue = self.browse()
            for service in queue:
                if sent >= INIT_CRON_SOFT_CAP:
                    backlog = True
                    break
                worst = INIT_ATTEMPT_WORST_SECONDS.get(service._init_type())
                if worst is None:
                    # 佇列是 tick 開頭的 snapshot；之後 template 改成不需 init 的那台，入口本來
                    # 就會略過（`_run_post_deploy_init` 對 none 回 False），這裡不能 KeyError 中斷整趟。
                    continue
                if _init_cron_clock() - started + worst > INIT_CRON_BUDGET_SECONDS:
                    backlog = True
                    break
                service_label = (service.name, service.id)
                try:
                    if service._run_post_deploy_init(client):
                        sent += 1
                except Exception:
                    # 入口的每一段都各自 commit／rollback；走到這裡的只剩入口之外的意外。
                    # Q36：回捲這一台、繼續後排；已 commit 的 claim／attempt 與其他服務不回捲。
                    # ⚠ #1052 標記（非裁定）：OperationalError 也在此收掉，與
                    # post-commit-bookkeeping-rollback-unit.md 規則一分歧（同短 poll 的站點）。
                    self.env.cr.rollback()
                    _logger.exception(
                        "[#1159] init cron: service %s (id=%s) failed", *service_label)
        self.env['ir.cron']._notify_progress(done=sent, remaining=0)
        if backlog:
            self.env.ref(_INIT_CRON_XMLID)._trigger()
        # Q38：反過來檢查短 poll cron 的 lastcall。
        self.env['woow_paas_platform.platform_monitor']._run_init_cron_checks()

    def _cron_reconcile_release_facts(self):
        """#627 (i)：抓「非由 Odoo 發起」的 chart 變動——**唯一會看 running 的路徑**。

        ## 為什麼需要獨立一條

        #552 層① 的第五條回寫路徑是 poll，設計目的就是抓「有人直接對叢集下
        `helm upgrade`、Odoo 完全不知道」。但 poll 的兩個入口與
        ``_cron_check_deploying_services`` 的 domain **都只收轉移狀態**
        （deploying／upgrading／initializing／deleting）——而那種變動發生時服務正是
        ``running`` ⇒ **要抓的場景與能執行的場景不相交**（#627 缺陷 (i)，stg
        `svc-bd9d6ed3` 漂移 3 天無人發現）。

        ## 與 ``_poll_and_update_status`` 的根本差異：**絕不碰 state**

        那個函式會依 release status 推狀態機（deployed→running、failed→error…）。
        對 ``running`` 服務跑它，等於讓一條對帳工作去改生命週期狀態——叢集暫時不健康
        就會把租戶的服務打成 ``error``，而 error 沒有任何路徑會自己 poll 回來。
        ⇒ **本 cron 只做「讀 → 比對 → 回寫 release 事實」**，一行 state 都不寫。
        repo-lint 的 ``poll-release-facts-single-source`` 判準 5 機械強制這件事。

        ## 頻率：每日一次，先不要調快

        漂移的後果是**帳面錯誤不是服務中斷** ⇒ 不需要分鐘級偵測。要不要縮短請看下面
        那條 log 累積出來的資料，**不要用直覺**——目前對「漂移多常發生」的全部認識
        只有 stg 一筆軼事。

        ## Log：**改 0 筆也要印**，而且要印出掃描範圍與是哪幾筆

        只在有改動時才記，會讓「一直沒漂移」與「cron 根本沒跑」的日誌畫面完全相同；
        只印筆數不印範圍，則會讓「suspended 沒漂移」與「suspended 根本沒被看」相同。
        兩者是同一個家族，差別只在它升到了範圍那一層。

        ## ⚠ 代價：`write_date` 從此每天變動

        本 cron 每天都會 stamp ``last_release_sync_at``（即使沒有漂移），而那是一次
        真正的 ORM write ⇒ ``write_date`` 每天都會動。**在此之前 `write_date` 可以
        用來判斷「這筆帳本多久沒被平台碰過」，#552／#627 的調查用過它兩次**（判定
        某次 helm upgrade 不是 Odoo 發起的、以及證明升版沒有順手回寫）。那個診斷
        能力從本版起消失——要判斷「平台最後一次主動操作」請改看 ``last_upgraded_at``
        ／``deployed_at``。這個取捨是刻意接受的（#627 留言有記）。
        """
        from ..services.paas_operator import get_paas_operator_client

        services = self.search([('state', '=', 'running')])

        # 掃描範圍要能被讀出來：只印「掃描 N 筆」會讓「範圍外的沒漂移」與「範圍外的
        # 根本沒被看」畫面相同。逐狀態點名，不是只給總數。
        out_of_scope = {}
        for svc in self.search([('state', '!=', 'running')]):
            out_of_scope[svc.state] = out_of_scope.get(svc.state, 0) + 1
        scope_note = (
            '另有 %s 不在本 cron 範圍' % ' / '.join(
                '%s %d' % (state, count)
                for state, count in sorted(out_of_scope.items()))
            if out_of_scope else '無其他狀態的服務')

        client = get_paas_operator_client(self.env)
        if not client:
            # 沒有 client 時**大聲說**：靜默 return 會讓「operator 沒設定」與
            # 「掃過了都沒漂移」的日誌畫面相同，正是本 cron 要避免的形態。
            _logger.warning(
                "reconcile-release-facts: operator client 未設定，本輪未執行"
                "（掃描範圍本應為 %d 筆 running，%s）", len(services), scope_note)
            return

        changed, failed = [], []
        for service in services:
            try:
                # 逐筆 savepoint：一筆失敗不得中斷整批。用 savepoint 而非裸 try 是
                # 因為 OperationalError 之後 cursor 已不可用，後續會連帶全部失敗。
                # ⚠ #1052 標記（非裁定）：本站點是逐筆 batch isolation，不是 post-
                # commit-bookkeeping-rollback-unit.md 講的「外部已生效後的記帳」；
                # 下方 `except Exception` 把 OperationalError 一併吞進 failed 清單，
                # 與該 doc 規則一（OperationalError 必須外拋）分歧。規則是否適用於
                # 非 post-effect 站點，待裁定；裁定前別照抄本站形狀。
                with self.env.cr.savepoint():
                    status = client.get_status(
                        namespace=service.helm_namespace,
                        release_name=service.helm_release_name,
                        include_vm=service._needs_vm_status(),
                    )
                    # 對外位址也在這裡對帳：本 cron 是**唯一會看 running 的路徑**，
                    # 而 ngrok 免費方案每次 agent 重連就換 TCP 位址——那種變動發生時
                    # 服務正是 running（poll 的 domain 只收轉移狀態）。
                    # 無此能力的商品 `_probe_public_tunnel_address` 直接回 None、不打
                    # operator，所以這行對其餘商品零成本。
                    tunnel_address, tunnel_error = service._probe_public_tunnel(client)
                    facts = service._release_facts(
                        status.get('release', {}),
                        tunnel_address=tunnel_address,
                        tunnel_error=tunnel_error,
                    )
                    # 舊值必須**在寫入之前**收起來：寫完之後 `service[field]` already
                    # 是新值，log 就只印得出「現在是什麼」而不是「從什麼變成什麼」，
                    # 而後者才是判斷漂移頻率與幅度的那份母體資料。
                    drift = {
                        field: (service[field], value)
                        for field, value in facts.items()
                        if field != 'last_release_sync_at'
                        and service[field] != value
                    }
                    # 有沒有漂移都寫同一個 dict：`drift` 只用於 log。
                    #
                    # 為什麼不在無漂移時只寫 `last_release_sync_at` 那一格：那會在這裡
                    # 出現一個 release 事實欄位的**字面鍵**，也就是 #627 的成因形態
                    # （逐路徑列欄位），repo-lint 判準 2 會擋。而且沒有好處——版本欄位
                    # 寫進相同的值本來就沒有語意變化，`write_date` 反正每天都會動
                    # （見本方法 docstring 的代價段）。
                    service.write(facts)
                    if drift:
                        changed.append((service, drift))
            except Exception as exc:
                failed.append((service, exc))

        _logger.info(
            "reconcile-release-facts: 掃描 %d 筆 running（%s），%d 筆漂移已修正"
            "%s", len(services), scope_note, len(changed),
            ("，%d 筆讀取失敗" % len(failed)) if failed else "")
        for service, drift in changed:
            _logger.info(
                "  id=%s %s  %s", service.id, service.name,
                '  '.join(
                    '%s %s→%s' % (field, old or '(空)', new or '(空)')
                    for field, (old, new) in sorted(drift.items())))
        for service, exc in failed:
            _logger.warning(
                "  id=%s %s 讀取失敗：%s（本筆略過，不影響其餘）",
                service.id, service.name, exc)

    # ------------------------------------------------------------------ #
    #  就緒判定：兩層共用的單一事實源（#423 W5a）                          #
    # ------------------------------------------------------------------ #

    def _needs_vm_status(self):
        """本服務的就緒判定是否需要 operator 回傳 VM 資訊。

        呼叫 operator ``get_status`` 時必須據此帶 ``include_vm``。**漏帶會讓
        VM 服務永遠停在 ``deploying``**：operator 在未查詢時回 ``vmis: None``，
        而下方 :meth:`_is_release_ready` 對 VM 是 fail-closed。
        """
        self.ensure_one()
        return self.template_id.workload_type == 'vm'

    def _is_halted(self):
        """這台 VM 是否應該處於停機（``runStrategy: Halted``）狀態。

        🔴 **不可以只看 ``state == 'suspended'``。** reconfigure 路徑
        （``controllers/paas.py``）**先**把 state 寫成 ``upgrading``、19 行後
        才組裝 values，屆時守衛讀到的已經不是 ``suspended`` → 組出 ``Always``
        → 以 ``reset_values=True`` 送出 → **停權中（欠費）的 VM 被喚醒**。
        （對照組：``action_redeploy`` 與 ``_reset_admin_password`` 都是先組裝
        再寫 state，只有 reconfigure 順序相反。）

        改讀 ``pre_suspend_state`` 作為不受呼叫順序影響的單一事實源：它只在
        :meth:`_do_suspend` 設、:meth:`_do_resume` 清，語意穩定，且在
        「state 已被中途改寫但服務仍處於停權」的窗口內仍為真。

        兩者取聯集而非擇一：``state == 'suspended'`` 涵蓋 pre_suspend_state
        因故為空的情形，``pre_suspend_state`` 涵蓋 state 被中途改寫的情形。
        """
        self.ensure_one()
        return self.state == 'suspended' or bool(self.pre_suspend_state)

    def _rescale_halted_after_upgrade(self, client):
        """upgrade 之後把仍在停權中的 workload 重新收回停機（#482）。

        三條 upgrade 路徑（``action_redeploy`` / controller ``_update_service``
        / ``_reset_admin_password``）都以 ``reset_values=True`` 重新渲染 chart，
        而平台組出的 values **完全不帶 replicas** —— 容器型的 replicas 鍵名因
        chart 而異（bitnami 是 ``primary.replicaCount``、n8n 又是另一套），平台
        對任意第三方 chart 無法猜鍵名。VM 型能在 values 注入 ``vm.runStrategy``
        只是因為 haos 是自家 chart、鍵固定。

        於是 chart 預設的 ``replicas: 1`` 會把停權（欠費）中的容器直接喚醒：
        2026-07-31 docker-desktop 實測（真 operator + helm 3.13.3 + 真 chart），
        n8n 的 Deployment 與 postgresql 的 StatefulSet 都 0→1，且 pod 實測
        Running/ready=true —— 喚醒是實質的，不只是欄位。
        ``subscription_lifecycle_cron`` 每天只跑一次，最壞情況白燒約一天。

        收斂走與 :meth:`_do_suspend` 相同的外科式 ``scale_release``：與 chart
        無關，且對 VM 型冪等（operator 把 ``replicas=0`` 映射成
        ``runStrategy: Halted``——依賴 operator #488 的三 workload 語意）。

        非停權服務一律 no-op —— 這裡誤動就是把正常服務關掉。失敗不 raise
        （upgrade 本身已經成功，讓整個操作看起來失敗是誤導），但**必須留下
        可觀察的痕跡**：停權服務照跑就是計費繞過，靜默吞掉等於把帳務問題藏起來。

        Returns:
            bool: ``True`` ＝ 這台的 workload 已確認被送去停機（或本來就不該停機）；
            ``False`` ＝ **沒能**收回（operator 不可達、或連 release 座標都沒有）。
            三條 upgrade 路徑忽略回傳值——它們的 error_message 已足夠。#497 的
            migration 用它決定要不要在 ``error_message`` 留可機械盤點的殘留記號。
        """
        self.ensure_one()
        if not self._is_halted():
            return True
        if not (client and self.helm_release_name and self.helm_namespace):
            return False
        try:
            client.scale_release(
                self.helm_release_name, self.helm_namespace, replicas=0,
            )
            _logger.info(
                "[CLOUD_SVC] _rescale_halted_after_upgrade scaled to 0 "
                "service=%s release=%s ns=%s",
                self.id, self.helm_release_name, self.helm_namespace,
            )
            return True
        except Exception as e:
            _logger.warning(
                "[CLOUD_SVC] _rescale_halted_after_upgrade failed service=%s: %s "
                "— suspended workload may still be running (billing bypass)",
                self.id, e,
            )
            self.write({
                'error_message': _(
                    'Service is suspended but could not be scaled back down '
                    'after the upgrade: %(error)s',
                    error=e,
                ),
            })
            return False

    def _is_release_ready(self, status, dns_state=None):
        """判定「這個 release 就緒了嗎」——單一事實源（#1159 起只剩背景 poll 呼叫）。

        這段邏輯原本在 model（``_poll_and_update_status``）與 controller
        （``PaasController._update_service_status``）各有一份完全同型的複製品。
        #423 設計審查抓到：只修其中一份，租戶只要打開前台服務詳情頁，
        controller 那份就會把還在 import 的 VM 標成 ``running``——cron 修好的
        東西被前台查詢覆寫回去。故抽成單一事實源，當時兩層都呼叫這裡。
        #1159 起 list/get 純讀、controller 那份 poll 已刪除，呼叫端只剩 model 的 poll。

        （post-deploy-init 兩層仍各自實作，那是既存且有實質差異的另一件事，
        不在本次收斂範圍。）

        Args:
            status: operator ``get_status`` 的回應 dict
            dns_state: 已經探過的 DNS 現況（`_public_url_dns_state()` 的回傳）。
                背景 poll（#1159 起唯一一份）會先探一次再把結果同時餵給本方法與 `_release_facts`，
                避免同一輪 poll 對同一個 hostname 探兩次（resolver 不可用時那是
                兩倍的阻塞）。``None`` ＝ 本方法自己探，讓單獨呼叫仍然自足。

        Returns:
            ``(ready: bool, detail: str)`` —— detail 供 log 判讀為何未就緒
        """
        self.ensure_one()

        # #1251：受 woow-odoo 契約管理的 release（依實際 `release.chart`，Q17）改用
        # Pod 身分判定取代 #1156 的通用規則；分流只在這裡，不在 cron／controller 任一邊。
        # ⛔ 不用 template slug 分流。規則見
        # docs/decisions/patterns/woow-odoo-readiness-identity.md
        contract = woow_odoo_contract(status, self.template_id.helm_chart_name)
        if contract == CONTRACT_MISMATCH:
            return False, chart_mismatch_detail(status)
        if contract == CONTRACT_MANAGED:
            ready, detail = woow_odoo_ready(status)
        elif self._needs_vm_status():
            vm_ready, detail = self._is_vm_ready(status)
            if not vm_ready:
                return vm_ready, detail
            # MINOR-2：VM 就緒後仍要看容器面。template 日後若啟用 MCP sidecar、
            # 或 chart 加了輔助 Deployment，只看 VMI 會在那些容器還沒起來時就
            # 報就緒。
            #
            # ⚠ 這裡原本寫「容器端對『pods 為空』回 True，所以這個合取不會讓純
            # VM 卡住」——那句話的**前提是錯的**（F-423-1 Step 1 實測推翻）：
            # KubeVirt 的 VM 跑在 virt-launcher pod 裡，而 haos chart 把
            # `app.kubernetes.io/instance` 標在 VirtualMachine 的 pod template 上，
            # virt-launcher 因此**帶著** operator selector 用的那個標籤（實測
            # `virt-launcher-svc-…-haos` 回報 `2/2`）。所以純 VM 的 `pods` 從來
            # 不是空的，容器閘是**憑自身條件**通過的，不靠任何 fail-open。
            # 這個事實在 Step 1 之後從「碰巧成立」變成「必要條件」。
            ready, detail = self._is_container_ready(status)
        else:
            ready, detail = self._is_container_ready(status)

        if not ready:
            return ready, detail

        # #648 (b)：pod／VM 都健康之後，最後一格是「租戶現在真的打得開嗎」。
        # 放在合取末端（而不是寫 `running` 的那 6 個分支上）是刻意的：那 6 個
        # 分支散在兩個檔案，等於再造一次 #627 的形狀；而且
        # 停在 `initializing` 的服務每輪都會重進 init 入口（#1257 起是
        # `_run_post_deploy_init`），閘放 write 上會讓它反覆進 init 去撞失敗額度
        # ——繞回同一個「健康服務被標 error」的結果。
        return self._is_public_url_ready(dns_state=dns_state)

    @staticmethod
    def _is_container_ready(status):
        """容器型就緒判定。

        **fail-closed**：排除 Succeeded／Failed／帶 deletion_timestamp 的收尾 Pod 後，
        至少要有一顆有效 Pod，且每顆都必須 Running、容器全 ready。Pending（含
        initContainer 執行中）不能被其他已 Ready 的 Pod 遮蔽（#1156）。
        ``pods`` 為空的兩個成因處置相同，只在理由字串上區分：

        1. **查詢本身失敗** —— ``pod_retrieval_error`` 有值（RBAC 掉了、API server
           抖動…）。operator 此時仍回 **HTTP 200**、``pods`` 保持空陣列
           （``api/releases.py``），所以呼叫端不讀這個欄位就會誤讀成第 2 種（#484）。
        2. **查詢成功、真的一顆 pod 都沒有** —— ``pod_retrieval_error`` 是 ``None``。

        第 2 種原本回 True（fail-open，F-423-1 / design §4.1），理由是「容器型有一個
        合法的短暫狀態是 helm release 已 deployed 但 pod 還沒被 controller 建出來，
        改 fail-closed 會讓所有現存租戶服務多卡一輪 poll」。**F-423-1 Step 1
        (2026-07-31) 翻掉了它**，因為評估推翻了這個取捨的兩邊：

        - **代價被高估**：poll 只撈 deploying / upgrading / initializing，所以「多卡
          一輪」只發生在**正在轉移**的服務上，對已 ``running`` 的現存機隊零影響；
          而一輪是前台 5 秒、cron 60 秒。
        - **收益被低估**：``pods`` 為空**不必然是短暫的**。ResourceQuota 拒絕 pod
          建立時，ReplicaSet 根本產不出 Pod 物件（``FailedCreate``），空陣列會**永久
          持續**；而誤報是**黏住**的（寫進 ``running`` 後沒有任何東西會再 poll 它），
          15 個 template 有 13 個 ``post_deploy_init_type='none'``、連 post-deploy
          init 那層巧合的網都沒有。#456 就是實錄：兩筆 hermes-agent 永久壞掉，
          DB 與 UI 卻都顯示 Running/ONLINE。

        上述 F-423-1 只改「空陣列」；#1156 另補混合 phase：Running 的 PG 與
        Pending 的 Odoo 同時存在時，仍須維持未就緒。

        woow-odoo release **不走這裡**：`_is_release_ready` 把它分流到
        `services/woow_odoo_readiness.py`（#1251：必要組＋例行 Job 豁免＋其他
        fail-closed），本規則對其他 chart 不變。

        殘餘風險與封頂：若某 chart 的 pod 不帶 ``app.kubernetes.io/instance``
        （operator 的 selector），服務會從「假就緒」變成「永久卡 deploying」。合併前
        已逐一實證現行 15 個 template 涵蓋的 9 支 chart 都帶該標籤；長期由 #489 的
        部署 age timeout 封頂。
        """
        if status.get('pod_retrieval_error'):
            # 硬失效：查詢失敗＝狀態未知。不會自己好 → 必須是 HARD
            # （`_log_not_ready` 會升 warning），否則正式環境的 INFO 級 log
            # 一行都看不到。
            return False, (
                f"HARD: pod lookup failed: {status['pod_retrieval_error']}"
            )

        pods = status.get('pods') or []
        # Pending（含 initContainer 執行中）仍須參與判定，不能被 Ready 的 PG 遮蔽。
        active_pods = [
            p for p in pods
            if p.get('phase') not in ('Succeeded', 'Failed')
            and not p.get('deletion_timestamp')
        ]
        if not pods:
            # 查得到、但一顆 pod 都沒有。可能是合法的建立中窗口，也可能是永久的
            # （quota 擋住 Pod 物件建立）。兩者在這一層**分不出來**，所以 HARD——
            # 分辨要靠時間，那是 #489 的職責。
            return False, 'HARD: no pods found for this release'
        if not active_pods:
            return False, 'no active pods'

        def containers_ready(pod):
            ready = pod.get('ready', '0/0')
            return '/' in ready and ready.split('/')[0] == ready.split('/')[1]

        # #1417：沒跑起來（Pending／Unknown，含 initContainer 執行中、等磁碟、等排程）與
        # 「Running 但未 ready」分開說——兩者該查的地方完全不同（pod events vs container
        # log／readiness probe）。10-04 svc 147 停在 Init:0/1，舊碼一律回 'pods not ready'，
        # 逾時訊息就叫人去看 container log。
        not_started = [p for p in active_pods if p.get('phase') != 'Running']
        not_ready = [p for p in active_pods
                     if p.get('phase') == 'Running' and not containers_ready(p)]
        if not (not_started or not_ready):
            return True, 'pods ready'
        return False, '; '.join(filter(None, (
            _describe_unready_pods('pods not started', not_started),
            _describe_unready_pods('pods not ready', not_ready),
        )))

    @staticmethod
    def _is_vm_ready(status):
        """VM 型就緒判定——**fail-closed**（#423 design §4.1）。

        與容器型的關鍵差異：**virt-launcher pod Running ≠ guest 開機完成**。
        pod 一起來就翻 running 會讓租戶拿到一台還在 import 磁碟的機器。故改讀
        VMI 的 ``phase`` ＋ ``Ready`` condition。

        fail-closed 的三種情形（全部「維持 deploying」而非翻 running）：

        1. ``vm_included`` 為 False —— 呼叫端漏帶 ``include_vm``。這是程式錯誤，
           但寧可卡住也不能誤報就緒。
        2. ``vmi_retrieval_error`` 有值 —— 查詢失敗，狀態未知。
        3. ``vmis`` 為 None（未查詢）或空 list（還沒建出來）。

        ⚠ ``datavolume_retrieval_error`` **不**參與就緒判定：DataVolume 只供
        進度顯示，它掛掉頂多讓進度條消失，不該擋住一台已經 Ready 的 VM。
        """
        if not status.get('vm_included'):
            # 硬失效：呼叫端漏帶 include_vm，或 operator 版本太舊沒有這個欄位。
            # 兩者都不會自己好，且外觀與「還在 import」完全相同 → 必須是
            # warning，否則正式環境的 INFO 級 log 一行都看不到（MINOR-1）。
            return False, (
                'HARD: operator was not asked for VM status (include_vm '
                'missing, or operator too old to echo it) — refusing to '
                'report ready'
            )
        if status.get('vmi_retrieval_error'):
            # 硬失效：多半是 RBAC 缺 virtualmachineinstances 權限。
            return False, (
                f"HARD: VMI lookup failed: {status['vmi_retrieval_error']}"
            )

        vmis = status.get('vmis')
        if not vmis:
            # 正常過渡：VMI 還沒建出來（磁碟 import 中）。
            return False, 'no VMI yet'

        not_ready = [
            v.get('name') for v in vmis
            if not (v.get('phase') == 'Running' and v.get('ready'))
        ]
        if not_ready:
            return False, f"VMI not ready: {', '.join(filter(None, not_ready))}"
        return True, 'VMI ready'

    def _log_not_ready(self, detail):
        """把「為何還沒就緒」記進 log，硬失效升 warning（MINOR-1）。

        為什麼分級很重要：VM 卡在 ``deploying`` 有兩種成因，在 Odoo 側**外觀
        完全相同**——
          * 正常過渡：磁碟還在 import（可能數十分鐘），
          * 硬失效：operator RBAC 缺 ``virtualmachineinstances`` 權限、或
            平台漏帶 ``include_vm``。
        後者不會自己好，而 ``_cron_check_deploying_services`` 沒有 age 逾時
        機制，所以若兩者都寫 debug，正式環境（INFO 級）不會留下任何線索，
        只能靠人去猜。這一個字決定 W6 上線時能不能直接從 log 定位（#315
        同型教訓）。
        """
        self.ensure_one()
        if detail.startswith('HARD:'):
            _logger.warning(
                "Service %s (id=%s) blocked from becoming ready: %s",
                self.name, self.id, detail,
            )
        else:
            _logger.debug(
                "Service %s not ready yet: %s", self.name, detail,
            )

    # ------------------------------------------------------------------ #
    #  轉移狀態的 age timeout（#489 / F-423-1 Step 2）                     #
    # ------------------------------------------------------------------ #

    def _is_timeout_governed(self):
        """本服務目前的狀態是否受 age timeout 管轄。

        兩層 poll 共用這個判準（controller 那層要用它決定 pending-* 要不要記
        stuck 理由），避免狀態清單在兩處雙寫後無聲漂移。
        """
        self.ensure_one()
        return self.state in _TIMEOUT_GOVERNED_STATES

    def _transition_anchor(self):
        """本輪狀態轉移的起算時間；``None`` ＝ 無法判定。

        取 ``deployed_at`` 與 ``last_upgraded_at`` 的**較晚者**，而不是依
        ``state`` 擇一。理由是寫入這兩個欄位的路徑與 state 不是一一對應：
        ``action_redeploy`` 寫的是 ``deploying``（首裝路徑的狀態）卻是 upgrade
        語意，依 state 擇一會取到**首裝**時間——一台部署了三個月的服務按下重新
        部署，第一輪 poll 就被判逾時。取較晚者天然涵蓋所有入口。

        （本 PR 一併把三個漏記錨的轉移入口補上 ``last_upgraded_at``：
        ``action_redeploy`` 的 upgrade 分支、``_reset_admin_password``、
        controller 的 rollback。少一個都會讓那條路徑用陳舊的錨誤殺。）

        回 ``None`` 的唯一情形是兩個欄位都空——只發生在 ``_execute_deploy``
        的 helm install 執行期間（install 回來就寫 ``deployed_at``，同一個
        transaction 內）。**此時不逾時**：沒有依據就不裁決，而誤殺不可自癒。
        這與「參數缺值走內建預設」的 fail-safe 方向鎖不衝突——那條鎖的是**設定
        漏設**（可預期、可補），這裡是**事實未知**（無從推算）。
        """
        self.ensure_one()
        stamps = [t for t in (self.deployed_at, self.last_upgraded_at) if t]
        return max(stamps) if stamps else None

    def _transition_timeout_minutes(self):
        """本服務的逾時門檻（分鐘）。VM 型與容器型各一檔。

        缺值、空白、非整數、非正數一律**退回內建預設**而不是停用逾時——否則
        這道保護會因為漏設一個參數而靜默失效（#404 apiServerCidrs 的教訓）。
        """
        self.ensure_one()
        is_vm = self._needs_vm_status()
        key = _VM_DEPLOY_TIMEOUT_PARAM if is_vm else _DEPLOY_TIMEOUT_PARAM
        default = (
            VM_DEPLOY_TIMEOUT_MINUTES_DEFAULT if is_vm
            else DEPLOY_TIMEOUT_MINUTES_DEFAULT
        )

        raw = self.env['ir.config_parameter'].sudo().get_param(key)
        # 未設定是正常的（走內建預設，不吵）。**設了但看不懂**才要吵——否則
        # 有人把門檻打錯字（"30m"、"0"）會靜默被忽略，而他以為自己調成功了。
        unset = raw in (None, False, '') or not str(raw).strip()
        try:
            minutes = int(str(raw).strip())
        except (TypeError, ValueError):
            minutes = 0
        if minutes <= 0:
            if not unset:
                _logger.warning(
                    "%s is %r (not a positive integer of minutes) — falling "
                    "back to the built-in default of %s minutes",
                    key, raw, default,
                )
            return default
        return minutes

    def _expose_failed(self, release_info):
        """讀 operator 對 expose 的回報；判定失敗就寫 `error` 並回 True（#648 (a)）。

        修前平台**一次都沒讀過** install 回應裡的 ``route``（`rg route_info src/` 與
        `rg` 找「`.get` 帶 route」兩種寫法都 0 命中）——`create_dns_record` 失敗被
        operator 就地吞掉、端點照樣回 HTTP 201，於是租戶拿到一個永遠不會解析的
        hostname 而服務標成 running。這是 #484 為 Smart Home 補過、CS 這半漏接的
        同一條縫。

        🔴 **判準是「明說的失敗」，不是「東西不在」**：

        - ``route`` 缺席 ⇒ **不是**失敗，不得 fail-closed。它同時涵蓋兩種正常情形：
          (1) 這個部署根本沒開 Cloudflare（operator 的 ``cloudflare_enabled``
          **預設 false**，且缺 account_id／tunnel_id／api_token／domain 任一個會被
          強制關回 False）；(2) operator 舊到還沒有這個欄位。把缺席當失敗，會讓沒開
          CF 的環境**每一次部署都翻 error**——比原 bug 嚴重得多。
          載體：``docs/tracking/pitfalls/operator-route-none-conflates-disabled-and-failed.md``
        - ``status == 'failed'`` ⇒ 要求了 expose 但整條 route 沒建成。
        - ``dns_failed`` 為真 ⇒ tunnel ingress 在、但 DNS record 沒建成 ⇒ hostname
          不會解析。鏡射 ``TunnelCreateResponse.dns_failed`` 的契約（#484）。

        **與 #484 的 SmartHome 不同構的地方（所以不需要 stored-id guard）**：SH 的
        ``tunnel_id`` 只存在於 provision 回應裡，丟了就是永久孤兒，所以那邊必須把
        欄位落地並加 guard。CS 的錨點（``subdomain`` / ``helm_release_name`` /
        ``helm_namespace``）在**建立當下**就寫好了，刪除路徑直接拿 ``subdomain``
        去拆 route；而 operator 走的是 ``helm install``（不是 ``upgrade --install``）
        ⇒ 對既存 release 重跑會 fail-loud，不會像 SH 的 ``create_tunnel`` 那樣默默
        建出第二份。兩個理由都不成立於 SH，也都成立於 CS。

        Returns:
            ``True`` 代表已經寫成 ``error``，呼叫端應直接返回。
        """
        self.ensure_one()
        route = release_info.get('route')
        if not route:
            # 沒有 route 可報 —— 見 docstring，這條**不**是失敗。
            return False

        hostname = route.get('hostname') or self._public_url_hostname() or self.subdomain
        if route.get('status') == 'failed':
            reason = _(
                "The service was deployed but its public route could not be "
                "created, so %(hostname)s will not be reachable.",
                hostname=hostname or _('it'),
            )
        elif route.get('dns_failed'):
            reason = _(
                "The service was deployed and its tunnel route exists, but "
                "the DNS record could not be created, so %(hostname)s will "
                "not resolve.",
                hostname=hostname or _('the public hostname'),
            )
        else:
            return False

        self.write({
            'state': 'error',
            'error_message': _(
                "%(reason)s The Helm release is kept so it is not orphaned — "
                "an administrator can create the missing record, or delete "
                "and re-create this service.",
                reason=reason,
            ),
        })
        _logger.error(
            "[CS] expose failed for service %s (id=%s) hostname=%s route=%s "
            "— kept in 'error' with the release anchored",
            self.name, self.id, hostname, route,
        )
        return True

    def _dns_grace_seconds(self):
        """Public URL DNS 閘的**有效**寬限期（秒）。

        缺值／空白／非正整數一律退回內建預設（同 `_transition_timeout_minutes`
        的 fail-safe 方向：漏設一個參數不該讓保護靜默失效）。
        """
        self.ensure_one()
        raw = self.env['ir.config_parameter'].sudo().get_param(_DNS_GRACE_PARAM)
        unset = raw in (None, False, '') or not str(raw).strip()
        try:
            seconds = int(str(raw).strip())
        except (TypeError, ValueError):
            seconds = 0
        if seconds <= 0:
            if not unset:
                _logger.warning(
                    "%s is %r (not a positive integer of seconds) — falling "
                    "back to the built-in default of %s seconds",
                    _DNS_GRACE_PARAM, raw, DNS_GRACE_SECONDS_DEFAULT,
                )
            seconds = DNS_GRACE_SECONDS_DEFAULT

        # 🔴 clamp —— 沒有這一步，上面那個「閘永遠不可能造成 error」的不變式
        # 就只在「沒人亂設參數」時成立。把有效值夾在 age timeout 之下（留一格
        # 餘裕，不能剛好相等，否則兩邊會在同一輪 poll 撞在一起）。
        ceiling = (self._transition_timeout_minutes() * 60
                   - _DNS_GRACE_SAFETY_MARGIN_SECONDS)
        return max(0, min(seconds, ceiling))

    def _dns_gate_enabled(self):
        """DNS 閘的總開關（prod kill switch）。未設＝開。"""
        raw = self.env['ir.config_parameter'].sudo().get_param(
            _DNS_GATE_ENABLED_PARAM)
        if raw in (None, False, ''):
            return True
        return str(raw).strip().lower() not in _FALSEY_PARAM_VALUES

    def _public_url_hostname(self):
        """對外主機名（不含 scheme）；沒有對外網址回 ``''``。

        刻意從 `_public_base_url()` 導出而不是自己組——domain 的組法收斂在那裡
        （`paas_domain` 讀 config_parameter），兩處各組一次就是下一個漂移點。
        """
        self.ensure_one()
        base = self._public_base_url()
        if not base:
            return ''
        return base.split('://', 1)[-1].rstrip('/')

    def _public_url_dns_state(self):
        """Public URL 的 DNS 現況：``ok`` / ``missing`` / ``unknown`` / ``skip``。

        ``skip`` ＝ 這台服務不適用（沒有對外網址，或閘被關掉），與「探了但不知道」
        （``unknown``）語意不同：前者不該留任何旗標，後者是平台自己的 resolver 出
        問題。呼叫端對兩者都放行，但只有前者連旗標都不寫。
        """
        self.ensure_one()
        if not self._dns_gate_enabled():
            return 'skip'
        hostname = self._public_url_hostname()
        if not hostname:
            return 'skip'
        # 結尾點＝FQDN，讓 resolver 跳過 search list。這不只是省時間：平台 pod 是
        # `ndots:5` ＋ 4 個 search domain，resolver 不可用時相對名最壞是 5 次查詢
        # ×（5s × 2 attempts）＝ 50 秒，FQDN 是 10 秒。而背景 cron 逐台序列跑、受
        # cron 硬上限約束（#1159 Q34）。有測試釘住這個性質。
        return dns_probe.resolve_state(hostname + '.')

    def _is_public_url_ready(self, dns_state=None):
        """就緒判定的最後一格：租戶的 Public URL 現在解析得到嗎（#648 (b)）。

        Returns:
            ``(ready: bool, detail: str)``——與 `_is_release_ready` 同形。

        🔴 **只在 `age(_transition_anchor()) < _dns_grace_seconds()` 這個窗口內
        可能回 False，窗口外恆放行。** 理由與不變式見模組頂部 #648 那段常數註解：
        #489 的 age timeout 與本閘共用同一把尺（`deployed_at` 在 helm install 一
        回來就寫），閘若自己另計時，「pods 很慢才 ready ＋ DNS 未落地」會把服務
        寫成不可自癒的 `error`。

        錨點未知（兩個時間戳都空）時**放行**——與 `_check_transition_timeout`
        的「沒有依據就不裁決」同方向；反過來擋會變成永遠卡。
        """
        self.ensure_one()

        # 便宜的判斷先做，探測擺在它後面：窗口外恆放行，那時不必再問。
        #
        # ⚠ **這在 production 熱路徑上省不到任何一次探測**，不要以為它是保護。
        # 背景 poll（#1159 起唯一一份）在呼叫本方法**之前**就無條件探好了（`release_status ==
        # 'deployed'` 時探一次，結果同時餵給本方法與 `_release_facts` 落旗標），
        # 所以走到這裡時 `dns_state` 已經有值、提前 return 只是少走幾行。
        # 真正受惠的是 `dns_state=None` 的**直接呼叫者**（測試，或日後某個沒先
        # 算的呼叫端）——順序這樣排是因為「便宜的條件先判」本來就對，不是因為
        # 它擋掉了什麼成本。
        #
        # resolver 不可用時的阻塞由**另一個機制**擋：`dns_probe` 的 backoff 閂
        # （見該模組的 `_RESOLVER_BACKOFF_SECONDS`）。那條才是熱路徑上有效的。
        anchor = self._transition_anchor()
        if not anchor:
            # 兩個時間錨都空 ⇒ 無從判斷 age。與 `_check_transition_timeout` 的
            # 「沒有依據就不裁決」同方向；反過來擋會變成永遠卡。
            return True, 'pods ready'

        age = fields.Datetime.now() - anchor
        if age.total_seconds() >= self._dns_grace_seconds():
            if dns_state == 'missing':
                # 這一輪 poll 已經探過而且是「查不到」——帶著保留放行（旗標由
                # `_release_facts` 落地），不永遠卡。
                _logger.warning(
                    "Service %s (id=%s) went ready with an unresolvable public "
                    "hostname %s — releasing after the DNS grace window",
                    self.name, self.id, self._public_url_hostname(),
                )
            return True, 'pods ready'

        state = self._public_url_dns_state() if dns_state is None else dns_state
        if state != 'missing':
            # ok / skip / unknown 一律放行。unknown＝resolver 自己不可用，那是
            # 平台自身故障；為了一個壞掉的探針把每一台租戶服務都扣住，比它要
            # 偵測的那個 bug 嚴重得多。
            return True, 'pods ready'

        return False, (
            'public URL %s does not resolve yet'
            % self._public_url_hostname()
        )

    def _refresh_public_url_dns_flag(self):
        """已標記「未驗證」的 running 服務重探一次，DNS 落地就清旗標（#648 (b)）。

        為什麼需要這條：`running` **不在任何 poll 的 domain 裡** ⇒ 放行時寫下的旗標
        沒有任何東西會回來清掉，會變成永久噪音——那比不警告更糟。

        #1159 Q16：原本掛在 list/get 的讀取路徑上（同步 DNS 外呼＋寫入），與 Q1 純讀
        互斥 ⇒ 改由背景 ``_cron_reprobe_public_url_dns`` 低頻呼叫。

        自我了結：只對「旗標已經是 True 的 running 服務」動作，清掉之後就不會再探。
        只有探到 ``ok`` 才清；``unknown``（resolver 自己不可用）不動。

        **fail-soft**：清旗標失敗只 log。DB 錯誤（``OperationalError``，如 40001）除外：
        它代表當台交易已不可用，交給呼叫端 ``_cron_reprobe_public_url_dns`` 的逐台邊界
        回捲後繼續後排（Q36／D4），不在這裡降成 warning。
        """
        self.ensure_one()
        if not self.public_url_dns_unverified or self.state != 'running':
            return
        try:
            if self._public_url_dns_state() == 'ok':
                self.sudo().write({'public_url_dns_unverified': False})
        except OperationalError:
            raise
        except Exception as exc:          # noqa: BLE001 —— 見 docstring
            _logger.warning(
                "Could not refresh the public-URL DNS flag for service %s "
                "(id=%s): %s", self.name, self.id, exc)

    @api.model
    def _cron_reprobe_public_url_dns(self):
        """#1159 Q16／D4：背景低頻重探「Public URL 未驗證」的 running 服務，每台 commit。

        頻率（每 15 分）寫在 ``cloud_service_cron.xml``。旗標只是軟提示（租戶服務
        已 running），偵測延遲 15 分可接受；每台一個回捲單位，一台失敗不影響其他台。
        """
        services = self.search([
            ('state', '=', 'running'),
            ('public_url_dns_unverified', '=', True),
        ])
        for service in services:
            service_label = (service.name, service.id)
            try:
                service._refresh_public_url_dns_flag()
                self.env.cr.commit()
            except Exception:
                # Q36／D4：每台一個回捲單位，回捲掉的只有這一台未 commit 的寫入，後排照常重探。
                # ⚠ #1052 標記（非裁定）：這是逐筆 batch isolation，OperationalError 也在此收掉，
                # 與 post-commit-bookkeeping-rollback-unit.md 規則一分歧（同短 poll 的站點）。
                self.env.cr.rollback()
                _logger.exception(
                    "[#1159] DNS re-probe failed for service %s (id=%s)", *service_label)

    def _timeout_classify(self, detail):
        """detail → ``(原因分型, hint key)``（#1417）；hint key 為 None＝沒有方向可給。

        停權中（#482）優先於一切：那時的 pod／quota 方向全是誤導。
        """
        self.ensure_one()
        if self._is_halted():
            return TIMEOUT_CAUSE_SUSPENDED, 'suspended'
        for needle, key, cause in _TIMEOUT_HINTS:
            if needle in (detail or ''):
                return cause, key
        return TIMEOUT_CAUSE_OTHER, None

    def _timeout_hint_text(self, env, key):
        """hint key → 給維運的方向（以 ``env`` 的語言翻譯）。

        逐條字面呼叫 ``env._``，Odoo 的抽取工具（babel，keywords ``_``）才抓得到——
        所以不能把英文留在模組常數裡再 ``env._(變數)``。
        """
        texts = {
            'chart_mismatch': env._(
                'the release runs a different chart than the template expects — an '
                'in-place upgrade cannot switch charts; a fresh install is required (see '
                '#1128)'),
            'operator_lacks_identity': env._(
                'the operator does not report Pod identity (component/owner) — upgrade '
                'paas-operator first (#1263), woow-odoo readiness fails closed without it'),
            'required_component_missing': env._(
                "the chart's server/postgresql Pod was never created (or is not owned by "
                'its ReplicaSet/StatefulSet) — check the ReplicaSet/StatefulSet events for '
                'FailedCreate (namespace ResourceQuota, admission)'),
            'required_pods_not_running': env._(
                'a required Pod exists but never reached Running — check its pod events '
                '(scheduling, Multi-Attach, image pull); an exempt backup Job listed in '
                'the detail may still hold the RWO data PVC'),
            'volume_attach': env._(
                'a volume could not be attached to the node — check the Longhorn volume '
                'and whether its replicas can be scheduled (disk space, storage '
                'over-provisioning, orphaned PVCs still holding capacity)'),
            'volume_mount': env._(
                'a volume could not be mounted — check the PVC/PV binding and the CSI '
                'driver events of the pod'),
            'scheduling': env._(
                'the pod could not be scheduled — check node CPU/memory capacity, unbound '
                'PVCs (storage class, Longhorn capacity) and node selectors'),
            'image_pull': env._(
                'the container image could not be pulled — check the image reference, the '
                'registry credentials (imagePullSecrets) and that the registry is reachable '
                'from the node'),
            'crash_loop': env._(
                'a container keeps crashing on start — check its previous logs (kubectl '
                'logs --previous) and its configuration'),
            'container_config': env._(
                'the container could not be created — check the Secrets/ConfigMaps it '
                'references and its container spec'),
            'pod_lookup_failed': env._(
                'the operator could not list pods — check its RBAC and the API server'),
            'no_pods_found': env._(
                'no Pod object was ever created — check the namespace ResourceQuota (a '
                'rejected Pod never materialises) and that the chart labels its pods '
                'with app.kubernetes.io/instance'),
            'no_active_pods': env._(
                'pods exist but none reached Running — check pod events (scheduling, '
                'image pull) and node capacity'),
            'pods_not_started': env._(
                'pods never started (still Pending) — check the pod events (scheduling, '
                'volume attach/mount, image pull) and node capacity'),
            'pods_not_ready': env._(
                'pods are Running but never became ready — check container logs and the '
                'readiness probe'),
            'vm_status_missing': env._(
                'the platform never received VM status — the operator is likely too old '
                'to report it'),
            'vmi_lookup_failed': env._(
                'the operator could not read the VirtualMachineInstance — check its RBAC '
                'for virtualmachineinstances'),
            'no_vmi_yet': env._(
                'no VirtualMachineInstance was created — check the DataVolume / CDI '
                'importer, the disk import may have stalled'),
            'vmi_not_ready': env._(
                'the VM is running but never reported Ready — check the guest boot '
                'inside the VM'),
            'helm_status_stuck': env._(
                'the helm release never reached "deployed" — a killed rollback leaves it '
                'stuck in pending-*; inspect `helm history` for the release'),
            'suspended': env._(
                'the service is suspended — its workload is intentionally halted, so it '
                'can never become ready while a deploy/upgrade is pending (see #482)'),
        }
        return texts[key]

    def _timeout_diagnosis(self, detail, env=None):
        """把就緒判定的 ``detail`` 翻成「維運該往哪查」的一句話（``env`` 的語言）。

        回空字串代表沒有對應的方向可給（fail-soft：訊息仍帶前綴與 detail）。
        """
        self.ensure_one()
        _cause, key = self._timeout_classify(detail)
        return self._timeout_hint_text(env or self.env, key) if key else ''

    def _timeout_tenant_summary(self, env, cause, minutes):
        """給租戶的那一句話（#1417）：發生什麼、要不要他做事、之後會怎樣。

        租戶看不懂 readiness probe，也處理不了平台的儲存或排程——除了「反覆重啟」可能
        是他剛改的設定造成的，其餘都不需要他動手；而問題排除後服務會自己恢復（#1416
        的背景自癒）。
        """
        if cause == TIMEOUT_CAUSE_SUSPENDED:
            return env._(
                'The service is suspended, so it cannot finish deploying. It will resume '
                'automatically once the subscription is active again.')
        if cause == TIMEOUT_CAUSE_STORAGE:
            return env._(
                'The service did not finish starting within %(minutes)s minutes because '
                'the platform could not provide the storage or computing resources it '
                'needs in time. This is a platform-side issue and you do not need to do '
                'anything; the service will recover automatically once it is resolved.',
                minutes=minutes)
        if cause == TIMEOUT_CAUSE_IMAGE:
            return env._(
                'The service did not finish starting within %(minutes)s minutes because '
                'its software image could not be downloaded. This is a platform-side '
                'issue and you do not need to do anything; the service will recover '
                'automatically once it is resolved.',
                minutes=minutes)
        if cause == TIMEOUT_CAUSE_CRASH:
            return env._(
                'The service did not finish starting within %(minutes)s minutes because '
                'it keeps restarting while starting up. If you recently changed its '
                'settings, please check them; otherwise you do not need to do anything. '
                'The service will recover automatically once the problem is resolved.',
                minutes=minutes)
        if cause == TIMEOUT_CAUSE_READINESS:
            return env._(
                'The service started but did not report that it was ready within '
                '%(minutes)s minutes. You do not need to do anything; the service will '
                'recover automatically once it becomes ready.',
                minutes=minutes)
        return env._(
            'The service did not finish starting within %(minutes)s minutes. You do not '
            'need to do anything for now; the service will recover automatically once '
            'the problem is resolved. If it does not recover, please contact support.',
            minutes=minutes)

    def _timeout_error_message(self, minutes, detail):
        """逾時轉 error 的 ``error_message``（#1417）：先一句給租戶，再附給管理員的技術細節。

        ``error_message`` 由 cron 寫入、畫面直接顯示原文，cron 的 env 語言不是租戶的
        ⇒ 以服務建立者的語言（沒有就用目前 env）組字串（同 `_storage_quota_error_message`）。
        技術細節沿用 #489 的「Deployment timed out after N minutes: <detail> — <hint>」；
        detail 本身是 Pod 名稱與 Kubernetes 的原因代碼，不翻。

        Returns:
            ``(message, cause)``
        """
        self.ensure_one()
        lang = self.sudo().create_uid.lang or self.env.lang
        env = self.with_context(lang=lang).env
        cause, key = self._timeout_classify(detail)
        technical = env._(
            "Deployment timed out after %(minutes)s minutes: %(detail)s",
            minutes=minutes, detail=detail,
        )
        if key:
            # 純格式串（無可翻內容）：兩端都已各自翻譯過。
            technical = '%s — %s' % (technical, self._timeout_hint_text(env, key))
        # 換行：後台表單（Text）分兩段；/woow 的 t-esc 會把它收成一個空白。
        message = env._(
            '%(summary)s\nTechnical details for the administrator: %(details)s',
            summary=self._timeout_tenant_summary(env, cause, minutes),
            details=technical,
        )
        return message, cause

    def _check_transition_timeout(self, detail):
        """轉移狀態逾時就翻 ``error``；回傳「是否已翻」。

        **只變狀態與訊息**——不刪任何叢集資源、不觸發 rollback。卡住的 release
        原樣留在叢集裡供診斷，維運看完再決定要 redeploy 還是清掉。#1416 起同時記下
        ``transition_timeout_at``：逾時只代表「等太久」，release 之後就緒時由
        `_cron_recover_timed_out_services` 照正常路徑推回來。

        呼叫端契約：必須在 poll 的 optimistic locking（re-fetch ＋ 比對
        ``original_state``）**之後**呼叫，且 ``self`` 是 re-fetch 過的 record。
        逾時寫入走同一套鎖，否則會蓋掉別的 process 剛寫進去的狀態。
        """
        self.ensure_one()
        if self.state not in _TIMEOUT_GOVERNED_STATES:
            return False

        anchor = self._transition_anchor()
        if not anchor:
            _logger.warning(
                "Service %s (id=%s) is %s but has neither deployed_at nor "
                "last_upgraded_at — age timeout cannot be evaluated",
                self.name, self.id, self.state,
            )
            return False

        minutes = self._transition_timeout_minutes()
        now = fields.Datetime.now()
        age = now - anchor
        if age.total_seconds() <= minutes * 60:
            return False

        message, cause = self._timeout_error_message(minutes, detail)

        # error 分兩種來源，log 要能一眼分辨：這條是「平台判定逾時」，
        # `release_status == 'failed'` 那條是「operator 明確回報失敗」。
        # （log 用英文 detail；error_message 是建立者語言。）
        _logger.error(
            "Service %s (id=%s) timed out in state %s after %s minutes "
            "(anchor=%s, cause=%s): %s",
            self.name, self.id, self.state, minutes, anchor, cause, detail,
        )
        # 標記只有平台寫得進去（`_reject_timeout_recovery_control_writes`）；poll 只在 cron
        # 跑，這裡的 sudo 只是不依賴呼叫端身分。
        self.sudo().write({
            'state': 'error',
            'error_message': message,
            'transition_timeout_at': now,
        })
        return True

    # ------------------------------------------------------------------ #
    #  #1416：部署逾時的有限度自癒                                         #
    # ------------------------------------------------------------------ #

    def _timeout_recovery_window_hours(self):
        """逾時後繼續重查多久（小時）。缺值／看不懂一律退回預設，不停用自癒。"""
        return self._init_positive_int_param(
            _TIMEOUT_RECOVERY_WINDOW_PARAM, TIMEOUT_RECOVERY_WINDOW_HOURS_DEFAULT)

    @api.model
    def _timed_out_services_to_recheck(self):
        """自癒的候選：逾時造成的 error、仍在時間窗內、未停權、有 release 座標。

        停權（``pre_suspend_state`` 有值，#482／#497）刻意排除：停權中的 workload 是
        被正確地停著，推回 running 就是計費繞過；復權由 `_do_resume` 走自己的路。
        刪除中的服務 state 是 ``deleting``，天然不在內。
        """
        cutoff = fields.Datetime.now() - timedelta(
            hours=self._timeout_recovery_window_hours())
        return self.search([
            ('state', '=', 'error'),
            ('transition_timeout_at', '!=', False),
            ('transition_timeout_at', '>=', cutoff),
            ('pre_suspend_state', '=', False),
            ('helm_release_name', '!=', False),
            ('helm_namespace', '!=', False),
        ], order='transition_timeout_at')

    @api.model
    def _cron_recover_timed_out_services(self):
        """#1416：逾時轉 error 的服務低頻重查；release 就緒就照正常路徑推進。

        **重用** `_poll_and_update_status`（#1159 Q1「由觀測推進離開狀態」的唯一寫入者）
        而不是另寫一份判定：就緒判定、release 事實、需要 init 的進 ``initializing``
        （否則 ``running``）、Redeploy 後的 image 核對全部同一條路。頻率寫在
        ``cloud_service_cron.xml``（每 5 分）；時間窗由 `_timeout_recovery_window_hours`。

        每台服務一個回捲單位（同 `_cron_check_deploying_services`，Q36）。
        """
        services = self._timed_out_services_to_recheck()
        if not services:
            return
        from ..services.paas_operator import get_paas_operator_client

        client = get_paas_operator_client(self.env)
        if not client:
            return
        for service in services:
            service_label = (service.name, service.id)
            try:
                service._poll_and_update_status(client)
                self.env.cr.commit()
            except Exception:
                # ⚠ #1052 標記（非裁定）：逐筆 batch isolation，同短 poll 的站點。
                self.env.cr.rollback()
                _logger.exception(
                    "[#1416] Error re-checking timed-out service %s (id=%s)",
                    *service_label)

    def _note_timeout_recovery(self, next_state, timed_out_at):
        """自癒成功留紀錄：log ＋ PaaS Platform Monitor 的 chatter（#1416）。

        cloud_service 不是 mail.thread（見 platform_monitor.py 檔頭的掛點裁決），所以
        紀錄掛在只有 ``base.group_system`` 看得到的 monitor 單例上；用 ``_message_log``
        ——只留紀錄、不通知任何人。留紀錄失敗不得擋住狀態推進。
        """
        self.ensure_one()
        _logger.info(
            "[#1416] Service %s (id=%s) recovered after the deployment timeout of %s: "
            "the release became ready, state -> %s",
            self.name, self.id, timed_out_at, next_state,
        )
        try:
            with self.env.cr.savepoint():
                monitor = self.env['woow_paas_platform.platform_monitor']._singleton()
                monitor._message_log(body=_(
                    'Service %(name)s (id=%(id)s) recovered on its own after the deployment '
                    'timeout of %(timed_out_at)s UTC: the release became ready, so it moved '
                    'to %(state)s.',
                    name=self.name, id=self.id, timed_out_at=timed_out_at, state=next_state))
        except OperationalError:
            raise
        except Exception:  # noqa: BLE001 —— 見 docstring
            _logger.exception(
                "[#1416] Could not log the timeout recovery of service %s (id=%s) on the "
                "platform monitor", self.name, self.id)

    @api.model
    def _woow_migrate_refresh_transition_anchor_489(self):
        """把當下處於轉移狀態的服務的錨刷成 now（#489 上線的一次性窗口）。

        **為什麼需要**：合併前有三條轉移入口不記錨（``action_redeploy`` 的
        upgrade 分支、``_reset_admin_password``、controller 的 rollback），經它們
        進入 ``deploying`` / ``upgrading`` 的服務，錨是 ``deployed_at``＝**首裝**
        時間。`-u` 完成後第一次 cron 會拿那個陳舊的錨去判 age，於是**正在正常
        部署**的服務也會被判逾時——而誤殺不可自癒（poll 只撈轉移狀態，寫進
        ``error`` 後沒有任何路徑會把它 poll 回 ``running``）。
        （#1416 起逾時造成的 error 會被 ``_cron_recover_timed_out_services`` 在時間窗
        內重查、就緒就推回來；本 method 寫於那之前，描述的是當時的狀況。）

        本 method 給它們一個新鮮的起點，把那個窗口降到零。**代價**：真正已經
        卡住的那批（#456 / #482 那類）會晚一個門檻週期才被抓到——一次性，且
        它們本來就已經卡了不知道多久。

        只動 ``last_upgraded_at``，不動 ``deployed_at``——後者的語意是「首次
        部署時間」，還掛在前台 Overview 上，改它會說謊。錨取兩者較晚者，所以
        刷新 ``last_upgraded_at`` 就足以讓錨變新鮮。

        **只動轉移中的服務**：``running`` / ``suspended`` / ``error`` 一律不碰
        （它們的 ``last_upgraded_at`` 是真實的升級歷史，改了就是竄改紀錄）。

        **冪等**：無條件把「當下轉移中」那一批的錨寫成 now，不累加、不依賴前次
        執行結果。重跑的效果只取決於執行當下的 state 集合。

        ``active_test=False``：本 model 目前沒有 ``active`` 欄位（此 context 是
        no-op），但漏掉任何一筆就等於那筆被誤殺，所以明確寫死不靠 model 的現況。
        """
        stuck = self.sudo().with_context(active_test=False).search([
            ('state', 'in', list(_TIMEOUT_GOVERNED_STATES)),
        ])
        if not stuck:
            _logger.info(
                "#489 migration: no service in a transitional state — "
                "nothing to re-anchor"
            )
            return stuck

        stuck.write({'last_upgraded_at': fields.Datetime.now()})
        _logger.info(
            "#489 migration: re-anchored %d service(s) in a transitional "
            "state so the new deploy age timeout starts counting from now: %s",
            len(stuck),
            ', '.join(f"{s.name}(id={s.id},state={s.state})" for s in stuck),
        )
        return stuck

    @api.model
    def _woow_migrate_converge_stuck_suspended_497(self):
        """把 #482 合併前就卡死的停權服務收斂回 ``suspended``（一次性窗口）。

        **為什麼需要**：#493 從源頭修掉了「停權服務被 redeploy／reconfigure 汙染
        state」，但那只對**新**操作有效。合併前就已經被汙染的 row 仍在，而 #489／
        #491 的 age timeout 一上線就會把其中 ``deploying`` / ``upgrading`` 那批逐一
        打成 ``error``。那批的 ``pre_suspend_state`` 仍在＝:meth:`_is_halted` 仍為真
        ＝**它其實還在停權中**，於是撞上 ``subscription_lifecycle_cron`` 的分支落差：
        訂閱已恢復（``in_progress``）走分支 2，而分支 2 要求 ``state ==
        'suspended'`` 才 resume → 既不 resume 也不再 suspend ＝ **租戶付了錢、服務
        永遠回不來，且沒有任何排程會修好它**（#497）。

        **收斂集合**：``pre_suspend_state`` 有值（停權的權威旗標）且 state 落在
        :data:`_STUCK_SUSPENDED_STATES_497`。``initializing`` 也在內——容器型被
        upgrade 喚醒後（#482 的 A/B 實證），poll 會判 pod ready 而把它從
        ``deploying`` 推進 ``initializing``（:meth:`_poll_and_update_status` 不看
        :meth:`_is_halted`）。

        **不誤傷**：``pre_suspend_state`` 全 repo 只在 :meth:`_do_suspend` 寫、
        :meth:`_do_resume` 清，沒有 seed、沒有其他寫入點——所以「真部署失敗」的
        ``error`` 服務該欄位恆為 ``False``，天然落在集合外。不再疊訂閱狀態這種條件：
        那會漏掉欠費那批（它們同樣卡死，容器型還可能在燒錢）。

        **``running`` 刻意不收**：容器型被喚醒後 poll 會一路把它標成 ``running``，
        那批同樣帶著 stale 旗標，但這裡誤動的代價是**把正在跑的服務關掉**。它們交給
        放寬後的 lifecycle cron 分支 2 處理（訂閱有效 → ``_do_resume`` 清掉旗標、
        scale 1 對在跑的服務冪等＝零停機；欠費 → 分支 3／4 照舊 suspend），最壞
        延遲一個 cron 週期。

        🔴 **收斂 state 必須連 workload 一起收**：:meth:`_do_suspend` 開頭就
        ``if self.state == 'suspended': return``，所以一旦把 state 寫成
        ``suspended``，cron 的欠費分支便永遠早退——唯一還會 ``scale_release(0)`` 的
        路徑會被這個 migration 自己關掉。故先走 #493 的
        :meth:`_rescale_halted_after_upgrade`（外科式、與 chart 無關、對 VM 型冪等）
        再寫 state。

        **operator 失敗一律不 raise**：migration 拋例外＝整支 ``-u`` 失敗＝平台起不
        來。state 收斂是本地事實修正、不依賴叢集，所以 scale 失敗也照收斂（不收斂
        的話這筆 row 留在 ``error``，自癒路徑仍然是斷的）。代價是 workload 可能還在
        跑，因此**必須留下可機械盤點的記號**：``error_message`` 以
        :data:`_UNCONFIRMED_WORKLOAD_MARKER_497` 起頭，prod rollout 的 checklist 用
        它掃出「收斂了但未確認停機」的那批。

        **冪等**：收斂後的 row 是 ``suspended``，不再落在判別式內，重跑即空集合。
        """
        stuck = self.sudo().with_context(active_test=False).search([
            ('pre_suspend_state', '!=', False),
            ('state', 'in', list(_STUCK_SUSPENDED_STATES_497)),
        ])
        if not stuck:
            _logger.info(
                "#497 migration: no stuck suspended service — nothing to converge"
            )
            return stuck

        from ..services.paas_operator import get_paas_operator_client

        client = get_paas_operator_client(self.env)
        unconfirmed = self.browse()
        for service in stuck:
            has_coordinates = bool(
                client and service.helm_release_name and service.helm_namespace
            )
            confirmed = service._rescale_halted_after_upgrade(client)
            vals = {'state': 'suspended'}
            if confirmed:
                # age timeout 留下的「部署逾時」訊息會把維運指向叢集，而真因是停權。
                vals['error_message'] = False
            elif has_coordinates:
                # helper 已把具體例外寫進 error_message；套上固定前綴讓它可被盤點。
                vals['error_message'] = (
                    f'{_UNCONFIRMED_WORKLOAD_MARKER_497}：{service.error_message}'
                )
            else:
                vals['error_message'] = '%s：%s' % (
                    _UNCONFIRMED_WORKLOAD_MARKER_497,
                    _('operator client or release coordinates unavailable'),
                )
            if not confirmed:
                unconfirmed |= service
            service.write(vals)

        _logger.info(
            "#497 migration: converged %d stuck suspended service(s) back to "
            "'suspended': %s",
            len(stuck),
            ', '.join(f"{s.name}(id={s.id})" for s in stuck),
        )
        if unconfirmed:
            _logger.warning(
                "#497 migration: %d service(s) converged but their workload "
                "could not be confirmed stopped (billing bypass risk) — %s",
                len(unconfirmed),
                ', '.join(f"{s.name}(id={s.id})" for s in unconfirmed),
            )
        return stuck

    @api.model
    def _woow_migrate_first_running_at_1431(self):
        """回填 ``first_running_at``（18.0.1.0.209 新欄位）——**一律保守**。

        欄位上線前沒有可靠的「曾經 running 過」紀錄（``init_ready_*`` 會被清掉）。回填錯的
        兩個方向代價不同：

        * 把**用過**的服務當成沒用過 ⇒ 刪除後資料只留 1 天（#1431）、error 時停止扣款
          （#1424）——資料保護承諾被打破，不可逆；
        * 把**沒用過**的服務當成用過 ⇒ 照舊保留 90 天、照舊扣款——就是上線前的行為。

        所以只留下「明確還在開通中、而且沒有任何曾經跑過的跡象」的服務為空：state 是
        pending／deploying／initializing，且沒有升級紀錄（``last_upgraded_at``）、helm
        revision 沒超過 1、init 沒開過第二代、沒有停權前狀態。其餘（running、error、
        upgrading、suspended、deleting、state 為空……）一律回填。它們之後第一次進 running
        時 ``write()`` 才會寫入真正的時間。

        回填值用 ``deployed_at``（首次 helm install 成功），沒有就用 ``create_date``——只是
        近似值，沒有任何邏輯依賴它的精確時間，只依賴「有沒有值」。

        冪等：只碰 ``first_running_at`` 為空的列；重跑不改已有的值。回傳被回填的服務。
        """
        Service = self.sudo().with_context(active_test=False)
        missing = Service.search([('first_running_at', '=', False)])

        def still_provisioning(svc):
            return (
                svc.state in ('pending', 'deploying', 'initializing')
                and not svc.last_upgraded_at
                and (svc.helm_revision or 1) <= 1
                and (svc.init_generation or 0) <= 1
                and not svc.pre_suspend_state
            )

        left_empty = missing.filtered(still_provisioning)
        backfilled = missing - left_empty
        for svc in backfilled:
            svc.write({'first_running_at': svc.deployed_at or svc.create_date})
        _logger.info(
            "#1431 migration: backfilled first_running_at on %d service(s); left %d "
            "still-provisioning service(s) empty: %s",
            len(backfilled), len(left_empty),
            ', '.join(f"{s.name}(id={s.id},state={s.state})" for s in left_empty) or '-',
        )
        return backfilled

    @api.model
    def _woow_migrate_omnigent_runner_image_017(self):
        """omnigent chart 0.1.7（上游 v0.17.0）：既有服務的 ``runner.image`` 改成範本現在的釘。

        Redeploy 的 #1408（``_refresh_template_images``）只刷 ``{repository, tag|digest}``
        形式的 image（``_iter_image_refs``）；omnigent 的 ``runner.image`` 是 chart 規定的
        **字串**（launcher 原樣寫進每個 runner Job），刷不到 ⇒ 既有服務 Redeploy 後 server
        換到 0.17，雲端工作機卻停在 0.16。本 migration 補這一格。

        只改「仍是平台鏡像 ``omnigent-host`` 的字串」：``runner`` 不是 dict、``image`` 不是
        字串、指向其他 repository（有人手改過）、或 helm_values JSON 壞掉的服務一律不動並記
        log。改寫只落 DB；叢集上的 release 要等下一次 helm upgrade（管理員 Redeploy）才換。

        冪等：已等於範本值的跳過。回傳被改寫的服務。
        """
        template = self.env.ref(
            'woow_paas_platform.cloud_app_omnigent', raise_if_not_found=False)
        if not template:
            return self.browse()
        host_prefix = _OMNIGENT_HOST_IMAGE + ':'
        try:
            target = json.loads(template.helm_default_values or '{}').get('runner', {}).get('image')
        except (ValueError, AttributeError):
            target = None
        if not isinstance(target, str) or not target.startswith(host_prefix):
            _logger.warning(
                "omnigent 0.17 migration: template runner.image is not an %s pin (%r); "
                "nothing rewritten", _OMNIGENT_HOST_IMAGE, target)
            return self.browse()

        Service = self.sudo().with_context(active_test=False)
        changed = Service.browse()
        skipped = []
        for svc in Service.search([('template_id', '=', template.id)]):
            try:
                values = svc._helm_values_dict()
            except ValueError:
                skipped.append(f"{svc.name}(id={svc.id},invalid json)")
                continue
            runner = values.get('runner') if isinstance(values, dict) else None
            current = runner.get('image') if isinstance(runner, dict) else None
            if current == target:
                continue
            if not isinstance(current, str) or not current.startswith(host_prefix):
                if current is not None:
                    skipped.append(f"{svc.name}(id={svc.id},runner.image={current!r})")
                continue
            runner['image'] = target
            svc._apply_helm_values(values)
            changed |= svc
        _logger.info(
            "omnigent 0.17 migration: runner.image -> %s on %d service(s): %s; skipped: %s",
            target, len(changed),
            ', '.join(f"{s.name}(id={s.id})" for s in changed) or '-',
            ', '.join(skipped) or '-',
        )
        return changed

    def _woow_migrate_init_execution_control_1257(self):
        """#1257 上線過渡（Q15＋Q26）：回填欠 init、替在途的 init 寫合成 legacy lease。

        **Q26 回填欠 init**（開第一代）三種：
          (a) ``upgrading`` 且 ``addon_update_pending``；
          (b) ``pending``／``deploying`` 且 init 類型不是 none；
          (c) ``initializing``。
        ``error`` 的列不動（Q15）。停權期間已被吃掉 init 的服務沒有資料可推回來，
        接受這個損失——只把「init 類型不是 none 的停權服務」列進 log 交人工抽查。

        **Q15 合成 legacy**：每台 ``initializing`` 寫一筆 outcome=legacy 的 attempt
        與 1200s lease，並把 ``init_retries`` 歸 0。新舊 code 並存由 Recreate＋
        cron-wait 擋住；舊 process 還在途的 remote exec 由這個 lease 擋住。legacy
        到期**不計入** unknown 額度（見 ``_init_settle_expired_lease``）。

        冪等：已經欠 init 的列不重開一代；已有 lease 的 initializing 不再寫 legacy。
        回傳 (回填的服務, 寫了 legacy 的服務)。
        """
        Service = self.sudo().with_context(active_test=False)
        candidates = Service.search([
            ('init_pending', '=', False),
            '|', '|',
            '&', ('state', '=', 'upgrading'), ('addon_update_pending', '=', True),
            ('state', 'in', ('pending', 'deploying')),
            ('state', '=', 'initializing'),
        ]).filtered(lambda s: s._init_type() != 'none')
        now = self._init_db_now()
        for svc in candidates:
            # 開第一代：計數一併歸零（舊 code 的 init_retries 不帶進新的一代）。
            svc.write({
                'init_pending': True,
                'init_generation': (svc.init_generation or 0) + 1,
                'init_retries': 0,
                'init_unknown_count': 0,
            })

        legacy = Service.search([
            ('state', '=', 'initializing'),
            ('init_lease_until', '=', False),
        ]).filtered(lambda s: s._init_type() != 'none')
        Attempt = self.env['woow_paas_platform.cloud_service_init_attempt'].sudo()
        lease_until = now + timedelta(seconds=LEGACY_LEASE_SECONDS)
        for svc in legacy:
            seq = (svc.init_attempt_seq or 0) + 1
            attempt = Attempt.create({
                'service_id': svc.id,
                'generation': svc.init_generation,
                'sequence': seq,
                'init_type': svc._init_type(),
                'claimed_at': now,
                'lease_until': lease_until,
                'outcome': OUTCOME_LEGACY,
            })
            svc.write({
                'init_attempt_seq': seq,
                'init_attempt_id': attempt.id,
                'init_lease_until': lease_until,
                'init_wait_since': now,
                'init_retries': 0,
            })

        suspended = Service.search([
            ('state', '=', 'suspended'),
        ]).filtered(lambda s: s._init_type() != 'none')
        _logger.info(
            "#1257 migration: owed init backfilled on %d service(s): %s; legacy "
            "lease (%ss) written for %d initializing service(s): %s",
            len(candidates),
            ', '.join(f"{s.name}(id={s.id},state={s.state})" for s in candidates) or '-',
            LEGACY_LEASE_SECONDS, len(legacy),
            ', '.join(f"{s.name}(id={s.id})" for s in legacy) or '-',
        )
        if suspended:
            # Q26：停權期間被吃掉的 init 無資料可推 ⇒ 交人工抽查。
            _logger.warning(
                "#1257 migration: %d suspended service(s) with a post-deploy init "
                "type — an init swallowed by an earlier suspension cannot be "
                "reconstructed; review manually: %s",
                len(suspended),
                ', '.join(f"{s.name}(id={s.id},pre_suspend_state={s.pre_suspend_state})"
                          for s in suspended),
            )
        return candidates, legacy

    @staticmethod
    def _extract_import_progress(status):
        """取 CDI DataVolume 的 import 進度字串（供前台進度顯示，design §7.4）。

        回 None 代表沒有進度可顯示——**呼叫端不得據此判斷就緒**
        （``datavolume_retrieval_error`` 時這裡就會回 None）。

        多顆 DataVolume 時取「仍在 import 的那顆」而非第一顆：v1 每台 VM 只有
        一顆 disk，但 chart 日後若加第二顆（例如獨立 data volume），先完成的
        那顆會是 100% 而遮住仍在跑的，進度條就會卡在 100% 不動。優先取
        ``ImportInProgress``，再退回百分比最小者。
        """
        dvs = status.get('datavolumes') or []
        in_progress = [
            dv for dv in dvs if dv.get('phase') == 'ImportInProgress'
        ]
        candidates = in_progress or dvs

        def _pct(dv):
            raw = (dv.get('progress') or '').rstrip('%')
            try:
                return float(raw)
            except ValueError:
                return None

        usable = [
            (pct, dv.get('progress'))
            for dv in candidates
            for pct in [_pct(dv)]
            if pct is not None
        ]
        if usable:
            return min(usable)[1]

        # 沒有可解析的百分比就退回第一個非 N/A 的原始字串（保守）。
        for dv in candidates:
            progress = dv.get('progress')
            if progress and progress != 'N/A':
                return progress
        return None

    def _store_import_progress(self, status):
        """把 import 進度寫進記錄供前台顯示（兩層 poll 共用）。

        只在值真的改變時 write，避免每次 poll 都髒一次記錄。就緒後清空
        （避免卡片一直掛著 "100.0%"）。
        """
        self.ensure_one()
        progress = self._extract_import_progress(status)
        if (self.import_progress or None) != progress:
            self.sudo().write({'import_progress': progress or False})

    def _release_facts(self, release, dns_state=None, tunnel_address=None, tunnel_error=None):
        """operator 回報的 release 事實 → 帳本回寫欄位。**所有寫入者的單一來源。**

        （#1159 起 list/get 純讀、controller 那份 poll 已刪除；以下是 #627 當時的背景。）
        #627：poll 有**兩份實作** —— `PaasController._update_service_status`
        （租戶打 list／detail 端點時走）與本模組的 `_poll_and_update_status`
        （**cron** 走）。原本兩份各自組自己的 write vals，結果 controller 那份
        寫了 `helm_chart_version`、cron 這份只寫 `helm_revision`，而唯一斷言
        「`release_facts` 必須到得了每個 write 分支」的測試**整份只驅動
        controller** ⇒ cron 那份整份沒有回寫也全綠（#552 層① 的第五條回寫路徑
        因此在無人值守時失效）。

        ⇒ **欄位集合收斂在這裡**：兩份實作都必須經由本方法取得 write vals。日後
        新增第三個 release 事實欄位時，兩份會同時跟上，不會再各漏各的。

        ⚠ 這裡刻意**只收斂「寫什麼」，不收斂「什麼時候寫」** —— 兩份的控制流
        （post-deploy init 的兩套實作、404／`deleting` 分支的 savepoint 差異，見
        `tests/test_poll_teardown_rollback_525.py`）差異是另一件事，收斂它的爆炸
        半徑遠大於本修法，見 #627 的討論。

        `or self.helm_chart_version`＝舊 operator 不回這個欄位（或回空字串）時
        **保留帳本原值**：未知不得覆蓋成空。
        """
        self.ensure_one()
        vals = {
            'helm_revision': release.get('revision', self.helm_revision),
            'helm_chart_version': (
                release.get('chart_version') or self.helm_chart_version
            ),
            # #627 (i)：`last_release_sync_at` 也走這個咽喉 ⇒ **三個寫入者**
            # （#1159 前是兩份 poll ＋ reconciliation cron；之後是背景 poll ＋ reconciliation cron）
            # 自動全部 stamp，不必逐路徑補。
            # 這正是本方法存在的理由，也避免這個新欄位重演 #627。
            #
            # ⚠ 語意是「最後一次**問過** operator」，不是「最後一次值有變」——
            # 所以無漂移時 reconciliation cron 仍會寫它（見該 cron 的 docstring）。
            # ⚠ 必須 `fields.Datetime.now()`（UTC naive）不能 `datetime.now()`
            # （本機時區）；repo-lint 的 transition-anchor-utc 已把本欄位納入覆蓋。
            'last_release_sync_at': fields.Datetime.now(),
        }
        # #648 (b)：DNS 觀測結果也走這個咽喉，理由與上面那三個一樣——不然它會
        # 變成「逐 write 分支各列一次」，正是 #627 的成因。
        #
        # 只有**探過而且有結論**（ok／missing）才寫。`unknown`（resolver 自己
        # 不可用）刻意不動旗標：那是平台故障，把它顯示成「你的網址還沒生效、請
        # 清本機 DNS 快取」是給租戶錯的指示；`skip`（沒有對外網址／閘關掉）更
        # 不該留下任何痕跡。
        if dns_state in ('ok', 'missing'):
            vals['public_url_dns_unverified'] = dns_state == 'missing'
        # 對外位址同樣走這個咽喉（理由同上：逐 write 分支各列一次正是 #627）。
        # `None` ＝ 這一輪沒探（商品沒這能力／operator 掛了）⇒ 帳本原值不動；
        # `''` ＝ 探過而且現在沒有位址 ⇒ 清掉（留著一個連不上的舊位址比空值更糟）。
        if tunnel_address is not None:
            vals['public_tunnel_url'] = tunnel_address or False
            # #1559：原因只描述「現在沒有位址」——有位址就清掉；同一個「探過才寫」的閘門。
            vals['public_tunnel_error'] = (
                False if tunnel_address else (tunnel_error or False))
        return vals

    def _poll_and_update_status(self, client):
        """背景短 poll：向 operator 取一次 status，推進單一服務的轉移狀態。

        #1159 Q1：這是「由觀測推進、離開轉移狀態」的**唯一**寫入者——transition 完成、
        init 分派、deleting 404→unlink 都只在這裡寫；list/get 自 #1159 起純讀。生產呼叫端
        是 ``_cron_check_deploying_services``（轉移狀態）與 #1416 的
        ``_cron_recover_timed_out_services``（逾時造成的 ``error``，標記
        ``transition_timeout_at``），兩者都 per-service commit。後者就緒時走與轉移狀態
        完全相同的推進路徑；未就緒時什麼都不改（``error`` 不受 age timeout 管轄）。

        它**不執行 init**（Q4：init 不與短 poll 同 job、同交易）：觀測到 initializing
        的 pods ready 時只記下來、trigger init cron；init 由 ``_cron_run_post_deploy_inits``
        經單一入口 ``_run_post_deploy_init`` 執行。
        """
        self.ensure_one()
        from ..services.paas_operator import PaaSOperatorError

        original_state = self.state

        try:
            status = client.get_status(
                namespace=self.helm_namespace,
                release_name=self.helm_release_name,
                # VM 型必須帶 include_vm，否則 operator 不回 vmis，
                # 而 _is_vm_ready 是 fail-closed → 服務永遠卡 deploying。
                include_vm=self._needs_vm_status(),
            )
        except PaaSOperatorError as e:
            if e.status_code == 404 and self.state == 'deleting':
                # #1159 Q1：deleting 的收尾（release 已不在叢集 → unlink，含訂閱結算與
                # quota 回推）自本票起只在這裡做。#1159 以前這條分支不可達（cron 的
                # domain 不含 deleting，只有 controller 的 list poll 會收），本票把
                # deleting 納入 cron domain、第一次啟用它。
                #
                # #525 B5：`unlink()` 是多步驟 teardown，中途拋錯就留下刪一半的狀態。
                # savepoint 只回捲這一次 teardown、例外照常外拋：呼叫端 cron 迴圈會
                # rollback 這一台、繼續下一台，服務留在 deleting、下一輪再試。形態同
                # `subscription_lifecycle_cron._cron_enforce_subscription_lifecycle`
                # 的 per-item savepoint。
                with self.env.cr.savepoint():
                    self.unlink()
            elif e.status_code == 404 and original_state == 'initializing':
                # #1257 Q8：initializing 一定會結束。release 被外部刪掉時 poll 永遠拿
                # 不到 status，只剩等待時限能收尾。operator 連不上等其他錯誤是平台
                # 故障，不計時（#1159 Q10「平台容量排隊不計時」同理），交給監控。
                _logger.warning("Error polling status for %s: %s", self.name, e)
                # 與 not-ready 分支一致：release 不在 ⇒ 不是 ready，退出 init cron 的
                # 佇列；先收斂在途 attempt 的過期 lease，再判時限。
                self._init_mark_not_ready()
                self._init_settle_expired_lease()
                if self.state == 'initializing':
                    self._init_check_wait_timeout()
            elif e.status_code == 404 and original_state == 'error':
                # #1416：release 已不在叢集 ⇒ 不會再就緒，停止自癒重查（error 與訊息照舊，
                # 只撤標記）。
                _logger.info(
                    "[#1416] Release of timed-out service %s (id=%s) is gone — no "
                    "longer re-checking it", self.name, self.id)
                self.sudo().write({'transition_timeout_at': False})
            else:
                _logger.warning("Error polling status for %s: %s", self.name, e)
            return
        except Exception as e:
            _logger.warning("Error polling status for %s: %s", self.name, e)
            return

        release = status.get('release', {})
        release_status = release.get('status', '')
        # Re-fetch for optimistic locking
        service = self.browse(self.id)
        if not service.exists() or service.state != original_state:
            return
        # #1416：error 只有逾時造成、且標記還在的才推（別的路徑同時改寫了 error_message
        # ＝來源已換，標記被 write() 清掉）。停權判準同 `_timed_out_services_to_recheck`。
        timed_out_at = service.transition_timeout_at
        if original_state == 'error' and (not timed_out_at or service._is_halted()):
            return

        # #1159 Q1／Q40：list/get 只回這個「最近觀測」。拿到 status 就 stamp，不論
        # 這一輪有沒有推進 state（沒推進正是「最後更新 X 前」要揭露的情形）。
        # episode 保持命令側等待邊界；consumer 比較它與本次真觀測，不以命令時間偽造證據。
        service.write({'observed_at': fields.Datetime.now()})
        if original_state == 'deleting':
            # #1159 Q1：deleting 只由上面的 404 收尾。release 還在時什麼都不推——
            # #1159 以前 controller 的 list poll 會讓它走下面的就緒判定、被寫回
            # running（刪除中的服務「復活」）。uninstall 一直沒生效的情形由 #1279 追。
            return

        # #627：欄位集合來自 `_release_facts`（#1159 起本方法是唯一的 poll 實作），
        # **不要**在下面的 write 分支裡各自列欄位——#627 就是兩份 poll 各列一次、
        # 其中一份漏掉 `helm_chart_version`。
        # #648 (b)：DNS 現況**一輪只探一次**，同時餵給就緒判定與 `_release_facts`
        # （旗標也走那個咽喉，理由與其他 release 欄位一樣——逐 write 分支各列一次
        # 正是 #627 的成因）。只在 release 已 `deployed` 時才探：還沒 deploy 的服務
        # 探了也不會影響任何判定，而每次探測在 resolver 不可用時都是一段阻塞，
        # cron 逐台序列跑（prod `max_cron_threads=2`，一條 thread 同時只跑一個 job）。
        dns_state = (
            service._public_url_dns_state() if release_status == 'deployed' else None
        )

        # 與 DNS 同一條紀律：**一輪只探一次**，結果餵進 `_release_facts`。
        # 只在 release 已 deployed 時探——還沒 deploy 的服務裡根本還沒有 agent。
        tunnel_address, tunnel_error = (
            service._probe_public_tunnel(client)
            if release_status == 'deployed' else (None, None)
        )

        release_facts = service._release_facts(
            release, dns_state=dns_state, tunnel_address=tunnel_address,
            tunnel_error=tunnel_error,
        )

        # 進度先落地（與就緒判定無關，且未就緒時才是最有用的時候）。
        service._store_import_progress(status)

        # #489：「這一輪 poll 沒能讓服務離開轉移狀態」的理由，供 age timeout
        # 組診斷訊息。None ＝ 這一輪有進展（就緒或明確失敗），不必判逾時。
        stuck_detail = None
        ready_for_init = False

        if release_status == 'deployed':
            all_ready, detail = service._is_release_ready(status, dns_state=dns_state)
            if not all_ready:
                service._log_not_ready(detail)
                stuck_detail = detail

            if all_ready:
                # #1267：去向只由欠 init 標記決定（與 controller 那份共用同一判準）。
                next_state = service._next_state_when_ready()
                if next_state != original_state:
                    # 進 initializing 時的等待錨點由 write() 集中 stamp（Q10）；離開 error
                    # 時逾時標記也由 write() 清（#1416）。
                    service.write({
                        'state': next_state,
                        **release_facts,
                        'error_message': False,
                    })
                if original_state == 'error':
                    service._note_timeout_recovery(next_state, timed_out_at)
                if next_state == 'initializing':
                    # #1159 Q1「init 分派」：只排進 init cron 的佇列，不在這裡執行。
                    ready_for_init = True
                    service._init_mark_ready_observed()
                # #1408：Redeploy 後第一次就緒——核對 workload 真的換到範本 image。
                if service.sudo().redeploy_verify_pending:
                    service._verify_redeploy_images(status.get('pods'))

        elif release_status == 'failed':
            service.write({
                'state': 'error',
                **release_facts,
                'error_message': release.get(
                    'description', _('Deployment failed')),
            })

        elif service._is_timeout_governed():
            # `pending-install` / `pending-upgrade` / `pending-rollback` /
            # `unknown`。既有碼對這些一律靜置等下一輪——**但它們同樣會永久卡**：
            # helm 的 subprocess 被 SIGKILL 在 rollback 途中就會把 release 留在
            # `pending-rollback`，沒有任何東西會再推它（同
            # `docs/reference/gotchas/helm-atomic-failure-double-timeout.md` 的實錄）。故一併
            # 納入封頂：狀態不動，只是讓 age timeout 有話可說。
            # （限縮在受管狀態，避免給 `deleting` / `initializing` 添 log 噪音。）
            #
            # ⚠ 刻意**不**加 `HARD:` 前綴：`pending-install` 在正常首裝時就會被
            # 撞到（helm 還在跑、cron 已經在 poll），升 warning 會讓每一次正常
            # 部署都刷一行「硬失效」——那正好稀釋掉 HARD 的語意（它專指「不會
            # 自己好」）。卡住與否由時間分辨，逾時本身會印 error 級。
            stuck_detail = f"helm release status is {release_status or 'unknown'}"
            service._log_not_ready(stuck_detail)

        if stuck_detail:
            service._check_transition_timeout(stuck_detail)

        if (original_state == 'initializing' and not ready_for_init
                and service.state == 'initializing'):
            service._init_mark_not_ready()
            # pods 沒 ready 時 init cron 不會 claim：在途 attempt 的過期 lease 也要在
            # 這裡收斂，否則之後的逾時會被記成 wait_timeout（實際上是 unknown），擋住
            # Q30 出口。
            service._init_settle_expired_lease()
            if service.state == 'initializing':
                # Q10：等 readiness 受時限約束（#489 age timeout 不管 initializing）；
                # #1159 D1-R：正式 fast-fail 接入位置；#1192 未接入前不使用累計重啟代判。
                if not service._init_check_fast_fail(status):
                    service._init_check_wait_timeout()

    # ------------------------------------------------------------------ #
    #  #1257：post-deploy init 的執行管控                                  #
    # ------------------------------------------------------------------ #
    #
    # 設計權威：docs/decisions/refactor/post-deploy-init-execution-model.md。
    # 本段是 Odoo／n8n 共用的**唯一**執行入口與它的守衛；各自的 init 內容（operator
    # 端點、憑證規則）仍分開。controller 自 #1257 起不再有 init 流程。

    def _observation_freshness(self):
        """回傳觀測時計的 ``(pending, started_at)``，供 payload 與 monitor 對帳。

        observed_at 必須不早於本次 episode 才是目前觀測；舊列先用既存證據，直到
        背景 monitor 補上一個有界初始 episode。本方法純讀，list/get 不因此寫入。
        """
        self.ensure_one()
        episode = self.observation_episode_started_at
        observed = self.observed_at
        pending = bool(episode and (not observed or observed < episode))
        if pending:
            return True, episode
        return False, observed or episode or self.write_date

    def _init_type(self):
        """本服務的 post-deploy init 類型；不認得的值一律當 ``none``。"""
        self.ensure_one()
        init_type = self.template_id.post_deploy_init_type
        return init_type if init_type in INIT_LEASE_SECONDS else 'none'

    def _init_db_now(self):
        """DB 時鐘（UTC naive）——Q2「時鐘一律用 DB」。

        用 ``statement_timestamp()`` 而不是 ``now()``：``now()`` 是**交易開始**時間，
        cron 迴圈的長交易裡會落後，拿它判 lease 會把剛到期的 lease 當成還在。
        """
        self.env.cr.execute("SELECT statement_timestamp() AT TIME ZONE 'UTC'")
        return self.env.cr.fetchone()[0]

    def _init_positive_int_param(self, key, default):
        """同 ``_transition_timeout_minutes``：缺值／看不懂一律退回預設，不停用守線。"""
        raw = self.env['ir.config_parameter'].sudo().get_param(key)
        unset = raw in (None, False, '') or not str(raw).strip()
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            if not unset:
                _logger.warning(
                    "%s is %r (not a positive integer) — falling back to the "
                    "built-in default of %s", key, raw, default,
                )
            return default
        return value

    def _init_unknown_limit(self):
        return self._init_positive_int_param(
            _INIT_UNKNOWN_LIMIT_PARAM, INIT_UNKNOWN_LIMIT_DEFAULT)

    def _init_wait_timeout_minutes(self):
        return self._init_positive_int_param(
            _INIT_WAIT_TIMEOUT_PARAM, INIT_WAIT_TIMEOUT_MINUTES_DEFAULT)

    def _open_init_generation(self):
        """Q24：開新的一代——**唯一**開代入口。

        會開新一代：create（init 類型不是 none）、addon 變更、人工 redeploy、
        **還欠 init 時的** rollback（M2 裁定 (A)）、從額度出口的 error 出發的
        reconfigure（#1159 Q30），以及 #1222 的 digest 升級（將來直接呼叫本方法）。
        **不**開：resume、reset、不改 addon 的 reconfigure、init 已完成服務的
        rollback、restore finalize（待 #1213 確認）。
        init 類型為 none 的服務不欠 init ⇒ no-op。
        """
        for svc in self:
            if svc._init_type() == 'none':
                continue
            # 管控欄位只有平台寫得進去（`_reject_init_control_writes`）；呼叫端可能是
            # 租戶身分的 reconfigure／rollback。
            svc.sudo().write({
                'init_pending': True,
                'init_generation': (svc.init_generation or 0) + 1,
                'init_retries': 0,
                'init_unknown_count': 0,
                'init_exit': False,
                'init_error': False,
                'init_wait_spent_seconds': 0,
                'init_wait_started_at': False,
                'init_retry_eligible_at': False,
                'init_queue_turn': False,
                # 新一代的 n8n owner 確認窗口重新起算：停權中 redeploy 或開代後在部署階段
                # 失敗時不會再「進入 initializing」，若不在此清，owner 監控會拿前一代的舊
                # 錨點對新一代誤報。init_ready_since 空著時由下一次短 poll 觀測寫回（排序以
                # init_queue_turn 為先，claim／capacity 不讀它）。init_ready_observed 刻意不清：
                # 它的讀者都限定 state='initializing'，而在 initializing 中開代（P1-1）時
                # 仍需立即可 claim；離開後再進 initializing 由 write() 清掉。
                'init_ready_since': False,
                'n8n_owner_alerted_at': False,
            })

    def _init_lease_active(self):
        """lease 是否仍在（DB 時鐘）。過期但還沒收斂的 lease 視為已結束。

        只讀：HTTP 入口（reconfigure／rollback）與後台 redeploy 用它擋，不寫 claim（Q36）。
        """
        self.ensure_one()
        self.flush_recordset(['init_lease_until'])
        self.env.cr.execute(
            f"SELECT init_lease_until > (statement_timestamp() AT TIME ZONE 'UTC') "
            f"FROM {self._table} WHERE id = %s",
            (self.id,),
        )
        row = self.env.cr.fetchone()
        return bool(row and row[0])

    def _next_state_when_ready(self):
        """pods ready 之後服務該去哪——背景 poll 的唯一判準（#1159 起 list/get 純讀，不再 poll）。

        #1267：需要 init 與否**只**看欠 init 標記，不再看 ``addon_update_pending``。
        reconfigure 不改 addon 時會把後者覆寫成 False、rollback 根本不設它，兩條路都
        曾讓欠 init 的服務直接變成 running。
        """
        self.ensure_one()
        if self._init_type() != 'none' and self.init_pending:
            return 'initializing'
        return 'running'

    def _init_guarded(self, query, params):
        """savepoint 內執行一段會拿 row lock 的 SQL，回傳 ``fetchall()``。

        Q2：撞到 40001（REPEATABLE READ 下別人剛改過這列）或 ``lock_timeout`` 視為
        「沒拿到」，回傳 ``None``，**不算 job 失敗**。
        """
        cr = self.env.cr
        # raw SQL 讀不到 ORM 還沒 flush 的值；不依賴呼叫端剛好先 commit 過。
        self.flush_recordset()
        try:
            # 同 core ir_cron 取鎖的做法：預期中的衝突不該在 sql_db 印成 ERROR「bad query」。
            with mute_logger('odoo.sql_db'), cr.savepoint(flush=False):
                cr.execute("SET LOCAL lock_timeout = %s", (_INIT_CLAIM_LOCK_TIMEOUT,))
                cr.execute(query, params)
                rows = cr.fetchall()
                cr.execute("SET LOCAL lock_timeout = DEFAULT")
                return rows
        except (pg_errors.SerializationFailure, pg_errors.LockNotAvailable) as exc:
            _logger.info(
                "[#1257] init lease row busy for service %s (%s) — treated as not "
                "acquired", self.id, type(exc).__name__,
            )
            return None

    def _init_to_error(self, exit_kind, message):
        """init 的三個出口（Q8）都在這裡轉 error；``init_pending`` 保留（仍欠 init）。"""
        self.ensure_one()
        self.sudo().write({
            'state': 'error',
            'init_exit': exit_kind,
            'error_message': message,
        })
        # 平台告警（Q8「用完轉 error 並告警平台」）：log ＋ #1159 Q38 的 mail.activity。
        # unknown 額度用完由監控的「24h unknown 次數」涵蓋；這裡追加等待類的放棄。
        _logger.error(
            "[#1257] post-deploy init gave up for service %s (id=%s, generation=%s): "
            "exit=%s", self.name, self.id, self.init_generation, exit_kind,
        )
        if exit_kind in ('wait_timeout', 'fast_fail'):
            self.env['woow_paas_platform.platform_monitor']._report_gave_up(
                self, exit_kind, message)

    def _init_check_exhausted(self, failed_step=None):
        """額度已用完（例如 unknown 在停權期間才被收斂計數）⇒ 轉 error。

        ``failed_step``：剛記下的那次確定失敗是在哪一步（#1534：owner 帳號類的失敗給專屬說明）。
        """
        self.ensure_one()
        if self.init_retries >= INIT_FAILED_LIMIT:
            self._init_to_error('failed', self._init_failed_message(failed_step))
            return True
        if self.init_unknown_count >= self._init_unknown_limit():
            self._init_to_error('unknown', self._init_unknown_message())
            return True
        return False

    def _init_failed_message(self, failed_step=None):
        if self._init_type() == 'n8n' and failed_step in _N8N_OWNER_FAILURE_STEPS:
            # #1534：n8n 本身照常運作，只是平台認不得現在的 owner——說清楚是哪件事、該找誰。
            return _(
                'The platform could not confirm the n8n administrator account: the account '
                'in n8n is no longer the one set when this service was created (its email or '
                'password was changed, or two-step verification was turned on, before the '
                'platform finished setting it up), or someone else completed the n8n setup '
                'first. The n8n service itself is still running. Please contact support to '
                'verify the account (attempt #%(seq)s).', seq=self.init_attempt_seq)
        return _(
            'Initialization failed %(count)s times (attempt #%(seq)s). Please '
            'contact support.', count=self.init_retries, seq=self.init_attempt_seq)

    def _init_unknown_message(self):
        return _(
            'The initialization result could not be confirmed %(count)s times '
            '(attempt #%(seq)s). Please contact support.',
            count=self.init_unknown_count, seq=self.init_attempt_seq)

    def _init_phase(self):
        """Q40：initializing 的細分（不新增 state 值）；非 initializing 回 False。

        - ``running``：lease 在途且那次 attempt 還沒結束（含 migration 的合成 legacy）。
        - ``waiting_ready``：背景還沒觀測到 pods ready。
        - ``retrying``：本代已有確定失敗或結果未知（含 unknown 之後刻意留到到期的 lease）。
        - ``queued``：pods ready，排在 init cron 的佇列裡。

        只讀：list/get 呼叫它（#1159 Q1 純讀）。
        """
        self.ensure_one()
        if self.state != 'initializing':
            return False
        if self._init_lease_active():
            attempt = self.sudo().init_attempt_id
            if attempt and not attempt.finished_at:
                return 'running'
        if not self.init_ready_observed:
            return 'waiting_ready'
        if self.init_retries or self.init_unknown_count:
            return 'retrying'
        return 'queued'

    def _init_reconfigure_exit(self):
        """#1159 Q30：這個 error 是不是 init 的**額度出口**（租戶唯一的出口是 reconfigure）。

        只有確定失敗／結果未知的額度用完才是；helm 失敗、等待逾時、fast-fail（D1）都不是。
        controller 的 reconfigure 守衛與前端按鈕讀同一個判準。
        """
        self.ensure_one()
        return self.state == 'error' and self.init_exit in ('failed', 'unknown')

    def _upgrade_failure_reconfigure_exit(self):
        """#1552：這個 error 是不是背景 upgrade 沒有確認套用造成的（出口＝重新送出修改配置）。

        controller 的 reconfigure 守衛與前端按鈕（``upgrade_retry_allowed``）讀同一個判準。
        """
        self.ensure_one()
        return self.state == 'error' and bool(self.upgrade_failed_at)

    def _upgrade_failure_message(self, exc, plan_change=False):
        """#1552：背景 upgrade 失敗時寫進 ``error_message`` 的說明（服務建立者的語言）。

        背景執行緒的 env 沒有語言 ⇒ 舊碼的 ``_()`` 一律英文、還把 operator 的原始例外
        （``('Connection aborted.', RemoteDisconnected(...))``）原樣給租戶看。這裡依失敗種類
        說清楚「新設定有沒有套用」與「下一步」：

        - 連線中斷／逾時：不確定有沒有套用 ⇒ 請重新送出；
        - operator 忙碌（503 ``operator_busy``）：確定沒有套用 ⇒ 請重新送出；
        - 其他 operator 錯誤：附上 operator 的說明（operator 端已是中文）。
        """
        self.ensure_one()
        from ..services.paas_operator import (
            OPERATOR_BUSY_ERROR_CODE,
            PaaSOperatorConnectionError,
            PaaSOperatorTimeoutError,
        )
        lang = self.sudo().create_uid.lang or self.env.lang
        env = self.with_context(lang=lang).env
        detail = getattr(exc, 'detail', None)
        if isinstance(exc, PaaSOperatorConnectionError):
            reason = env._(
                'The connection to the deployment service was interrupted, so it is not '
                'certain whether the new settings were applied.')
        elif isinstance(exc, PaaSOperatorTimeoutError):
            reason = env._(
                'The deployment service did not answer in time, so it is not certain '
                'whether the new settings were applied.')
        elif isinstance(detail, dict) and detail.get('error_code') == OPERATOR_BUSY_ERROR_CODE:
            reason = env._(
                'The deployment service was busy with other changes, so the new settings '
                'were not applied.')
        else:
            text = detail.get('message') if isinstance(detail, dict) else detail
            reason = env._(
                'The deployment service could not apply the new settings: %(detail)s',
                detail=str(text or getattr(exc, 'message', None) or exc)[:500])
        if plan_change:
            return env._(
                'The plan was changed, but resizing the service did not complete. '
                '%(reason)s Submit the configuration again to retry, or contact support.',
                reason=reason)
        return env._(
            '%(reason)s Submit the configuration again to retry. If it keeps failing, use '
            'Rollback or contact support.',
            reason=reason)

    def _init_mark_ready_observed(self):
        """短 poll 觀測到 initializing 的 pods ready ⇒ 排進 init cron 的佇列（#1159 Q1）。

        D2（#1159 實作期裁定，擴充 Q10 錨點字面）：排隊等 init cron 屬「平台容量排隊」，
        **不計時** ⇒ 等待錨點前推到這次觀測（錨點＝max(進入 initializing, 上次實際執行的
        attempt 結束, 最近一次觀測到 ready)）。只有「由 not ready 轉成 ready」這一刻才
        trigger init cron：每分鐘都 trigger 只會堆 ``ir.cron.trigger``，積壓由 init cron
        自己 ``_trigger()``。
        """
        self.ensure_one()
        newly_ready = not self.init_ready_observed
        now = self._init_db_now()
        vals = {'init_ready_observed': True, 'init_wait_since': now}
        if not self.init_ready_since:
            vals['init_ready_since'] = now
        if not self.init_queue_turn:
            vals['init_queue_turn'] = now
        self.sudo().write(vals)
        if newly_ready:
            # Q28a：n8n 在 owner 確認前公開可達，不讓它多等一個 cron 週期。
            self.env.ref(_INIT_CRON_XMLID)._trigger()

    def _init_mark_not_ready(self):
        """短 poll 觀測到 initializing 的 pods 不 ready ⇒ 退出 init cron 的佇列。"""
        self.ensure_one()
        if self.init_ready_observed:
            self.sudo().write({'init_ready_observed': False})

    @staticmethod
    def _init_fast_fail_pod(status):
        """#1159 D1-R：保留 #1192 正式訊號的接入位置，本版不以累計 restarts 代判。

        PodInfo.restarts 是整個 Pod 的歷史累計，不能證明本次初始化失敗。
        正式 initContainer／CrashLoopBackOff 契約尚未接入前不短路，仍走既定等待時限；
        fast_fail 出口與消費端保留，供歷史記錄與 #1192 後續使用。
        """
        return None

    def _init_check_fast_fail(self, status):
        """#1159 D1：fast-fail 訊號短路等待 → error（``init_exit='fast_fail'``，不耗額度）。

        lease 在途時不判（同 ``_init_check_wait_timeout``：那段時間由 lease／額度收尾）。
        回傳是否已轉 error。
        """
        self.ensure_one()
        if self.state != 'initializing' or self._init_lease_active():
            return False
        hit = self._init_fast_fail_pod(status)
        if not hit:
            return False
        pod, restarts = hit
        self._init_to_error('fast_fail', _(
            'Pod %(pod)s restarted %(count)s times without becoming ready, so '
            'initialization stopped waiting. Please contact support.',
            pod=pod, count=restarts))
        return True

    def _init_operator_wait_elapsed(self, now=None):
        """D2-R：目前這段 not_executed cooldown 已累計的秒數（不含容量排隊）。"""
        self.ensure_one()
        if not self.init_wait_started_at:
            return 0
        now = now or self._init_db_now()
        until = self.init_retry_eligible_at or now
        return max(0, (min(now, until) - self.init_wait_started_at).total_seconds())

    def _init_check_operator_wait_timeout(self):
        """D2-R：不依賴 client／Ready 的 operator readiness 預算收尾。"""
        self.ensure_one()
        if self.state != 'initializing' or self._init_lease_active():
            return False
        spent = (self.init_wait_spent_seconds or 0) + self._init_operator_wait_elapsed()
        if spent < self._init_wait_timeout_minutes() * 60:
            return False
        self._init_to_error('wait_timeout', _(
            'The operator kept reporting that initialization was not executed for '
            '%(minutes)s minutes.', minutes=self._init_wait_timeout_minutes()))
        return True

    @api.model
    def _init_settle_operator_wait_timeouts(self):
        """cron 全域到期掃描：缺 client 或 Ready 觀測不能遮蔽已耗盡的時計。

        只由 init cron 呼叫（會 commit）。Q36：每台一個回捲單位——一台轉 error 或告警時
        撞到 DB 故障，只回捲那一台，前面已收尾的服務保留、後面照常收尾，整趟 init tick
        也不因此中斷（之後的佇列照常處理）。
        """
        services = self.sudo().search([
            ('state', '=', 'initializing'),
            '|', ('init_wait_started_at', '!=', False), ('init_wait_spent_seconds', '>', 0),
            ('init_lease_until', '=', False),
        ])
        for service in services:
            service_label = (service.name, service.id)
            try:
                service._init_check_operator_wait_timeout()
                self.env.cr.commit()
            except Exception:
                # Q36：回捲這一台未 commit 的收尾，下一趟再判；後排照常處理。
                # ⚠ #1052 標記（非裁定）：這是逐筆 batch isolation，OperationalError 也在此
                # 收掉，與 post-commit-bookkeeping-rollback-unit.md 規則一分歧（同短 poll 的站點）。
                self.env.cr.rollback()
                _logger.exception(
                    "[#1159] init wait-timeout settle failed for service %s (id=%s)",
                    *service_label)

    def _init_check_wait_timeout(self):
        """Q10：等待時限到期 → error，**不耗額度**。回傳是否已轉 error。

        lease 在途時不判（那段時間由 lease／額度收尾）。錨點缺值（理論上只有
        migration 以前的列）就從現在開始計，不當成已逾時。
        """
        self.ensure_one()
        if self.state != 'initializing' or self._init_lease_active():
            return False
        now = self._init_db_now()
        if not self.init_wait_since:
            self.sudo().write({'init_wait_since': now})
            return False
        minutes = self._init_wait_timeout_minutes()
        if now - self.init_wait_since < timedelta(minutes=minutes):
            return False
        self._init_to_error('wait_timeout', _(
            'The application did not become ready for initialization within '
            '%(minutes)s minutes.', minutes=minutes))
        return True

    def _init_settle_expired_lease(self):
        """lease 到期的收尾（Q8），一次只有一條連線贏得收尾；結束時 commit。

        - 在途（沒有 outcome）的 attempt → unknown，計入本代 unknown 額度；
          合成 legacy（Q15）與舊一代的 attempt 不計。
        - 已經有 outcome 的（unknown／partial 刻意留著 lease 等它到期）→ 只釋放。
        """
        self.ensure_one()
        cr = self.env.cr
        # lease 到期清理前先補套已 commit 的結果，保住 worker crash 後的 D2-R 記帳。
        if self._init_recover_recorded_attempt():
            cr.commit()
            return
        rows = self._init_guarded(
            f"""SELECT init_attempt_id, COALESCE(init_generation, 0)
                  FROM {self._table}
                 WHERE id = %s AND init_lease_until IS NOT NULL
                   AND init_lease_until <= (statement_timestamp() AT TIME ZONE 'UTC')
                   FOR NO KEY UPDATE""",
            (self.id,),
        )
        if not rows:
            return
        attempt_id, generation = rows[0]
        svc = self.sudo()
        vals = {'init_lease_until': False, 'init_attempt_id': False}
        attempt = self.env['woow_paas_platform.cloud_service_init_attempt'].sudo().browse(
            attempt_id).exists() if attempt_id else None
        counted = False
        if attempt and not attempt.finished_at:
            now = self._init_db_now()
            counted = (attempt.outcome != OUTCOME_LEGACY
                       and attempt.generation == generation)
            attempt.write({
                'finished_at': now,
                'outcome': attempt.outcome or OUTCOME_UNKNOWN,
                'error_kind': attempt.error_kind or (
                    False if attempt.outcome == OUTCOME_LEGACY else OUTCOME_UNKNOWN),
                'error': attempt.error or 'lease expired before a result was recorded',
            })
            # 上次實際執行的 attempt 結束（Q10 錨點）。
            vals['init_wait_since'] = now
            if counted:
                vals['init_unknown_count'] = (svc.init_unknown_count or 0) + 1
        svc.write(vals)
        if counted and svc.state == 'initializing':
            svc._init_check_exhausted()
        cr.commit()

    def _init_capacity_wait_eligible(self, now):
        """容量監控的純讀判準，對齊 runner 與原子 claim 的 dispatch 閘門。

        過期但未 settle 的 lease 仍不是可 claim 的空槽；不呼叫會改 state 的
        exhausted／timeout helper，也不將 UI phase 或 first-ready 當充分條件。
        """
        self.ensure_one()
        return bool(
            self.state == 'initializing' and self.init_pending
            and self._init_type() != 'none' and self.init_ready_observed
            and not self.sudo().init_redeploy_requested and not self.init_lease_until
            and self.init_retries < INIT_FAILED_LIMIT
            and self.init_unknown_count < self._init_unknown_limit()
            and (not self.init_retry_eligible_at or self.init_retry_eligible_at <= now)
            and (self.init_wait_spent_seconds or 0) + self._init_operator_wait_elapsed(now)
                < self._init_wait_timeout_minutes() * 60
        )

    def _init_ready_to_claim(self):
        """claim 之前的前置條件（在 settle 之後的新交易裡讀）。

        不欠 init 的 initializing 不 claim：背景 poll 經 ``_next_state_when_ready``
        遇到它會直接寫 running，這裡與它一致，不另立收斂方向。
        """
        self.ensure_one()
        if self.state != 'initializing' or not self.init_pending:
            return False
        if self.sudo().init_redeploy_requested:
            # Q27：背景看到 redeploy 意圖就停止自動重送；lease 結束後 cron 先執行 redeploy。
            return False
        # #1159 D2：只判額度。等待時限只在短 poll 觀測到 not ready 時判——排隊等 init
        # cron 屬平台容量排隊，不計時。
        if self._init_check_exhausted():
            return False
        return True

    def _init_claim(self, init_type):
        """Q2：單一條件式 UPDATE 取得 lease，插入 attempt，**在呼叫 operator 之前 commit**。"""
        self.ensure_one()
        cr = self.env.cr
        wait_budget_seconds = self._init_wait_timeout_minutes() * 60
        # WHERE 是唯一的 claim 裁決點：它在取得 lease 的同一個 UPDATE 重驗最新
        # Ready、cooldown eligibility 與已累計預算，避免過時 queue snapshot 在並發
        # not-ready 觀測或逾時後仍 dispatch。
        rows = self._init_guarded(
            f"""UPDATE {self._table}
                   SET init_lease_until = (statement_timestamp() AT TIME ZONE 'UTC')
                                          + make_interval(secs => %s),
                       init_attempt_seq = COALESCE(init_attempt_seq, 0) + 1,
                       init_queue_turn = (statement_timestamp() AT TIME ZONE 'UTC'),
                       init_wait_spent_seconds = COALESCE(init_wait_spent_seconds, 0)
                           + CASE WHEN init_wait_started_at IS NULL THEN 0 ELSE GREATEST(0,
                               EXTRACT(EPOCH FROM (LEAST(
                                   (statement_timestamp() AT TIME ZONE 'UTC'),
                                   COALESCE(init_retry_eligible_at,
                                            (statement_timestamp() AT TIME ZONE 'UTC')))
                                   - init_wait_started_at))) END,
                       init_wait_started_at = NULL,
                       init_retry_eligible_at = NULL,
                       init_capacity_wait_since = NULL
                 WHERE id = %s
                   AND state = 'initializing'
                   AND init_pending
                   AND init_ready_observed
                   AND NOT COALESCE(init_redeploy_requested, FALSE)
                   AND init_lease_until IS NULL
                   AND (init_retry_eligible_at IS NULL
                        OR init_retry_eligible_at <= (statement_timestamp() AT TIME ZONE 'UTC'))
                   AND COALESCE(init_wait_spent_seconds, 0)
                       + CASE WHEN init_wait_started_at IS NULL THEN 0 ELSE GREATEST(0,
                           EXTRACT(EPOCH FROM (LEAST(
                               (statement_timestamp() AT TIME ZONE 'UTC'),
                               COALESCE(init_retry_eligible_at,
                                        (statement_timestamp() AT TIME ZONE 'UTC')))
                               - init_wait_started_at))) END < %s
             RETURNING init_attempt_seq, COALESCE(init_generation, 0), init_lease_until,
                       (statement_timestamp() AT TIME ZONE 'UTC')""",
            (INIT_LEASE_SECONDS[init_type], self.id, wait_budget_seconds),
        )
        if not rows:
            return None
        seq, generation, lease_until, claimed_at = rows[0]
        attempt = self.env['woow_paas_platform.cloud_service_init_attempt'].sudo().create({
            'service_id': self.id,
            'generation': generation,
            'sequence': seq,
            'init_type': init_type,
            'claimed_at': claimed_at,
            'lease_until': lease_until,
        })
        attempt.flush_recordset()
        cr.execute(
            f"UPDATE {self._table} SET init_attempt_id = %s WHERE id = %s",
            (attempt.id, self.id),
        )
        self.invalidate_recordset([
            'init_lease_until', 'init_attempt_seq', 'init_attempt_id',
            'init_wait_spent_seconds', 'init_wait_started_at', 'init_retry_eligible_at',
            'init_queue_turn', 'init_capacity_wait_since'])
        cr.commit()
        return attempt

    def _init_call_params(self, init_type):
        """呼叫 operator 需要的全部輸入，**在 claim 之前**讀好。

        claim commit 之後到 operator 回應之前不得再碰 ORM：任何一次 SELECT 都會開一個
        REPEATABLE READ 交易，idle 地跨過整段 operator 呼叫（Odoo 最長約 1020s），回來
        之後的寫入跑在呼叫前的舊 snapshot 上，只要期間服務列被別人改過就撞 40001
        （Crosswind r1 S1）。
        """
        self.ensure_one()
        params = {
            'namespace': self.helm_namespace,
            'release_name': self.helm_release_name,
        }
        if init_type == 'n8n':
            svc = self.sudo()  # n8n_owner_* 有 groups='base.group_system'
            params['owner_email'] = (svc.n8n_owner_email
                                     or svc.template_id.post_deploy_init_email
                                     or 'admin@woowtech.io')
            # Fallback for services created before this feature
            params['owner_password'] = (svc.n8n_owner_password
                                        or 'W' + str(uuid.uuid4()).upper())
            # #1534：曾 init 成功＝owner 是平台建立、之後歸租戶管理。operator 在 owner 已存在時
            # 不再用上面那組密碼登入（租戶可能已改密碼／開 MFA）；沒確認過的才核對 Email。
            params['owner_confirmed'] = bool(svc.n8n_api_key)
        return params

    @staticmethod
    def _init_send(client, init_type, params):
        """送出 init；回傳 (結果, 例外)，二擇一。不碰 ORM、不在這裡分類。"""
        try:
            if init_type == 'n8n':
                return client.init_n8n(**params), None
            return client.init_odoo(**params), None
        except Exception as exc:  # 交給分類（Q3）
            return None, exc

    def _init_recover_recorded_attempt(self):
        """worker crash 後對已持久化結果補套恰好一次，不重送 operator。

        結果先 commit、服務端記帳與 ``applied_at`` marker 同交易 commit。是否已套用只能
        在下游 CAS 取得 service 列鎖後判斷，不能在此處以舊 snapshot 提前判斷。n8n
        success 的 key 不存 attempt；重播時從同一 service 受保護欄位重建 context。
        """
        self.ensure_one()
        attempt = self.sudo().init_attempt_id.sudo()
        if not attempt or not attempt.finished_at or not attempt.outcome:
            return False
        api_key = None
        if (attempt.init_type == 'n8n' and attempt.outcome == OUTCOME_SUCCESS
                and attempt.n8n_key_persisted):
            # service 可能還留上一把 key；只有本 attempt 的持久化證據才可用於重播。
            api_key = self.sudo().n8n_api_key
        outcome = InitOutcome(
            attempt.outcome,
            error_step=attempt.error_step or None,
            error_kind=attempt.error_kind or None,
            message=attempt.error or None,
            api_key=api_key,
        )
        return self._init_apply_outcome(attempt, outcome, attempt.error or False)

    def _run_post_deploy_init(self, client):
        """post-deploy init 的**唯一**執行入口（#1257）：收斂 lease → claim → operator
        → 先寫 attempt → CAS 套到服務。

        ⚠ 只能從背景（cron）呼叫：它會 ``commit`` 呼叫端的 cursor——Q2 要求 claim
        在呼叫 operator 之前 commit，另一條連線才看得到 lease；Q36 要求背景 per-service
        commit。本方法唯一的生產呼叫端是 init cron ``_cron_run_post_deploy_inits``
        （#1159 Q4）；短 poll 只把服務排進它的佇列，list/get 純讀。

        回傳是否真的送出了一次 init（init cron 的軟上限只數這個）。

        不承諾 exactly-once（Q2）：lease 擋的是「同一時間兩條連線各送一次」與 worker
        被殺之後的立即重跑，不擋「結果未知之後的重送」（那由 unknown 額度封頂）。
        """
        self.ensure_one()
        init_type = self._init_type()
        if init_type == 'none':
            return False
        cr = self.env.cr
        # 呼叫端先前的寫入先落地，claim 在新的 snapshot 上做。
        cr.commit()
        # D2-R：先補套已持久化但尚未套用的結果；不可再送 remote init。
        if self._init_recover_recorded_attempt():
            cr.commit()
            return False
        self._init_settle_expired_lease()
        self._init_check_operator_wait_timeout()
        if not self._init_ready_to_claim():
            cr.commit()
            return False
        params = self._init_call_params(init_type)
        attempt = self._init_claim(init_type)
        if not attempt:
            return False
        attempt_id = attempt.id  # commit 之後只帶 id，operator 呼叫期間不碰 ORM
        result, exc = self._init_send(client, init_type, params)
        outcome = classify_init_result(init_type, result=result, exc=exc)
        self._init_finish(
            self.env['woow_paas_platform.cloud_service_init_attempt'].sudo().browse(attempt_id),
            outcome, (params.get('owner_password'),))
        return True

    def _init_store_n8n_api_key(self, api_key):
        """Q8：n8n 拿到的 API key 先存（success 與 partial 都是），不進 attempt 記錄。

        存放處沿用既有的 ``n8n_api_key``（``groups='base.group_system'``）——單一來源，
        MCP helm values 也從這裡取（#1257 r2 裁定 g(i)）。回傳是否確實寫入；呼叫端將
        非敏感 marker 與 key 同交易 commit。success 的 CAS 仍可用活 worker 持有的 wire
        key 補寫，但 crash recovery 不可拿 service 的舊 key 假裝本次已保存。
        """
        self.ensure_one()
        rows = self._init_guarded(
            f"SELECT 1 FROM {self._table} WHERE id = %s FOR NO KEY UPDATE", (self.id,))
        if not rows:
            # 保存失敗不代表遠端失敗；留告警，由 marker 擋住不具 key 證據的成功補套。
            _logger.error(
                "[#1257] could not store the n8n API key of service %s (row busy or missing)",
                self.id,
            )
            return False
        self.sudo().write({'n8n_api_key': api_key})
        self.flush_recordset(['n8n_api_key'])
        return True

    def _init_release_unapplied(self, attempt_id, reason, outcome=None):
        """結果沒套上服務時釋放 lease（不耗額度）；只放自己持有的那一個。

        ``outcome`` 有給時（attempt 記錄那一段本身失敗、已被 rollback），同一步把
        attempt 補成「已結束」：lease 一放，之後再也不會有 settle 來收它，不補的話
        它會永遠像還在跑，而且當代的 attempt 不會被 purge。
        """
        if outcome is not None:
            self.env.cr.execute(
                "UPDATE woow_paas_platform_cloud_service_init_attempt "
                "SET finished_at = (statement_timestamp() AT TIME ZONE 'UTC'), "
                "    outcome = COALESCE(outcome, %s), "
                "    error = COALESCE(error, %s) "
                "WHERE id = %s AND finished_at IS NULL",
                (outcome, 'local bookkeeping error: %s' % reason, attempt_id),
            )
        self.env.cr.execute(
            f"UPDATE {self._table} SET init_lease_until = NULL, init_attempt_id = NULL "
            f"WHERE id = %s AND init_attempt_id = %s",
            (self.id, attempt_id),
        )
        self.invalidate_recordset(['init_lease_until', 'init_attempt_id'])
        _logger.warning(
            "[#1257] init attempt %s of service %s not applied (%s) — lease released, "
            "no quota consumed", attempt_id, self.id, reason,
        )

    def _init_finish(self, attempt, outcome, secrets_to_mask=()):
        """Q2：結果先寫 attempt 記錄（commit），再用 CAS 套到服務（commit）。

        結果記錄與 n8n key 是「遠端已發生的事實」在平台這側的副本，先各自 commit；
        後段 CAS 服務記帳與 ``applied_at`` 同交易 commit。記錄段失敗沒有可恢復結果，
        才釋放 lease 且不耗額度；已記錄結果的套用段失敗保留 pointer／lease，交下一趟
        冪等補套，並以 `_logger.exception` 告警。

        ⚠ #1052 標記（非裁定）：這裡的 ``except Exception`` 連 ``OperationalError``
        一併收掉並 ``rollback``，與該 doc 規則一（OperationalError 必須外拋）分歧——
        那條規則的依據是 HTTP 的 ``retrying()`` 會重播 handler；本入口只跑在 cron，
        沒有 retrying，外拋只會讓 attempt 留在「在途」、lease 到期後被記成 unknown
        而**耗額度**，正好違反 Q3。
        """
        self.ensure_one()
        cr = self.env.cr
        masked = mask_init_error(
            outcome.message, tuple(filter(None, secrets_to_mask)) + (outcome.api_key,))
        try:
            attempt.write({
                'finished_at': self._init_db_now(),
                'outcome': outcome.outcome,
                'error_step': outcome.error_step or False,
                'error_kind': outcome.error_kind or False,
                'error': masked,
            })
            if outcome.api_key and self._init_store_n8n_api_key(outcome.api_key):
                attempt.write({'n8n_key_persisted': True})
            cr.commit()
        except Exception:
            cr.rollback()
            _logger.exception(
                "[#1257] local bookkeeping error recording init attempt %s of service "
                "%s (outcome=%s)", attempt.id, self.id, outcome.outcome,
            )
            self._init_release_unapplied(
                attempt.id, 'attempt record failed', outcome=outcome.outcome)
            cr.commit()
            return
        try:
            # n8n owner 監控不在此觸發：短 poll 掃描同時涵蓋 live、冷補套與等待／失敗。
            self._init_apply_outcome(attempt, outcome, masked)
            cr.commit()
        except Exception:
            cr.rollback()
            # 結果已 commit，保留 lease pointer 讓下一趟只補套記帳；此時清掉會遺失
            # 唯一的冪等 anchor，連帶丟掉 D2-R 已耗時間。
            _logger.exception(
                "[#1257] local bookkeeping error applying init attempt %s of service "
                "%s (outcome=%s); keeping it for recovery", attempt.id, self.id,
                outcome.outcome,
            )
            cr.commit()
            return

    _INIT_CAS_TRIES = 3

    def _init_apply_outcome(self, attempt, outcome, masked_error):
        """CAS：attempt 仍持有 lease 且服務仍在 initializing 才套用。回傳是否套用。

        「列鎖忙」與「服務已經離開 initializing」分開處理（Crosswind r1 S6）：
        - 列鎖忙（40001／lock_timeout）：以新交易重試；用完仍忙 ⇒ 釋放 lease、不耗額度。
        - 已離開（停權、刪除……）：結果確定（success／failed／not_executed）⇒ 遠端已
          結束，直接釋放 lease，resume／rollback 不必等到期；unknown／partial ⇒ 遠端
          可能仍在跑，lease 留到到期。
        """
        self.ensure_one()
        cr = self.env.cr
        rows = None
        for _try in range(self._INIT_CAS_TRIES):
            rows = self._init_guarded(
                f"""SELECT COALESCE(init_generation, 0) FROM {self._table}
                     WHERE id = %s AND init_attempt_id = %s AND state = 'initializing'
                       FOR NO KEY UPDATE""",
                (self.id, attempt.id),
            )
            if rows is not None:
                break
            cr.rollback()  # 丟掉舊 snapshot，下一次在新交易重試
        if rows is None:
            # 結果已持久化時不可清掉復原 anchor；下一輪仍能以同一 attempt 補套。
            _logger.warning(
                "[#1257] init attempt %s of service %s row busy; keeping recovery pointer",
                attempt.id, self.id,
            )
            return False
        if rows:
            if attempt.generation != rows[0][0]:
                # 舊代結果不得套入新代；過期 lease 交 settle 清 pointer，新代照常 claim。
                _logger.info(
                    "[#1257] init attempt %s generation %s is stale for service %s generation %s",
                    attempt.id, attempt.generation, self.id, rows[0][0],
                )
                return False
            # 必須在 service 列鎖後讀 marker：兩個 recovery caller 都曾看見 marker 空時，
            # 後到者經 40001 重試會在新 snapshot 看見前者的 applied_at，不能再計一次。
            cr.execute(
                "SELECT applied_at FROM woow_paas_platform_cloud_service_init_attempt "
                "WHERE id = %s", (attempt.id,))
            marker = cr.fetchone()
            if not marker or marker[0]:
                return False
        if not rows:
            if outcome.outcome in (OUTCOME_SUCCESS, OUTCOME_FAILED, OUTCOME_NOT_EXECUTED):
                self._init_release_unapplied(attempt.id, 'service moved on')
            else:
                _logger.info(
                    "[#1257] init attempt %s (outcome=%s) not applied: service %s moved "
                    "on; lease kept until expiry", attempt.id, outcome.outcome, self.id,
                )
            return False
        svc = self.sudo()
        kind = outcome.outcome
        release = {'init_lease_until': False, 'init_attempt_id': False}
        if kind == OUTCOME_SUCCESS:
            if attempt.init_type == 'n8n' and not outcome.api_key:
                # 活 worker 可以用 wire key 補寫；冷重播缺持久化證據時，不能套舊 key。
                # 保留 active lease／pointer，待既有到期收尾後才可建立新 attempt。
                _logger.error(
                    "[#1257] recorded n8n success for service %s attempt %s lacks a "
                    "persisted key; keeping lease and pending initialization",
                    self.id, attempt.id,
                )
                return False
            # #1333：n8n init 成功時 operator 會 rollout restart（sidecar 要從 Secret
            # 讀到真的 API key，Recreate 策略＝先砍舊 Pod 再建新 Pod），所以「init 成功」
            # 的當下 n8n 其實正在重啟——直接寫 running，租戶點網址就是 502（10-02 正式站
            # 兩種帳號都重現）。改成留在 initializing、清掉欠 init 標記：init cron 不會
            # 再 claim（它只撈 init_pending），短 poll 等重啟後的 Pod 全部 ready 時經
            # `_next_state_when_ready` 轉 running；等待受 `_init_check_wait_timeout`
            # 封頂（錨點在這裡重設成現在）。舊 Pod 帶 deletion_timestamp，已被
            # `_is_container_ready` 排除，不會被誤認為就緒。
            # Odoo init 不重啟 Pod，維持直接 running。
            restarts_pods = attempt.init_type == 'n8n'
            vals = dict(release, **{
                'state': 'initializing' if restarts_pods else 'running',
                'init_pending': False,
                'addon_update_pending': False,
                'init_retries': 0,
                'init_unknown_count': 0,
                'init_exit': False,
                'init_error': False,
                'init_wait_spent_seconds': 0,
                'init_wait_started_at': False,
                'init_retry_eligible_at': False,
                'init_queue_turn': False,
            })
            if restarts_pods:
                vals.update({
                    'init_ready_observed': False,
                    'init_wait_since': self._init_db_now(),
                })
            if outcome.api_key:
                # 在列鎖下再寫一次：前一步的保底寫入若撞鎖失敗，這裡仍會落地。
                vals['n8n_api_key'] = outcome.api_key
            merged_helm_values = None
            template = svc.template_id
            if outcome.api_key and template.mcp_api_key_helm_path:
                try:
                    # #559：走唯一入口（欄位有 groups=，屬性直讀會 AccessError）
                    merged_helm_values = _deep_merge(
                        svc._helm_values_dict(),
                        _unflatten_dotpath_keys(
                            {template.mcp_api_key_helm_path: outcome.api_key}),
                    )
                except (json.JSONDecodeError, TypeError):
                    _logger.warning(
                        "Failed to update helm_values with real API key for service %s",
                        svc.name)
            if merged_helm_values is not None:
                svc._apply_helm_values(merged_helm_values, extra_vals=vals)
            else:
                svc.write(vals)
            if attempt.init_type == 'n8n':
                # 即時 CAS 可能補上先前未保存的 wire key，與 service 更新同交易成立。
                attempt.write({'n8n_key_persisted': True})
            _logger.info(
                "[#1257] %s init succeeded for service %s (attempt #%s)",
                attempt.init_type, svc.name, attempt.sequence)
        elif (kind == OUTCOME_FAILED and attempt.init_type == 'n8n' and svc.n8n_api_key
                and _n8n_owner_login_rejected(outcome)):
            # #1534 過渡期（平台先於 operator 上線、或 operator 回退）：舊 operator 在 owner 已
            # 存在時仍用平台存的 owner 密碼登入。這台服務 init 成功過（有 n8n_api_key）⇒ owner 是
            # 平台當時建立、之後歸租戶管理的那個；登入 401（MFA 是 code 998）＝租戶自己改了
            # 密碼或開了 MFA。init 要保護的事（不讓別人搶先完成 owner setup）早已成立，API key
            # 沿用上一次的——不計額度、不重試，直接完成。新 operator 不會走到這裡（owner 已存在
            # 時不登入）。舊 operator 失敗在登入，沒有重啟 Pod ⇒ 直接 running。
            svc.write(dict(release, **{
                'state': 'running',
                'init_pending': False,
                'addon_update_pending': False,
                'init_retries': 0,
                'init_unknown_count': 0,
                'init_exit': False,
                'init_error': masked_error,
                'init_wait_spent_seconds': 0,
                'init_wait_started_at': False,
                'init_retry_eligible_at': False,
                'init_queue_turn': False,
            }))
            _logger.warning(
                "[#1534] n8n init for service %s (attempt #%s): the owner password or MFA was "
                "changed in n8n; init completed without rotating the API key",
                svc.name, attempt.sequence)
        elif kind == OUTCOME_FAILED:
            # D2-R2：明確 failed 已證明 request 執行，重置 operator readiness wait；
            # failed quota itself still increments in this generation.
            svc.write(dict(release, **{
                'init_retries': (svc.init_retries or 0) + 1,
                'init_error': masked_error,
                'init_wait_since': attempt.finished_at,
                'init_wait_spent_seconds': 0,
                'init_wait_started_at': False,
                'init_retry_eligible_at': False,
            }))
            svc._init_check_exhausted(failed_step=outcome.error_step)
        elif kind in (OUTCOME_UNKNOWN, OUTCOME_PARTIAL):
            # Q8：重送前必須等 lease 到期，由 settle 釋放。D2-R2：partial 已證明執行，
            # 重置 operator 等待；unknown 保留既有預算，兩者都不提前釋放 lease。
            vals = {
                'init_unknown_count': (svc.init_unknown_count or 0) + 1,
                'init_error': masked_error,
                # Pod readiness 等待錨點是既有另一軸；D2-R 只裁 operator clock。
                'init_wait_since': attempt.finished_at,
            }
            if kind == OUTCOME_PARTIAL:
                vals.update(
                    init_wait_spent_seconds=0,
                    init_wait_started_at=False,
                    init_retry_eligible_at=False,
                )
            svc.write(vals)
            svc._init_check_exhausted()
        else:  # not_executed：call wall time + full cooldown，額度不動。
            call_seconds = max(0, (attempt.finished_at - attempt.claimed_at).total_seconds())
            svc.write(dict(release, **{
                'init_error': masked_error,
                'init_wait_spent_seconds': (svc.init_wait_spent_seconds or 0) + call_seconds,
                'init_wait_started_at': attempt.finished_at,
                'init_retry_eligible_at': attempt.finished_at + timedelta(
                    seconds=INIT_NOT_EXECUTED_COOLDOWN_SECONDS),
            }))
            svc._init_check_operator_wait_timeout()
        # 結果套用與 marker 同交易 commit，重播只會看到「都沒有」或「都已有」，
        # 因此任何 outcome（尤其 not_executed 時計）都不會重複套用。
        attempt.write({'applied_at': self._init_db_now()})
        if kind != OUTCOME_SUCCESS:
            _logger.warning(
                "[#1257] init attempt #%s for service %s: %s (step=%s)",
                attempt.sequence, svc.name, kind, outcome.error_step)
        return True

    # ------------------------------------------------------------------ #
    #  Cron：redeploy 意圖（Q27）                                         #
    # ------------------------------------------------------------------ #

    @api.model
    def _cron_run_requested_redeploys(self):
        """lease 結束後先執行登記過的 redeploy（Q27）。

        由 init cron ``_cron_run_post_deploy_inits`` 開頭呼叫（#1257 f／#1159：它掌管
        lease 的生命週期，意圖要在它下一次自動重送之前執行）。意圖在執行前就清掉：redeploy 失敗時不每
        分鐘重撞同一個錯誤，而是把原因寫進 ``error_message``（admin 在後台與租戶在
        服務頁都看得到），不讓「Redeploy queued」之後靜默消失（Crosswind r1 S3）。

        ⚠ #1052 標記（非裁定）：``_redeploy`` 是先 helm upgrade 再寫 DB。``UserError``
        全都發生在提交點之前（operator 未設定、#552 沒釘版、upgrade 被拒），回捲安全；
        其他例外可能落在提交點之後，回捲會讓 DB 與叢集不一致——與 HTTP 路徑（request
        回捲）同一個既有缺口，這裡只多留一行 ERROR 讓它看得見。
        """
        services = self.sudo().search([
            ('init_redeploy_requested', '=', True),
            ('state', 'not in', ('deleting', 'pending')),
        ])
        for svc in services:
            try:
                svc._init_settle_expired_lease()
                if svc._init_lease_active():
                    continue
                svc.write({'init_redeploy_requested': False})
                self.env.cr.commit()
                svc._redeploy()
                self.env.cr.commit()
                _logger.info("[#1257] ran the requested redeploy of service %s", svc.name)
            except UserError as exc:
                self.env.cr.rollback()
                _logger.error(
                    "[#1257] requested redeploy of service %s (id=%s) was refused: %s",
                    svc.name, svc.id, exc)
                svc.write({'error_message': _(
                    'The queued redeploy did not run: %(reason)s', reason=str(exc))})
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "[#1257] requested redeploy of service %s (id=%s) failed — the helm "
                    "upgrade may already have run; platform-side state may be stale",
                    svc.name, svc.id,
                )

    # ------------------------------------------------------------------ #
    #  Subscription lifecycle: suspend / resume / cleanup                #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    #  #744：CS Access teardown（形態比照 SA `_teardown_route_access`）     #
    # ------------------------------------------------------------------ #

    def _access_hostname(self):
        """本服務受 Access 保護的主機名（無 subdomain 回 ''）。

        與 `_public_base_url()` 同源但只回 host——Cloudflare Access app 的錨
        是 hostname 不是 URL（payload 無 scheme），兩處各自組一次 domain 是
        漂移的起點。
        """
        self.ensure_one()
        base = self._public_base_url()
        return base.split('://', 1)[-1] if base else ''

    def _access_to_dict(self):
        """本服務 Access 設定的對外形狀（#745）。

        單一裁決處：`_format_service` 的 detail payload 與兩支 access endpoint
        的回應都讀它，三處各拼一次就會漂移（前端只會發現其中一處對）。

        **刻意不吐** `access_app_id`：那是伺服器擁有的 Cloudflare 物件 id，前端
        一格都用不到，吐出去只是把身分錨點的值送給每一個看得到這個頁面的成員。
        `access_unresolved_idp_types` 相反——它是「登入限制被靜默放寬了」的訊號，
        不讓租戶看到才是問題。
        """
        self.ensure_one()
        return {
            'is_protected': self.is_protected,
            'session_duration': self.session_duration,
            'login_google': self.login_google,
            'login_email_otp': self.login_email_otp,
            'access_unresolved_idp_types':
                self.access_unresolved_idp_types or '',
            # #794 fail-open 訊號：宣稱受保護、但 CF 上的 app 已經被拆掉。不吐
            # 的話 UI 只讀得到 is_protected 這個**意圖**欄位。
            'access_protection_lost': self.access_protection_lost,
            'hostname': self._access_hostname(),
            'access_policies': [p.to_dict() for p in self.access_policy_ids],
        }

    def _access_canonical_name(self):
        """本服務在 Cloudflare Access 上的命名根（帶 env token，#745）。

        單一裁決處：sync 與 teardown 兩條 payload 都讀它，兩邊各算一次就是漂移
        的起點。實際公式與「為什麼不能反推自 `subdomain`」寫在
        `services/naming.py::make_cs_access_canonical_name`。
        """
        self.ensure_one()
        deploy_env = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.deploy_env', 'prod')
        return make_cs_access_canonical_name(
            self.workspace_id.ws_hash, self.reference_id, self.name,
            env=deploy_env)

    def _get_operator_client(self):
        """operator client；未設定就 fail-loud。

        刻意**不**回 None 讓呼叫端靜默跳過：Access 是安全功能，「設定寫進 DB 但
        沒人告訴 Cloudflare」＝租戶以為已保護、實際完全開放（fail-open）。
        """
        from ..services.paas_operator import get_paas_operator_client
        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(_(
                "PaaS Operator is not configured. "
                "Please set Operator URL and API Key in Settings → Woow PaaS."
            ))
        return client

    def _build_existing_policy_ids(self):
        """operator 這一輪必須刪掉的舊 reusable policy id（形態同 SA route）。

        兩個來源，兩個都是伺服器端讀到的：尚存列的 `cf_policy_id`，加上這個錨點
        在待回收帳本上還欠的（`cf_access_policy_reap._pending_for`）。

        policy 的 unlink 會先跑 `super().unlink()` 才 re-sync，所以剛被刪那幾列
        的 cf id 已經不在 DB 裡、永遠進不了這份清單，而 operator 唯一的刪除來源
        就是它 ⇒ 每刪一條 policy 就漏一顆永久 CF 孤兒。它們是**經帳本**進來的：
        `unlink()` 在同一個 savepoint 內先 `Reap._record_ids()` 落帳，才觸發
        re-sync。

        #528：帳要離開一次性的通道、落到 DB——那一次 re-sync 可能被下面的
        liveness gate 整個擋掉，只活一次呼叫的東西被擋掉之後就沒有任何記錄指向
        那些 id 了。帳本讓它們活到下一次推得動為止。

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
        """本服務的 desired-state payload（operator `PUT /api/access/sync`）。

        逐欄位鏡射 `SecurityAccessRoute._build_access_payload`——operator 那端只
        有一個 `AccessSyncRequest` schema，兩邊形狀分家只會在 422 才發現，而 422
        說不出是哪一邊寫錯。保護粒度是整個 subdomain（拍板 5），所以
        `protected_hostnames` 恆為 `[primary_hostname]` 或空。
        """
        self.ensure_one()
        login_idp_types = []
        if self.login_google:
            login_idp_types.append('google')
        if self.login_email_otp:
            login_idp_types.append('onetimepin')
        hostname = self._access_hostname()
        protected = [hostname] if (self.is_protected and hostname) else []
        canonical = self._access_canonical_name()
        return {
            'enabled': bool(self.is_protected),
            'canonical_name': canonical,
            # app display name：與 SA route 同慣例（走對外可見的那個名字，
            # env 區隔靠 per-env `paas_domain`）。env token 掛在 canonical，
            # 那才是 reusable policy 前綴的來源。
            'app_name': self.subdomain or canonical,
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
        """把本服務完整的 Access 狀態推給 operator（冪等宣告式）。

        liveness gate 三道，各擋一種「推出去只會製造垃圾」的情形：

        * **沒有 `subdomain`** ⇒ 沒有可保護的 hostname，Access app 的錨點本身
          不存在。
        * **`state == 'deleting'`** ⇒ teardown 已經跑過（`_do_cleanup`），再推
          一次等於把剛拆掉的 app 建回來，而且沒有任何後續路徑會再拆它。
        * **未保護且沒有存過 app id** ⇒ 這台服務從來沒有 Access app，是真正的
          no-op（不是「假裝成功」）。

        `is_protected=False` 但存過 app id ⇒ 送 `enabled=False` 的 payload
        （operator 據此拆 app＋policies），成功後才清掉存著的 id。

        與 SA route 的刻意差異：**沒有 `force` 旁路**。SA 需要它是因為
        provision/resume 會在 state 尚未回到 'active' 時做收斂；CS 的
        suspend/resume 依設計完全不碰 Access app（design checklist 7），沒有這種
        收斂點，多開一個旁路只會多一條沒人走、也沒人測的分支。

        #528：三道 gate 收斂成 `_access_sync_skip_reason()` 一個裁決處，略過時回
        ``False``、推出去了回 ``True``。CS 這三格在現行碼下**帶不著待回收 id**
        （見該方法的 docstring 逐格說明），機制照樣套上去的理由是形狀一致：
        `state == 'deleting'` 那格的不變式住在 gate 外面、沒有任何守線，而三腿
        寫法分家的話下一個人只會照著最近的那一腿抄。
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
                '[CLOUD_SVC] service %s (%s)' % (self.id, self.subdomain),
                skip)
            return False
        client = self._get_operator_client()
        payload = self._build_access_payload()
        result = client.sync_access_app(payload)
        # operator 重指 app 之後刪不掉的舊 reusable policy＝**永久** CF 孤兒
        # （沒有任何後續 re-sync 會再碰到它們），所以要讓它在 log 裡看得見。
        orphaned = result.get('orphaned_policy_ids') or []
        if orphaned:
            _logger.warning(
                "[CLOUD_SVC] access sync service=%s (%s) left %d orphan CF "
                "policy(ies): %s", self.id, self.subdomain, len(orphaned),
                ', '.join(orphaned))
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
            '[CLOUD_SVC] service %s (%s)' % (self.id, self.subdomain))
        # 身分錨點：只有 sentinel 路徑寫得動（對所有人生效，含 superuser）。
        _internal_cs_access_identity(self).access_app_id = (
            result.get('app_id') or False)
        # operator 的 fail-open 訊號：要求了但 CF 帳號上沒有對應 IdP 的登入方式
        # ——登入限制被靜默放寬了，刻意曝光而不是丟掉。
        unresolved = result.get('unresolved_idp_types') or []
        new_unresolved = ', '.join(unresolved) if unresolved else False
        if self.access_unresolved_idp_types != new_unresolved:
            self.access_unresolved_idp_types = new_unresolved
        # #794：推成功了 ⇒ CF 上的狀態就是這筆記錄宣稱的狀態，fail-open 旗標的
        # 前提（app 被拆掉但記錄還在）已經不成立。旗標由 sync 結果驅動，與
        # `access_unresolved_idp_types` 同一個形狀。
        if self.access_protection_lost:
            self.access_protection_lost = False
        # 回傳的 policy id 依 (sequence, id) 順序對映回列——與 payload 送出去的
        # 排序是同一條 key，錯開就會把 A 的 cf id 記到 B 頭上。
        returned = result.get('policy_ids') or []
        # #798：包 recordset 不是包 record。⚠ 下面是**兩個不同的迴圈**，各包一次。
        policies = _internal_policy_cf_write(self.access_policy_ids).sorted(
            key=lambda x: (x.sequence, x.id))
        for pol, cf_id in zip(policies, returned):
            if pol.cf_policy_id != cf_id:
                pol.cf_policy_id = cf_id
        if not result.get('app_id'):
            # 已拆除 ⇒ 清掉這台服務底下 policy 的過期 cf id
            for pol in _internal_policy_cf_write(self.access_policy_ids):
                if pol.cf_policy_id:
                    pol.cf_policy_id = False
        return True

    def _access_sync_skip_reason(self):
        """``None`` ＝ 這一輪推得動；否則回一句人話說明為什麼不推（#528）。

        本腿**所有** liveness gate 的唯一裁決處，新增 gate 一律加在這裡，訊號
        自動跟上。三格各自的可達性（動 gate 前先讀）：

        * **沒有 `subdomain`**：`cf_policy_id` 的唯一寫入處在本 gate 的下游，
          所以某列有 cf id ⇒ 它被寫入的那一刻 subdomain 非空。結構上帶不著
          待回收 id。
        * **`state == 'deleting'`**：`_do_cleanup` 是先 teardown 成功、清掉每列
          的 cf id，才寫 `deleting`；controller 的 `_delete_service` 順序相反，
          目前靠「unlink 拋的 UserError 沒有人接住 ⇒ 整個 request 回捲」撐著。
          **這條不變式住在 gate 外面、沒有任何守線**——這一格是本腿唯一會因為
          別處改動而變成活漏洞的，所以訊號要在。
        * **未保護且沒有 app id**：清空 `access_app_id` 的兩處都在同一個原子區塊
          裡把每列的 cf id 一起清掉，所以這三個條件湊不齊。
        """
        self.ensure_one()
        if not self.subdomain:
            return 'the service has no public subdomain'
        if self.state == 'deleting':
            return "the service state is 'deleting'"
        return (None if (self.is_protected or self.access_app_id)
                else 'the service is unprotected and never had an Access app')

    def _push_access_teardown(self, app_id, policy_ids, app_name, hostname):
        """拆除用的最小 `enabled=False` payload（#745 接上 T2 留的 seam）。

        與 `_sync_access_to_operator` 分開的理由和 SA 的 `_teardown_route_access`
        一樣：呼叫端（unlink / `_do_cleanup`）手上只剩**捕捉下來的 id**，記錄
        可能正在消失中，不能再去 walk `access_policy_ids`。

        本方法不吞例外——`_teardown_cs_access` 負責把它收成 False（fail-loud 那
        一側）。安靜的 no-op 會讓呼叫端把「根本沒清」當成「清乾淨了」並清掉
        `access_app_id`，孤兒就此永久失去重試錨點且零訊號。
        """
        self.ensure_one()
        client = self._get_operator_client()
        canonical = self._access_canonical_name()
        result = client.sync_access_app({
            'enabled': False,
            'canonical_name': canonical,
            'app_name': app_name or canonical,
            'primary_hostname': hostname or '',
            'protected_hostnames': [],
            'session_duration': '24h',
            'login_idp_types': [],
            'policies': [],
            'existing_app_id': app_id or None,
            'existing_policy_ids': policy_ids or [],
        })
        orphaned = result.get('orphaned_policy_ids') or []
        if orphaned:
            _logger.warning(
                "[CLOUD_SVC] access teardown service=%s app=%s left %d orphan "
                "policy(ies): %s", self.id, app_id, len(orphaned),
                ', '.join(orphaned))
        return result

    def _teardown_cs_access(self, app_id, policy_ids, app_name, hostname,
                            torn_down=None):
        """拆掉本服務的 Cloudflare Access app（＋ reusable policies）。

        契約逐字對齊 `SecurityAccess._teardown_route_access`：乾淨拆完回
        ``True``（呼叫端才可以清掉存著的 app id），失敗回 ``False`` 且**不**
        丟例外——由呼叫端決定要 fail-loud（unlink）還是落 error 狀態
        （`_do_cleanup`）。`access_app_id` 只在 True 時清除，失敗一律保留：
        那個 id 是唯一的重試錨點，清掉就變成永久 CF 孤兒且沒有任何路徑找得回
        來（[[two-stage-remote-provision-idempotency]]）。

        沒有 app id 也沒有 policy id ⇒ 這台服務從來沒有 Access app，直接回
        True（不是「假裝成功」，是真的沒有東西要拆）。

        ⚠ **「只回 bool、不丟例外」有一個明載的例外**（#791 覆核 F4）：提交點之後
        的記帳段若拋 ``OperationalError``（serialization failure / 連線類）會
        **往外拋**。吞掉它 Odoo 的 ``retrying()`` 就永遠看不到，而此刻 PG 交易已
        aborted，硬回一個 bool 只會讓後續 flush 再炸一次（#518）。呼叫端不必自己
        接：request 級 retry 會用新 snapshot 重播整支流程，而重播是安全的
        （CF 的 DELETE 把 404 當成功）。
        """
        self.ensure_one()
        if not app_id and not policy_ids:
            return True
        try:
            result = self._push_access_teardown(
                app_id=app_id, policy_ids=policy_ids,
                app_name=app_name, hostname=hostname)
        except Exception as e:
            _logger.warning(
                "[CLOUD_SVC] access teardown failed service=%s app=%s: %s",
                self.id, app_id, e)
            return False
        # ── 提交點之後 ─────────────────────────────────────────────────
        # #784：operator 回報「送去刪但刪不掉」的那幾顆現在就是 CF 孤兒，而呼叫端
        # 拿到 True 之後會清掉列上的 cf id／把列刪掉 ⇒ 舊寫法（只 log 一行）之後
        # 就再也沒有任何記錄指向它們。落帳，回收者是
        # `cf_access_policy_reap._cron_reap_dead_anchors`（錨點死掉之後）或這台
        # 服務自己的下一次成功 sync（錨點還活著時）。
        # 記帳失敗不翻轉回傳值（[[post-commit-bookkeeping-rollback-unit]]）。
        #
        # 🔴 #794 二輪覆核 [HIGH]：「CF 已經拆成功」要在**落帳之前**就記——
        # `_settle_teardown_after_commit` 明文 re-raise `OperationalError`，
        # 落帳一撞到並發例外本方法就不會 `return True` ⇒ 呼叫端的 `if ...:`
        # 不執行 ⇒ 補償清單空 ⇒ 補償空轉，而 CF 上的 app 已經真的沒了。
        # 回傳值語意不動（仍表示「這一趟整體成不成功」）。
        if torn_down is not None:
            torn_down.append((self._name, self.id))
        self.env[_CF_REAP_MODEL]._settle_teardown_after_commit(
            self._name, self.id, app_id or None, policy_ids or [],
            (result or {}).get('orphaned_policy_ids') or [], _logger,
            '[CLOUD_SVC] access teardown service=%s app=%s' % (self.id, app_id))
        return True

    def _access_mark_protection_lost(self):
        """#794：把「本服務的 CF Access app 已經被拆掉、而記錄還在」寫成列上的
        事實。三腿同名，唯一呼叫端是
        `cf_access_policy_reap._compensate_fail_open_after_rollback`（獨立交易）。

        完整理由（為什麼清 id 不違反 two-stage-remote-provision-idempotency、
        為什麼只清 id 不夠、為什麼不動 `is_protected`）寫在 SA 那一份，見
        `security_access.py` 的同名方法——**不在這裡重述**：三份平行的散文一定
        會漂，而漂掉的那一份讀起來和權威版本一模一樣。
        """
        self.ensure_one()
        _internal_cs_access_identity(self).access_app_id = False
        # #798：包 recordset（同上）。
        for pol in _internal_policy_cf_write(self.access_policy_ids):
            if pol.cf_policy_id:
                pol.cf_policy_id = False
        if not self.access_protection_lost:
            self.access_protection_lost = True

    def _has_access_teardown(self):
        """本服務的**列上**還有沒有 CF Access 物件要拆（app id 或 policy cf id）。

        #794 拆出來的：`unlink()` 需要知道「這一趟到底有沒有真的對 CF 拆掉東西」
        才能決定要不要補償，而 `_teardown_access_if_any()` 的 ``True`` 同時代表
        「拆乾淨了」與「本來就沒東西要拆」，兩者在回傳值上不可區分。呼叫端自己
        再寫一次同樣的判準是漂移的起點（SA 那一腿的 `_has_access_teardown()`
        正是同一個名字、同一個理由）。

        🔴 **只讀列上的狀態，不查待回收帳本**——把帳本聯集進來會讓
        `_teardown_access_if_any()` 的閘永遠回 True、服務永久卡在 `error`，
        完整推導見該方法的 docstring。
        """
        self.ensure_one()
        return bool(self.access_app_id or any(
            p.cf_policy_id for p in self.access_policy_ids))

    def _teardown_access_if_any(self, torn_down=None):
        """本服務目前的 Access 狀態 → 拆除，成功才清 id。回傳是否乾淨。

        單一裁決處：unlink（fail-loud）與 `_do_cleanup`（落 error 狀態）兩條
        路徑共用，免得兩邊各自組一次 policy id 清單而漏掉其中一種。

        🔴 **不要把待回收帳本（`cf_access_policy_reap`）的查詢加進這裡**（#784
        F6 的教訓，寫在這是因為這裡最像「順手移植」的落點）。

        本方法把三件事合在一起：「有沒有東西要拆」（閘）、「要送哪些 id」
        （payload）、「清乾淨了沒」（回傳值餵給呼叫端的 fail-loud／落 error）。
        今天沒事，是因為三者讀的都是**列上的狀態**。SA 那一腿曾經在同型的方法裡
        補上 `_pending_ids()` 聯集——想讓刪除時順手把帳本舊帳一起送出去——結果
        連帶把「清乾淨了沒」也變成「列 ∪ 帳本」：只要 operator 回報過某顆 policy
        刪不掉，那筆帳就掛在帳本上（錨點還活著 ⇒ dead-anchor cron 依設計不收）
        ⇒ 閘永遠回 True ⇒ **SA 永久卡在 `state='error'`**，每輪 lifecycle cron
        白打一次 operator，七天後只多一行 stuck warning。

        SA 那邊的解法是把兩個問題拆開各自命名（`_has_access_teardown()` 只答
        「這列乾淨了沒」、`_access_teardown_payload_ids()` 才答「這趟送哪些
        id」），見 `security_access.py`。這裡真要做同樣的事，就照那個形狀拆，
        **不要**在本方法裡直接查帳本。repo-lint 的 `access-gate-no-ledger-read`
        會擋住「filter 的 lambda 讀帳本」那一格，但**擋不到**本方法這種
        「gate 與 payload 合體」的形狀——那正是需要這段註解的原因。
        """
        self.ensure_one()
        pol_ids = [p.cf_policy_id for p in self.access_policy_ids
                   if p.cf_policy_id]
        if not self._has_access_teardown():
            return True
        ok = self._teardown_cs_access(
            app_id=self.access_app_id or None,
            policy_ids=pol_ids,
            app_name=self.subdomain or self.helm_release_name,
            hostname=self._access_hostname(),
            torn_down=torn_down,
        )
        if ok:
            # 身分錨點：清除也只有 sentinel 路徑寫得動。
            _internal_cs_access_identity(self).access_app_id = False
            # #798：包 recordset（同上）。
            for pol in _internal_policy_cf_write(self.access_policy_ids):
                if pol.cf_policy_id:
                    pol.cf_policy_id = False
        return ok

    # ------------------------------------------------------------------ #
    # #1563 — 停權說明頁（共用頁；operator route park／unpark）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _route_park_error_text(error):
        """operator 錯誤的可讀說明：``detail`` 是 dict（帶 ``error_code``）時取 ``message``。"""
        detail = getattr(error, 'detail', None)
        if isinstance(detail, dict):
            return detail.get('message') or detail.get('error_code') or str(error)
        return detail or getattr(error, 'message', None) or str(error)

    def _park_public_route(self, client):
        """停權後把對外 hostname 指向共用的停權說明頁（#1563）。**永不 raise**。

        說明頁位址由 operator 自己的設定決定（平台不傳 URL）。這一步純粹是給訪客看的
        說明：停權的提交點是 scale 0，這裡任何失敗（舊 operator 沒有端點的 404、說明頁
        還沒部署的 409、Cloudflare 錯誤…）都只記 log——停權本身照樣成功，訪客頂多看到
        和以前一樣的逾時／502。

        只處理有對外網址的服務（``_build_expose_config``：template 開 ingress 且有
        subdomain，與安裝時同一個判準）；Security Access／Smart Home 不在本範圍。
        """
        self.ensure_one()
        expose = self._build_expose_config()
        if not expose:
            return
        from ..services.paas_operator import PaaSOperatorError
        try:
            result = client.park_release_route(
                self.helm_namespace, self.helm_release_name, expose['subdomain'],
            )
        except PaaSOperatorError as e:
            if e.status_code == 404:
                _logger.info(
                    "[CLOUD_SVC] park skipped service=%s: operator has no route/park "
                    "endpoint yet (older image)", self.id)
            else:
                _logger.warning(
                    "[CLOUD_SVC] park failed service=%s subdomain=%s (suspend still "
                    "effective): %s", self.id, expose['subdomain'],
                    self._route_park_error_text(e))
            return
        except Exception as e:  # noqa: BLE001 — 說明頁失敗不得讓停權失敗
            _logger.warning(
                "[CLOUD_SVC] park failed service=%s subdomain=%s (suspend still "
                "effective): %s", self.id, expose['subdomain'], e)
            return
        _logger.info(
            "[CLOUD_SVC] park service=%s subdomain=%s status=%s",
            self.id, expose['subdomain'], (result or {}).get('status'))

    def _unpark_public_route(self, client):
        """復權後把對外 hostname 從停權說明頁改回服務（#1563）。

        Returns:
            ``None``＝完成（含「本來就沒 park」「沒開 Cloudflare」「沒有對外網址」「舊
            operator 沒有端點」——舊 operator 不可能 park 過任何東西）；否則回可讀的失敗
            原因，呼叫端（``_do_resume``）據此 fail-loud。
        """
        self.ensure_one()
        expose = self._build_expose_config()
        if not expose:
            return None
        from ..services.paas_operator import PaaSOperatorError
        try:
            result = client.unpark_release_route(
                self.helm_namespace, self.helm_release_name, expose,
            )
        except PaaSOperatorError as e:
            if e.status_code == 404:
                _logger.warning(
                    "[CLOUD_SVC] unpark skipped service=%s: operator has no route/unpark "
                    "endpoint (older image, so nothing can have been parked)", self.id)
                return None
            reason = self._route_park_error_text(e)
            _logger.warning(
                "[CLOUD_SVC] unpark failed service=%s subdomain=%s: %s",
                self.id, expose['subdomain'], reason)
            return reason
        except Exception as e:  # noqa: BLE001 — 交給 _do_resume fail-loud
            _logger.warning(
                "[CLOUD_SVC] unpark failed service=%s subdomain=%s: %s",
                self.id, expose['subdomain'], e)
            return str(e)
        _logger.info(
            "[CLOUD_SVC] unpark service=%s subdomain=%s status=%s",
            self.id, expose['subdomain'], (result or {}).get('status'))
        return None

    def _do_suspend(self):
        """Scale down Helm release to 0 replicas.

        #744：**刻意不動 Access app**。停權是「機器停下來」，由 helm 層縮到 0
        replica 處理；Access 設定是租戶的組態，停權期間拆掉它，復權時就得重建
        並重新拿一個 app id——中間任何一步失敗都會讓一台復權的服務變成無保護
        對外曝光（fail-open 的方向）。設計拍板明列此項（design checklist 7）。
        """
        self.ensure_one()
        _logger.info(
            "[CLOUD_SVC] _do_suspend service=%s name=%s state=%s sub=%s",
            self.id, self.name, self.state,
            self.subscription_id.id if self.subscription_id else None,
        )
        if self.state == 'suspended':
            return
        if self._provision_rejected_nothing_created():
            # #1424：開通被 operator 拒絕、訂閱已自動取消的服務。叢集上沒有可停的 workload，
            # 打 operator 只會 404、把寫給租戶的說明蓋成「Suspend failed」，而且 lifecycle
            # cron 每天都會再來一次。維持 error 與說明，等租戶刪除（或 cleanup 期滿）。
            _logger.info(
                "[CLOUD_SVC] _do_suspend skipped service=%s: provisioning was rejected "
                "(%s), nothing to suspend", self.id, self.provision_rejected_code)
            return

        from ..services.paas_operator import get_paas_operator_client

        client = get_paas_operator_client(self.env)
        if client and self.helm_release_name and self.helm_namespace:
            try:
                client.scale_release(
                    self.helm_release_name, self.helm_namespace, replicas=0,
                )
                _logger.info(
                    "[CLOUD_SVC] _do_suspend scaled to 0 service=%s release=%s ns=%s",
                    self.id, self.helm_release_name, self.helm_namespace,
                )
            except Exception as e:
                _logger.warning(
                    "[CLOUD_SVC] _do_suspend scale failed service=%s: %s",
                    self.id, e,
                )
                self.write({
                    'state': 'error',
                    'error_message': _('Suspend failed: %(error)s', error=e),
                })
                return
            # #1563：停權已生效（提交點＝scale 0）。把對外網址指到共用的停權說明頁只是讓
            # 訪客看得懂——刻意放在上面那個 try 之外，失敗只記 log，不能讓停權回報失敗。
            self._park_public_route(client)

        self.write({
            # #497 縱深：已經在停權中的服務（旗標仍在、state 被別的路徑改寫過）再走
            # 一次 suspend 時，**不覆寫**既有的停權前狀態。這裡讀的是**當下**的
            # state，而卡死 row 的當下 state 是 age timeout 寫的 'error' —— 覆寫後
            # 租戶付款復權會拿回一台顯示 error 的服務（癒後帶傷）。首次停權（旗標為
            # 空）仍如實記下當下狀態。
            # #1266：轉移狀態也如實記下（原本一律記成 running），resume 才分得出
            # 「停權前 init 還沒做完」。欠 init 標記刻意不動（Q9：停權不清標記）。
            'pre_suspend_state': self.pre_suspend_state or (
                self.state
                if self.state in ('running', 'error') + _INIT_TRANSITIONAL_STATES
                else 'running'
            ),
            'state': 'suspended',
        })
        _logger.info("[CLOUD_SVC] _do_suspend done service=%s new_state=suspended", self.id)

    def _acquire_resume_lock(self):
        """#1599：復權前對這一列取 ``FOR NO KEY UPDATE NOWAIT``；拿不到回 ``False``。

        Cloud Service 的復權有兩個執行者：每日生命週期 cron，以及付款入帳後立即跑的
        ``_cron_resume_paid_cloud_services``。兩顆是不同的 ir.cron、可能同時在跑；兩邊都
        經過 ``_enforce_single``，在呼叫 operator 之前先拿這把鎖：

        * 對方正持有（正在復權、還沒 commit）⇒ ``LockNotAvailable``；
        * 對方已經 commit 了一次復權，但本交易的 REPEATABLE READ 快照比它舊（快照裡
          這台仍是停權）⇒ PostgreSQL 回 ``SerializationFailure``（40001）。

        兩種都回 ``False``：這一輪跳過，不重複 scale／unpark，也不讓那筆 write 撞
        40001 後整段回捲。鎖跟著交易結束釋放。``FOR NO KEY UPDATE`` 與 ORM 的 UPDATE
        互斥，但不擋其他表以外鍵指向這一列（例如 chatter）。
        """
        self.ensure_one()
        cr = self.env.cr
        try:
            # 同 `_init_guarded`／core ir_cron：預期中的衝突不該在 sql_db 印成 ERROR。
            with mute_logger('odoo.sql_db'), cr.savepoint(flush=False):
                cr.execute(
                    f"SELECT id FROM {self._table} WHERE id = %s "
                    "FOR NO KEY UPDATE NOWAIT",
                    (self.id,),
                )
        except (pg_errors.SerializationFailure, pg_errors.LockNotAvailable) as exc:
            _logger.info(
                "[CLOUD_SVC] resume lock busy service=%s (%s): another transaction is "
                "handling it", self.id, type(exc).__name__,
            )
            return False
        return True

    def _do_resume(self):
        """Scale up Helm release to restore replicas."""
        self.ensure_one()
        _logger.info(
            "[CLOUD_SVC] _do_resume service=%s name=%s state=%s pre_suspend=%s",
            self.id, self.name, self.state, self.pre_suspend_state,
        )
        # #482 縱深：guard 讀 _is_halted() 的聯集，而非只認 state == 'suspended'。
        #
        # 舊 guard 讓任何把 state 改寫掉的路徑（upgrade 寫 deploying/upgrading）
        # 都變成單向門：服務仍在停權中，卻再也回不來 —— 租戶付了款也復不了權，
        # 只能人工 write({'state': 'suspended'}) 再 resume（#482 的脫困 runbook）。
        # 主修已讓那三條路不再汙染 state；這裡保證**即使**將來又有新路徑改寫
        # state，或要處理既有已卡死的資料，復權仍然走得通。這是縱深，不是主修的
        # 替代品。
        if not self._is_halted():
            return

        from ..services.paas_operator import get_paas_operator_client

        client = get_paas_operator_client(self.env)
        if client and self.helm_release_name and self.helm_namespace:
            try:
                client.scale_release(
                    self.helm_release_name, self.helm_namespace, replicas=1,
                )
                _logger.info(
                    "[CLOUD_SVC] _do_resume scaled to 1 service=%s release=%s ns=%s",
                    self.id, self.helm_release_name, self.helm_namespace,
                )
            except Exception as e:
                _logger.warning(
                    "[CLOUD_SVC] _do_resume scale failed service=%s: %s",
                    self.id, e,
                )
                self.write({
                    'state': 'error',
                    'error_message': _('Resume failed: %(error)s', error=e),
                })
                return
            # #1563：對外網址從停權說明頁改回服務。這一步失敗＝付了錢的服務網址仍是說明頁，
            # 必須 fail-loud：與上面 scale 失敗同形——寫 error＋說明、**保留**
            # pre_suspend_state（_is_halted() 仍為真），生命週期 cron 下一輪會再走一次
            # _do_resume（scale 1 冪等、unpark 冪等）。
            unpark_error = self._unpark_public_route(client)
            if unpark_error:
                self.write({
                    'state': 'error',
                    'error_message': _(
                        'Resume failed: the service was started again, but its public URL '
                        'still shows the suspension notice (%(error)s). It will be retried '
                        'automatically.', error=unpark_error),
                })
                return

        # MINOR-4（既存設計，本 PR 未改）：resume 後直接寫回 pre_suspend_state。
        # 對 VM 型而言，patch runStrategy=Always 只是「叫它開機」，guest 真正
        # 可用還要數分鐘，所以這段期間 UI 會顯示 running 但服務尚未就緒。
        # 容器型的 resume 成本低、窗口短，故沿用同一路徑；VM 型是否改成回
        # deploying 交給 poll 收斂，列為 v1.1 評估（design §7.2）。
        restore_state = self.pre_suspend_state or 'running'
        vals = {'pre_suspend_state': False}
        if restore_state == 'error':
            # #1266／Q25：原本是 error 的維持 error，額度也不重置——否則「欠費→繳費」
            # 就能重置失敗額度。
            pass
        elif self._init_type() != 'none' and self.init_pending:
            # #1266／Q9：欠 init 就導回 initializing，交給背景 init（不開新一代、不動
            # 計數）。含 pre_suspend_state='running' 但停權期間開了新一代的情形（改
            # addon 的 reconfigure／redeploy 在 halted 路徑上只寫 suspended）——#1257
            # r2 裁定 h(i)。
            restore_state = 'initializing'
        elif restore_state in _INIT_TRANSITIONAL_STATES:
            # 不欠 init 的轉移狀態：沿用 #1266 以前的行為（記錄值原本就是 running）。
            restore_state = 'running'
        vals['state'] = restore_state
        if restore_state != 'error':
            # 復權成功、服務不是回到 error：停權期間留下的錯誤說明都過時了——最常見的是
            # 上一輪復權失敗寫的「Resume failed …will be retried automatically」（#1563
            # unpark 失敗、scale 失敗），由每日 cron 重試成功之後若不清，租戶會看到一台
            # running 的服務掛著紅色錯誤橫幅（ServiceDetailPage 的 banner 只看
            # error_message、不看 state）。#1599 沙盒端到端發現。回到 error 的維持原說明。
            vals['error_message'] = False
        self.write(vals)
        _logger.info(
            "[CLOUD_SVC] _do_resume done service=%s new_state=%s",
            self.id, restore_state,
        )

    def _compute_ns_quota(self, workspace):
        """#278 A1 權威值：以 workspace 內『非 deleting』存活 service 加總算 quota。

        domain 已排除 state='deleting' → 呼叫時機在 state 翻 deleting /
        super().unlink() 之後即天然反映刪除後存活集。回傳字串 quantity
        （與 operator NamespaceQuotaUpdateRequest 及既有 create_namespace 一致）。
        floor 為 8 vCPU / 8Gi / 100Gi（workspace 清空後 survivor 為空亦不 skip）。
        """
        Svc = self.env['woow_paas_platform.cloud_service'].sudo()
        live = Svc.search([
            ('workspace_id', '=', workspace.id),
            ('state', '!=', 'deleting'),
        ])
        vcpu = sum(live.mapped('allocated_vcpu'))
        ram = sum(live.mapped('allocated_ram_gb'))
        storage = sum(live.mapped('allocated_storage_gb'))
        return (
            str(max(vcpu * 3, 8)),
            f"{max(int(ram * 3), 8)}Gi",
            f"{max(storage * 3, 100)}Gi",
        )

    def _compute_ns_ephemeral_quota(self, workspace):
        """#1434：ns ResourceQuota 的 ``requests/limits.ephemeral-storage``。

        與 ``_compute_ns_quota`` 同一個存活集（非 deleting），但按**服務數**而非方案
        規格算：ephemeral-storage 不是方案賣的資源，上限的目的只是「一個租戶不能把節點
        系統碟寫滿」。每個容器的預設上限由 operator 的 LimitRange 給（4Gi／request
        128Mi）；這裡的總額按每台服務 2Gi request／40Gi limit 估——R8 盤點單一 release
        最多 6 個容器（6×4Gi＝24Gi），滾動更新新舊 Pod 並存時翻倍約 48Gi，floor 80Gi
        蓋得住；額度太緊的代價是**開通／更新時 Pod 被 admission 拒絕**，所以寧寬勿緊。
        回傳字串 quantity（與其他 quota 欄位一致）。
        """
        Svc = self.env['woow_paas_platform.cloud_service'].sudo()
        live_count = Svc.search_count([
            ('workspace_id', '=', workspace.id),
            ('state', '!=', 'deleting'),
        ])
        return (
            f"{max(live_count * 2, 4)}Gi",
            f"{max(live_count * 40, 80)}Gi",
        )

    def _push_ns_quota(self, workspace, helm_namespace):
        """best-effort：重算並 PUT ns quota；operator 未設定 / 失敗只 log，
        絕不 abort 刪除。"""
        if not helm_namespace:
            return
        from ..services.paas_operator import get_paas_operator_client
        client = get_paas_operator_client(self.env)
        if not client:
            return
        cpu, mem, storage = self._compute_ns_quota(workspace)
        eph_request, eph_limit = self._compute_ns_ephemeral_quota(workspace)
        try:
            client.update_namespace_quota(
                helm_namespace, cpu, mem, storage,
                ephemeral_storage_request=eph_request,
                ephemeral_storage_limit=eph_limit,
            )
        except Exception as e:
            _logger.warning(
                "quota recompute PUT failed for %s: %s", helm_namespace, e,
            )

    def _do_cleanup(self):
        """Helm uninstall — permanently remove the service.

        Order matters: unlink sidecar-backed user-scope MCP → helm uninstall
        → state='deleting'. Re-ordering opens a time window where mcp_server
        rows reference a torn-down sidecar URL (silent network failures in
        AI chat tool calls).

        The ``sidecar_backed`` filter is load-bearing: only sidecar-backed
        user-scope MCP follows the service lifecycle; user-provided external
        MCP rows have URLs independent of this service and must survive the
        teardown.
        """
        self.ensure_one()
        _logger.info(
            "[CLOUD_SVC] _do_cleanup service=%s name=%s release=%s ns=%s",
            self.id, self.name, self.helm_release_name, self.helm_namespace,
        )

        # Step 1 — unlink sidecar-backed user MCP first, before the sidecar
        # is torn down. M:N: project may also bind other cloud_services whose
        # sidecar-backed MCP must SURVIVE — narrow by URL prefix to clear only
        # rows pointing at *this* service's sidecar. External-URL user MCP
        # (sidecar_backed=False) stays. Services without a sidecar identity
        # fall back to the broad filter (URL-prefix unknown).
        url_prefix = self._sidecar_url_prefix()
        user_mcps = self._collect_sidecar_user_mcps(url_prefix)
        if user_mcps:
            user_mcps.sudo().unlink()
            _logger.info(
                "[CLOUD_SVC] _do_cleanup unlinked %s user-scope MCP server(s) "
                "for service=%s", len(user_mcps), self.id,
            )

        from ..services.paas_operator import get_paas_operator_client

        # Step 2 — helm uninstall (physical sidecar teardown).
        client = get_paas_operator_client(self.env)
        if client and self.helm_release_name and self.helm_namespace:
            try:
                self._uninstall_release(client)
                _logger.info(
                    "[CLOUD_SVC] _do_cleanup helm uninstalled service=%s",
                    self.id,
                )
            except Exception as e:
                if getattr(e, 'status_code', None) == 404:
                    # #1424：release 本來就不在（開通被拒、從沒建立過，或已被別的路徑拆掉）
                    # ＝要的結果已經成立，與 `_cleanup_helm` 同一個判準。之前會寫成
                    # 「Cleanup failed」，而 lifecycle cron 每天再撞一次、永遠收不了尾。
                    _logger.info(
                        "[CLOUD_SVC] _do_cleanup release already gone service=%s",
                        self.id)
                else:
                    _logger.warning(
                        "[CLOUD_SVC] _do_cleanup helm uninstall failed service=%s: %s",
                        self.id, e,
                    )
                    self.write({
                        'state': 'error',
                        'error_message': _('Cleanup failed: %(error)s', error=e),
                    })
                    return

        # Step 2.5 — #744：拆掉本服務的 Cloudflare Access app（＋ reusable
        # policies）。放在 helm 之後、狀態改寫之前，理由與 SA 的 `_do_cleanup`
        # 同一條：寫 'deleting' 是一個**成功訊號**，在 CF 還留著一個 Access
        # app 的情況下寫下去，維運看到的「已清乾淨」與「清失敗」長得一模一樣。
        # 失敗改落 'error' + error_message，且 `access_app_id` 保留 ⇒ 下一輪
        # cron 重入時 `_teardown_access_if_any` 會再試一次。
        if not self._teardown_access_if_any():
            self.write({
                'state': 'error',
                'error_message': _(
                    'Cloudflare Access teardown failed; the Access '
                    'application is still live. Will retry on the next '
                    'cron pass.'),
            })
            _logger.warning(
                "[CLOUD_SVC] _do_cleanup incomplete service=%s access_app=%s "
                "(state=error)", self.id, self.access_app_id)
            return

        # Step 3 — mark state so cron/UI know teardown is in progress.
        self.write({'state': 'deleting'})

        # Step 4 — #278 A1：本 record 已 state=deleting、被 domain 排除，
        # 以存活集重算 ns quota 並權威覆寫（best-effort，不影響刪除）。
        self._push_ns_quota(self.workspace_id, self.helm_namespace)

    def _orphan_grace_period_days(self):
        """#1431：刪除時請 operator 把孤兒 PVC／DataVolume 的保留期縮短成幾天；``None``＝預設。

        從未進過 running（``first_running_at`` 空）的服務沒有租戶資料要保護 ⇒ 1 天；其餘照
        預設（正式站 90 天，#317）。
        """
        self.ensure_one()
        return None if self.first_running_at else NEVER_RUNNING_ORPHAN_GRACE_DAYS

    def _uninstall_release(self, client):
        """三條刪除路徑（租戶刪除 `_delete_service`、lifecycle `_do_cleanup`、`unlink` 兜底的
        `_cleanup_helm`）共用的 operator 呼叫——保留期的判準只在這裡（#1431）。

        曾經 running 的服務呼叫形狀與 #1431 之前完全相同（不帶 ``grace_period_days``）。
        """
        self.ensure_one()
        kwargs = {'subdomain': self.subdomain}
        grace = self._orphan_grace_period_days()
        if grace:
            kwargs['grace_period_days'] = grace
        return client.uninstall_release(
            self.helm_namespace, self.helm_release_name, **kwargs)

    def _cleanup_helm(self):
        """Idempotent helm uninstall for the direct-unlink / workspace-cascade
        path (F-ENV-5).

        Returns True when the release is gone — physically uninstalled,
        already-absent (operator 404, e.g. controller already ran its own
        uninstall before calling unlink), or operator unconfigured (dev/test,
        no live release to tear down). Returns False ONLY when a configured
        operator was asked to uninstall and genuinely failed (release still
        live). Does NOT write state — callers decide their own policy
        (``_do_cleanup``→state='error', ``unlink``→raise). KEEPS
        ``helm_release_name`` on failure so a retry still has the anchor
        (analogous to SecurityAccess keeping ``tunnel_id``); clearing it would
        make the helm release un-findable = a permanent orphan
        (cf-ns-orphan-no-account-gc).
        """
        self.ensure_one()
        if not (self.helm_release_name and self.helm_namespace):
            return True
        from ..services.paas_operator import (
            get_paas_operator_client,
            PaaSOperatorError,
        )
        client = get_paas_operator_client(self.env)
        if not client:
            return True  # operator unconfigured — no live release to tear down
        try:
            self._uninstall_release(client)
            return True
        except PaaSOperatorError as e:
            if getattr(e, 'status_code', None) == 404:
                return True  # already gone — idempotent success
            _logger.warning(
                "[CLOUD_SVC] _cleanup_helm failed service=%s: %s — kept for retry",
                self.id, e,
            )
            return False
        except Exception as e:
            _logger.warning(
                "[CLOUD_SVC] _cleanup_helm failed service=%s: %s — kept for retry",
                self.id, e,
            )
            return False

    @api.model
    def _assert_binding_authorized(self, service_ids):
        """#698：變更 project／task 與 cloud_service 的關聯需要 owner／admin。

        呼叫者是 ``cloud_service_binding_guard`` mixin（唯一入口；四個受保護
        欄位共用這一個判準）。``service_ids`` 已經是「這次寫入實際**新增 ∪
        移除**」的集合，兩個方向套同一條線。

        為什麼 ir.rule 不夠：Many2many 的 ``(3)``/``(4)``/``(6)`` 只改 relation
        表，Odoo 不會對 comodel 做任何權限檢查（#698；完整說明見
        ``cloud_service_binding_guard.py`` 檔頭）。

        判準走 ``workspace.check_user_access(user, required_role=ROLE_ADMIN)``
        （``ROLE_HIERARCHY`` ⇒ admin 與 owner 通過），刻意不自己再寫一套 role
        比較——#556 的根因就是同一個判準有兩種寫法。

        ⚠ 一律以 ``sudo()`` 解析 service 與 workspace：判準要看**真實歸屬**，
        不能被呼叫者自己的讀取權限左右。讀不到的 service 若因此被跳過檢查，
        守衛就會在「攻擊者對受害者 service 沒有 read」——也就是最該擋的那格
        ——失效。``check_user_access`` 收的 ``user`` 仍是原始呼叫者
        （``sudo()`` 不改 uid）。
        """
        if not service_ids or self.env.su:
            return
        user = self.env.user
        services = self.sudo().browse(sorted(service_ids)).exists()
        denied = services.filtered(
            lambda svc: not svc.workspace_id.check_user_access(
                user, required_role=ROLE_ADMIN)
        )
        if denied:
            raise AccessError(
                _("Only a workspace owner or admin can change cloud service "
                  "bindings (adding and removing alike). Denied cloud "
                  "services: woow_paas_platform.cloud_service %s")
                % ', '.join('#%s' % svc_id for svc_id in denied.ids)
            )

    def _assert_subscription_binding(self, check_state=True):
        """#519 提案 B：`subscription_id` 的**綁定當下**判準。

        不變式（守衛成立後）：任一 cloud_service 的 `subscription_id` 在**綁上去
        的那一刻**必然是一筆 `sh_partner_id == workspace_id.owner_id.partner_id`
        且 `state == 'in_progress'` 的訂閱；再配合 DB 的 partial unique index，
        一筆 in_progress 訂閱最多被一筆 cloud_service 指向。

        ## 🔴 這是 transition guard，不是常駐不變式

        訂閱到期／關閉之後**仍然合法地**綁在 service 上——`_cron_enforce_
        subscription_lifecycle` 正是靠「找得到指向已過期訂閱的服務」才做得到停權。
        所以 `state` 這一格只在**綁定真的改變的那一次**檢查（`check_state=True`），
        事後不回頭驗。寫成常駐不變式的話，任何一次無關的 write 都會把「訂閱已到期
        但服務還在停權流程裡」這個**正常狀態**判成違規。

        ## 兩側為什麼都不可由呼叫端控制

        * 訂閱側：租戶對 `sh.subscription.subscription` 零 ACL row；他唯一造得出
          訂閱的路是第三方 addon 的 `/renew-subscription`（sudo controller，另見
          #725），而那條鑄出來的停在 `state='draft'` ⇒ 過不了 `in_progress` 這格。
          **只驗歸屬不驗狀態會被無限鑄幣打穿**，這一格不可省。
        * workspace 側：`owner_id` 只能指向呼叫者自己（`workspace.py` 的 create 端
          守衛），且事後不可改（#658 的 identity anchor）⇒ 可控方向與判準要求的
          方向一致。

        兩側都用 `.sudo()` 讀：這是**授權判斷**，不可以被呼叫者自己的讀取權限縮窄
        （縮窄＝漏檢）。租戶對訂閱本來就沒有讀權限，不 sudo 這裡會變成 AccessError
        而不是判斷。
        """
        for record in self.sudo():
            sub = record.subscription_id
            if not sub:
                continue
            owner_partner = record.workspace_id.owner_id.partner_id
            if not owner_partner or sub.sh_partner_id != owner_partner:
                raise SubscriptionBindingError(_(
                    "This subscription does not belong to the owner of this "
                    "service's workspace and cannot be bound."))
            if check_state and sub.state != 'in_progress':
                raise SubscriptionBindingError(_(
                    'Only an active (in_progress) subscription can be bound. '
                    'Current state: %(state)s.',
                    state=sub.state or 'unset'))

    def _assert_reverse_binding_authorized(self, vals):
        """#698 反向端：從 **service 側**寫 `project_ids`（同一張 relation 表
        的另一頭）。

        ⚠ **這一端不能用 `cloud_service_binding_guard` 的 after-diff**，原因是
        結構性的，不是實作偷懶：那個 mixin 在 ``super().write()`` **之後**比對
        差異再判斷授權，而這一端的判準是「呼叫者對那個 project 有沒有 write」
        ——綁定一旦寫進去，那個 project 就落進呼叫者的 bound 分支
        （`cloud_service_ids.workspace_id.access_ids.user_id`），事後檢查反而
        **通過**。實測過：接上 mixin 之後攻擊照樣成功（RPC 回 True）。
        自我實現的判準必須前置評估。

        判準用 Odoo 自己的 ``check_access('write')``，不複製一套 project 授權
        邏輯——`project_rule_workspace_member` 有 bound／unbound 兩條分支，
        抄過來必然漂。

        ## 候選 id 收集為什麼是**刻意的**超集（不要「修」成精確判斷）

        下面只從指令 tuple 的 id 位置撈整數，**不判斷 add／remove、不套用指令
        語意**，再併上**寫入前**的 `self.project_ids`。這不是還沒做完——它是
        本修法的核心取捨：

        - 精確判斷 add／remove 就必須重演 x2many 的指令語意，而那正是同一個
          issue 在 project／task 側**刻意避開**的陷阱（見
          `cloud_service_binding_guard` 檔頭與 #657：逐一重演框架的解析規則，
          就是會漏掉下一個形態）。這一端不能用 after-diff（理由見上），所以
          「不重演」的代價只能由「收保守超集」來付。
        - 反過來若改成精確集合，漏判一個形態的後果是**靜默放行**——與本 issue
          的原始缺陷同一種失敗模式。

        ⇒ 收窄這裡之前先問：新寫法在**未知的**指令形態下會漏還是會過寬？只有
        「仍然只會過寬」的寫法才可以取代它。

        ## 這個候選集實際保證什麼（#705 F3：舊版的宣稱是假的）

        ⚠ 舊版只撈指令裡的 id，於是 `(5, 0, 0)`（`command[1]` 是 falsy、
        `command[2]` 非 list）與 `(6, 0, [])` 的候選集是**空的**，而
        `(6, 0, [保留的])` 永遠不含被移除的那些 ⇒ **移除方向整條沒有被檢查**，
        卻在同一份 docstring 裡寫著「撈到的是超集、只會過寬不會過窄」。
        那句話對移除方向為假，而它是寫給未來維護者的判準。

        現在的保證，逐方向寫清楚：

        - **新增方向**：新綁上的 project id 必然出現在指令 tuple 裡
          ⇒ 落在候選集內。唯一例外是 `(0, 0, vals)`（就地新建 project）——見
          下一節，那一格**兩端都沒有檢查**。
        - **移除方向**：被解掉的 project 必然在**寫入前**的 `self.project_ids`
          裡 ⇒ 由下面那行覆蓋，與指令形態無關。⚠ 那次讀取帶
          `active_test=False`（#716 ②）：`project.project` 有 `active`，預設
          會在 `Many2many.convert_to_record_multi` 那一步把已封存的 project
          從 recordset 濾掉，relation 那一列卻還在。
        - ⇒ 候選集 ⊇（新增 ∪ 移除）\\ {`(0, 0, vals)` 新建的那些}。

        🔑 `active_test=False` 那一格**不是**在補一個可被利用的漏洞——實測見
        `test_project_cs_m2m_authz_715_716.py` ::
        `TestReverseCandidateArchivedProject716` 的 `B5`：
        `Many2many.write_real` 算 `old_relation` 用的是同一個 `record[fname]`，
        `active_test` 對它一樣生效 ⇒ `(3)` / `(5)` / `(6)` 全都**動不到**已封存
        的那一列。舊寫法沒出事，是因為**框架兩邊一起瞎**，不是因為候選集真的
        是超集。加上 `active_test=False` 之後，「候選集 ⊇ 實際變動」這句話自己
        成立，不再掛在框架某個沒寫下來的行為上；方向只會過寬，符合上一節那條
        「只有仍然只會過寬的寫法才可以取代它」。

        ## `(0, 0, vals)`：兩端都沒有檢查（#705 F6 → #716 ③ 更正）

        🔴 **舊版這一格的理由是假的，而且是「更正動作本身鑄出新假宣稱」的
        第二輪。** 舊版寫「那個 project 是呼叫者自己建的、且它的 `create` 會走
        `project.project` 側的 mixin」——第二句在授權上不是 load-bearing，實測
        那個 mixin 一次都沒被呼叫（`test_project_cs_m2m_authz_715_716.py` ::
        `TestNestedCreateCommand0Claim716` 的 `B6`，`examined=[]`）。

        機制：`Many2many.write_real` 對 `(0, 0, vals)` 先 `comodel.create(vals)`，
        relation 那一列是**之後**用直接 SQL insert 完成的——不是「建出來的
        project 收到一份帶 `cloud_service_ids` 的 vals」。所以巢狀 vals 本身
        不含載體 key 時，project 側的 mixin 看不到任何要比對的欄位。這是
        `(4)`／`(6)` 那個原始缺陷的**同一個形狀**，只是換成 `(0)`。

        指得出證據的部分只有這些，就只寫這些：

        - 巢狀 vals **自己**帶載體 key（含經 `default_` context 補進來的）時，
          project 側 mixin 看得見 ⇒ 擋得住（`B7` / `B8`）。
        - 剩下的窄縫＝「純 `(0, 0, {'name': ...})`」。那個 project 是這次呼叫
          當場建出來的，**沒有第三方受害者**（本方法的判準是「呼叫者對那個
          project 有沒有 write」，而新建的 project 不屬於任何別人）；能走到
          這一行的呼叫者本來就對這顆 service 有 write（#676 的 rule ⇒
          owner／admin）。⇒ 今天不構成越權。
        - ⚠ 「今天不構成越權」不等於「有守線」。若日後 `project.project` 的
          建立側出現「呼叫者建得出別人名下的 project」這種形態，這一格就要
          重新評估——不要因為這裡寫著「不是提權面」就跳過。

        ## 與 project 側的不對稱（刻意，且只有這裡記著）

        同一個 `(6, 0, [])`：**project 側擋得住**（mixin 的 after-diff 看得見
        「綁定少了一顆」，判準是 service 的 workspace 角色，寫入前後都一樣
        ⇒ after-diff 安全）；**反向端擋不住**，因為這一端的判準
        （`project.check_access('write')`）會被寫入本身改掉，只能前置評估、
        只能靠候選集是超集。兩端用不同機制不是實作不一致，是判準性質不同。

        ⚠ 併入舊值不會誤傷合法呼叫者：能走到這裡的人必然是該 service 所屬
        workspace 的 owner／admin（#676 的 rule），而
        `project_rule_workspace_member` 的 bound 分支
        （`cloud_service_ids.workspace_id.access_ids.user_id`，**無 role 條件**）
        讓該 workspace 的任何成員對「綁著這顆 service 的每一個 project」都有
        write ⇒ 多檢查到的那些必然通過。行為面由 `Z4` 鎖住。
        """
        commands = vals.get('project_ids')
        if not commands or self.env.su:
            return
        # 移除方向的舊值：與指令形態無關，`(5)`／`(6, 0, [])` 也涵蓋得到。
        # sudo ⇒ 看真實歸屬，不被呼叫者自己的讀取權限縮窄（縮窄＝漏檢）。
        # `active_test=False` 同理（#716 ②）：`project.project` 有 `active`，
        # 而 `Many2many.convert_to_record_multi` 會依 env context 的
        # `active_test` 把已封存的 comodel record 濾掉（`Many2many.read` 自己
        # 撈 SQL 時帶的 `active_test=False` 救不到這一步）⇒ relation 那一列
        # 還在、候選集卻沒有它。⚠ 這一格今天不可利用（框架的 `write_real`
        # 同樣看不到那一列 ⇒ 根本解不掉），加它是為了讓「候選集 ⊇ 實際變動」
        # 自己成立——完整論證見上面 docstring 的 🔑 段。
        candidates = set(self.sudo().with_context(active_test=False).project_ids.ids)
        for command in commands:
            if not isinstance(command, (list, tuple)) or len(command) < 2:
                continue
            if isinstance(command[1], int) and command[1]:
                candidates.add(command[1])
            if len(command) > 2 and isinstance(command[2], (list, tuple)):
                candidates |= {i for i in command[2] if isinstance(i, int)}
        if candidates:
            self.env['project.project'].browse(sorted(candidates)) \
                .exists().check_access('write')

    def write(self, vals):
        """#279 Path B: a CloudService's ``workspace_id`` / ``project_ids`` binding
        change shifts which workspaces its bound projects can reach, so re-render the
        agent isolation legs. project.write (Path A) covers ``cloud_service_ids``
        changes made from the project side; this covers changes made from the service
        side, which fire no project hook even though ``project.workspace_ids``
        recomputes.
        """
        # #744：Access 身分錨點是硬不變式，對**所有人**（含 superuser、含
        # sudo'd controller）擋，唯一豁免是模組私有 sentinel。放在方法第一行
        # ——後面每一段都有副作用或成本，被拒絕的呼叫不該碰到其中任何一段。
        _reject_cs_access_identity_anchor_writes(self.env, vals)
        _reject_init_control_writes(self.env, vals)
        _reject_observation_control_writes(self.env, vals)
        _reject_timeout_recovery_control_writes(self.env, vals)
        _reject_upgrade_failure_control_writes(self.env, vals)
        _reject_service_history_writes(self.env, vals)
        _reject_chart_version_writes(self.env, vals)
        # #698 反向端：前置檢查（見 `_assert_reverse_binding_authorized` 的
        # docstring——這一端的判準會被寫入本身改掉，事後檢查無效）。
        self._assert_reverse_binding_authorized(vals)
        # [F: revoke-txn low] A deployed CS's namespace identity (paas-ws-{hash}) is
        # fixed at create; retargeting workspace_id would leave the pod in the OLD ns
        # while agent egress is re-pointed at the new workspace → silent reach break to
        # the still-bound CS. Reject reassignment on a live-namespace service.
        if 'workspace_id' in vals:
            for cs in self:
                if (cs.helm_namespace and cs.workspace_id.id
                        and vals['workspace_id'] != cs.workspace_id.id):
                    raise UserError(
                        _("The workspace of a deployed cloud service cannot "
                          "be changed; its namespace identity is fixed at "
                          "creation.")
                    )
        # ⚠ 這裡**刻意沒有**全域上限守衛。曾經有過（#501 B3，擋「先建成 `deleting`
        # 再 write 回計數態」的繞道），已移除，理由記在 #519 與 PR #517 的第二輪覆核：
        #
        # 1. 它會誤傷刪除失敗的善後路徑。`_delete_service` 先寫 `state='deleting'`，
        #    uninstall 失敗後在 except 分支寫 `state='error'`——那是「從終態寫回非
        #    終態」，守衛判定為進入者。而裁決點 `_reject_reason_for_new_instance`
        #    第一件事是 paused 檢查、**在計數之前就 return** ⇒ 營運一勾「暫停販售」，
        #    任何刪除失敗都會 raise、整筆 rollback，`error_message` 遺失、狀態退回，
        #    租戶看到的是「目前暫停販售」。與設定畫面 help 明載的「只擋新下單，不影響
        #    已開通的服務」直接矛盾。
        # 2. 它擋不完。`write({'template_id': …})` 不帶 state ⇒ 守衛不求值；
        #    `state` 是 nullable Selection（無 `required=True`），插一個
        #    `write({'state': False})` 當跳板即可整段跳過。
        #
        # ⚠ 這段原本寫著「正解是收緊 ACL……create() 的閘門是防意外、不是安全
        # 邊界——不要在這裡補守衛來製造已經守住的錯覺」。**那個前提在 #519 已被
        # 推翻**，整段改寫如下，否則 repo 裡會留下一句權威語氣的話，教未來維護者
        # 不要信現在真的存在的那道守線。
        #
        # 為什麼「收緊 ACL」不是正解：owner 資格**可自我滿足**——任何
        # `base.group_user` 帳號免費自建 workspace 就自動成為該 workspace 的
        # owner，滿足 #676 的 `cloud_service_rule_owner_admin_manage`。而「自建
        # workspace 即為 owner」是**產品設計**（2026-08-14 裁決），所以 ACL／
        # ir.rule 那層對本案結構性無效，不動它。
        #
        # 現在的分工：**`create()` 的閘門就是安全邊界**（模組頂端
        # `_INTERNAL_PROVISION`，對所有入口含 superuser 一律生效），它保證沒有
        # 任何 RPC 呼叫端建得出 cloud_service。上面那兩條「為什麼這裡不放全域上限
        # 守衛」的理由**仍然成立且仍然適用**（誤傷刪除失敗的善後路徑、擋不完），
        # 不因為 create 有閘了就該把它加回來——那是配額語意，不是身分語意。
        #
        # 真正還開著、且**要在 write() 這一端處理**的，是 `subscription_id` 的
        # 歸屬／狀態／唯一性（租戶可把自己 service 的 subscription_id 指向別人的
        # 活訂閱以逃避停權）。那是 #519 提案 B，見
        # `docs/decisions/519-cloud-service-provision-gate.md`。
        # #519 提案 B：`subscription_id` 的綁定守衛（write 端）。
        #
        # `write()` **不**套 `default_get`（`_add_missing_default_values` 在
        # `odoo/models.py` 全檔唯一呼叫點在 `_prepare_create_values` 裡，只有 create
        # 會經過），所以這一端直接讀 vals 是安全的——create 端才需要讀落地值。
        #
        # 只在綁定**真的改變**時檢查：整批送出未修改欄位（前端常態）不得誤擋，而且
        # 事後回頭驗會把「訂閱已到期、服務還在停權流程裡」這個正常狀態判成違規。
        rebinding = self.browse()
        if 'subscription_id' in vals:
            new_sub_id = vals['subscription_id'] or False
            rebinding = self.filtered(
                lambda r: (r.subscription_id.id or False) != new_sub_id)
            if not new_sub_id and rebinding:
                # 解除綁定對誰都不開放：`_cron_enforce_subscription_lifecycle` 的
                # domain 是 `subscription_id != False` ⇒ 清空指標＝讓這筆服務**永久**
                # 脫離生命週期管理（停權／結算都再也摸不到它）。平台自己也沒有任何
                # 一條路徑會這樣寫（全 repo 只有兩處寫這個欄位，都是指向新訂閱）。
                raise SubscriptionBindingError(_(
                    'A service cannot be unbound from its subscription.'))
        tracked = 'workspace_id' in vals or 'project_ids' in vals
        projects = self.mapped('project_ids') if tracked else self.browse()
        old = {p.id: set(p.workspace_ids.ids) for p in projects}
        # #1257 Q10：等待錨點在每次**進入** initializing 時重新計時。集中在這裡而不是
        # 逐路徑 stamp（背景 poll、resume……；pitfall
        # reaped-since-single-path-stamp-gc-immunity）：新路徑寫進 initializing 時
        # 自動跟上。明寫 `init_wait_since` 的呼叫端（migration）優先。
        # #1159：同一個咽喉順帶清掉上一段 initializing 的 ready 觀測——剛進來的服務在
        # 背景 poll 看過之前一律算「還沒觀測到 ready」，不沿用舊值排進 init cron。
        entering_init = self.browse()
        if vals.get('state') == 'initializing' and 'init_wait_since' not in vals:
            entering_init = self.filtered(lambda r: r.state != 'initializing')
        # episode 必須看到 tick 之間的 lifecycle 中斷；只靠 monitor 再查一次會漏掉
        # Ready 撤回又恢復、state 退出又進入或開新 generation。無關／同值 write 不重開。
        new_capacity_episode = self.filtered(lambda service: (
            ('state' in vals and service.state != vals['state'])
            or ('init_generation' in vals and service.init_generation != vals['init_generation'])
        ))
        # 同值 state 或無關記帳不給新 grace；在 super() 前挑出真正進入者，
        # mixed recordset 也只替該 subset 建立新的等待 episode。
        entering_observation_episode = self.browse()
        if vals.get('state') in OBSERVATION_TRANSITION_STATES:
            entering_observation_episode = self.filtered(
                lambda r: r.state != vals['state'])
        # #1159 Q30 的 reconfigure 放行只認「init 額度出口」造成的 error：服務一離開
        # error（刪除、redeploy……）`init_exit` 就不再描述現況，之後刪除失敗等路徑寫回
        # 的 error 不得沿用它。停權（error → suspended → error）不算離開：Q25 要求停權
        # 前是 error 的維持 error、額度不重置，出口也跟著保留。
        leaving_init_exit = self.browse()
        if ('state' in vals and 'init_exit' not in vals
                and vals['state'] not in ('error', 'suspended')):
            leaving_init_exit = self.filtered(
                lambda r: r.init_exit and r.state != vals['state'])
        # #1416：`transition_timeout_at` 說的是「目前這則 error_message 是部署逾時寫的」。
        # 改寫 error_message（別的來源接手，或推回正常時清空）、或離開 error 都讓它失效——
        # 自癒 cron 因此只會碰到真正由逾時造成、之後沒人動過的 error。停權（error →
        # suspended → error，Q25）不算離開：復權回到 error 後仍可自癒。集中在這裡而不是
        # 逐路徑清（pitfall reaped-since-single-path-stamp-gc-immunity）。
        leaving_timeout = self.browse()
        if 'transition_timeout_at' not in vals and (
                'error_message' in vals
                or ('state' in vals and vals['state'] not in ('error', 'suspended'))):
            leaving_timeout = self.filtered('transition_timeout_at')
        # #1552：`upgrade_failed_at` 與逾時標記同一套生命週期——它說的是「目前這則
        # error_message 是背景 upgrade 失敗寫的」，別的來源接手或離開 error 就失效。
        leaving_upgrade_failure = self.browse()
        if 'upgrade_failed_at' not in vals and (
                'error_message' in vals
                or ('state' in vals and vals['state'] not in ('error', 'suspended'))):
            leaving_upgrade_failure = self.filtered('upgrade_failed_at')
        # #1431：第一次進 running 就記下來（之後不再動）。集中在這裡，任何把 state 寫成
        # running 的路徑（poll、resume、init 完成、逾時自癒……）自動跟上。明寫的呼叫端優先。
        first_running = self.browse()
        if vals.get('state') == 'running' and 'first_running_at' not in vals:
            first_running = self.filtered(lambda r: not r.first_running_at)
        # #745：Access 設定的寫入與 operator sync 必須是同一個原子單位。
        #
        # 沒有 savepoint 時的失效形狀：BFF 端點把 sync 的例外收成
        # `{'success': False, ...}` 這個**正常回應**回去，交易照樣 commit ⇒ DB
        # 宣稱「已保護」而 Cloudflare 上什麼都沒有。方向是 fail-open——租戶看到
        # 一個開著的開關，服務其實完全公開。SA route 的 write() 為同一個理由包
        # savepoint（那邊的註解也寫明：這只保證 Odoo 這一側原子，operator 那側
        # 是宣告式全狀態推送、沒有補償動作，殘留分歧靠下一次冪等 re-sync 收斂）。
        #
        # 只在真的碰到 access 欄位時才開 savepoint：其餘寫入路徑（每分鐘的 poll、
        # helm 狀態回寫…）一行都不變，不替它們付 savepoint 的成本。
        access_sync = (
            not _is_cs_sync_control(self.env, _CTX_CS_SKIP_ACCESS_SYNC)
            and any(k in vals for k in _CS_ACCESS_SYNC_FIELDS))
        with contextlib.ExitStack() as stack:
            if access_sync:
                stack.enter_context(self.env.cr.savepoint())
            res = super().write(vals)
            if entering_observation_episode:
                # 命令時間只作 episode 邊界；observed_at 仍保留真實 status poll 證據。
                super(CloudService, entering_observation_episode).write({
                    'observation_episode_started_at': fields.Datetime.now(),
                })
            if entering_init:
                entering_vals = {
                    'init_wait_since': self._init_db_now(),
                    # D2-R2：同代重新進 initializing（含 resume）是 operator wait 的重置邊界。
                    'init_wait_spent_seconds': 0,
                    'init_wait_started_at': False,
                    'init_retry_eligible_at': False,
                    'init_queue_turn': False,
                }
                for key in ('init_ready_observed', 'init_ready_since', 'n8n_owner_alerted_at'):
                    if key not in vals:
                        entering_vals[key] = False
                super(CloudService, entering_init).write(entering_vals)
            capacity_fields = (_INIT_CONTROL_FIELDS - {
                'init_capacity_wait_since', 'n8n_owner_alerted_at'}) | {
                'state', 'template_id'}
            if capacity_fields.intersection(vals):
                tracked_capacity = self.filtered('init_capacity_wait_since')
                if tracked_capacity:
                    now = self._init_db_now()
                    reset_capacity = tracked_capacity.filtered(
                        lambda service: service in new_capacity_episode
                        or not service._init_capacity_wait_eligible(now))
                    if reset_capacity:
                        super(CloudService, reset_capacity).write({'init_capacity_wait_since': False})
            if leaving_init_exit:
                super(CloudService, leaving_init_exit).write({'init_exit': False})
            if leaving_timeout:
                super(CloudService, leaving_timeout).write({'transition_timeout_at': False})
            if leaving_upgrade_failure:
                super(CloudService, leaving_upgrade_failure).write({'upgrade_failed_at': False})
            if first_running:
                super(CloudService, first_running).write({
                    'first_running_at': fields.Datetime.now(),
                })
            if rebinding:
                rebinding._assert_subscription_binding()
            elif 'workspace_id' in vals:
                # 換 workspace 會換掉判準的另一半（owner 的 partner）。只驗歸屬、不驗
                # 狀態——這一次沒有動到綁定，訂閱早就到期也仍然合法。
                self._assert_subscription_binding(check_state=False)
            if tracked:
                # Union of projects bound before AND after (a project_ids write can add or
                # drop bindings); each reconciles against its own pre-write reach set.
                for project in (projects | self.mapped('project_ids')):
                    project._reconcile_agent_runtime_reach(old.get(project.id, set()))
            if access_sync:
                # liveness gate 在 `_sync_access_to_operator` 裡（沒有 subdomain
                # ／deleting／從未保護過都是 no-op），這裡只負責轉發。
                for service in self:
                    service._sync_access_to_operator()
        return res

    @api.model
    def _backfill_reach_isolation_p5(self):
        """#279 P5 one-time backfill (invoked from migration 18.0.1.0.42).

        (a) CloudService namespaces — apply the O4 baseline NP bundle + re-stamp
            workspace-id/env labels (leg-1 egress selector + baseline NP4 key on them)
            on every CS namespace created before O4/O1.
        (b) Agent-runtime namespaces — re-stamp the env label (+ reach-ws) so a legacy
            agent ns matches NP4's env predicate once #280 removes the chart from-less
            (else the running agent silently loses reach to its bound CS).

        Idempotent, re-runnable, per-ns logged, best-effort (one failure never aborts
        the sweep — a re-run + the enforcing cron converge the rest). MUST run before
        #280. Returns a per-leg ok/fail tally.
        """
        from ..services.paas_operator import get_paas_operator_client
        from ..services.agent_runtime_client import get_agent_runtime_client
        ns_client = get_paas_operator_client(self.env)
        agent_client = get_agent_runtime_client(self.env)

        cs_done = cs_fail = ar_done = ar_fail = 0

        # (a) CloudService namespaces — addressed by their stored helm_namespace.
        if ns_client:
            services = self.sudo().search([
                ('helm_namespace', '!=', False),
                ('workspace_id', '!=', False),
            ])
            for svc in services:
                try:
                    ns_client.backfill_baseline_netpol(
                        svc.helm_namespace, svc.workspace_id.id)
                    cs_done += 1
                except Exception as exc:
                    cs_fail += 1
                    _logger.warning(
                        "P5 backfill: CS ns %s failed: %s", svc.helm_namespace, exc)
        else:
            _logger.warning("P5 backfill: PaaS operator unconfigured — CS leg skipped")

        # (b) Agent-runtime namespaces — addressed by hash (operator derives the ns).
        if agent_client:
            Session = self.env['woow_paas_platform.agent_session'].sudo()
            sessions = Session.search([
                ('project_namespace_hash', '!=', False),
                ('project_id.use_agent_runtime', '=', True),
            ])
            for session in sessions:
                try:
                    agent_client.restamp_labels(
                        session.project_namespace_hash,
                        reach_workspace_ids=session.project_id.workspace_ids.ids)
                    ar_done += 1
                except Exception as exc:
                    ar_fail += 1
                    _logger.warning(
                        "P5 backfill: agent ns (hash %s) failed: %s",
                        session.project_namespace_hash, exc)
        else:
            _logger.warning(
                "P5 backfill: agent-runtime operator unconfigured — agent leg skipped")

        _logger.info(
            "P5 backfill done: CS %s ok / %s fail; agent %s ok / %s fail",
            cs_done, cs_fail, ar_done, ar_fail)
        return {"cs_done": cs_done, "cs_fail": cs_fail,
                "ar_done": ar_done, "ar_fail": ar_fail}

    @api.model
    def _allocated_vals_from(self, template, plan):
        """#332：規格顯示值——plan cs_* 優先、0 值 fallback template.min_*。"""
        return {
            'allocated_vcpu': plan.cs_vcpu or template.min_vcpu,
            'allocated_ram_gb': plan.cs_ram_gb or template.min_ram_gb,
            'allocated_storage_gb': plan.cs_storage_gb or template.min_storage_gb,
        }

    @api.model
    def _plan_storage_size(self, current_size, plan_gb):
        """#1158 Q2：既有服務套方案時 ``persistence.size`` 的規則——**只擴不縮**。

        ⚠ 暫定做法（討論票 #1512）：改策略只改這一個函式。PVC 不能縮（K8s API 直接拒絕，
        整次 helm upgrade 失敗），所以新方案較小時保留目前大小、只調 CPU／RAM／workers。
        現值缺 ⇒ 用方案值；現值看不懂 ⇒ 保留原字串（不冒險送出可能更小的值）。
        """
        plan_size = f'{plan_gb}Gi'
        if not current_size:
            return plan_size
        current_gib = _storage_quantity_gib(current_size)
        if current_gib is None:
            _logger.warning(
                "#1158 persistence.size %r 無法解析，保留原值不套方案 %sGi",
                current_size, plan_gb)
            return current_size
        return current_size if current_gib >= plan_gb else plan_size

    def _current_plan(self):
        """服務目前訂閱的方案（sudo；沒有訂閱回空 recordset）。"""
        self.ensure_one()
        return self._sub().sh_subscription_plan_id

    def _plan_values_for(self, plan):
        """#1158：這台**既有**服務改套 ``plan`` 之後應存的 helm values（新 dict，不寫 DB）。

        與 create 同一條 overlay，但走既有服務模式（儲存只擴不縮、不碰 Bitnami 字串替換）。
        woow-odoo values 缺 ``conf`` 時照 overlay 的 fail-loud raise ``ValueError``。
        """
        self.ensure_one()
        values = copy.deepcopy(self._helm_values_dict() or {})
        return self._apply_plan_spec_overlay(
            values, self.template_id, plan, existing=True)

    def _plan_allocated_vals(self, plan, values):
        """#1158 Q4：``plan`` 套在 ``values`` 上時帳面 ``allocated_*`` 應有的值。

        CPU／RAM 照 ``_allocated_vals_from``；儲存記**實際**大小（只擴不縮之下可能大於
        方案值），讀不到實際大小時退回方案值。大小從 chart 真正讀的路徑取（#1532）。
        """
        self.ensure_one()
        vals = self._allocated_vals_from(self.template_id, plan)
        if plan.cs_storage_gb:
            size = _value_at(values, _plan_spec_paths(self.template_id)['storage'])
            gib = _storage_quantity_gib(size) if size else None
            if gib is not None:
                vals['allocated_storage_gb'] = math.ceil(gib)
        return vals

    def _plan_change_allocated_vals(self, old_plan, new_plan, old_values, new_values):
        """#1158 Q4：換方案要寫的 ``allocated_*``（只回有變的欄位）。

        逐欄比對「現值」與「舊方案推得的值」：不相等＝營運手調過（例：prod service 174
        刻意調大 allocated 來放大 ns quota），保留現值、只 log——租戶換方案不該把它洗掉。

        #1532：「舊方案推得的值」以「舊 values 疊上舊方案」計（與部署時
        ``_assemble_deploy_values`` 同一條 overlay）。overlay 已寫對路徑的服務這是
        no-op；#1532 以前開通的 n8n 存的是範本的 ``main.persistence.size``（2Gi），直接讀
        會被誤判成營運手調而保留舊的 ``allocated_storage_gb``。
        """
        self.ensure_one()
        try:
            old_effective = self._apply_plan_spec_overlay(
                copy.deepcopy(old_values or {}), self.template_id, old_plan, existing=True)
        except ValueError:
            old_effective = old_values
        expected_old = self._plan_allocated_vals(old_plan, old_effective)
        target = self._plan_allocated_vals(new_plan, new_values)
        vals = {}
        for field, new in target.items():
            current = self[field]
            if abs((current or 0) - (expected_old[field] or 0)) > 1e-6:
                _logger.info(
                    "[CLOUD_SVC] change_plan service=%s 保留手調的 %s=%s"
                    "（舊方案推得 %s，新方案 %s）",
                    self.id, field, current, expected_old[field], new)
                continue
            if abs((current or 0) - (new or 0)) > 1e-6:
                vals[field] = new
        return vals

    @api.model
    def _apply_plan_spec_overlay(self, values, template, plan, existing=False):
        """#332 runtime overlay：把選定 plan 的 cs_* 疊進 helm values。

        刻意不改 template seed（避開 sticky noupdate）；cs_* 為 0 的維度一律
        不碰＝與現狀 bit-for-bit 相同。就地疊加並回傳同一 dict。

        #1158 ``existing=True``（既有服務：換方案、``_assemble_deploy_values`` 重算）：
        儲存走 ``_plan_storage_size``（只擴不縮），且不做下方 Bitnami 字串替換——
        那段只認 seed 原文 ``workers = 0``，建好之後就已被換掉，重算必定 raise。

        workers／memory 用精確字串替換（裁定② 2026-07-12 by Eugene）：seed 內
        為 `echo 'workers = 0'`（非裸行），故 body.replace + 逐 marker「全 script
        合計恰 1 處」的 count 斷言 fail-loud——base 漂移（0 或多處）即 raise、
        不得靜默 no-op。

        #1130：woow-odoo chart 的 template 改寫 typed `conf.workers`／
        `conf.limitMemory{Hard,Soft}`（同一組公式）；上面的字串替換只剩 Bitnami 形。

        #1532：規格寫到 ``_plan_spec_paths(template)``——chart 真正讀的路徑（n8n 是
        ``main.resources``／``main.persistence.size``）。路徑不是頂層的 chart，順手移除舊
        overlay 留在頂層的殘值（``_drop_legacy_top_level_plan_spec``）。
        """
        paths = _plan_spec_paths(template)
        if plan.cs_vcpu:
            res = _values_dict_at(values, paths['resources'])
            for key in ('requests', 'limits'):
                _values_dict_at(res, (key,))['cpu'] = str(plan.cs_vcpu)
        if plan.cs_ram_gb:
            mem = (f'{int(plan.cs_ram_gb)}Gi'
                   if float(plan.cs_ram_gb).is_integer()
                   else f'{int(plan.cs_ram_gb * 1024)}Mi')
            res = _values_dict_at(values, paths['resources'])
            for key in ('requests', 'limits'):
                _values_dict_at(res, (key,))['memory'] = mem
        if plan.cs_storage_gb:
            *parent_path, size_key = paths['storage']
            persistence = _values_dict_at(values, parent_path)
            persistence[size_key] = (
                self._plan_storage_size(persistence.get(size_key), plan.cs_storage_gb)
                if existing else f'{plan.cs_storage_gb}Gi')
        if paths is not _PLAN_SPEC_DEFAULT_PATHS and (
                plan.cs_vcpu or plan.cs_ram_gb or plan.cs_storage_gb):
            _drop_legacy_top_level_plan_spec(values)
        conf = values.get('conf')
        if plan.cs_odoo_workers and template._is_woow_odoo_chart():
            # #1130：woow-odoo 的 odoo.conf 是 chart render 的 Secret，plan 只能寫 typed
            # `conf.*` 鍵（#1160 plan overlay 答案；公式沿用下方 Bitnami 版：hard 0.85／
            # soft 0.70 per worker）。seed 沒有 `conf` 區塊＝寫不進去 ⇒ fail-loud，
            # 不靜默退回 chart 預設（workers 2、每 worker 2GiB，會被 plan 的 pod 記憶體
            # 上限 OOM）。何時對既有服務重算屬 #1158，這裡只是 create 當下的同一條 overlay。
            if not isinstance(conf, dict):
                raise ValueError(
                    "woow-odoo template 的 helm values 缺 conf 區塊，"
                    "cs_odoo_workers 無處可寫（fail-loud，不靜默 no-op）")
            n = plan.cs_odoo_workers
            conf['workers'] = n
            if plan.cs_ram_gb:
                ram_bytes = int(plan.cs_ram_gb * 1024 ** 3)
                conf['limitMemoryHard'] = int(ram_bytes * 0.85 / n)
                conf['limitMemorySoft'] = int(ram_bytes * 0.70 / n)
            return values
        scripts = values.get('customPostInitScripts')
        if plan.cs_odoo_workers and scripts and not existing:
            n = plan.cs_odoo_workers
            replacements = [('workers = 0', f'workers = {n}')]
            if plan.cs_ram_gb:
                # workers>0 ⟹ cs_ram_gb>0（A6-1 constraint 保證），per-worker
                # memory 上限＝官方建議比例（hard 0.85／soft 0.70）。
                ram_bytes = int(plan.cs_ram_gb * 1024 ** 3)
                replacements += [
                    ('limit_memory_hard = 0',
                     f'limit_memory_hard = {int(ram_bytes * 0.85 / n)}'),
                    ('limit_memory_soft = 0',
                     f'limit_memory_soft = {int(ram_bytes * 0.70 / n)}'),
                ]
            for marker, repl in replacements:
                total = sum(body.count(marker) for body in scripts.values())
                if total != 1:
                    raise ValueError(
                        "customPostInitScripts 內 '%s' 預期恰 1 處、實得 %d"
                        "（base 已漂移，fail-loud 不靜默 no-op）" % (marker, total)
                    )
                for name, body in list(scripts.items()):
                    if marker in body:
                        scripts[name] = body.replace(marker, repl)
        return values

    @api.model
    def _find_active_for_partner(self, template, partner):
        """回傳該 partner 名下、該 template 的活躍實例（排除刪除終態）。

        供 max_per_partner enforcement 與開通 API 冪等（#337）共用。
        排除的刪除終態＝module-level TERMINAL_STATES（Task 0 校驗＝('deleting',)）。
        """
        return self.sudo().search([
            ('template_id', '=', template.id),
            ('workspace_id.owner_id.partner_id', '=', partner.id),
            ('state', 'not in', list(TERMINAL_STATES)),
        ])

    @api.model
    def _count_active_for_template(self, template):
        """該 template 的**全站**活躍實例數（#501 全域上限與售罄現算共用）。

        口徑與 `_find_active_for_partner` 同源（`state not in TERMINAL_STATES`，
        而 TERMINAL_STATES 只有 `deleting`）——`suspended` 與 `error` 照樣佔名額，
        因為它們隨時可能被復原，位置沒有真的釋放出來。兩邊口徑漂移會讓同一台服務
        在兩個上限裡算法不同。

        ⚠ `sudo()` 是**正確性前提，不是效能選擇**：ir.rule
        `cloud_service_rule_workspace_member` 把可見範圍限制在「呼叫者是成員的
        workspace」，而全域計數必然跨租戶——不 sudo 就會少算，守衛靜默 fail-open。
        這比 `_find_active_for_partner` 更依賴 sudo（那邊同 partner 常在同一個
        workspace，這邊必然不是）。
        回傳的是計數而非 recordset，跨租戶的 row 從不外流給呼叫端。
        """
        return self.sudo().search_count([
            ('template_id', '=', template.id),
            ('state', 'not in', list(TERMINAL_STATES)),
        ])

    @api.model
    def _claim_global_cap_slots(self, records):
        """對每個「有上限的 template」寫一次衝突列，讓並發第二者拿到 40001。

        機制與取捨見 `_GLOBAL_CAP_CONFLICT_SQL` 的註解。排序去重是為了讓多筆
        vals 跨 template 時的加鎖順序在所有 tx 中一致（避免死鎖）；沒有上限的
        template 完全不進來——否則全站每次建服務都在同一列上排隊，代價會落到
        與限量無關的商品身上。

        用原生 SQL 而非 ORM `write()`：只需要製造 row 版本變更，不要觸發
        template 的 constrains／cache 失效／`write_date` 之外的任何副作用。
        """
        capped_ids = sorted({
            rec.template_id.id for rec in records
            if rec.template_id._global_instance_cap()
        })
        for tpl_id in capped_ids:
            self.env.cr.execute(_GLOBAL_CAP_CONFLICT_SQL, (tpl_id,))

    @api.model
    def _enforce_supply_gate(self, records):
        """逐筆重跑供給閘門（停售／售罄），超過就 raise 讓整筆撤回。

        判準的唯一定義點是 `cloud_app_template._reject_reason_for_new_instance`
        ——marketplace payload 的 availability 呼叫的是同一個，不是第二份判準。
        這裡傳的計數**已含剛落庫的這筆**（順序：寫入 → 取衝突列 → count →
        超過就 raise），所以判定式是嚴格大於。

        供給閘門先於 `max_per_partner` 判：「這個商品現在還賣不賣」邏輯上先於
        「你還能不能再買一台」，訊息也比較貼近租戶當下的處境。
        """
        for rec in records:
            template = rec.template_id
            reason = template._reject_reason_for_new_instance(
                lambda tpl=template: self._count_active_for_template(tpl))
            if reason == 'paused':
                raise SupplyGateError(_(
                    'Sales of "%(tpl)s" are currently paused.',
                    tpl=template.name))
            if reason == 'unpinned_chart':
                # #552 層 4：這是**平台端設定缺陷**不是營運狀態，所以 log 成 error
                # ——租戶看到的是下面那句，但營運要在 log 裡看得到是哪個 template。
                # 只在真的有人嘗試開通時才 log（判準本身每次 marketplace 渲染都會
                # 跑一次，寫在裁決點會洗版）。
                _logger.error(
                    "供給閘門擋下開通：template %s (id=%s, slug=%s) 沒有 "
                    "helm_chart_version ⇒ helm 會解析成 registry 當下的 latest。"
                    "請在 seed 釘版後重新發版（#552）。",
                    template.name, template.id, template.slug,
                )
                raise SupplyGateError(_(
                    '"%(tpl)s" cannot be provisioned right now: the platform '
                    'has not pinned a deployment version for this product. '
                    'Please contact support.', tpl=template.name))
            if reason == 'sold_out':
                raise SupplyGateError(_(
                    '"%(tpl)s" is sold out; no new instances can be '
                    'provisioned at the moment.',
                    tpl=template.name))

    @api.model_create_multi
    def create(self, vals_list):
        """#336：每帳號實例上限 enforcement（#345 補並發防護）。

        放 model create（controller、#337 開通 API、shell 等所有入口共用），
        controller 不另做早驗——避免同一規則兩份漂移。計數口徑＝該 partner 名下、
        該 template、state 不在刪除終態的 CloudService 數。enforcement 對所有入口
        （含測試 su env）一律生效、無 su/group 豁免（否則 TransactionCase 假綠）。

        #345 並發防護：原為 check-after-act（super().create() 後才 count、無鎖），
        兩並發 create 同 (template, partner) 各見自身 count=1 雙雙放行 → 可繞上限。
        改為 count 前先取 (template_id, partner_id) 為鍵的 advisory xact lock 序列化
        （比照 provision 端點 _HA_PROVISION_LOCK_NS 慣例，見 ha_paas.py）。順序
        INSERT→lock→count 仍正確：READ COMMITTED 下，輸家取得鎖後的 count SELECT
        用新 snapshot，看得到贏家已 commit 的列 → count>limit 觸發 rollback（撤回
        自身 INSERT）。xact lock 於本 tx 結束（commit/rollback）自動釋放。
        max_per_partner=0 的常態不進迴圈＝零鎖開銷。多筆 vals：先收集 distinct
        (template, partner) 排序後依序取鎖，消除跨組交錯的 lock-ordering 死鎖。

        DB unique-index 無法表達「每 (partner, template) ≤N」的一般 N 上限
        （partial unique index 只能保證「至多一列」，見 provision_partner_id），
        故泛化上限只能靠 advisory lock；單連線 TransactionCase 無法真並發，既有
        功能測試僅證非回歸、鎖的競態正確性靠與 provision 先例一致性 + code review。

        ⚠⚠ **上面那段「READ COMMITTED」是錯的前提**（#345 起沿用，#518 證偽）。
        Odoo 的 cursor 是 **REPEATABLE READ**，取鎖**不會**刷新 snapshot，所以
        「輸家取鎖後的 count 看得到贏家已 commit 的列」不成立——實測兩並發各自
        只看得到自己那筆、雙雙放行。`max_per_partner` 因此在並發下仍可被繞過；
        它的修法（衝突列粒度需為 (template, partner)）另見 #518，本方法未修。
        HA PaaS 由 `provision_partner_id` 的 partial unique index 兜底，不受影響。

        #501 供給閘門（全域實例上限＋營運停售）改用**衝突列**而非 advisory lock：
        `_claim_global_cap_slots` 對 template row 做 UPDATE，並發第二者在 RR 下拿到
        SQLSTATE 40001，交給 `retrying()` 以新 tx／新 snapshot 重試（見
        `_GLOBAL_CAP_CONFLICT_SQL` 的完整論證與實測）。判準本身在
        `cloud_app_template._reject_reason_for_new_instance`，marketplace 的
        availability payload 呼叫的是同一個——單點裁決，不做兩份漂移。

        ⚠ 衝突列只有 template 一維，代價是**同一 template 的全站下單被序列化**：
        輸家會阻塞到贏家整個 tx 結束（不只計數那一瞬，還包含建訂閱等後續工作），
        且會多付一次重試。HAOS 是 VM 型、上限個位數、下單頻率極低，可接受。
        **不要把這個形制直接套到熱門的容器型 template**——那需要先量測 create tx
        的實際長度，或改用更細的分片鍵。

        ⚠ 這條路徑要求呼叫端**不得吞掉並發例外**：`SerializationFailure` 是
        `Exception` 子類，controller 若用裸 `except Exception` 收掉，`retrying()`
        永遠看不到、不會重試，而且當下 PG 交易已 aborted，後續 flush 會再炸。
        `_create_service` 已顯式重拋（見該處註解）。savepoint 幫不上忙——40001 之後
        整個 tx 必須中止，`ROLLBACK TO SAVEPOINT` 繼續跑只會帶著同一個陳舊 snapshot。

        ⚠ 本方法對所有入口一律生效、無 su/group 豁免（否則 TransactionCase 假綠，
        見 docs/decisions/patterns/su-env-exempt-guard-false-green.md）。

        #519 建立閘：整個方法的第一件事就是拒絕非平台開通脈絡的建立。放在最前面
        是刻意的——後面每一段（`_assert_reverse_binding_authorized`、
        `super().create()` 的 INSERT、`_claim_global_cap_slots` 的衝突列 UPDATE）
        都有副作用或成本，被拒絕的呼叫不該碰到其中任何一段。完整論證見模組頂端
        `_INTERNAL_PROVISION` 的註解。
        """
        # #519：唯一對所有入口（含 sudo、含 superuser、含 TransactionCase 的 su
        # env）都生效的那一層。判準只有一個：context 裡那個值是不是**那一個**
        # 模組層級私有物件。不讀 vals、不讀 env.su、不讀 env.user、不讀 role。
        if not _is_internal_provision(self.env):
            raise ProvisionGateError(_(
                'Cloud services can only be created by the platform '
                'provisioning flow (%(model)s). Please order from the '
                'marketplace instead of writing to this model directly.',
                model=self._name))
        # #744：Access 身分錨點的 create 側。**不是**被上面那道閘覆蓋掉的冗餘
        # ——開通閘擋的是「誰能建」，本組擋的是「建的時候能不能自帶別人的 CF
        # Access app id」，而開通流程本身（走 `_internal_provision`）是被上面
        # 放行的那一類呼叫者。兩個把手刻意分開，開通不順便獲得搬錨點的能力。
        _strip_cs_access_identity_anchors(self.env, vals_list)
        # #698 反向端：create 也能帶 `project_ids`（同一張 relation 表的另一頭），
        # 且同樣必須前置檢查——建完之後那個 project 就落進呼叫者的 bound 分支。
        # 🔴 #705 F1：必須看**框架解析後**的 vals。`default_project_ids` 是在
        # `BaseModel.create` 內部才展開的（見 `cloud_service_binding_guard.create`
        # 的說明），只看 raw vals 會讓整道前置檢查跳過。
        for vals in vals_list:
            self._assert_reverse_binding_authorized(
                self._add_missing_default_values(vals))
        normalized_vals_list = []
        observation_started = fields.Datetime.now()
        for vals in vals_list:
            normalized = dict(vals)
            # 新服務尚無 operator 觀測；明寫 False 才能同時中和 raw vals、context 與
            # ir.default。僅 pop 會在 super().create() 展開 default 時再被注入。
            normalized['observed_at'] = False
            # capacity 時計只能由背景觀測開始，開通不能注入 raw／context／ir.default。
            normalized['init_capacity_wait_since'] = False
            # owner 告警 marker 同理：只由背景 monitor 寫，開通不能預先壓掉告警。
            normalized['n8n_owner_alerted_at'] = False
            # 其他 init 管控欄位（init_ready_observed／init_ready_since／init_queue_turn／
            # init_wait_*）刻意不在此中和：create 結尾的 `_open_init_generation()` 會重設
            # wait／queue 時計，進入 initializing 的 write() 會重設 ready／wait／queue；而
            # queue／claim／phase／capacity 都只在 state='initializing' 時讀它們，租戶的開通
            # 路徑（`_internal_provision`＋controller 白名單）也不會直接建出 initializing。
            # capacity 時計與 owner marker 不同：它們由背景 monitor 首次寫入、沒有上述重設點。
            defaulted = self._add_missing_default_values(normalized)
            normalized['observation_episode_started_at'] = (
                observation_started if defaulted.get('state') in OBSERVATION_TRANSITION_STATES
                else False
            )
            # #1431：直接建成 running 的列（遷移資料、測試）也算「第一次進 running」，與
            # write() 同一個規則；明寫的值優先（建立閘已保證只有平台程式碼走得到這裡）。
            if 'first_running_at' not in normalized:
                normalized['first_running_at'] = (
                    observation_started if defaulted.get('state') == 'running' else False
                )
            raw_values = normalized.get('helm_values')
            if raw_values:
                try:
                    normalized['helm_values'] = json.dumps(
                        self._strip_mcp_sensitive_values(json.loads(raw_values))
                    )
                except (json.JSONDecodeError, TypeError):
                    pass
            normalized_vals_list.append(normalized)

        records = super().create(normalized_vals_list)
        # #519 提案 B：讀**落地值**，不讀 vals。`default_subscription_id`（context）
        # 與 `ir.default`（租戶自己寫得了的持久化載體）都是在 `BaseModel.create`
        # 內部才展開的，只看 raw vals 會讓整道檢查跳過——這正是 C4 那條陷阱。判準
        # 本身沒有「自我實現」問題（訂閱的 sh_partner_id / state 不會因為這次
        # create 而改變），所以事後檢查是安全的，與同方法內 `_enforce_supply_gate`
        # / `max_per_partner` 的既有形狀一致。post-super raise 的 rollback 義務已由
        # #520 在 `controllers/paas.py` 處理過。
        records._assert_subscription_binding()
        self._claim_global_cap_slots(records)
        # 收集需 enforcement 的 (template_id, partner_id)，排序去重後依序取鎖：
        # 排序消除多筆跨組的 lock-ordering 死鎖；max_per_partner=0 常態集合為空、零開銷。
        lock_pairs = sorted({
            (rec.template_id.id, rec.workspace_id.owner_id.partner_id.id)
            for rec in records
            if rec.template_id.max_per_partner and rec.workspace_id.owner_id
        })
        for tpl_id, partner_id in lock_pairs:
            self.env.cr.execute(
                'SELECT pg_advisory_xact_lock(%s, %s)', (tpl_id, partner_id))
        self._enforce_supply_gate(records)
        for rec in records:
            limit = rec.template_id.max_per_partner
            if limit and rec.workspace_id.owner_id:
                partner = rec.workspace_id.owner_id.partner_id
                count = len(self._find_active_for_partner(rec.template_id, partner))
                if count > limit:
                    raise SupplyGateError(_(
                        '"%(tpl)s" is limited to %(limit)s instance(s) per '
                        'account, and you have reached that limit.',
                        tpl=rec.template_id.name, limit=limit))
        # #1257 Q24：create 開第一代（init 類型不是 none 才會真的開）。
        records._open_init_generation()
        # #1130：woow-odoo 的初始 Odoo 管理員密碼同樣在這裡鑄造、存 cloud_service_secret
        # （#1160：平台鑄造＋create response 一次性顯示）。閘門是 chart，不是 has_admin_gui
        # ——後者會把 odoo 接上 MCP 的 config.sensitive／reset 鏈（#1141 ⛔ 條款）。
        for rec in records.filtered(
                lambda r: r.template_id.has_admin_gui
                or r.template_id._is_woow_odoo_chart()):
            if not rec._get_secret_record():
                secret_password = self.env.context.get('mcp_admin_password')
                if not secret_password:
                    # #357 C20：走單一生成點，不在此處自行 token_urlsafe。
                    secret_password = self._generate_admin_password()
                rec._set_admin_password_secret(secret_password)
        return records

    def unlink(self):
        """Cleanup sidecar-backed user-scope MCP before deleting the service.

        Catches every unlink path — controllers calling ``service.unlink()``
        directly after their own helm uninstall, cron taking down a stuck
        service, manual ORM ops — without forcing callers to remember to
        call ``_do_cleanup`` first. ``project.cloud_service_id`` is
        ``ondelete='set null'``, so a naked ``unlink()`` would leave
        ``project_id`` populated on sidecar-backed MCP rows while their
        backing sidecar is gone — zombie URL. ``_do_cleanup`` does the same
        step but only triggers on the lifecycle path; this override is the
        catch-all guard.

        The ``sidecar_backed`` filter keeps user-provided external MCP rows
        intact — their URL is physically independent of this service.
        Idempotent against ``_do_cleanup``: when both run, the second finds
        the matching recordset already empty.

        M:N (Phase A): each cloud_service narrows by its own URL prefix so
        tearing down service A does not collateral-unlink service B's
        sidecar-backed MCP rows on a project that binds both.

        #279 Path B (revoke): capture bound projects BEFORE delete; once the record
        (and its M:N rows) are gone their ``workspace_ids`` recompute — dropping
        workspaces only reachable via this service — so each is reconciled as a pure
        revoke (best-effort; the enforcing cron is authoritative).

        #744：同一個 fail-loud 家族再加一員——Cloudflare Access app 與 helm
        release 一樣，不得比記錄長壽。`access_policy_ids` 是 ``ondelete=
        'cascade'``，DB 的 FK cascade 會**繞過** Python `unlink()` 把子列直接
        帶走 ⇒ 刪完之後那些 `cf_policy_id` 就再也拿不到，operator 唯一的刪除
        入口（existing_policy_ids）永遠收不到它們。所以拆除必須在
        `super().unlink()` 之前完成。
        """
        # #318: 在 super().unlink() 前捕獲仍 in_progress 的關聯訂閱。
        # subscription_id 是 ondelete='set null'（只保護反向），刪 service
        # 不會動訂閱 → 孤兒訂閱會被計費 cron 續扣。super().unlink() 後
        # record 離場但訂閱 record 存活，逐一結算（取消＋按比例退點）。
        # 僅結算 in_progress；lifecycle-cleanup / dunning 已取消者跳過。
        subs_to_settle = self.sudo().mapped('subscription_id').filtered(
            lambda s: s.state == 'in_progress'
        )
        affected = self.mapped('project_ids')
        old = {p.id: set(p.workspace_ids.ids) for p in affected}
        # #278 A1：pre-delete 捕獲 (workspace, ns)，super().unlink() 後這些
        # record 離場、workspace_ids 重算，即以存活集重算 ns quota。
        quota_targets = [
            (s.workspace_id, s.helm_namespace)
            for s in self if s.helm_namespace
        ]
        # 🔴 #794：本段**新增一層 savepoint**，這是 #744 那段註解（「不需要
        # savepoint 這層機關」）之後的刻意改變，理由與原註解說的不衝突：
        #
        #   原註解對的是「拆在 super().unlink() 之前 ⇒ 失敗時什麼都還沒刪 ⇒
        #   記錄與 id 原樣留著當重試錨點」。**那句話只涵蓋失敗的那一台。**
        #   批次刪除時，失敗那台之前**已經拆成功**的那幾台，CF 上的 app 已經
        #   沒了、而 `access_app_id = False` 那筆 DB 寫入會隨整筆交易被丟棄
        #   ⇒ 記錄帶著 `is_protected=True` 與指向死 app 的 id 復活 ⇒ UI 顯示
        #   已保護、subdomain 實際免登入即可存取（#794 的形狀，SA/SH 兩腿同型）。
        #
        # savepoint 在這裡有一個**功能性**的作用，不只是整潔：補償寫必須跑在
        # 一條獨立連線上（見 `_compensate_fail_open_after_rollback`），而只要
        # 外層交易對那幾列的 UPDATE 還活著，獨立交易的 UPDATE 就會撞上 PG 的
        # 寫寫衝突、等到 `lock_timeout` 為止。**先讓 savepoint 回捲、再補償**
        # 是補償寫得進去的前提，不是順手加的保險。
        torn_down = []
        Reap = self.env[_CF_REAP_MODEL]
        try:
            with self.env.cr.savepoint():
                for service in self:
                    # F-ENV-5 helm fail-loud guard: the helm release / namespace must
                    # never outlive the record. The workspace-cascade routing path
                    # (Workspace.unlink → service_ids.unlink) relies on THIS to tear
                    # down helm, since the DB FK cascade bypasses Python unlink. The
                    # controller delete path that already uninstalled helm hits an
                    # idempotent 404 here (treated as success). On a real failure the
                    # row + release are kept and the deletion is aborted for retry.
                    if service.helm_release_name and service.helm_namespace:
                        if not service._cleanup_helm():
                            raise UserError(
                                _("Helm uninstall failed; release '%s' is still live in "
                                  "namespace '%s' (billed compute / leaked namespace). "
                                  "The service record was kept so the deletion can be "
                                  "retried. Please try again shortly.") % (
                                    service.helm_release_name, service.helm_namespace,
                                )
                            )
                    # #744 Access fail-loud guard：Cloudflare Access app 不得比記錄長
                    # 壽。拆在 `super().unlink()` **之前**——這是與 SA route 那條的刻意
                    # 差異：route 的 cf policy id 住在會被 DB cascade 帶走的子列上，所以
                    # 那邊被迫「先抓 id、刪完再拆、失敗用 savepoint 回捲」；CS 這裡在
                    # 刪除前 `access_app_id` 與 `access_policy_ids.cf_policy_id` 都還
                    # 在手上，直接先拆再刪，失敗就 raise（什麼都還沒刪 ⇒ 記錄與 id 原
                    # 樣留著當重試錨點）。
                    #
                    # ⚠ #794 修正這段原本的結語（「不需要 savepoint 這層機關」）：
                    # 它只涵蓋**失敗的那一台**。批次刪除時失敗那台之前已經拆成功
                    # 的那幾台，CF 上的 app 已經沒了、`access_app_id = False` 卻
                    # 隨交易被丟棄 ⇒ 與 SA/SH 兩腿完全同型的無聲 fail-open。
                    # savepoint 現在加上去了，理由見本 method 開頭那段。
                    # `torn_down` 由 `_teardown_cs_access` 在 operator 回 2xx
                    # 當下就 append（見該方法的 [HIGH] 註解）——**不能**靠回傳值
                    # 判斷：落帳撞到 `OperationalError` 時它拿不到 True。沒東西
                    # 要拆時那一路在更前面就 `return True`，不會誤記。
                    if not service._teardown_access_if_any(
                            torn_down=torn_down):
                        raise UserError(
                            _("Cloudflare Access teardown failed; the Access "
                              "application for '%s' is still live. The service record "
                              "was kept so the deletion can be retried.") % (
                                service.name or service.id,
                            )
                        )
                    user_mcps = service._collect_sidecar_user_mcps(
                        service._sidecar_url_prefix()
                    )
                    if user_mcps:
                        user_mcps.sudo().unlink()
                        _logger.info(
                            "[CLOUD_SVC] unlink() unlinked %s user-scope MCP server(s) "
                            "for service id=%s", len(user_mcps), service.id,
                        )
                res = super().unlink()
                # #318: teardown 成功後才結算訂閱（取消＋按比例退點）。放在
                # super().unlink() 之後，故 helm fail-loud raise 時不會執行（全交易
                # rollback、record 保留、訂閱不動）。訂閱是獨立 record，不隨 service
                # 刪除而消失。
                for sub in subs_to_settle:
                    sub._settle_on_entity_delete(reason='Service deleted')
                for workspace, helm_namespace in quota_targets:
                    self._push_ns_quota(workspace, helm_namespace)
                for project in affected.exists():
                    project._reconcile_agent_runtime_reach(old.get(project.id, set()))
        except Exception:
            Reap._compensate_fail_open_after_rollback(
                torn_down, _logger,
                '[CLOUD_SVC] unlink (services=%s)' % (self.ids,))
            raise
        return res

    def _collect_sidecar_user_mcps(self, url_prefix):
        """Return sidecar-backed user-scope MCP rows attached via M:N
        projects whose URL belongs to this service's sidecar (or all of
        them when no URL prefix is available, e.g. pre-deploy fixtures).
        """
        self.ensure_one()
        candidates = self.project_ids.mapped('mcp_server_ids').filtered(
            lambda s: s.scope == 'user' and s.sidecar_backed
        )
        if url_prefix is None:
            return candidates
        return candidates.filtered(
            lambda s: (s.url or '').startswith(url_prefix)
        )

    def _sidecar_url_prefix(self):
        """Return the URL prefix used by sidecar-backed user-scope MCP rows
        whose backing sidecar is *this* cloud_service, or ``None`` if this
        service has no sidecar identity (no helm_release_name/namespace).

        ``None`` lets callers fall back to the legacy filter that prunes any
        sidecar-backed user-scope row — required so services created without
        helm metadata (older fixtures, pre-deploy state) still drop their
        MCPs on teardown.
        """
        self.ensure_one()
        if not self.helm_release_name or not self.helm_namespace:
            return None
        return (
            f'http://{self.helm_release_name}-mcp.'
            f'{self.helm_namespace}.svc.cluster.local'
        )
