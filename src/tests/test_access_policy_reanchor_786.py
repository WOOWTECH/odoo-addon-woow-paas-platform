"""#786：換錨點時 `_access_sync_targets()` 在 `super().write()` 之後才求值
⇒ 舊錨點永不重推，它的 CF Access application 繼續掛著已經搬走的那顆 policy。

## 守什麼

`SecurityAccessAccessPolicy.write()` 的 re-sync 迴圈原本長這樣：

    with self.env.cr.savepoint():
        result = super().write(vals)          # ← 錨點已經變成 B
        ...
        for anchor in self._access_sync_targets():   # ← 只拿得到 B
            anchor._sync_access_to_operator()

錨點欄位**有**被列進觸發條件（`_ANCHOR_FIELDS` 在 `any(k in vals ...)` 裡），
所以這不是「忘了觸發」而是「觸發了但對象取錯」——症狀因此更難發現：換錨點的
write 看起來確實打了 operator，只是打的是新錨點。

漏掉舊錨點 A 的後果有兩層，第二層比第一層嚴重：

1. **A 的 CF Access application 仍然引用那顆 reusable policy** ——DB 上這條
   policy 已經不屬於 A 了，CF 上 A 那顆 app 還照著它放行。這是**授權發散**，
   不只是成本。
2. 那顆 cf id 進不了 A 的 `existing_policy_ids`（operator 唯一的刪除通道，且
   它是從「A 底下**尚存**的列」推導出來的），於是也刪不掉。而新錨點 B 就算把
   同一顆 id 送去刪也刪不掉——operator 是 **detach-before-delete**，A 的 app
   還引用著它時 CF 會以 "in use" 拒絕，回進 `orphaned_policy_ids`。

## 本檔守四件事

1. **舊錨點必須被重推**，而且 payload 要帶得到被搬走那顆 cf id
   （`existing_policy_ids`）。
2. **推送順序：舊錨點先、新錨點後**。operator 是 detach-before-delete，舊錨點
   先推才會把那顆 policy 從舊 app 上 detach 掉；反過來的話新錨點送去刪的那一顆
   會被 CF 判 "in use"，變成 orphan。順序在這裡是正確性不是偏好。
3. **帳要 durable**：舊錨點這一次重推可能被它自己的 liveness gate 擋掉（停權
   中的 SA 就是），被擋掉時那顆 id 必須留在 `cf_access_policy_reap` 帳本上等
   下一次推得動（#528 的同一套機制，同一個理由）。
4. **回存不得錯位**：搬移後兩個錨點各自 `zip(policies_sorted, returned)` 回存，
   兩側排序基準不同（同一條 policy 在 A 排第一、在 B 可能排最後），錯位的話
   兩顆 cf id 會互換而**沒有任何錯誤**。

## 判別式紀律（這檔最容易寫成假綠的三格）

* **否定性斷言一律配同一支 mock 的陽性對照**（同一個測試方法、同一支 mock）。
  patch 點寫錯時「不該打 operator」會**無聲通過**——根本沒有東西被呼叫。
* **`assertLogs` 的 level 必須傳數值**（`logging.WARNING`）。Odoo 的
  `addLevelName(25, "INFO")` 覆寫了名稱→數值表 ⇒ 字串 `'WARNING'` 會解析錯，
  `assertLogs` 恆紅、`assertNoLogs` 恆綠（repo-lint `assertlogs-numeric-level`）。
* **zip 那一格不能只斷言「有值」**。搬移後兩邊都會被寫回某個字串，只斷言
  truthy 的話錯位與正確在輸出上不可區分——所以 D 系列刻意讓同一條 policy 在
  舊錨點排**第一**、在新錨點排**最後**，錯位就會拿到另一顆 id。
* **mock 的回傳一律是真的 response 形狀**（五個 key），不用裸 MagicMock——裸
  mock 會讓回存那段拿到 MagicMock 而不是字串，測試綠但 prod 炸。
"""
import logging
import uuid
from unittest.mock import MagicMock, patch
from .publication_receipt_fixture import access_receipt, routes_receipt

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged

from ..models.cloud_service import _cs_skip_access_sync, _internal_provision
from ..models.security_access import (
    _internal_identity as _internal_sa, _skip_route_sync,
)
from ..services.naming import make_service_subdomain

# #798：`cf_policy_id` 有欄位級寫入守衛（create 壓值／write 拒絕，對 su 也生效）。
# fixture 擺狀態必須經開閘 helper，否則會撞到自己要測的守衛。
from ..models.security_access import (
    _internal_policy_cf_write as _internal_policy_cf,
)

#: 🔴 兩腿兩個 patch 點，判準是「該 model 怎麼取得 client」不是「client 定義在
#: 哪」。SA route 走自己 module 裡的 import，CS 走 services 那支——互抄會讓
#: 否定性斷言無聲通過。
MOCK_SA_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
                  'get_paas_operator_client')
MOCK_CS_CLIENT = ('odoo.addons.woow_paas_platform.services.paas_operator.'
                  'get_paas_operator_client')

LOG_SA = 'odoo.addons.woow_paas_platform.models.security_access'

_REAP = 'woow_paas_platform.cf_access_policy_reap'
_ROUTE = 'woow_paas_platform.security_access_route'
_POLICY = 'woow_paas_platform.security_access_access_policy'


def _sync_result(app_id='app-786', policy_ids=None, orphaned=None):
    """operator `PUT /api/access/sync` 的真實回傳形狀（五個 key 一個都不少）。"""
    return {
        'app_id': app_id,
        'policy_ids': list(policy_ids or []),
        'allowed_idps': [],
        'unresolved_idp_types': [],
        'orphaned_policy_ids': list(orphaned or []),
    }


class _Fixture:
    """兩條 SA route（＋需要時一台 CS）與帳本查詢 helper。

    fixture 擺狀態一律走 `_skip_route_sync`：讓 fixture 自己打 operator 的話，
    「機制有沒有接好」與「fixture 多打了一趟」在 call_args_list 上不可區分。
    """

    def _mock(self, result=None):
        client = MagicMock()
        if result is None:
            client.sync_access_app.side_effect = access_receipt
        else:
            client.sync_access_app.return_value = result
        client.sync_tunnel_routes.side_effect = routes_receipt
        client.park_release_route.return_value = {'status': 'parked'}
        client.unpark_release_route.return_value = {'status': 'unparked'}
        return client

    def _responder(self, client, by_app_id, default=None):
        """讓 `sync_access_app` 依 payload 決定回什麼，**不綁呼叫次序**。

        用 `side_effect=[...]` 的話，回傳值本身就編碼了「誰先誰後」——順序錯了
        會表現成別的欄位對不上，而不是順序斷言變紅。判準與被判的東西要分開。
        """
        def _fn(payload):
            key = payload.get('existing_app_id')
            template = by_app_id.get(key, default)
            if template is None:
                return access_receipt(payload)
            count = len(payload.get('policies') or [])
            mapped_ids = list(template.get('policy_ids') or [])
            return access_receipt(
                payload, app_id=template.get('app_id'),
                policy_ids=(mapped_ids if len(mapped_ids) == count else None),
                allowed_idps=template.get('allowed_idps') or None,
                orphaned_policy_ids=template.get('orphaned_policy_ids') or [])
        client.sync_access_app.side_effect = _fn
        return client

    def _calls_by_app(self, client):
        """`{existing_app_id: payload}`，外加一份保序的 app id 清單。"""
        payloads = [c.args[0] for c in client.sync_access_app.call_args_list]
        return ({p.get('existing_app_id'): p for p in payloads},
                [p.get('existing_app_id') for p in payloads])

    def _pending(self, anchor):
        return self.env[_REAP]._pending_ids(anchor._name, anchor.id)

    def _make_sa(self, state='active', tunnel_id=None, workspace=None):
        sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'SA 786 %s' % uuid.uuid4().hex[:6],
            'workspace_id': (workspace or self.workspace).id})
        _internal_sa(sa).write({
            'state': state,
            'tunnel_id': tunnel_id or 'tun-%s' % uuid.uuid4().hex[:6]})
        return sa

    def _make_route(self, sa, app_id, protected=True):
        route = _skip_route_sync(self.env[_ROUTE].sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'r%s' % uuid.uuid4().hex[:8],
            'is_protected': protected})
        _internal_sa(route).access_app_id = app_id
        return route.with_context(sa_skip_route_sync=False)

    def _make_service(self, name=None):
        svc = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': name or 'svc-786-%s' % uuid.uuid4().hex[:6],
                'workspace_id': self.workspace.id,
                'template_id': self.template.id,
                'state': 'running'})
        svc.subdomain = make_service_subdomain(
            self.workspace.ws_hash, svc.reference_id, svc.name)
        # #787：`cs_skip_access_sync` 只認模組私有 sentinel，裸 context 值
        # 打不開閘門 —— fixture 用 helper，不然這裡會真的去打 operator。
        _cs_skip_access_sync(svc).write({'is_protected': True})
        return svc

    def _policy(self, anchor_field, anchor, cf_policy_id=None, sequence=None,
                name=None):
        vals = {
            anchor_field: anchor.id,
            'name': name or 'pol-%s' % uuid.uuid4().hex[:6],
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        }
        if sequence is not None:
            vals['sequence'] = sequence
        pol = _skip_route_sync(self.env[_POLICY].sudo()).create(vals)
        if cf_policy_id:
            _internal_policy_cf(pol).cf_policy_id = cf_policy_id
        # 🔴 `create()` 回的 recordset 帶著呼叫當下的 context；不關掉的話之後
        # 每一次 write 都還套著 skip，「機制沒接好」與「fixture 把 sync 關掉了」
        # 會長得一模一樣。
        return pol.with_context(sa_skip_route_sync=False)


# ====================================================================== #
#  A 系列：#786 主線 —— 舊錨點必須被重推，且帶得到那顆 cf id              #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestReanchorResyncsOldAnchor(TransactionCase, _Fixture):
    """同 model 換錨點（route A → route B）。"""

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 786A %s' % uuid.uuid4().hex[:6]})
        self.sa = self._make_sa()
        self.route_a = self._make_route(self.sa, 'app-a')
        self.route_b = self._make_route(self.sa, 'app-b')

    def test_A1_old_anchor_is_resynced_and_carries_the_moved_cf_id(self):
        """#786 原案：搬家之後**兩個**錨點都要被推，且舊錨點的刪除通道帶得到
        那顆被搬走的 cf id。

        修前：`_access_sync_targets()` 在 `super().write()` 之後求值 ⇒ 只回新
        錨點 ⇒ 紅燈落在「舊錨點 app-a 根本沒被推」。
        """
        moved = self._policy('route_id', self.route_a, cf_policy_id='cf-moved',
                             sequence=10, name='moved')
        stay = self._policy('route_id', self.route_a, cf_policy_id='cf-stay',
                            sequence=20, name='stay')
        client = self._responder(self._mock(), {
            'app-a': _sync_result(app_id='app-a', policy_ids=['cf-a-new']),
            'app-b': _sync_result(app_id='app-b', policy_ids=['cf-b-new']),
        })

        with patch(MOCK_SA_CLIENT, return_value=client):
            moved.write({'route_id': self.route_b.id})

        by_app, order = self._calls_by_app(client)
        self.assertIn(
            'app-a', by_app,
            '舊錨點必須也被重推——它的 CF app 還引用著剛搬走的那顆 policy（#786）')
        self.assertIn('app-b', by_app, '陽性對照：新錨點本來就會被推')
        self.assertIn(
            'cf-moved', by_app['app-a']['existing_policy_ids'],
            'operator 唯一的刪除通道就是這份清單；被搬走那顆 id 進不去的話，'
            '舊 app 上那條 policy 永遠拆不掉')
        self.assertEqual(
            [p['name'] for p in by_app['app-a']['policies']], ['stay'],
            '舊錨點的期望狀態裡不該再有搬走的那條')
        self.assertIn(
            'moved', [p['name'] for p in by_app['app-b']['policies']],
            '新錨點的期望狀態必須含搬進來的那條')
        # 順序是正確性：operator 是 detach-before-delete，舊錨點先推才會把那顆
        # policy 從舊 app 上 detach 掉，新錨點之後送去刪才不會被判 "in use"。
        self.assertEqual(
            order, ['app-a', 'app-b'],
            '舊錨點必須排在新錨點之前（detach-before-delete）')
        self.assertEqual(stay.cf_policy_id, 'cf-a-new')
        self.assertEqual(moved.cf_policy_id, 'cf-b-new')

    def test_A2_plain_write_still_pushes_exactly_once(self):
        """陽性對照的另一半：不換錨點的一般 write 行為完全不變。

        「多推一次」不會有任何錯誤訊號（operator 的 sync 是冪等的全量推送），
        所以這一格必須斷言**次數**而不是「有沒有推」。
        """
        pol = self._policy('route_id', self.route_a, cf_policy_id='cf-plain')
        client = self._responder(self._mock(), {
            'app-a': _sync_result(app_id='app-a', policy_ids=['cf-a-new']),
        })

        with patch(MOCK_SA_CLIENT, return_value=client):
            pol.write({'name': 'renamed'})

        _, order = self._calls_by_app(client)
        self.assertEqual(order, ['app-a'],
                         '沒換錨點就只有一個錨點該被推，一次')
        self.assertEqual(self._pending(self.route_a), [],
                         '沒有東西搬家就不該落任何帳')
        self.assertEqual(self._pending(self.route_b), [])

    def test_A3_moving_back_and_forth_keeps_each_leg_self_consistent(self):
        """搬過去再搬回來：每一次都推兩邊，帳每一次都被結掉。

        這一格守的是**冪等**：反覆搬移不該讓帳本越積越多（`_settle` 沒被呼叫
        或呼叫參數錯的話，症狀就是帳留在上面而 operator 一切正常）。
        """
        pol = self._policy('route_id', self.route_a, cf_policy_id='cf-p0')
        client = self._responder(self._mock(), {
            'app-a': _sync_result(app_id='app-a', policy_ids=['cf-a1']),
            'app-b': _sync_result(app_id='app-b', policy_ids=['cf-b1']),
        })

        with patch(MOCK_SA_CLIENT, return_value=client):
            pol.write({'route_id': self.route_b.id})
        self.assertEqual(pol.cf_policy_id, 'cf-b1')

        client.sync_access_app.reset_mock()
        with patch(MOCK_SA_CLIENT, return_value=client):
            pol.write({'route_id': self.route_a.id})

        by_app, order = self._calls_by_app(client)
        self.assertEqual(order, ['app-b', 'app-a'],
                         '這一趟的舊錨點是 B，所以 B 要排在 A 前面')
        self.assertIn('cf-b1', by_app['app-b']['existing_policy_ids'])
        self.assertEqual(self._pending(self.route_a), [],
                         'operator 確認回收之後帳要被結掉')
        self.assertEqual(self._pending(self.route_b), [])

    def test_A4_batch_write_orders_by_who_lost_a_policy_not_by_mapped(self):
        """一批 policy 收攏到同一條 route 時，順序必須由「誰失去了 policy」決定。

        判別式：`self` 裡刻意放一條**已經在新錨點上**的 policy，而且讓它的
        `sequence` 比要搬的那條小 —— policy 的 `_order = 'sequence, id'` 會讓
        `mapped('route_id')` 先吐出 **B**。任何拿 `_access_sync_targets()` 的
        順序當推送順序的實作，這裡都會排成 `['app-b', 'app-a']`。

        （Odoo 的 `mapped()` docstring 逐字寫著回傳順序 arbitrary，所以這不是
        「剛好會過」的實作細節，是不能依賴的東西。）
        """
        already_on_b = self._policy('route_id', self.route_b,
                                    cf_policy_id='cf-b-old', sequence=1,
                                    name='already-on-b')
        moving = self._policy('route_id', self.route_a, cf_policy_id='cf-a4',
                              sequence=99, name='moving')
        client = self._responder(self._mock(), {
            'app-a': _sync_result(app_id='app-a', policy_ids=[]),
            'app-b': _sync_result(app_id='app-b',
                                  policy_ids=['cf-b-1', 'cf-b-2']),
        })

        batch = already_on_b + moving
        self.assertEqual(
            [p.id for p in batch.sorted(key=lambda x: (x.sequence, x.id))],
            [already_on_b.id, moving.id],
            '前提：B 上那條的 sequence 必須較小，否則這個判別式沒有鑑別力')

        with patch(MOCK_SA_CLIENT, return_value=client):
            batch.write({'route_id': self.route_b.id})

        by_app, order = self._calls_by_app(client)
        self.assertEqual(
            order, ['app-a', 'app-b'],
            '失去 policy 的 app-a 必須排在前面——順序要從語意算，'
            '不能借用 mapped() 的順序')
        self.assertIn('cf-a4', by_app['app-a']['existing_policy_ids'])
        self.assertEqual(self._pending(self.route_a), [],
                         'app-a 推成功就該結帳')
        self.assertEqual(
            already_on_b.cf_policy_id, 'cf-b-1',
            '本來就在 B 上那條不該被當成搬家（它的舊錨點就是 B）')
        self.assertEqual(moving.cf_policy_id, 'cf-b-2')
    def test_A5_new_anchor_does_not_claim_the_old_apps_cf_policy(self):
        """搬過去那顆 cf id **不得**出現在新錨點的 `existing_policy_ids`。

        那顆 reusable policy 此刻還掛在**舊** app 上。新錨點送它去刪的話，會發生
        什麼 repo 內沒有證據（operator 的 detach-before-delete 註解講的是「本 app
        自己那顆」；`delete_reusable_policy` 只有 (200,202,404) vs 其他的二分，
        沒有 in-use／409 分支）。兩種可能都不好：

        * 被 CF 拒絕 ⇒ 落進 `orphaned_policy_ids` ⇒ 被 `_settle` 記進**新**錨點
          的帳 ⇒ 每一輪重送、每一輪再記，一筆自我延續的假孤兒。
        * 真的被刪掉 ⇒ 把**舊 app 正在用的** policy 無聲拆掉。

        所以那顆 id 在搬家當下就從列上清掉，唯一的記錄點是舊錨點的帳本。
        """
        moved = self._policy('route_id', self.route_a, cf_policy_id='cf-a5')
        client = self._responder(self._mock(), {
            'app-a': _sync_result(app_id='app-a', policy_ids=[]),
            'app-b': _sync_result(app_id='app-b', policy_ids=['cf-b-new']),
        })

        with patch(MOCK_SA_CLIENT, return_value=client):
            moved.write({'route_id': self.route_b.id})

        by_app, _ = self._calls_by_app(client)
        self.assertIn('cf-a5', by_app['app-a']['existing_policy_ids'],
                      '陽性對照：舊錨點那一側必須帶得到它')
        self.assertNotIn(
            'cf-a5', by_app['app-b']['existing_policy_ids'],
            '這個結果不該出現：新錨點不是那顆 policy 的 owner，'
            '不該把它列進自己的刪除清單')

# ====================================================================== #
#  B 系列：帳要 durable —— 舊錨點推不動時那顆 id 不能消失                 #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestReanchorLedgerDurability(TransactionCase, _Fixture):
    """舊錨點被自己的 liveness gate 擋掉的那一格（#528 的同一個形狀）。"""

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 786B %s' % uuid.uuid4().hex[:6]})
        # 兩個 SA：舊錨點那邊停權（gate 會擋），新錨點那邊活著（gate 不擋）。
        self.sa_dead = self._make_sa(state='active')
        self.sa_live = self._make_sa(state='active')
        self.route_a = self._make_route(self.sa_dead, 'app-a')
        self.route_b = self._make_route(self.sa_live, 'app-b')

    def test_B1_gated_old_anchor_keeps_the_id_on_the_ledger_and_is_loud(self):
        """舊錨點推不動 ⇒ 那顆 id 留在**舊錨點**的帳上，而且要大聲。

        沒有帳的話這顆 id 只剩「還黏在搬走那一列上」這個通道，而那個通道的
        消費者是**新**錨點——新錨點送去刪會被 CF 判 "in use"（舊 app 還引用
        著），於是永遠刪不掉。帳落在舊錨點才有正確的消費者。
        """
        moved = self._policy('route_id', self.route_a, cf_policy_id='cf-b1')
        client = self._responder(self._mock(), {
            'app-b': _sync_result(app_id='app-b', policy_ids=['cf-b-new']),
        })
        self.sa_dead.write({'state': 'suspended'})

        with patch(MOCK_SA_CLIENT, return_value=client):
            with self.assertLogs(LOG_SA, level=logging.WARNING) as captured:
                moved.write({'route_id': self.route_b.id})

        _, order = self._calls_by_app(client)
        self.assertEqual(
            order, ['app-b'],
            '停權中的舊錨點被它自己的 state gate 擋掉（本 issue 不改這個行為）')
        self.assertEqual(
            self._pending(self.route_a), ['cf-b1'],
            '被擋掉時那顆 id 必須留在舊錨點的 durable 帳上（#528 的機制）')
        self.assertTrue(
            any('cf-b1' in line for line in captured.output),
            '帶著待回收 id 被 gate 擋掉必須指名是哪一顆，否則維運要自己翻 DB')

    def test_B2_next_successful_old_anchor_sync_reaps_the_booked_id(self):
        """帳的消費者：舊錨點自己的下一次成功 sync。

        B1 證明帳留下來了，這一格證明它**出得去**——只證前者的話，機制等於把
        「靜默丟失」換成「靜默堆積」，一樣是一次性機制（#528 的根因）。
        """
        moved = self._policy('route_id', self.route_a, cf_policy_id='cf-b2')
        client = self._responder(self._mock(), {
            'app-b': _sync_result(app_id='app-b', policy_ids=['cf-b-new']),
        })
        self.sa_dead.write({'state': 'suspended'})
        with patch(MOCK_SA_CLIENT, return_value=client):
            moved.write({'route_id': self.route_b.id})
        self.assertEqual(self._pending(self.route_a), ['cf-b2'])

        self.sa_dead.write({'state': 'active'})
        client.sync_access_app.reset_mock()
        self._responder(client, {
            'app-a': _sync_result(app_id='app-a', policy_ids=[]),
        })
        with patch(MOCK_SA_CLIENT, return_value=client):
            self.route_a.write({'session_duration': '8h'})

        by_app, _ = self._calls_by_app(client)
        self.assertIn(
            'cf-b2', by_app['app-a']['existing_policy_ids'],
            '帳上的 id 必須進得了 operator 唯一的刪除通道')
        self.assertEqual(self._pending(self.route_a), [],
                         '確認回收成功之後才消帳')

    def test_B3_moving_a_policy_that_never_synced_books_nothing(self):
        """沒有 cf id 的 policy 搬家 ⇒ 沒有東西要回收，帳必須是空的。

        這一格是「這個結果不該出現」那一格：無條件落帳的實作會在這裡留下一筆
        `False` / 空字串，而 `existing_policy_ids` 帶著空值送出去會讓 operator
        去刪一顆 id 是空字串的 policy。
        """
        moved = self._policy('route_id', self.route_a)
        client = self._responder(self._mock(), {
            'app-a': _sync_result(app_id='app-a', policy_ids=[]),
            'app-b': _sync_result(app_id='app-b', policy_ids=['cf-b-new']),
        })

        with patch(MOCK_SA_CLIENT, return_value=client):
            moved.write({'route_id': self.route_b.id})

        by_app, order = self._calls_by_app(client)
        self.assertEqual(order, ['app-a', 'app-b'],
                         '陽性對照：沒有 cf id 也一樣要推兩邊')
        self.assertEqual(self._pending(self.route_a), [])
        self.assertEqual(by_app['app-a']['existing_policy_ids'], [])


# ====================================================================== #
#  C 系列：#536 的 sentinel 不能被改壞                                    #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestReanchorHonoursSkipSentinel(TransactionCase, _Fixture):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 786C %s' % uuid.uuid4().hex[:6]})
        self.sa = self._make_sa()
        self.route_a = self._make_route(self.sa, 'app-a')
        self.route_b = self._make_route(self.sa, 'app-b')

    def test_C1_sentinel_still_suppresses_both_legs(self):
        """帶模組私有 sentinel 時兩個錨點都不推（migration / 批次搬列）。

        否定性斷言配陽性對照，同一支 mock、同一組 fixture：沒有對照的話
        「sentinel 有效」與「patch 點寫錯」不可區分。
        """
        moved = self._policy('route_id', self.route_a, cf_policy_id='cf-c1')
        client = self._responder(self._mock(), {
            'app-a': _sync_result(app_id='app-a', policy_ids=[]),
            'app-b': _sync_result(app_id='app-b', policy_ids=['cf-b-new']),
        })

        with patch(MOCK_SA_CLIENT, return_value=client):
            _skip_route_sync(moved).write({'route_id': self.route_b.id})

        self.assertFalse(client.sync_access_app.called,
                         'sentinel 開著時一個錨點都不該被推')
        self.assertEqual(
            self._pending(self.route_a), ['cf-c1'],
            '推被略過但帳照落——帳的壽命不能綁在「這一次 sync 有沒有跑」上，'
            '那正是 #528 的根因')

        # 陽性對照：同一支 mock、同一條 policy，不帶 sentinel 就必須打得出去。
        client.sync_access_app.reset_mock()
        with patch(MOCK_SA_CLIENT, return_value=client):
            moved.write({'route_id': self.route_a.id})
        self.assertTrue(client.sync_access_app.called,
                        '陽性對照：不帶 sentinel 必須真的打到 operator')

    def test_C2_forged_context_value_does_not_open_the_hatch(self):
        """#536：裸 context 值打不開閘門（`call_kw` 把呼叫端 context 整包換進
        env，只判 truthy 的話租戶自己就開得了）。"""
        moved = self._policy('route_id', self.route_a, cf_policy_id='cf-c2')
        client = self._responder(self._mock(), {
            'app-a': _sync_result(app_id='app-a', policy_ids=[]),
            'app-b': _sync_result(app_id='app-b', policy_ids=['cf-b-new']),
        })

        with patch(MOCK_SA_CLIENT, return_value=client):
            moved.with_context(sa_skip_route_sync=True).write(
                {'route_id': self.route_b.id})

        self.assertTrue(
            client.sync_access_app.called,
            '偽造的 context 值不得關掉 sync（#536）')


# ====================================================================== #
#  D 系列：跨錨點的 cf id 回存不得錯位                                    #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestReanchorCfIdWriteBackAlignment(TransactionCase, _Fixture):
    """`zip(policies_sorted, returned)` 在兩個錨點各跑一次，排序基準不同。"""

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 786D %s' % uuid.uuid4().hex[:6]})
        self.sa = self._make_sa()
        self.route_a = self._make_route(self.sa, 'app-a')
        self.route_b = self._make_route(self.sa, 'app-b')

    def test_D1_moved_policy_takes_the_id_of_its_new_position(self):
        """刻意讓同一條 policy 在舊錨點排**第一**、在新錨點排**最後**。

        判別式：錯位的實作會把新錨點回傳的第一顆 id 寫到搬進來那條上。只斷言
        「cf_policy_id 有值」的話兩種實作都綠。
        """
        # 舊錨點 A：moved(seq=5) 在前，stay(seq=50) 在後。
        moved = self._policy('route_id', self.route_a, cf_policy_id='cf-old-1',
                             sequence=5, name='moved')
        stay = self._policy('route_id', self.route_a, cf_policy_id='cf-old-2',
                            sequence=50, name='stay')
        # 新錨點 B：既有的 first(seq=1) 會排在 moved(seq=5) 前面。
        first = self._policy('route_id', self.route_b, cf_policy_id='cf-b-old',
                             sequence=1, name='first')

        client = self._responder(self._mock(), {
            # A 搬走 moved 之後只剩 stay 一條。
            'app-a': _sync_result(app_id='app-a', policy_ids=['cf-a-stay']),
            # B 的期望順序是 [first(1), moved(5)]。
            'app-b': _sync_result(app_id='app-b',
                                  policy_ids=['cf-b-first', 'cf-b-moved']),
        })

        with patch(MOCK_SA_CLIENT, return_value=client):
            moved.write({'route_id': self.route_b.id})

        by_app, _ = self._calls_by_app(client)
        self.assertEqual(
            [p['name'] for p in by_app['app-b']['policies']],
            ['first', 'moved'],
            'payload 的順序就是回存要對齊的順序（precedence 由 sequence 決定）')
        self.assertEqual(
            first.cf_policy_id, 'cf-b-first',
            '新錨點原有的那條必須拿到自己位置上的 id')
        self.assertEqual(
            moved.cf_policy_id, 'cf-b-moved',
            '搬進來那條在新錨點排最後，就該拿最後一顆 id——拿到 cf-b-first '
            '代表回存沿用了舊錨點的位置')
        self.assertEqual(stay.cf_policy_id, 'cf-a-stay',
                         '留在舊錨點那條要拿舊錨點這一輪回傳的 id')


# ====================================================================== #
#  E 系列：跨 model 搬家（route → cloud service）                         #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestReanchorAcrossModels(TransactionCase, _Fixture):
    """arc 讓「單次 write 同時清 A、設 B」成為跨 model 搬家的唯一合法寫法。

    `_access_sync_targets()` 回的是 **list 不是 recordset**（三個錨點是不同
    model，union 不起來），所以 pre/post 聯集必須自己用 (model, id) 去重——
    這一格是那段程式碼唯一的守線。
    """

    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 786E %s' % uuid.uuid4().hex[:6]})
        self.template = self.env[
            'woow_paas_platform.cloud_app_template'].sudo().create({
                'name': 'AC 786 tmpl',
                'slug': 'ac786-%s' % uuid.uuid4().hex[:6],
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/ac786',
                'helm_chart_version': '1.0.0',
                'workload_type': 'container',
                'post_deploy_init_type': 'none',
                'mcp_enabled': False,
            })
        self.sa = self._make_sa()
        self.route_a = self._make_route(self.sa, 'app-a')
        self.svc = self._make_service()

    def test_E1_route_to_cloud_service_resyncs_both_legs(self):
        """跨 model 搬家：舊腿（SA route）與新腿（CS）各自被自己的 client 推。

        兩腿的 client 取得點不同，所以這一格順帶證明「舊錨點被推」不是靠新腿
        那支 mock 誤打誤撞——`sa_client` 完全沒被 CS 的路徑碰過。
        """
        moved = self._policy('route_id', self.route_a, cf_policy_id='cf-e1')
        sa_client = self._responder(self._mock(), {
            'app-a': _sync_result(app_id='app-a', policy_ids=[]),
        })
        cs_client = self._mock(_sync_result(app_id='app-cs',
                                            policy_ids=['cf-cs-new']))

        with patch(MOCK_SA_CLIENT, return_value=sa_client):
            with patch(MOCK_CS_CLIENT, return_value=cs_client):
                moved.write({'route_id': False,
                             'cloud_service_id': self.svc.id})

        self.assertTrue(
            sa_client.sync_access_app.called,
            '舊腿（SA route）必須被重推——跨 model 時 pre/post 聯集不能靠 '
            'recordset 的 `|`，那正是這一格守的東西')
        sa_payload = sa_client.sync_access_app.call_args.args[0]
        self.assertIn('cf-e1', sa_payload['existing_policy_ids'],
                      '被搬走那顆 id 必須進得了舊腿的刪除通道')
        self.assertEqual([p['name'] for p in sa_payload['policies']], [],
                         '舊腿的期望狀態已經沒有這條 policy 了')
        self.assertTrue(cs_client.sync_access_app.called,
                        '陽性對照：新腿本來就會被推')
        self.assertEqual(moved.cf_policy_id, 'cf-cs-new')


# ====================================================================== #
#  F 系列：所有錨點都授權過才准開始推（fail-closed 不得被改鬆）           #
# ====================================================================== #

def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號。"""
    login = '%s_%s' % (name, uuid.uuid4().hex[:8])
    return env['res.users'].sudo().create({
        'name': '%s 786' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestReanchorAuthorizesEveryTargetFirst(TransactionCase, _Fixture):
    """錨點欄位在 ORM 層沒有歸屬檢查——arc 只管 arity，不管這條 route／CS 是誰的。

    Odoo 只在 `write()` 開頭做一次 `check_access('write')`，對 Many2one 的
    **目標**不做任何檢查 ⇒ 裸 ORM／RPC 寫得進別人 workspace 的錨點 id。

    修這張單之前那條路徑是 fail-closed 的：唯一那一次推送就是新錨點，讀不到
    它就 AccessError、savepoint 整段回捲、**Cloudflare 一次都沒被碰**。改成推
    兩個之後，若不先授權就開推，自己那個舊錨點會先把一輪 delete-then-recreate
    打上 CF（回捲不掉），租戶手上就多了一根穩定製造 CF 孤兒的槓桿。

    🔴 **判準是 operator 一次都沒被呼叫**，不是「有沒有 raise」——raise 之前
    已經打過 CF 的實作也會通過「有 raise」那種寫法。

    ⚠ 錨點型別在這裡有鑑別力差異，別互抄：換到別人的 **route** 時，
    `security_access_id` 是 stored related，它的重算在 `super().write()` 內就
    先炸了（Odoo 自己 fail-closed，與本守衛無關）；`cloud_service_id` 沒有這種
    related 欄位，所以那條路徑才真正走到推送迴圈——本 class 兩格都留著，並在
    各自的 docstring 註明誰在守。
    """

    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        Workspace = self.env['woow_paas_platform.workspace']
        self.alice = _new_group_user(self.env, 'alice786')
        self.bob = _new_group_user(self.env, 'bob786')
        self.assertFalse(
            self.alice.has_group('base.group_system'),
            '前提：租戶只能有 base.group_user，否則 ir.rule 整個不生效（假綠）')
        self.w1 = Workspace.with_user(self.alice).create({'name': 'W1 786F'})
        self.w2 = Workspace.with_user(self.bob).create({'name': 'W2 786F'})
        self.template = self.env[
            'woow_paas_platform.cloud_app_template'].sudo().create({
                'name': 'AC 786F tmpl',
                'slug': 'ac786f-%s' % uuid.uuid4().hex[:6],
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/ac786f',
                'helm_chart_version': '1.0.0',
                'workload_type': 'container',
                'post_deploy_init_type': 'none',
                'mcp_enabled': False,
            })

        self.workspace = self.w1
        self.sa1 = self._make_sa(workspace=self.w1)
        self.route_a = self._make_route(self.sa1, 'app-a')
        self.route_a2 = self._make_route(self.sa1, 'app-a2')
        self.sa2 = self._make_sa(workspace=self.w2)
        self.route_b = self._make_route(self.sa2, 'app-b')
        self.workspace = self.w2
        self.foreign_svc = self._make_service()
        self.workspace = self.w1

    def _clients(self):
        sa_client = self._responder(self._mock(), {
            'app-a': _sync_result(app_id='app-a', policy_ids=[]),
            'app-a2': _sync_result(app_id='app-a2', policy_ids=['cf-a2-new']),
            'app-b': _sync_result(app_id='app-b', policy_ids=['cf-b-new']),
        })
        cs_client = self._mock(_sync_result(app_id='app-cs',
                                            policy_ids=['cf-cs-new']))
        return sa_client, cs_client

    def test_F1_foreign_cloud_service_anchor_touches_cloudflare_zero_times(
            self):
        """把 policy 搬到**別人 workspace 的 cloud service** ⇒ CF 零變更。

        🔴 判別式的兩個刻意選擇，改動時別破壞：

        1. **錨點選 cloud service 不選 route。** 換到別人的 route 時，
           `security_access_id`（`related='route_id.security_access_id',
           store=True`）的重算會讓 Odoo 自己先炸；`cloud_service_id` 沒有這種
           related 欄位，推送迴圈才真的會被走到。
        2. **搬的那條 policy 刻意沒有 `cf_policy_id`。** 有 cf id 的話，
           `_apply_anchor_move()` 的清列 write 會先撞到 ir.rule 而擋下來——那個
           保護是隱性的副作用，不是本守衛。沒有 cf id 的列繞過它，於是唯一站在
           租戶與「一輪打到 CF 的 delete-then-recreate」之間的，就只剩推送前的
           授權檢查。

        舊錨點 route_a 上另外掛一條**有** cf id 的 policy：沒有它的話，
        route_a 那一趟就算真的推出去也沒什麼可刪可建，這格的損害不成立。
        """
        self._policy('route_id', self.route_a, cf_policy_id='cf-f1-sibling',
                     name='sibling')
        pol = self._policy('route_id', self.route_a, name='no-cf-id')
        self.assertFalse(pol.cf_policy_id,
                         '前提：這條刻意沒有 cf id，否則判別式會被清列 write 吃掉')
        sa_client, cs_client = self._clients()

        with patch(MOCK_SA_CLIENT, return_value=sa_client):
            with patch(MOCK_CS_CLIENT, return_value=cs_client):
                with self.assertRaises(AccessError):
                    pol.with_user(self.alice).write({
                        'route_id': False,
                        'cloud_service_id': self.foreign_svc.id})

        self.assertFalse(
            sa_client.sync_access_app.called,
            '不可回捲的 CF 變更必須排在授權檢查之後——舊錨點那一趟也不准發生')
        self.assertFalse(cs_client.sync_access_app.called)
        self.assertEqual(self._pending(self.route_a), [], '整段回捲，帳不該留下')

        # 陽性對照：同一組 mock、同一個 alice、同一條 policy，搬到自己 workspace
        # 內的另一條 route 必須兩個錨點都推得出去。
        with patch(MOCK_SA_CLIENT, return_value=sa_client):
            pol.with_user(self.alice).write({'route_id': self.route_a2.id})
        _, order = self._calls_by_app(sa_client)
        self.assertEqual(
            order, ['app-a', 'app-a2'],
            '陽性對照：自己 workspace 內的搬家必須兩個錨點都推得出去')

    def test_F2_foreign_route_anchor_is_still_fail_closed(self):
        """換到別人的 route 同樣零 CF 變更（回歸網，不是本守衛的功勞）。

        這一格今天是被 Odoo 自己擋下來的：`security_access_id` 是
        `related='route_id.security_access_id', store=True`，重算在
        `super().write()` 內就要讀別人的 route ⇒ AccessError 早於推送迴圈。
        留著是因為**那個保護是隱性的**（沒有任何一行程式碼寫著「這裡在擋跨
        workspace」），把 related 拿掉或改成非 stored 就會靜默失去它。
        """
        pol = self._policy('route_id', self.route_a, cf_policy_id='cf-f2')
        sa_client, _ = self._clients()

        with patch(MOCK_SA_CLIENT, return_value=sa_client):
            with self.assertRaises(AccessError):
                pol.with_user(self.alice).write({'route_id': self.route_b.id})

        self.assertFalse(sa_client.sync_access_app.called)
        self.assertEqual(pol.cf_policy_id, 'cf-f2')
