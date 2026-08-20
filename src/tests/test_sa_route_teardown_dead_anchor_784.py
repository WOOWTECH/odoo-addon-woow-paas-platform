"""#784：刪整條 SA route 時 Access teardown 被 gate 靜默略過，而 FK CASCADE 讓
#781 的落帳完全不觸發。

## 守什麼

`SecurityAccessRoute.unlink()` 在 `super().unlink()` **之前**捕獲每條 doomed
route 的 teardown id，而那個捕獲迴圈原本有兩道 `continue`（`is_protected`／
SA liveness）。被擋掉的 route 一路刪到底，operator 一次都沒被呼叫——全程無 log、
無例外、無狀態欄位，**與乾淨刪除在輸出上完全不可區分**，一次丟掉整個
`access_app_id` ＋ 該 route 底下全部 policy 的 `cf_policy_id`。

#781 加在 `SecurityAccessAccessPolicy.unlink()` 裡的落帳救不到這條：policy 的三
個錨點欄位都是 `ondelete='cascade'`，Odoo 把它落成 PostgreSQL FK ON DELETE
CASCADE，而 `BaseModel.unlink` 是 raw `DELETE FROM ... WHERE id IN (...)`
⇒ 刪 route 時子列由 **PG 直接刪除**，那支 Python `unlink()` 整支不會被執行。
route 的捕獲迴圈是這條路徑上**唯一**的攔截點。

本檔守四件事：

1. **必拆**：判準改成「手上還有沒有 id 要拆」，`is_protected=False` 但仍持有
   stored app_id（程式自己承認的合法持久狀態）與停權中刪 route 都必須拆。
2. **必記**：拆不掉／operator 說刪不掉的 id 一顆都不能丟——落進
   `cf_access_policy_reap` 帳本。
3. **不得卡死**：推不動的狀態下拆失敗**不 raise**（保住列救不了任何人，沒有
   任何 sync 會再為它跑）；推得動的狀態維持既有 fail-loud（列是真的重試錨點）。
4. **dead anchor 有人收**：錨點列已經不存在的帳，由
   `cf_access_policy_reap._cron_reap_dead_anchors` 送去 operator 刪掉。

## 判別式紀律（這檔最容易寫成假綠的三格）

* **否定性斷言一律配同一支 mock 的陽性對照**（同一個測試方法、同一支 mock）。
  patch 點寫錯時「不該打 operator」會**無聲通過**——根本沒有東西被呼叫。
* **「拆了」不等於「拆對了」**：每一條都斷言 payload 內容（`enabled=False`、
  `existing_app_id`、`existing_policy_ids` 逐顆），不是只斷言 `called`。
* **帳本的兩種 kind 必須分得開**：app id 混進 `existing_policy_ids` 會被 CF 當成
  「刪一顆不存在的 policy」回 404＝成功 ⇒ 帳被消掉而 app 留在 CF，零訊號。
  所以有一格專門斷言「這個結果不該出現」（app id 不得出現在 policy 清單裡）。
"""
import logging
import uuid
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.security_access import (
    _internal_identity as _internal_sa, _skip_route_sync,
)

#: SA 腿的 client 取得點（判準是「該 model 怎麼取得 client」不是「client 定義在
#: 哪」）。cron 住在帳本 model 裡、自己 import 一份，所以 patch 點不同——這兩個
#: 常數互抄就會得到「否定性斷言無聲通過」。
MOCK_SA_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
                  'get_paas_operator_client')
MOCK_REAP_CLIENT = ('odoo.addons.woow_paas_platform.models.'
                    'cf_access_policy_reap.get_paas_operator_client')

LOG_SA = 'odoo.addons.woow_paas_platform.models.security_access'
LOG_REAP = 'odoo.addons.woow_paas_platform.models.cf_access_policy_reap'

_REAP = 'woow_paas_platform.cf_access_policy_reap'
_ROUTE = 'woow_paas_platform.security_access_route'
_POLICY = 'woow_paas_platform.security_access_access_policy'


def _sync_result(app_id='app-784', policy_ids=None, orphaned=None):
    """operator `PUT /api/access/sync` 的真實回傳形狀（五個 key 一個都不少）。

    🔴 不用裸 MagicMock：回存那段會拿到 MagicMock 而不是字串／list，測試綠但
    prod 炸（`orphaned_policy_ids` 尤其——裸 mock 永遠 truthy）。
    """
    return {
        'app_id': app_id,
        'policy_ids': list(policy_ids or []),
        'allowed_idps': [],
        'unresolved_idp_types': [],
        'orphaned_policy_ids': list(orphaned or []),
    }


def _teardown_result(orphaned=None):
    """`enabled=False` 那一趟的回傳（operator 把 app_id 收成 None）。"""
    return _sync_result(app_id=None, policy_ids=[], orphaned=orphaned)


class _Fixture:
    """SA ＋ 一條 route ＋ 帳本查詢 helper。"""

    def _mock(self, result=None):
        client = MagicMock()
        client.sync_access_app.return_value = (
            _sync_result() if result is None else result)
        client.sync_tunnel_routes.return_value = {'routes': []}
        client.delete_tunnel.return_value = {'success': True}
        return client

    def _pending_policies(self, anchor_id, anchor_model=_ROUTE):
        return self.env[_REAP]._pending_ids(anchor_model, anchor_id)

    def _pending_apps(self, anchor_id, anchor_model=_ROUTE):
        return self.env[_REAP]._pending_ids(anchor_model, anchor_id, kind='app')

    def _make_sa(self, state='active', tunnel_id='tun-784'):
        sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'SA 784 %s' % uuid.uuid4().hex[:6],
            'workspace_id': self.workspace.id})
        _internal_sa(sa).write({'state': state, 'tunnel_id': tunnel_id})
        return sa

    def _make_route(self, sa, protected=True, app_id=None, policy_cf_ids=(),
                    prefix=None):
        """一條帶著（或不帶）CF id 的 route。全程 `_skip_route_sync`，所以
        fixture 自己不會打 operator——被測的動作才會。"""
        route = _skip_route_sync(self.env[_ROUTE].sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': prefix or ('r%s' % uuid.uuid4().hex[:8]),
            'is_protected': protected})
        route = route.with_context(sa_skip_route_sync=False)
        if app_id:
            _internal_sa(route).access_app_id = app_id
        for cf_id in policy_cf_ids:
            pol = _skip_route_sync(self.env[_POLICY].sudo()).create({
                'route_id': route.id,
                'name': 'pol-%s' % uuid.uuid4().hex[:6],
                'decision': 'allow',
                'rule_ids': [(0, 0, {'group': 'include',
                                     'rule_type': 'email_list',
                                     'rule_value': 'a@example.com'})]})
            pol.with_context(sa_skip_route_sync=False).cf_policy_id = cf_id
        return route


# ====================================================================== #
#  A 系列：主路徑 —— unlink 的捕獲迴圈                                    #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestSaRouteUnlinkTeardown(TransactionCase, _Fixture):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 784A %s' % uuid.uuid4().hex[:6]})

    def test_A1_suspended_protected_route_unlink_tears_down_all_cf_ids(self):
        """#784 原案：停權中刪 protected route ⇒ operator 必須收到 enabled=False
        的 teardown，且帶著該 route 的**全部** cf id（app ＋ 每一條 policy）。

        修前：`sa.state not in (active, active_no_route, error)` 這道 continue 把
        整條 route 判成「沒東西要拆」⇒ operator 一次都沒被呼叫，而 unlink 照常
        完成。紅燈落在「operator 根本沒被呼叫」。
        """
        sa = self._make_sa(state='suspended')
        route = self._make_route(sa, protected=True, app_id='app-a1',
                                 policy_cf_ids=('cf-a1-1', 'cf-a1-2'))
        client = self._mock(_teardown_result())

        with patch(MOCK_SA_CLIENT, return_value=client):
            route.unlink()

        self.assertTrue(
            client.sync_access_app.called,
            '停權中刪 protected route 必須拆掉它的 CF Access app（#784）')
        payload = client.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'], 'teardown 必須是 enabled=False')
        self.assertEqual(payload['existing_app_id'], 'app-a1')
        self.assertEqual(sorted(payload['existing_policy_ids']),
                         ['cf-a1-1', 'cf-a1-2'],
                         'route 底下每一顆 policy cf id 都要跟著送出去')
        self.assertEqual(self._pending_policies(route.id), [],
                         '拆乾淨了就不該留帳')
        self.assertEqual(self._pending_apps(route.id), [])

    def test_A2_unprotected_route_with_stored_app_id_tears_down(self):
        """`is_protected=False` 但仍持有 stored app_id ⇒ 同樣必拆。

        這是程式自己承認的**合法持久狀態**（停權期間被關掉保護的 route 仍帶著
        stored app_id），而舊寫法的第一道 continue 恰好把它判成「沒東西要拆」。

        陽性／否定性對照在同一支 mock 上：同一條 SA 上另一條「什麼 id 都沒有」
        的 route 被刪時**不可以**打 operator。
        """
        sa = self._make_sa(state='active')
        route = self._make_route(sa, protected=False, app_id='app-a2',
                                 policy_cf_ids=('cf-a2',))
        client = self._mock(_teardown_result())

        with patch(MOCK_SA_CLIENT, return_value=client):
            route.unlink()

        teardowns = [c.args[0] for c in client.sync_access_app.call_args_list
                     if not c.args[0]['enabled']]
        self.assertEqual(len(teardowns), 1,
                         '未保護但仍持有 app_id 的 route 必須被拆（#784）')
        self.assertEqual(teardowns[0]['existing_app_id'], 'app-a2')
        self.assertEqual(teardowns[0]['existing_policy_ids'], ['cf-a2'])

        # 否定性對照（同一支 mock）：真的沒東西要拆的 route 不該打 operator。
        bare = self._make_route(sa, protected=False)
        client.sync_access_app.reset_mock()
        with patch(MOCK_SA_CLIENT, return_value=client):
            bare.unlink()
        self.assertFalse(
            client.sync_access_app.called,
            '沒有任何 cf id 的 route 不該產生 operator 呼叫（判準是 id 不是旗標）')

    def test_A3_live_route_teardown_failure_still_fails_loud(self):
        """陽性對照組：**推得動**的 route 拆失敗 ⇒ 既有 fail-loud 一格不變。

        回滾把列救回來，而列活著就會被下一次 sync 再推一次 ⇒ 列是一個真的會被
        走到的重試錨點。此時**不落帳**（帳跟著回滾是對的：cf id 還在列上）。
        """
        sa = self._make_sa(state='active')
        route = self._make_route(sa, protected=True, app_id='app-a3',
                                 policy_cf_ids=('cf-a3',))
        route_id = route.id
        client = self._mock()
        client.sync_access_app.side_effect = Exception('cf access down')

        with patch(MOCK_SA_CLIENT, return_value=client):
            with self.assertRaises(UserError):
                route.unlink()

        survivor = self.env[_ROUTE].browse(route_id)
        self.assertTrue(survivor.exists(),
                        '推得動的 route 拆失敗必須保留列當重試錨點')
        self.assertEqual(survivor.access_app_id, 'app-a3')
        self.assertEqual(self._pending_policies(route_id), [],
                         '列活著、cf id 還在列上 ⇒ 不需要帳（且帳應隨回滾消失）')
        self.assertEqual(self._pending_apps(route_id), [])

    def test_A4_unsyncable_teardown_failure_books_and_still_deletes(self):
        """**推不動**的 route 拆失敗 ⇒ 刪除照常完成，id 全部落帳。

        保住列在這一格救不了任何人：沒有任何 sync 會再為停權中的 route 跑（那
        正是本 issue 的形狀）⇒ raise 只是把「靜默孤兒」換成「刪不掉」。

        斷言的是**載體內容**（哪幾顆、哪一種 kind），不是「沒有拋例外」。
        """
        sa = self._make_sa(state='suspended')
        route = self._make_route(sa, protected=True, app_id='app-a4',
                                 policy_cf_ids=('cf-a4-1', 'cf-a4-2'))
        route_id = route.id
        client = self._mock()
        client.sync_access_app.side_effect = Exception('cf access down')

        with patch(MOCK_SA_CLIENT, return_value=client):
            route.unlink()

        self.assertFalse(self.env[_ROUTE].browse(route_id).exists(),
                         '推不動的狀態下刪除不得被 teardown 失敗卡住')
        self.assertEqual(self._pending_apps(route_id), ['app-a4'],
                         'app id 必須落帳（不是只有 policy）')
        self.assertEqual(sorted(self._pending_policies(route_id)),
                         ['cf-a4-1', 'cf-a4-2'])

    def test_A5_operator_reported_orphans_are_booked_on_delete_path(self):
        """teardown 成功但 operator 說「有幾顆刪不掉」⇒ 那幾顆要落帳。

        舊寫法在這一格 log 一行就回 True，呼叫端接著把列刪掉 ⇒ 之後再也沒有
        任何記錄指向它們（與 #528 同型，只是發生在 teardown 這一側）。
        """
        sa = self._make_sa(state='active')
        route = self._make_route(sa, protected=True, app_id='app-a5',
                                 policy_cf_ids=('cf-a5-ok', 'cf-a5-stuck'))
        route_id = route.id
        client = self._mock(_teardown_result(orphaned=['cf-a5-stuck']))

        with patch(MOCK_SA_CLIENT, return_value=client):
            route.unlink()

        self.assertFalse(self.env[_ROUTE].browse(route_id).exists())
        self.assertEqual(self._pending_policies(route_id), ['cf-a5-stuck'],
                         'operator 說刪不掉的那顆必須留在帳上')
        self.assertEqual(self._pending_apps(route_id), [],
                         'app 拆掉了（回 2xx）⇒ 不留帳')

    def test_A6_active_happy_path_is_unchanged(self):
        """陽性對照組：active＋protected 的正常刪除路徑行為一格都不能變。"""
        sa = self._make_sa(state='active')
        route = self._make_route(sa, protected=True, app_id='app-a6',
                                 policy_cf_ids=('cf-a6',))
        route_id = route.id
        client = self._mock(_teardown_result())

        with patch(MOCK_SA_CLIENT, return_value=client):
            route.unlink()

        payload = [c.args[0] for c in client.sync_access_app.call_args_list
                   if not c.args[0]['enabled']][0]
        self.assertEqual(payload['existing_app_id'], 'app-a6')
        self.assertEqual(payload['existing_policy_ids'], ['cf-a6'])
        self.assertFalse(self.env[_ROUTE].browse(route_id).exists())
        self.assertEqual(self._pending_policies(route_id), [])


# ====================================================================== #
#  B 系列：兩個相鄰出口                                                   #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestSaAdjacentTeardownExits(TransactionCase, _Fixture):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 784B %s' % uuid.uuid4().hex[:6]})

    def test_B1_cleanup_sees_route_with_policy_ids_but_no_app_id(self):
        """相鄰出口 A：`_cleanup_cloudflare` 舊判準是
        `route_ids.filtered('access_app_id')` ⇒「app_id 已空但 policy 仍帶 cf
        id」的 route 從頭到尾看不到。

        陽性／否定性對照同一支 mock：同一顆 SA 下另一條什麼 id 都沒有的 route
        不得產生 teardown 呼叫。
        """
        sa = self._make_sa(state='active')
        route = self._make_route(sa, protected=False, app_id=None,
                                 policy_cf_ids=('cf-b1',))
        self._make_route(sa, protected=False)  # 什麼都沒有的對照 route
        client = self._mock(_teardown_result())

        with patch(MOCK_SA_CLIENT, return_value=client):
            sa._cleanup_cloudflare()

        teardowns = [c.args[0] for c in client.sync_access_app.call_args_list
                     if not c.args[0]['enabled']]
        self.assertEqual(
            len(teardowns), 1,
            '只有帶著 cf id 的那條 route 該被拆——一條都不能少，也不能多')
        self.assertEqual(teardowns[0]['existing_policy_ids'], ['cf-b1'])
        self.assertIsNone(teardowns[0]['existing_app_id'])
        self.assertFalse(route.access_policy_ids[0].cf_policy_id,
                         '拆成功之後列上的 cf id 要清掉')

    def test_B2_sa_unlink_without_tunnel_still_tears_down_route_access(self):
        """相鄰出口 B：`SecurityAccess.unlink()` 舊寫法在 `tunnel_id` 已空時整段
        跳過 cleanup ⇒ 「tunnel 已刪、Access 還在」這個再正常不過的中間態，整組
        app + policy id 靜默消失。

        operator 拆 Access 的 payload 從頭到尾不碰 tunnel，所以無 tunnel 不是
        「拆不動」，只是舊判準寫錯了。
        """
        sa = self._make_sa(state='error', tunnel_id=False)
        route = self._make_route(sa, protected=True, app_id='app-b2',
                                 policy_cf_ids=('cf-b2',))
        route_id = route.id
        client = self._mock(_teardown_result())

        with patch(MOCK_SA_CLIENT, return_value=client):
            sa.unlink()

        teardowns = [c.args[0] for c in client.sync_access_app.call_args_list
                     if not c.args[0]['enabled']]
        self.assertTrue(teardowns,
                        'tunnel 已空不代表 Access 沒東西要拆（#784 出口 B）')
        self.assertEqual(teardowns[0]['existing_app_id'], 'app-b2')
        self.assertEqual(teardowns[0]['existing_policy_ids'], ['cf-b2'])
        self.assertFalse(client.delete_tunnel.called,
                         '沒有 tunnel 就不該去刪 tunnel（否定性對照）')
        self.assertFalse(self.env[_ROUTE].browse(route_id).exists())


# ====================================================================== #
#  C 系列：dead anchor 的回收路徑（帳本唯一會被走到的出口）               #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestDeadAnchorReaper(TransactionCase, _Fixture):

    def setUp(self):
        super().setUp()
        self.Reap = self.env[_REAP]
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 784C %s' % uuid.uuid4().hex[:6]})

    def _dead_route_id(self):
        """一個保證不存在的 route id（真的刪過一條，不是憑空編號）。"""
        sa = self._make_sa(state='active')
        route = self._make_route(sa, protected=False)
        route_id = route.id
        route.unlink()
        self.assertFalse(self.env[_ROUTE].browse(route_id).exists())
        return route_id

    def test_C1_cron_reaps_dead_anchor_app_and_policies(self):
        """帳本的 dead-anchor 帳**真的會被走到**：cron 把 app ＋ policy 一起送
        去 operator 刪掉，回 2xx 之後消帳。

        沒有這一條，落帳只是把「靜默丟失」換成「靜默堆積」——一樣是一次性機制
        （#528 的根因）。
        """
        dead_id = self._dead_route_id()
        self.Reap._record_teardown(_ROUTE, dead_id, 'app-c1',
                                   ['cf-c1-1', 'cf-c1-2'])
        client = self._mock(_teardown_result())

        with patch(MOCK_REAP_CLIENT, return_value=client):
            self.Reap._cron_reap_dead_anchors()

        self.assertTrue(client.sync_access_app.called,
                        'dead anchor 的帳必須有人送出去')
        payload = client.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['existing_app_id'], 'app-c1')
        self.assertEqual(sorted(payload['existing_policy_ids']),
                         ['cf-c1-1', 'cf-c1-2'])
        self.assertEqual(self._pending_apps(dead_id), [])
        self.assertEqual(self._pending_policies(dead_id), [])

    def test_C2_cron_leaves_live_anchors_alone(self):
        """活錨點的帳**不歸 cron 管**：對活錨點送 `existing_app_id` 會把一顆正在
        保護租戶服務的 Access app 拆掉。

        陽性對照在同一支 mock 上：同一輪裡的 dead anchor 必須被送出去，否則
        「cron 跳過了活錨點」與「cron 根本沒跑」不可區分。
        """
        sa = self._make_sa(state='active')
        live = self._make_route(sa, protected=True, app_id='app-c2-live',
                                policy_cf_ids=())
        self.Reap._record_teardown(_ROUTE, live.id, 'app-c2-live', ['cf-c2-live'])
        dead_id = self._dead_route_id()
        self.Reap._record_teardown(_ROUTE, dead_id, 'app-c2-dead', ['cf-c2-dead'])
        client = self._mock(_teardown_result())

        with patch(MOCK_REAP_CLIENT, return_value=client):
            self.Reap._cron_reap_dead_anchors()

        sent = [c.args[0] for c in client.sync_access_app.call_args_list]
        self.assertEqual(len(sent), 1, '一輪只該送出 dead anchor 那一筆')
        self.assertEqual(sent[0]['existing_app_id'], 'app-c2-dead')
        self.assertEqual(self._pending_apps(live.id), ['app-c2-live'],
                         '活錨點的帳原封不動（它有自己的消費路徑）')
        self.assertEqual(self._pending_policies(live.id), ['cf-c2-live'])
        self.assertEqual(self._pending_apps(dead_id), [])

    def test_C3_cron_keeps_the_books_when_operator_fails(self):
        """operator 丟例外 ⇒ 帳一格都不能少（下一輪 cron 才是消帳時機）。"""
        dead_id = self._dead_route_id()
        self.Reap._record_teardown(_ROUTE, dead_id, 'app-c3', ['cf-c3'])
        client = self._mock()
        client.sync_access_app.side_effect = RuntimeError('operator down')

        with patch(MOCK_REAP_CLIENT, return_value=client):
            with self.assertLogs(LOG_REAP, level=logging.WARNING) as captured:
                self.Reap._cron_reap_dead_anchors()

        self.assertTrue(any('app-c3' in line for line in captured.output),
                        '失敗要指名是哪一筆，不能只說「失敗了」')
        self.assertEqual(self._pending_apps(dead_id), ['app-c3'])
        self.assertEqual(self._pending_policies(dead_id), ['cf-c3'])

    def test_C4_cron_keeps_orphaned_policies_on_the_books(self):
        """operator 回報「送去刪但刪不掉」⇒ 那幾顆留在帳上等下一輪，app 照樣消帳。

        迴路要閉合：下一輪必須把它**再送一次**。只斷言「帳還在」與漏掉沒兩樣。
        """
        dead_id = self._dead_route_id()
        self.Reap._record_teardown(_ROUTE, dead_id, 'app-c4',
                                   ['cf-c4-ok', 'cf-c4-stuck'])
        stuck = self._mock(_teardown_result(orphaned=['cf-c4-stuck']))

        with patch(MOCK_REAP_CLIENT, return_value=stuck):
            self.Reap._cron_reap_dead_anchors()

        self.assertEqual(self._pending_policies(dead_id), ['cf-c4-stuck'])
        self.assertEqual(self._pending_apps(dead_id), [],
                         'app 回 2xx ⇒ 消帳')

        clean = self._mock(_teardown_result())
        with patch(MOCK_REAP_CLIENT, return_value=clean):
            self.Reap._cron_reap_dead_anchors()
        payload = clean.sync_access_app.call_args.args[0]
        self.assertEqual(payload['existing_policy_ids'], ['cf-c4-stuck'],
                         '上一輪刪不掉的那顆必須被重送')
        self.assertIsNone(payload['existing_app_id'],
                          'app 已消帳 ⇒ 這一輪不該再帶 app id')
        self.assertEqual(self._pending_policies(dead_id), [])

    def test_C5_app_ids_never_leak_into_the_policy_channel(self):
        """🔴「這個結果不該出現」那一格：app id 不得混進 `existing_policy_ids`。

        混進去的話 CF 會拿它去 DELETE 一顆不存在的 policy 並回 404，而 404 在
        operator 端**視為成功** ⇒ 帳被消掉、app 永遠留在 CF，全程零訊號。
        兩種 kind 分得開是這張表的安全前提，不是整潔。
        """
        sa = self._make_sa(state='active')
        route = self._make_route(sa, protected=True, app_id='app-c5-live')
        self.Reap._record_teardown(_ROUTE, route.id, 'app-c5-booked',
                                   ['cf-c5'])

        self.assertEqual(self._pending_policies(route.id), ['cf-c5'])
        self.assertEqual(self._pending_apps(route.id), ['app-c5-booked'])
        self.assertNotIn(
            'app-c5-booked', route._build_existing_policy_ids(),
            'app id 混進 policy 刪除通道會被 CF 的 404=成功靜默吃掉（#784）')
        self.assertIn('cf-c5', route._build_existing_policy_ids())
