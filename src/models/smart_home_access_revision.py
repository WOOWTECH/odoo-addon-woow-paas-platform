"""Smart Home 撤權帳本（自動撤權 v1，2026-10-14 跨 repo 契約）。

## 這張表存在的理由

Cloudflare Access 的政策收緊（刪 email、關 IdP、縮 session……）只擋**新**連線；
HA 那側既有的遠端 WebSocket 不會因此斷線。v1 的切斷手段是 HA 整合
（woow_paas_smart_home）重設整條 connector（整個家庭短暫斷線，使用者 10-14 拍板可
接受）。HA 需要一個訊號知道「該斷了」——就是本表：

* 平台在 ``smart_home_route._sync_access_to_operator()`` 收到 operator 成功回條之後，
  算出這條 route「安全相關 desired state」的指紋（``access_security_fingerprint``）。
* 指紋與這條 route 上一筆**已提交**的列不同（或沒有任何列）⇒ 插一列。
* ``smart_home.to_dict()['access_revision']`` ＝ 該 home 所有列的最大 id（沒有就 0）；
  HA 輪詢 ``GET /api/smarthome/homes/<id>/status`` 看到數值改變就重設 connector。
  **數值本身沒有意義，只能比大小／比相等。**

## 🔴 為什麼寫在獨立 cursor、而且立刻 commit

Cloudflare 那半一旦生效就**回捲不掉**。若帳只寫在主交易裡，主交易之後任何一步
失敗回捲（同 request 的其他寫入、request 層 flush 的 constraint……），CF 上的收緊
已生效、帳卻跟著消失 ⇒ HA 永遠不會斷線 ⇒ 被撤權的人保有既有連線。這是安全缺口。
反方向（帳記了、主交易回捲）只會讓 HA 多斷一次線，是可用性代價。兩側量級不同，
所以帳要活過主交易的回捲（形狀同 ``cf_access_policy_reap._compensate_fail_open_after_rollback``
的獨立交易；理由見 ``docs/decisions/patterns/post-commit-bookkeeping-rollback-unit.md``
規則二：回捲單位對齊事實邊界）。

「上一筆指紋」也必須在同一個獨立 cursor 裡讀：主交易是 REPEATABLE READ，快照在
交易第一個查詢就定了，讀不到之後別的交易（包括本 request 稍早自己開的獨立交易）
commit 的列。

## 🔴 為什麼 ``smart_home_ref`` / ``route_ref`` 是 Integer 不是 Many2one

外鍵檢查會對被參照列取 ``FOR KEY SHARE`` 鎖。獨立交易插入時，主交易可能正持有
（或剛在 savepoint 裡寫過）那一列 smart_home / route——最壞情況是主交易「新建」
那一列尚未 commit，FK 檢查會去**等主交易結束**，而主交易正在同一條 Python 執行緒
上同步等這個獨立交易回來 ⇒ 互鎖，PG deadlock detector 看不到這個環（一邊是 PG 鎖
等待、一邊是 Python 函式呼叫）。純量 id 沒有任何跨表鎖。代價是列不會隨 home 刪除
而消失——那無害（HA 已經沒有東西可輪詢），而且本表只能 insert。

## 存取

租戶零 ACL row，只給 ``base.group_system`` 唯讀（與 ``smart_home_provision_operation``
同型）；平台碼以 SUPERUSER 在獨立 cursor 寫入。``write`` / ``unlink`` 對所有人一律
拒絕（含 superuser）——只能 insert 的帳本，任何改寫都會讓「最大 id 只增不減」這個
HA 依賴的性質失效。⚠ 刻意不做 ``env.su`` 豁免
（``docs/decisions/patterns/su-env-exempt-guard-false-green.md``）。
"""
import hashlib
import json
import logging

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

#: 指紋涵蓋的 route 層 payload 欄位（契約 §1）。刻意**不含**
#: ``existing_app_id`` / ``existing_policy_ids`` / ``canonical_name`` / ``app_name`` /
#: ``primary_hostname`` 與 policy 名稱：它們是 CF 物件身分或顯示名，變動不代表誰的
#: 存取被收緊，算進去只會讓每次 delete-then-recreate 都多斷一次線。
_FINGERPRINT_ROUTE_KEYS = (
    'enabled', 'protected_hostnames', 'session_duration', 'login_idp_types',
)
#: 每條 policy 涵蓋的欄位。
_FINGERPRINT_POLICY_KEYS = (
    'decision', 'precedence', 'include', 'require', 'exclude',
)


def access_security_fingerprint(payload):
    """``/api/access/sync`` payload 的安全相關 desired state 指紋（sha256 hex）。

    canonical JSON：``sort_keys`` ＋ 無空白分隔。list 保留 payload 的順序——policy
    已依 ``(sequence, id)`` 排好、precedence 也在指紋裡；規則重排造成的多一次斷線
    是可接受的可用性代價，漏判則不是。
    """
    data = {key: payload.get(key) for key in _FINGERPRINT_ROUTE_KEYS}
    data['policies'] = [
        {key: policy.get(key) for key in _FINGERPRINT_POLICY_KEYS}
        for policy in (payload.get('policies') or [])
    ]
    canonical = json.dumps(
        data, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


class AccessRevisionLedgerError(UserError):
    """撤權帳本寫不進去。Cloudflare 那半可能已經生效——必須讓操作失敗、出聲。"""


class SmartHomeAccessRevision(models.Model):
    _name = 'woow_paas_platform.smart_home_access_revision'
    _description = 'Smart Home Access Revocation Revision'
    _order = 'id'

    smart_home_ref = fields.Integer(
        string='Smart Home ID', required=True, index=True, readonly=True,
        help='Id of woow_paas_platform.smart_home. Deliberately a scalar, not a '
             'Many2one: the row is inserted from an independent transaction '
             'while the request transaction may hold (or have just created) '
             'that smart_home row; a foreign-key check would wait on it.',
    )
    route_ref = fields.Integer(
        string='Route ID', index=True, readonly=True,
        help='Id of woow_paas_platform.smart_home_route whose Access desired '
             'state produced this revision (scalar for the same reason).',
    )
    fingerprint = fields.Char(
        string='Security Fingerprint', readonly=True,
        help='sha256 of the canonical security-relevant Access desired state '
             'that Cloudflare confirmed for this route.',
    )

    #: 獨立交易等鎖的上限。本表沒有外鍵、也不更新任何既有列，正常情況下不會等
    #: 任何鎖；這是防止「未知情境下獨立交易去等主交易」變成 hang 的安全參數
    #: （同 ``cf_access_policy_reap._COMPENSATE_LOCK_TIMEOUT`` 的論證）。
    _LEDGER_LOCK_TIMEOUT = '3s'

    def write(self, vals):
        raise AccessError(_(
            'Smart Home access revisions are append-only and cannot be '
            'changed.'))

    def unlink(self):
        raise AccessError(_(
            'Smart Home access revisions are append-only and cannot be '
            'deleted.'))

    @api.model
    def _revision_for_home(self, smart_home_id):
        """該 home 目前的撤權版本：帳本最大 id，沒有就 0（一個查詢）。"""
        self.env.cr.execute(
            'SELECT COALESCE(MAX(id), 0) FROM %s WHERE smart_home_ref = %%s'
            % self._table, (smart_home_id,))
        return self.env.cr.fetchone()[0]

    @api.model
    def _record_confirmed_state(self, smart_home_id, route_id, fingerprint,
                                force=False):
        """在**獨立交易**裡比對並（必要時）插入一列，立刻 commit。

        回傳新列 id；與這條 route 最新一列指紋相同（且非 ``force``）時回 ``0``。

        ``force``：這一輪是從「沒有生效中的 Access app」變成「有」（第一次開保護、
        關掉後重開、``access_protection_lost`` 後重建）。那段期間 hostname 沒有登入
        閘，任何人都可能建立了連線；就算新指紋恰好等於很久以前那一列，也必須斷線。

        🔴 失敗一律 raise ``AccessRevisionLedgerError``（fail-loud）：呼叫端此時
        已經讓 Cloudflare 套用新的收緊，吞掉錯誤 ＝ 靜默漏掉一次撤權。raise 讓這一次
        操作回報失敗、主交易回捲；租戶或 ``access_resync_cron`` 的下一次 sync 會重推
        同一份 desired state 並重新嘗試記帳。
        """
        try:
            with self.env.registry.cursor() as cr:
                cr.execute('SET LOCAL lock_timeout = %s',
                           (self._LEDGER_LOCK_TIMEOUT,))
                cr.execute(
                    'SELECT fingerprint FROM %s WHERE route_ref = %%s '
                    'ORDER BY id DESC LIMIT 1' % self._table, (route_id,))
                latest = cr.fetchone()
                if not force and latest and latest[0] == fingerprint:
                    return 0
                env = api.Environment(cr, SUPERUSER_ID, {})
                revision = env[self._name].create({
                    'smart_home_ref': smart_home_id,
                    'route_ref': route_id,
                    'fingerprint': fingerprint,
                })
                revision_id = revision.id
            _logger.info(
                '[SH] access revision %s recorded sh=%s route=%s force=%s',
                revision_id, smart_home_id, route_id, force)
            return revision_id
        except Exception as error:
            _logger.exception(
                '[SH] access revision could NOT be recorded sh=%s route=%s — '
                'Cloudflare may already enforce the new Access state while '
                'Home Assistant will not reset existing connections; failing '
                'the operation so it is retried', smart_home_id, route_id)
            raise AccessRevisionLedgerError(_(
                'Access settings reached Cloudflare, but the revocation record '
                'could not be saved, so existing remote connections were not '
                'reset. Retry this operation.')) from error
