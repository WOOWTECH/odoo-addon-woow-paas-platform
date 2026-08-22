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
from datetime import timedelta
from unittest.mock import MagicMock, patch

from psycopg2 import OperationalError

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.security_access import (
    _internal_identity as _internal_sa, _skip_route_sync,
)

# #798：`cf_policy_id` 有欄位級寫入守衛（create 壓值／write 拒絕，對 su 也生效）。
# fixture 擺狀態必須經開閘 helper，否則會撞到自己要測的守衛。
from ..models.security_access import (
    _internal_policy_cf_write as _internal_policy_cf,
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
            # cf id 的回填留在 skip context 裡：這是 fixture 在擺狀態，不是被測
            # 的動作——讓它打 operator 會把「機制有沒有接好」與「fixture 自己多
            # 打了一趟」混在一起。
            _internal_policy_cf(pol).cf_policy_id = cf_id
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

        # 陽性對照（#791 覆核：否定性斷言不配對照就分不出「沒呼叫」與「mock 沒
        # 接上」）：同一支 mock、同樣的 unlink，SA **有** tunnel 時必須刪 tunnel。
        sa2 = self._make_sa(state='active', tunnel_id='tun-b2-live')
        self._make_route(sa2, protected=True, app_id='app-b2b')
        with patch(MOCK_SA_CLIENT, return_value=client):
            sa2.unlink()
        self.assertTrue(client.delete_tunnel.called,
                        '有 tunnel 的 SA 被刪時必須呼叫 delete_tunnel')
        self.assertEqual(client.delete_tunnel.call_args.args[0],
                         'tun-b2-live')


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
        """一個保證不存在的 route id（真的刪過一條，不是憑空編號）。

        SA 刻意不給 tunnel：route 的 unlink 對「active＋live tunnel」的 SA 會做
        一次全量 route re-sync（那一條**不看** `sa_skip_route_sync`），fixture
        不該在這裡產生 operator 呼叫。
        """
        sa = self._make_sa(state='pending', tunnel_id=False)
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


# ====================================================================== #
#  D 系列：#791 覆核發現（F1 佇列堵死／F2 卡帳訊號／F4 例外契約／F9 偽造） #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestReaperReviewFindings791(TransactionCase, _Fixture):
    """ccsu 對 PR #791 的覆核發現，每條一個守衛。"""

    def setUp(self):
        super().setUp()
        self.Reap = self.env[_REAP]
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 784D %s' % uuid.uuid4().hex[:6]})

    def _dead_route_id(self):
        sa = self._make_sa(state='pending', tunnel_id=False)
        route = self._make_route(sa, protected=False)
        route_id = route.id
        route.unlink()
        self.assertFalse(self.env[_ROUTE].browse(route_id).exists())
        return route_id

    def _age_rows(self, rows, days):
        """把帳齡往前推（`create_date` 是 magic column，用 SQL 改最不含糊）。"""
        old = fields.Datetime.now() - timedelta(days=days)
        self.env.cr.execute(
            "UPDATE woow_paas_platform_cf_access_policy_reap "
            "SET create_date = %s WHERE id IN %s",
            (old, tuple(rows.ids)))
        rows.invalidate_recordset(['create_date'])

    # ---------------- F1 [HIGH] ---------------- #

    def test_D1_unreapable_rows_cannot_block_the_queue(self):
        """🔴 F1：前面塞滿「永遠收不掉」的列時，後面的 dead-anchor 帳仍要被收走。

        第一版是 `search([], limit=500)` ——窗口套在「錨點死了沒」判定**之前**，
        而 `_order='id'` 讓兩類永遠消不掉的列（活錨點卡帳、未知 model）永遠佔住
        最小 id ⇒ 累積過 500 筆之後，cron 每小時做 0 件事、回 0、零 log，**回收
        機制自己把自己堵死，而且與「今天沒事做」在輸出上完全一樣**。

        判準是行為不是實作：不管前面卡了幾筆，可回收的那筆要在**同一輪**被送出。
        """
        live = self._make_route(self._make_sa(state='active'), protected=True,
                                app_id='app-d1-live')
        # 舊窗口是 500，這裡塞 520 筆「永遠收不掉」的列擋在前面：
        # 一半掛在活錨點上，一半掛在根本不存在的 model 上。
        blockers = self.Reap.sudo().create([{
            'anchor_model': (_ROUTE if i % 2 == 0
                             else 'woow_paas_platform.model_that_never_was'),
            'anchor_id': live.id,
            'kind': 'policy',
            'cf_object_id': 'cf-block-%03d' % i,
        } for i in range(520)])
        self.assertEqual(len(blockers), 520)

        dead_id = self._dead_route_id()
        self.Reap._record_teardown(_ROUTE, dead_id, 'app-d1', ['cf-d1'])
        client = self._mock(_teardown_result())

        with patch(MOCK_REAP_CLIENT, return_value=client):
            self.Reap._cron_reap_dead_anchors()

        self.assertTrue(
            client.sync_access_app.called,
            '前面卡著 520 筆收不掉的列，可回收的那筆仍必須在同一輪被送出（F1）')
        payload = client.sync_access_app.call_args.args[0]
        self.assertEqual(payload['existing_app_id'], 'app-d1')
        self.assertEqual(payload['existing_policy_ids'], ['cf-d1'])
        self.assertEqual(self._pending_apps(dead_id), [])
        self.assertEqual(self._pending_policies(dead_id), [])
        # 擋路的那些一筆都不該被動到——它們不是可回收的。
        self.assertEqual(len(blockers.exists()), 520,
                         '不可回收的列要原封不動留著（出聲，但不刪不送）')

        # 再證一次游標真的會跨批走完（而不是靠「一批剛好裝得下」）。
        dead2 = self._dead_route_id()
        self.Reap._record_teardown(_ROUTE, dead2, 'app-d1b', [])
        client.sync_access_app.reset_mock()
        with patch(MOCK_REAP_CLIENT, return_value=client):
            self.Reap._cron_reap_dead_anchors(scan_batch=7)
        self.assertEqual(
            client.sync_access_app.call_args.args[0]['existing_app_id'],
            'app-d1b', '掃描要沿 id 游標跨批走完，不是只看第一批')

    # ---------------- F2 [MEDIUM] ---------------- #

    def test_D2_stuck_books_on_live_anchors_are_loud(self):
        """F2：活錨點上的帳卡超過門檻要出聲；沒卡的要安靜。

        活錨點的帳只有一個消費者（錨點的下一次成功 sync）。錨點若進了永久終態，
        那個消費者永遠不會來，帳就永遠卡著——而 cron 依設計會跳過活錨點，
        `_warn_if_deferred` 又只在 sync 真的被觸發時才響 ⇒ 這一格今天完全無聲。
        本測試守的是「有聲」，不是「cron 去動它」（判準寫錯的代價是拆掉一顆正在
        保護租戶服務的 Access app，所以刻意只出聲不動手）。
        """
        live = self._make_route(self._make_sa(state='active'), protected=True,
                                app_id='app-d2')
        fresh = self.Reap._record_ids(_ROUTE, live.id, ['cf-d2-fresh'])
        client = self._mock(_teardown_result())

        # 安靜的那一半先驗，避免被下一段的資料污染。
        with patch(MOCK_REAP_CLIENT, return_value=client):
            with self.assertNoLogs(LOG_REAP, level=logging.WARNING):
                self.Reap._cron_reap_dead_anchors()

        self._age_rows(fresh, days=30)
        with patch(MOCK_REAP_CLIENT, return_value=client):
            with self.assertLogs(LOG_REAP, level=logging.WARNING) as captured:
                self.Reap._cron_reap_dead_anchors()
        self.assertTrue(
            any('cf-d2-fresh' in line for line in captured.output),
            '訊息要指名是哪一顆 id——只說「有幾筆」維運還是得自己翻 DB')
        self.assertFalse(client.sync_access_app.called,
                         '只出聲不動手：活錨點的 app 可能還在線上服務')

    # ---------------- F4 [MEDIUM] ---------------- #

    def test_D3_operational_error_propagates_out_of_the_bool_contract(self):
        """F4：記帳段的 OperationalError 要**往外拋**，不得被收成 False。

        吞掉它 Odoo 的 retrying() 永遠看不到，而此刻 PG 交易已 aborted，硬回一個
        bool 只會讓後續 flush 再炸一次（#518）。陽性對照放同一個方法：其他例外
        照舊被吞掉、teardown 仍回 True（CF 那側已經刪掉了，記帳失敗不翻轉語意）。
        """
        sa = self._make_sa(state='active')
        route = self._make_route(sa, protected=True, app_id='app-d3',
                                 policy_cf_ids=('cf-d3',))
        client = self._mock(_teardown_result())
        Reap = type(self.env[_REAP])

        with patch(MOCK_SA_CLIENT, return_value=client):
            with patch.object(Reap, '_settle_teardown',
                              side_effect=OperationalError('40001')):
                with self.assertRaises(OperationalError):
                    sa._teardown_route_access(
                        app_id='app-d3', policy_ids=['cf-d3'],
                        app_name=route.subdomain_prefix,
                        hostname=route.hostname,
                        anchor_model=_ROUTE, anchor_id=route.id)

            # 陽性對照：非 OperationalError 照舊吞掉，回傳值不變。
            with patch.object(Reap, '_settle_teardown',
                              side_effect=ValueError('bookkeeping blew up')):
                ok = sa._teardown_route_access(
                    app_id='app-d3', policy_ids=['cf-d3'],
                    app_name=route.subdomain_prefix,
                    hostname=route.hostname,
                    anchor_model=_ROUTE, anchor_id=route.id)
        self.assertTrue(
            ok, 'CF 已經刪掉了 ⇒ 記帳失敗不得翻轉成「拆失敗」')

    # ---------------- F9 [LOW] ---------------- #

    def test_D4_unknown_ids_never_become_durable_delete_orders(self):
        """🔴 F9：operator 回報的 orphaned id 只認「列值 ∪ 帳上已知」。

        **兩道分層，這一條同時驗兩道**：

        1. **#783 擋送出**：`existing_policy_ids` 曾經是「列值 ∪ 帳本 ∪ context」，
           而 `sa_unlinked_cf_policy_ids` 是一把 `call_kw` 直達的裸 context key ⇒
           #783 把 context 那條腿拿掉，偽造值**連這一輪的 payload 都進不去**。
        2. **#791 F9 擋落帳**：即使某顆 id 從別的地方冒出來（這裡模擬 operator 回報
           了一顆我們**根本沒送過**的 id），`_settle()` 的 `trusted_ids` 過濾也不讓
           它落進 DB——一次性的異常不得升級成持久的刪除指令，之後每小時重送到刪掉
           為止。

        ⚠ 第 2 道刻意保留：operator 的回應與「這一輪送了什麼」是兩件事，
        `orphaned_policy_ids` 裡冒出未知 id 是**外部輸入**，不會因為 #783 而消失。

        兩格陽性對照（缺任一格，「過濾生效」與「整段壞掉」不可區分）：payload 裡
        列上那顆必須在、帳上列值那顆必須落帳。
        """
        sa = self._make_sa(state='active')
        route = self._make_route(sa, protected=True, app_id='app-d4',
                                 policy_cf_ids=('cf-d4-real',))
        # operator 把兩顆都回報成「刪不掉」：一顆是列值，一顆是偽造的 context 值。
        client = self._mock(_sync_result(
            app_id='app-d4', policy_ids=['cf-new'],
            orphaned=['cf-d4-real', 'cf-d4-forged']))

        with patch(MOCK_SA_CLIENT, return_value=client):
            with self.assertLogs(LOG_REAP, level=logging.WARNING) as captured:
                route.with_context(
                    sa_unlinked_cf_policy_ids=['cf-d4-forged']
                )._sync_access_to_operator()

        payload = client.sync_access_app.call_args.args[0]
        self.assertIn(
            'cf-d4-real', payload['existing_policy_ids'],
            '陽性對照：列上真實存在的 cf id 必須在這一輪的 payload 裡——少了這格，'
            'payload 整個壞掉也會讓下面那條 assertNotIn 通過')
        self.assertNotIn(
            'cf-d4-forged', payload['existing_policy_ids'],
            '第一道（#783）：呼叫端 context 塞的 id 連送出端都進不去')
        pending = self._pending_policies(route.id)
        self.assertIn('cf-d4-real', pending,
                      '陽性對照：列上真實存在過的孤兒必須落帳')
        self.assertNotIn(
            'cf-d4-forged', pending,
            '第二道（#791 F9）：operator 回報的未知 id 不得經結帳變成持久的刪除'
            '指令')
        self.assertTrue(any('cf-d4-forged' in line for line in captured.output),
                        '拒收要出聲，否則與「根本沒發生」不可區分')


# ====================================================================== #
#  E 系列：批次 unlink 的回滾撕裂（F3）與帳本舊帳一起帶出去（F6）         #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestBatchUnlinkTearing791(TransactionCase, _Fixture):

    def setUp(self):
        super().setUp()
        self.Reap = self.env[_REAP]
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 784E %s' % uuid.uuid4().hex[:6]})

    def test_E1_failing_fail_loud_route_does_not_tear_the_best_effort_one(self):
        """🔴 F3：批次刪除中，pass 1 失敗時 pass 2 的 CF 物件一根手指都不能碰。

        這一段的 raise 讓 savepoint 回捲 `super().unlink()`，但**回捲不掉已經對
        CF 發出的刪除** ⇒ 先被拆掉的那幾條會以「列復活、cf id 指向 CF 已不存在的
        物件」收場。本 PR 新捕獲的那些 route（未保護但仍持有殘 id／停權中）如果
        也被拆了再回捲，就是本 PR 自己加的新殘留——所以它們排在 pass 2，而 pass 1
        一有失敗就直接 raise。

        第二段同時鎖住工單要求的「批次中途 raise 之後重試刪除可收斂」。
        """
        sa = self._make_sa(state='active')
        # pass 1 那組：protected ＋ live SA ⇒ 拆失敗要回滾保住列
        p = self._make_route(sa, protected=True, app_id='app-e1-p')
        # pass 2 那組：未保護、沒有 app_id、只剩 policy 殘 id ⇒ 拆失敗只落帳
        u = self._make_route(sa, protected=False, app_id=None,
                             policy_cf_ids=('cf-e1-u',))
        p_id, u_id = p.id, u.id
        client = self._mock(_teardown_result())

        def _fail_protected(payload):
            if payload.get('existing_app_id') == 'app-e1-p':
                raise Exception('cf access down')
            return _teardown_result()

        client.sync_access_app.side_effect = _fail_protected
        with patch(MOCK_SA_CLIENT, return_value=client):
            with self.assertRaises(UserError):
                (p | u).unlink()

        sent = [c.args[0] for c in client.sync_access_app.call_args_list]
        self.assertTrue(
            any(c.get('existing_app_id') == 'app-e1-p' for c in sent),
            '前提檢查：pass 1 那條確實被試過（否則整條測試沒有鑑別力）')
        # 🔴 鑑別力補強（#792 二輪覆核）：上面那條若只靠「p 比 u 早建立」，一個
        # 「單迴圈＋第一條失敗就 raise」的錯誤實作也會通過。所以再斷言**順序**
        # ——pass 1 那條必須排在所有呼叫的最前面，而且整輪只有它一個 teardown。
        teardown_calls = [c for c in sent if not c.get('enabled')]
        self.assertEqual(
            len(teardown_calls), 1,
            'pass 1 失敗那一輪只該有它自己那一個 teardown 被送出去')
        self.assertEqual(teardown_calls[0].get('existing_app_id'), 'app-e1-p')
        self.assertFalse(
            any('cf-e1-u' in (c.get('existing_policy_ids') or [])
                for c in sent),
            'pass 1 失敗之後不得再去拆 pass 2 那條——那是回捲不掉的 CF 刪除')
        self.assertTrue(self.env[_ROUTE].browse(p_id).exists())
        survivor = self.env[_ROUTE].browse(u_id)
        self.assertTrue(survivor.exists())
        self.assertEqual(
            survivor.access_policy_ids[0].cf_policy_id, 'cf-e1-u',
            '列復活時 cf id 必須還在列上（它就是這條的重試錨點）')
        self.assertEqual(self._pending_policies(u_id), [],
                         '沒拆就不該落帳（帳跟著回滾是對的）')

        # 收斂：operator 恢復之後重試刪除，兩條都拆得掉、兩列都消失。
        client.sync_access_app.side_effect = None
        client.sync_access_app.return_value = _teardown_result()
        client.sync_access_app.reset_mock()
        with patch(MOCK_SA_CLIENT, return_value=client):
            (self.env[_ROUTE].browse(p_id)
             | self.env[_ROUTE].browse(u_id)).unlink()
        retried = [c.args[0] for c in client.sync_access_app.call_args_list]
        self.assertTrue(any(c.get('existing_app_id') == 'app-e1-p'
                            for c in retried))
        self.assertTrue(any('cf-e1-u' in (c.get('existing_policy_ids') or [])
                            for c in retried))
        self.assertFalse(self.env[_ROUTE].browse(p_id).exists())
        self.assertFalse(self.env[_ROUTE].browse(u_id).exists())

    def test_E2_ledger_debt_rides_along_on_the_delete_teardown(self):
        """F6：**被捕獲的** route，帳本上還欠的 id 要跟著這一趟 teardown 一起送。

        ⚠ 本測試在 #792 三輪覆核時改過標的（原版已被推翻，保留紀錄以免下次有人
        照原樣改回去）。原版用的是「列已乾淨、只有帳本有欠款」的 route，斷言它
        會被刪除路徑捕獲並把帳送出去。項 2 之後那個斷言不再成立、而且**不該**成
        立：捕獲的閘只看列狀態，因為「列已乾淨＋帳本有欠款」的 route 若被捕獲，
        且剛好 `is_protected=True` ＋ SA live ＋ state 在允許清單內，就會落進
        pass 1 ⇒ 它一失敗就把同批其他 route 已經對 CF 發出、回捲不掉的刪除一起
        捲回去（#794 那條無聲 fail-open 的燃料；可達性見 `unlink()` 的註解與
        `test_H3`）。那種 route 的帳改由 dead-anchor cron 回收。

        F6 原本要的保證在**列上有 id**的 route 上仍然成立，這裡守的就是那一格。
        """
        sa = self._make_sa(state='active')
        route = self._make_route(sa, protected=True, app_id='app-e2',
                                 policy_cf_ids=('cf-e2-row',))
        self.Reap._record_ids(_ROUTE, route.id, ['cf-e2-debt'])
        route_id = route.id
        client = self._mock(_teardown_result())

        with patch(MOCK_SA_CLIENT, return_value=client):
            route.unlink()

        teardowns = [c.args[0] for c in client.sync_access_app.call_args_list
                     if not c.args[0]['enabled']]
        self.assertEqual(len(teardowns), 1)
        self.assertEqual(teardowns[0]['existing_app_id'], 'app-e2')
        self.assertEqual(sorted(teardowns[0]['existing_policy_ids']),
                         ['cf-e2-debt', 'cf-e2-row'],
                         '列上的與帳本上的都要跟著這一趟送出去（F6 的原意）')
        self.assertEqual(self._pending_policies(route_id), [],
                         'operator 回 2xx ⇒ 舊帳當場結清，不必等 cron')


# ====================================================================== #
#  G 系列：#792 二輪覆核（F6 回歸、F1(a) 預算、cron 小項）                 #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestSecondRoundReview792(TransactionCase, _Fixture):

    def setUp(self):
        super().setUp()
        self.Reap = self.env[_REAP]
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 784G %s' % uuid.uuid4().hex[:6]})

    def _dead_route_id(self):
        sa = self._make_sa(state='pending', tunnel_id=False)
        route = self._make_route(sa, protected=False)
        route_id = route.id
        route.unlink()
        return route_id

    # ---------------- F6（真回歸，擋 merge） ---------------- #

    def test_G1_orphaned_policy_must_not_pin_the_sa_in_error_forever(self):
        """🔴 F6：operator 說某顆 policy 刪不掉 ⇒ 帳留著，但 SA **不可以**永久卡
        在 `state='error'`。

        `_do_cleanup` 的「清乾淨了沒」閘一旦把帳本聯集進來，就會被一筆永遠收不掉
        的帳（錨點還活著 ⇒ dead-anchor cron 依設計不收）釘死：閘對該 route 永遠
        回 True ⇒ 每輪 lifecycle cron 白打一次 operator、SA 永遠回不到 `deleting`。
        判準是**列乾淨了沒**（`_has_access_teardown()`），不是「帳上還有沒有」；
        要送出去的集合才用 `_access_teardown_payload_ids()`。
        """
        sa = self._make_sa(state='active')
        route = self._make_route(sa, protected=True, app_id='app-g1',
                                 policy_cf_ids=('cf-g1-stuck',))
        # operator 回報「送去刪但刪不掉」⇒ 落帳、列上的 id 照樣被清掉
        client = self._mock(_teardown_result(orphaned=['cf-g1-stuck']))

        with patch(MOCK_SA_CLIENT, return_value=client):
            sa._do_cleanup()

        self.assertEqual(self._pending_policies(route.id), ['cf-g1-stuck'],
                         '前提檢查：那顆確實留在帳上（否則本測試沒有鑑別力）')
        self.assertFalse(route.access_app_id,
                         '前提檢查：teardown 回 True ⇒ 列上的 id 已被清掉')
        self.assertEqual(
            sa.state, 'deleting',
            '列已經乾淨 ⇒ SA 必須收斂到 deleting；卡在 error 代表閘把帳本讀進去了')

        # 陽性對照（同一支 mock）：列上真的還有 id 時，閘必須照舊擋住。
        sa2 = self._make_sa(state='active')
        r2 = self._make_route(sa2, protected=True, app_id='app-g1b')
        failing = self._mock()
        failing.sync_access_app.side_effect = Exception('cf down')
        with patch(MOCK_SA_CLIENT, return_value=failing):
            sa2._do_cleanup()
        self.assertEqual(r2.access_app_id, 'app-g1b')
        self.assertEqual(sa2.state, 'error',
                         '列上還有 id ⇒ 這才是該落 error 的那一格')
        # #792 三輪覆核 1(b)：陽性對照原本只斷言 state=='error'，而那個值在
        # 「tunnel 也沒刪掉」時同樣會出現 ⇒ 鑑別力靠 mock 恰好讓 delete_tunnel
        # 成功，不是靠斷言鎖住。把「error 是**因為 Access 沒清掉**而不是因為
        # tunnel 還在」釘死。
        self.assertFalse(sa2.tunnel_id,
                         'tunnel 這半是成功的 ⇒ error 只可能來自 Access 那半')

    def test_G2_payload_still_carries_the_ledger_debt_on_delete(self):
        """F6 的另一半：閘不看帳本，但**送出去的集合**仍要帶帳本舊帳。

        兩件事拆開之後最容易的退化就是「順手把帳本那半也拿掉」，那會把 F6 原本
        要的好處退回去。
        """
        sa = self._make_sa(state='active')
        route = self._make_route(sa, protected=False, app_id=None)
        self.Reap._record_ids(_ROUTE, route.id, ['cf-g2-debt'])

        self.assertFalse(route._has_access_teardown(),
                         '閘只看列狀態：列上沒有 id ⇒ 這一列是乾淨的')
        self.assertEqual(route._access_teardown_payload_ids(),
                         (None, ['cf-g2-debt']),
                         '要送出去的集合仍須包含帳本上還欠的')

    # ---------------- F1(a) ---------------- #

    def test_G3_failed_anchors_count_against_the_limit_budget(self):
        """F1(a)：`limit` 管的是**外呼次數**（成敗都算），不是成功次數。

        只算成功的話，operator 持續失敗時一輪會一路打到 `max_scan`——失敗時打得
        比成功時還多，而且與 docstring 自己的宣稱矛盾。
        """
        dead_ids = [self._dead_route_id() for _ in range(5)]
        for i, rid in enumerate(dead_ids):
            self.Reap._record_teardown(_ROUTE, rid, 'app-g3-%d' % i, [])
        client = self._mock()
        client.sync_access_app.side_effect = Exception('operator down')

        with patch(MOCK_REAP_CLIENT, return_value=client):
            reaped = self.Reap._cron_reap_dead_anchors(limit=2)

        self.assertEqual(reaped, 0, '全都失敗 ⇒ 收成 0')
        self.assertEqual(
            client.sync_access_app.call_count, 2,
            'limit=2 ⇒ 這一輪最多只能對 operator 發 2 次，失敗也要算進預算')

        # 陽性對照：成功時同樣受 limit 管。
        ok = self._mock(_teardown_result())
        with patch(MOCK_REAP_CLIENT, return_value=ok):
            n = self.Reap._cron_reap_dead_anchors(limit=2)
        self.assertEqual(n, 2)
        self.assertEqual(ok.sync_access_app.call_count, 2)

    # ---------------- (c) 截斷警報 ＋ (e) 假警報 ---------------- #

    def test_G4_scan_cap_is_loud_but_does_not_cry_wolf(self):
        """(c) 掃描上界踩到一定出聲；(e) 帳本剛好整除 `scan_batch` 時不得假警報。

        被截斷卻安靜＝謊報「都收完了」；沒被截斷卻報＝訊號通膨。兩半放同一個
        方法，因為它們是同一個判斷的兩面。
        """
        live = self._make_route(self._make_sa(state='active'), protected=True,
                                app_id='app-g4')
        self.Reap.sudo().create([{
            'anchor_model': _ROUTE, 'anchor_id': live.id, 'kind': 'policy',
            'cf_object_id': 'cf-g4-%03d' % i} for i in range(10)])
        client = self._mock(_teardown_result())

        # (e) 10 筆、scan_batch=5、max_scan=10 ⇒ 剛好掃完，不該報截斷
        with patch(MOCK_REAP_CLIENT, return_value=client):
            with self.assertNoLogs(LOG_REAP, level=logging.WARNING):
                self.Reap._cron_reap_dead_anchors(scan_batch=5, max_scan=10)

        # (c) max_scan=5 ⇒ 真的被截斷，必須出聲
        with patch(MOCK_REAP_CLIENT, return_value=client):
            with self.assertLogs(LOG_REAP, level=logging.WARNING) as captured:
                self.Reap._cron_reap_dead_anchors(scan_batch=5, max_scan=5)
        self.assertTrue(
            any('scan cap reached' in line for line in captured.output),
            '被截斷卻安靜等於謊報「都收完了」')

    # ---------------- (d) 早退吞訊號 ---------------- #

    def test_G5_unconfigured_operator_does_not_swallow_the_other_signals(self):
        """(d)：operator 未設定的早退不得吞掉同一輪已經算出來的 stuck 警告。

        那正好是「有東西要講、卻剛好講不出來」的那一格。
        """
        live = self._make_route(self._make_sa(state='active'), protected=True,
                                app_id='app-g5')
        rows = self.Reap._record_ids(_ROUTE, live.id, ['cf-g5-stuck'])
        self.env.cr.execute(
            "UPDATE woow_paas_platform_cf_access_policy_reap "
            "SET create_date = %s WHERE id IN %s",
            (fields.Datetime.now() - timedelta(days=30), tuple(rows.ids)))
        rows.invalidate_recordset(['create_date'])
        dead_id = self._dead_route_id()
        self.Reap._record_teardown(_ROUTE, dead_id, 'app-g5-dead', [])

        with patch(MOCK_REAP_CLIENT, return_value=None):
            with self.assertLogs(LOG_REAP, level=logging.WARNING) as captured:
                self.Reap._cron_reap_dead_anchors()

        joined = '\n'.join(captured.output)
        self.assertIn('cf-g5-stuck', joined,
                      'operator 沒設定不代表卡帳這件事就不用講')
        self.assertIn('not configured', joined,
                      '同時也要講 operator 沒設定（兩段訊號都要在）')


# ====================================================================== #
#  H 系列：#792 三輪覆核 —— 「靜默誤用」那一格                            #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestSilentGateMisuse792(TransactionCase, _Fixture):
    """gate 選錯版本時**完全沒有輸出**，所以要直接斷言選中的是誰。"""

    def setUp(self):
        super().setUp()
        self.Reap = self.env[_REAP]
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 784H %s' % uuid.uuid4().hex[:6]})

    def test_H1_cleanup_filter_must_not_select_rows_that_are_already_clean(self):
        """🔴 覆核 ④：現有測試只驗「該選中的有選中」，沒驗「不該選中的沒選中」。

        `_cleanup_cloudflare` 的 filter 若被誤用成 payload 版（列 ∪ 帳本），會把
        **每一條** route 都選中；而選中之後 `_teardown_route_access()` 自己的
        「沒 id 就回 True」早退讓它**完全靜默**——不打 operator、不寫欄位、不留
        log。也就是說那個誤用在整套測試裡不會紅。

        所以判準不能建立在 operator 呼叫上（那正是誤用不會改變的東西），要**直接
        斷言選中的集合**。
        """
        sa = self._make_sa(state='active')
        dirty = self._make_route(sa, protected=True, app_id='app-h1')
        ledger_only = self._make_route(sa, protected=True, app_id=None)
        self.Reap._record_ids(_ROUTE, ledger_only.id, ['cf-h1-debt'])
        spotless = self._make_route(sa, protected=False, app_id=None)

        selected = sa.route_ids.filtered(lambda r: r._has_access_teardown())
        self.assertEqual(
            selected, dirty,
            '只有「列上真的還有 id」的那條該被選中——帳本欠款不算，完全乾淨的更不算')
        self.assertNotIn(ledger_only, selected,
                         '帳本有欠款但列已乾淨 ⇒ 不得進 cleanup 批次（F6 回歸的形狀）')
        self.assertNotIn(spotless, selected)

    def test_H2_cleanup_payload_still_carries_ledger_debt(self):
        """1(a)：`_cleanup_cloudflare` 的 payload 呼叫點被靜默還原成 row-only 時，
        三條既有測試都量不到——因為它們送的 route 帳本上本來就沒有欠款。

        這裡讓同一條 route **列上有 id、帳本也有欠款**，斷言送出去的清單兩邊都
        帶到。還原成 row-only 就會少掉帳本那顆 ⇒ 紅。
        """
        sa = self._make_sa(state='active')
        route = self._make_route(sa, protected=True, app_id='app-h2',
                                 policy_cf_ids=('cf-h2-row',))
        self.Reap._record_ids(_ROUTE, route.id, ['cf-h2-ledger'])
        client = self._mock(_teardown_result())

        with patch(MOCK_SA_CLIENT, return_value=client):
            sa._cleanup_cloudflare()

        teardowns = [c.args[0] for c in client.sync_access_app.call_args_list
                     if not c.args[0]['enabled']]
        self.assertEqual(len(teardowns), 1)
        self.assertEqual(teardowns[0]['existing_app_id'], 'app-h2')
        self.assertEqual(sorted(teardowns[0]['existing_policy_ids']),
                         ['cf-h2-ledger', 'cf-h2-row'],
                         'cleanup 送出去的清單要同時帶列上的與帳本上的')

    def test_H3_unlink_capture_gate_is_row_state_not_payload(self):
        """項 2：捕獲迴圈的閘用**列狀態版**——「列已乾淨、帳本有欠款」的 route
        不得被拉進批次。

        它若被拉進來，且剛好是 `is_protected=True` ＋ SA live ＋ state 在允許清單
        內，就會落進 pass 1 ⇒ 它的 teardown 一失敗就把同批其他 route **已經對 CF
        發出、回捲不掉**的刪除一起捲回去（#794 那條無聲 fail-open 的燃料）。

        那個狀態是可達的：`_do_cleanup` 的 Access teardown 成功但 operator 回報
        orphaned（清掉列上的 id、orphaned 落帳）＋ 同一輪 tunnel 刪除失敗
        （保留 tunnel_id、寫 state='error'）⇒ 三個條件同時成立。
        """
        sa = self._make_sa(state='error')          # 'error' 在允許清單內
        route = self._make_route(sa, protected=True, app_id=None)
        self.Reap._record_ids(_ROUTE, route.id, ['cf-h3-debt'])
        route_id = route.id
        # 前提檢查：這條 route 確實會落進 pass 1（若被捕獲的話）
        self.assertIsNone(route._access_sync_skip_reason(),
                          '前提：這條 route 推得動 ⇒ 被捕獲的話就是 pass 1 成員')
        client = self._mock(_teardown_result())

        with patch(MOCK_SA_CLIENT, return_value=client):
            route.unlink()

        self.assertFalse(client.sync_access_app.called,
                         '列已乾淨 ⇒ 不該被捕獲，也就不該產生 teardown 呼叫')
        self.assertFalse(self.env[_ROUTE].browse(route_id).exists())
        self.assertEqual(self._pending_policies(route_id), ['cf-h3-debt'],
                         '帳留著等 dead-anchor cron——那才是它的回收路徑')

        # 陽性對照（同一支 mock）：列上有 id 的照樣要被捕獲並送出去。
        dirty = self._make_route(sa, protected=True, app_id='app-h3')
        client.sync_access_app.reset_mock()
        with patch(MOCK_SA_CLIENT, return_value=client):
            dirty.unlink()
        self.assertTrue(client.sync_access_app.called)
        self.assertEqual(
            client.sync_access_app.call_args.args[0]['existing_app_id'],
            'app-h3')
