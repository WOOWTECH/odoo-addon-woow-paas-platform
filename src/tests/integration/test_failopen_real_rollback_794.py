"""#794 真交易邊界探針：補償寫必須活過**真正的** rollback。

## 為什麼另外一支

同 module 的 `src/tests/test_access_failopen_compensation_794.py` 跑在
`TransactionCase` 上，而 `TransactionCase` 的 cursor **不可以 commit／rollback**
（Odoo 18 對它們打了 `forbidden` patch，見 `odoo/tests/common.py` 的
`TransactionCase.setUpClass`），每個 test method 只是外層交易裡的一個
`SAVEPOINT`。也就是說那一檔**結構上無法製造出「整筆交易被丟棄」這個事件**
——而那正是 #794 的成因本身。

所以那一檔守的是「補償的內容與時機對」（拿到哪幾條錨點、不誤傷重試錨點、不自動
降 `is_protected`、pass 1 有沒有 short-circuit），**本檔才守「它活得下來」**。
兩者缺一不可：前者綠而後者紅 ＝ 補償邏輯對但寫入蒸發（與完全沒修同一個結局，
而且更難察覺，因為單元測試全綠）。

## 形狀

繼承 `BaseCase` 而不是 `TransactionCase`：`BaseCase` 不建 class 級 cursor、不打
commit/rollback 的 forbidden patch，也不進 test mode ⇒ `Registry.cursor()` 回的是
**真的 cursor、真的另一條連線**。三筆真交易：

1. **交易 1**：建 fixture 並**真的 commit**（不 commit 的話第 2、3 筆看不到它們，
   而「看不到」與「被回捲掉了」在斷言上不可區分）。
2. **交易 2**：扮演那一次 request——`unlink()` 一批 route，第一條 teardown 成功、
   第二條失敗 ⇒ `UserError` ⇒ **`cr.rollback()`**（request 收尾實際做的事）。
   同一筆交易裡另外寫一個**對照值**（p2 的 `session_duration`），它必須被這次
   rollback 抹掉——沒有它，本探針無法區分「補償活下來了」與「這次 rollback
   根本沒東西可回捲」。
3. **交易 3**：另一條連線讀。p1 的死 app id 不見了、`access_protection_lost`
   打開了、`is_protected` 沒被動過；而對照值回到 commit 當時的樣子。

## ⚠ 它同時是鎖競態的實測

補償跑的時候，交易 2 還開著、而且剛剛才在一個已回捲的 savepoint 裡對那幾列發過
`DELETE`。若 PG 判定那個 subxid「還在進行中」（subxid cache 溢位時會退化成這樣），
補償的 `UPDATE` 會去等交易 2 的 xact lock，而交易 2 正同步等補償回來 ⇒ 互鎖，
且 deadlock detector 看不到那個環。補償進場的 `SET LOCAL lock_timeout` 就是為了
把 hang 換成一個接得住的錯誤——**那條路徑上本探針會紅**（補償沒寫進去），這是
刻意的：它紅了才代表這個 repo 的真實情況與註解不符。

## 怎麼跑

    odoo --test-tags failopen_794 -u woow_paas_platform \\
         --stop-after-init -d <db> -c /etc/odoo/odoo.conf

`-standard` ⇒ 預設測試套件**不會**選中它（本檔會真的寫進 DB 並 commit）。
fixture 用一次性 marker 命名，`addCleanup` 以 raw SQL 清掉自己建的那幾列。
"""
import logging
import uuid
from unittest.mock import MagicMock, patch

import odoo
from odoo import api
from odoo.exceptions import UserError
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged

from ...models.security_access import (
    _internal_identity as _internal_sa, _skip_route_sync,
)

_logger = logging.getLogger(__name__)

MOCK_SA_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
                  'get_paas_operator_client')
_ROUTE = 'woow_paas_platform.security_access_route'
_ROUTE_TABLE = 'woow_paas_platform_security_access_route'


def _teardown_result():
    """`enabled=False` 那一趟的真實回傳形狀（五個 key 一個都不少）。"""
    return {
        'app_id': None,
        'policy_ids': [],
        'allowed_idps': [],
        'unresolved_idp_types': [],
        'orphaned_policy_ids': [],
    }


@tagged('-standard', '-at_install', 'post_install', 'failopen_794')
class TestFailOpenCompensationSurvivesRealRollback(BaseCase):

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.marker = 'p794%s' % uuid.uuid4().hex[:8]
        self.app1 = 'app-%s-1' % self.marker
        self.app2 = 'app-%s-2' % self.marker
        self.addCleanup(self._purge)
        # ── 交易 1：建 fixture 並真的 commit ─────────────────────────
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            ws = env['woow_paas_platform.workspace'].create(
                {'name': 'WS %s' % self.marker})
            sa = env['woow_paas_platform.security_access'].create(
                {'name': 'SA %s' % self.marker, 'workspace_id': ws.id})
            _internal_sa(sa).write(
                {'state': 'active', 'tunnel_id': 'tun-%s' % self.marker})
            p1 = _skip_route_sync(env[_ROUTE]).create({
                'security_access_id': sa.id,
                'subdomain_prefix': '%sa' % self.marker,
                'is_protected': True})
            p2 = _skip_route_sync(env[_ROUTE]).create({
                'security_access_id': sa.id,
                'subdomain_prefix': '%sb' % self.marker,
                'is_protected': True,
                'session_duration': '24h'})
            _internal_sa(p1).access_app_id = self.app1
            _internal_sa(p2).access_app_id = self.app2
            self.p1_id, self.p2_id = p1.id, p2.id

    # ------------------------------------------------------------------ #
    #  helpers                                                            #
    # ------------------------------------------------------------------ #
    def _purge(self):
        """清掉自己建的那幾列。raw SQL：走 ORM 的 `unlink()` 會再打一次
        operator（那正是被測的路徑），清理不該觸發被測行為。"""
        try:
            with self.registry.cursor() as cr:
                like = '%%%s%%' % self.marker
                cr.execute(
                    'DELETE FROM woow_paas_platform_security_access '
                    'WHERE name LIKE %s', (like,))
                cr.execute(
                    'DELETE FROM woow_paas_platform_workspace '
                    'WHERE name LIKE %s', (like,))
        except Exception:
            _logger.exception(
                '#794 探針的 fixture 清理失敗（marker=%s）——請手動確認',
                self.marker)

    def _read(self, route_id):
        """另一條連線、另一筆交易讀這一列。看得到才叫「活過 rollback」。"""
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT access_app_id, is_protected, access_protection_lost, '
                'session_duration FROM %s WHERE id = %%s' % _ROUTE_TABLE,
                (route_id,))
            return cr.fetchone()

    def _client(self, side_effect):
        client = MagicMock()
        client.sync_access_app.side_effect = side_effect
        client.sync_tunnel_routes.return_value = {'routes': []}
        return client

    def _run_unlink_then_rollback(self, client, control_write=True):
        """交易 2：扮演那一次 request。回傳送出去的 payload 清單。"""
        sent = []
        cr = self.registry.cursor()
        try:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            if control_write:
                # 對照值：同一筆交易裡的一個普通寫入，**必須**被下面的
                # rollback 抹掉。少了它，本探針分不出「補償活下來」與「這次
                # rollback 根本沒東西可回捲」。刻意寫在 p2（補償不碰它）。
                _skip_route_sync(env[_ROUTE].browse(self.p2_id)).write(
                    {'session_duration': '1h'})
            routes = env[_ROUTE].browse([self.p1_id, self.p2_id])
            with patch(MOCK_SA_CLIENT, return_value=client):
                try:
                    routes.unlink()
                except UserError:
                    pass
                else:
                    self.fail('unlink 必須以 UserError 收場，否則本探針沒有'
                              '「交易被丟棄」這個事件可以測')
            sent = [c.args[0] for c in client.sync_access_app.call_args_list]
            cr.rollback()
        finally:
            cr.close()
        return sent

    # ------------------------------------------------------------------ #
    #  探針本體                                                            #
    # ------------------------------------------------------------------ #
    def test_compensation_write_survives_a_real_transaction_rollback(self):
        """p1 拆成功、p2 拆失敗 ⇒ 交易整筆丟棄，補償的寫入必須還在。"""

        def _side_effect(payload):
            if payload.get('enabled'):
                # 補償的立即重推：operator 還沒恢復 ⇒ 旗標留著給 UI 說
                raise Exception('operator still down')
            if payload.get('existing_app_id') == self.app1:
                return _teardown_result()
            raise Exception('operator down')

        sent = self._run_unlink_then_rollback(self._client(_side_effect))

        # 前提：p1 的 teardown 真的送出去過（少了它，「app id 不見了」在
        # 「根本沒拆」的世界也會綠——而那是相反的世界）。
        self.assertTrue(
            any(c.get('enabled') is False
                and c.get('existing_app_id') == self.app1 for c in sent),
            '前提檢查：p1 的 teardown 必須真的被送出去過')

        # 🔴 對照值：證明那次 rollback 真的抹掉了同一筆交易裡的普通寫入。
        self.assertEqual(
            self._read(self.p2_id)[3], '24h',
            '對照值必須回到 commit 當時的樣子——沒有它，下面那組斷言可能只是'
            '因為這次 rollback 根本沒東西可回捲')

        app_id, is_protected, lost, _sd = self._read(self.p1_id)
        self.assertFalse(
            app_id,
            '補償清掉的死 app id 必須活過真正的 rollback；還在＝補償寫進了'
            '那個被丟棄的交易（與完全沒修同一個結局）')
        self.assertTrue(
            lost,
            'access_protection_lost 必須活過真正的 rollback——它是 UI 唯一'
            '說得出「保護沒有生效」的載體')
        self.assertTrue(
            is_protected,
            '不得自動降 is_protected（租戶設定的意圖，產品可見變更）')

        # 拆**失敗**那條原樣留著：它的 app id 是真的重試錨點。
        self.assertEqual(self._read(self.p2_id)[0], self.app2)
        self.assertFalse(self._read(self.p2_id)[2])

    def test_positive_control_a_clean_batch_leaves_no_compensation(self):
        """陽性對照：兩條都拆得掉 ⇒ 交易照樣被丟棄，但**不該**有任何補償。

        沒有它，上一條可能只是因為補償對每一次 rollback 都無差別執行。這裡刻意
        讓 `super().unlink()` 之後的一步失敗（route sync 拋例外），製造出「拆都
        成功、交易仍被丟棄」那一格。
        """
        client = self._client(lambda payload: _teardown_result())
        client.sync_tunnel_routes.side_effect = UserError('route sync down')
        cr = self.registry.cursor()
        try:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            routes = env[_ROUTE].browse([self.p1_id, self.p2_id])
            with patch(MOCK_SA_CLIENT, return_value=client):
                with self.assertRaises(UserError):
                    routes.unlink()
            cr.rollback()
        finally:
            cr.close()
        self.assertEqual(
            client.sync_access_app.call_count, 0,
            '前提檢查：route sync 在 Access teardown 之前就炸了 ⇒ 這一輪一顆'
            'Access app 都沒被拆')
        for route_id, app_id in ((self.p1_id, self.app1),
                                 (self.p2_id, self.app2)):
            stored, is_protected, lost, _sd = self._read(route_id)
            self.assertEqual(
                stored, app_id,
                '沒有任何 CF 刪除發生 ⇒ 列上的 app id 一個字都不該被動')
            self.assertTrue(is_protected)
            self.assertFalse(
                lost, '沒有 fail-open ⇒ 不得打開 access_protection_lost')
