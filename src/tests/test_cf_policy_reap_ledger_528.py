"""#528：停權中刪 access policy ⇒ CF 同步被 state gate 靜默略過 ⇒ CF policy 永久孤兒。

## 守什麼

被刪掉的 CF reusable policy，其 id 原本**只在 unlink 當下**經
`sa_unlinked_cf_policy_ids` context 注入那一次 re-sync。三腿的
`_sync_access_to_operator()` 各有一組 liveness gate，gate 一擋，operator 一次都
不會被呼叫，而 DB 列已刪、context 用完即丟 ⇒ 那顆 CF policy 從此**沒有任何記錄
指向它**，之後任何 re-sync 都不會回收（2026-08-02 在 stg 實際漏了兩顆，其中一條
是 allow everyone）。

🔴 **那條 context 通道本身已於 #783 整條移除**（它是 `call_kw` 直達的裸值，而它
餵的是 operator 的**帳號級**刪除清單）。帳本因此不只是「更耐久的第二條腿」，而是
**唯一**的一條——所以本檔的 B/C/D 系列同時是 #528 的守線與 #783 的回歸證據：它們
走真實 unlink，拿掉 context 之後一格都沒有變。

本檔守五件事：

1. **帳要 durable**：unlink 當下把 doomed id 寫進 DB（`cf_access_policy_reap`），
   不再依賴「當次 sync 一定會跑」。
2. **之後任何一次成功的 sync 都會併進 reap**：`existing_policy_ids` 是 operator
   唯一的刪除通道，帳上的 id 必須進得去。
3. **確認回收成功才消帳**：判準是 operator 的 `orphaned_policy_ids`；sync 失敗
   或被 gate 擋掉時帳原封不動。
4. **被 gate 擋掉且手上有待回收 id ⇒ 大聲**（方向 3）。反過來，帳上是空的時候
   必須**安靜**——停權下 sync 被擋是常態，無條件 warning 等於 log 洪水。
5. **呼叫端塞進 context 的 id 不得進 `existing_policy_ids`**（#783，B8/C3/D4）。
   ⚠ 那三條的觸發器刻意用**普通的 write** 而不是 unlink：偽造面不在 unlink 那一
   行，而在「任何一次會觸發 `_sync_access_to_operator()` 的操作都沿用呼叫端的
   context」。

## 判別式紀律（這檔最容易寫成假綠的兩格）

* **否定性斷言（`sync_access_app` 沒被呼叫）一律配同一支 mock 的陽性對照。**
  patch 點寫錯時，「不該打 operator」會**無聲通過**（根本沒有東西被呼叫），而
  「應該打」那半會炸。所以兩半放在同一個測試方法、同一支 mock 上。
  ⚠ 三腿的 patch 點**不同**（見下方三個常數），不要互抄。
* **`assertLogs` 的 level 必須傳數值**（`logging.WARNING`），不可傳字串。Odoo 的
  `addLevelName(25, "INFO")` 覆寫了名稱→數值表 ⇒ 字串 `'WARNING'` 會解析錯，
  `assertLogs` 恆紅、`assertNoLogs` 恆綠（repo-lint `assertlogs-numeric-level`）。

## 三腿的可達性不同（PR 有完整證據，這裡記結論）

| 腿 | 帶著 doomed id 撞得到 gate 嗎 |
| --- | --- |
| SA route | **會**——`state not in (active, active_no_route, error)`，停權期間刪 policy 就是 #528 的原案 |
| SH route | **會**——`sh.state not in (active, error)`，同型 |
| CS | 現行碼**撞不到**（三道 gate 的前提與「列上還有 cf id」互斥），但 `state=='deleting'` 那格的不變式住在 gate 外面、沒有任何守線 ⇒ 一併套用同一套機制，不留形狀不一致 |
"""
import logging
import uuid
from unittest.mock import MagicMock, patch
from .publication_receipt_fixture import (
    access_receipt,
    configure_receipt,
    routes_receipt,
)

from odoo.tests import TransactionCase, tagged

# #787：`cs_skip_access_sync` 只認模組私有 sentinel，不能傳 `True`
# （呼叫端 context 會被 `call_kw` 整包換進 env ⇒ truthy 值是租戶可偽造的）。
from ..models.cloud_service import (
    _cs_skip_access_sync, _internal_cs_access_identity, _internal_provision,
)
from ..models.security_access import (
    _force_access_sync, _internal_identity as _internal_sa,
    _skip_route_sync,
)
from ..models.smart_home_route import (
    _internal_sm_route_identity,
    _sm_forced_access_sync,
    _sm_skip_access_sync,
)
from ..services.naming import make_service_subdomain
from .smart_home_fixture import sh_model

# #798：`cf_policy_id` 有欄位級寫入守衛（create 壓值／write 拒絕，對 su 也生效）。
# fixture 擺狀態必須經開閘 helper，否則會撞到自己要測的守衛。
from ..models.security_access import (
    _internal_policy_cf_write as _internal_policy_cf,
)

#: 🔴 三腿三個 patch 點，判準是「該 model 怎麼取得 client」不是「client 定義在哪」。
MOCK_SA_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
                  'get_paas_operator_client')
MOCK_CS_CLIENT = ('odoo.addons.woow_paas_platform.services.paas_operator.'
                  'get_paas_operator_client')
MOCK_SH_CLIENT = ('odoo.addons.woow_paas_platform.models.smart_home.'
                  'get_paas_operator_client')

LOG_SA = 'odoo.addons.woow_paas_platform.models.security_access'
LOG_CS = 'odoo.addons.woow_paas_platform.models.cloud_service'
LOG_SH = 'odoo.addons.woow_paas_platform.models.smart_home_route'

_REAP = 'woow_paas_platform.cf_access_policy_reap'
_POLICY = 'woow_paas_platform.security_access_access_policy'


def _sync_result(app_id='app-528', policy_ids=None, orphaned=None):
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


class _ReapMixin:
    """三腿共用的帳本查詢 helper。"""

    def _pending(self, anchor):
        return self.env[_REAP]._pending_ids(anchor._name, anchor.id)

    def _mock(self, result=None):
        client = MagicMock()
        if result is None:
            client.sync_access_app.side_effect = access_receipt
        else:
            client.sync_access_app.return_value = result
        client.sync_tunnel_routes.side_effect = routes_receipt
        client.configure_tunnel.side_effect = configure_receipt
        client.park_release_route.return_value = {'status': 'parked'}
        client.unpark_release_route.return_value = {'status': 'unparked'}
        return client


# ====================================================================== #
#  A 系列：帳本 model 本身                                                #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestReapLedgerModel(TransactionCase, _ReapMixin):
    """帳本的四個動作各自的契約。"""

    def setUp(self):
        super().setUp()
        self.Reap = self.env[_REAP]
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 528A %s' % uuid.uuid4().hex[:6]})
        self.anchor = self.env['woow_paas_platform.security_access'].sudo(
        ).create({'name': 'SA 528A', 'workspace_id': self.workspace.id})

    def test_A1_record_dedupes_and_drops_empties(self):
        self.Reap._record(self.anchor, ['cf-a', 'cf-b', False, '', 'cf-a'])
        self.Reap._record(self.anchor, ['cf-b', 'cf-c'])
        self.assertEqual(
            self._pending(self.anchor), ['cf-a', 'cf-b', 'cf-c'],
            '同一錨點同一 id 只記一次，空值不記，順序是 FIFO')

    def test_A2_ledger_outlives_the_anchor_row(self):
        """錨點列消失之後帳還在——這正是本表不用 Many2one+cascade 的理由。

        「錨點被刪掉、帳還沒回收」是最不能靜默丟掉的那一格；cascade 會讓它與
        「已回收」在 DB 上長得一模一樣。
        """
        anchor_model, anchor_id = self.anchor._name, self.anchor.id
        self.Reap._record(self.anchor, ['cf-outlives'])
        self.anchor.sudo().unlink()
        self.assertEqual(self.Reap._pending_ids(anchor_model, anchor_id),
                         ['cf-outlives'])

    def test_A3_settle_clears_only_what_the_operator_confirmed(self):
        """送出去了 ≠ 刪掉了。判準是 operator 的 `orphaned_policy_ids`。"""
        self.Reap._record(self.anchor, ['cf-gone', 'cf-stuck'])
        # #791 覆核 F9：`trusted_ids` 是必填的「這一輪送出去的 id 裡來自 DB 列
        # 的那個子集」。這裡兩顆都是列值。
        self.Reap._settle(self.anchor._name, self.anchor.id,
                          submitted_ids=['cf-gone', 'cf-stuck'],
                          orphaned_ids=['cf-stuck'],
                          trusted_ids=['cf-gone', 'cf-stuck'])
        self.assertEqual(
            self._pending(self.anchor), ['cf-stuck'],
            'operator 回報刪不掉的必須留在帳上等下一輪，不能一併消掉')

    def test_A4_settle_books_newly_orphaned_ids(self):
        """存活列的舊 id 刪不掉時也要進帳——今天那格是 log 一行然後永遠忘記。"""
        self.Reap._settle(self.anchor._name, self.anchor.id,
                          submitted_ids=['cf-old'], orphaned_ids=['cf-old'],
                          trusted_ids=['cf-old'])
        self.assertEqual(self._pending(self.anchor), ['cf-old'])

    def test_A5_pending_for_reads_only_the_ledger_783(self):
        """`_pending_for` 只認帳本，呼叫端 context 一律不採信（#783）。

        這裡原本驗的是「帳本 ∪ 本次 context」。`sa_unlinked_cf_policy_ids` 是一把
        `/web/dataset/call_kw` 直達的裸 context key，而它餵的是 operator 的
        **帳號級**刪除清單（`delete_reusable_policy` 打
        `/accounts/{id}/access/policies/{pid}`，沒有 app 也沒有租戶邊界）⇒ #783
        把那條腿整個拿掉。

        拿掉不減功能：生產路徑上 context 本來就是冗餘的——`unlink()` 在同一個
        savepoint 內**先**把同一批 id 落帳（`_record_ids`）才推 sync，而且落帳對
        每一個錨點無條件執行、context 只注入給 `exists()` 的那個子集 ⇒ 帳本是
        context 的**嚴格超集**。

        陽性對照與否定性斷言放同一個方法：帳上那顆必須照回，否則「context 被忽略」
        與「整個回空集」不可區分。
        """
        self.Reap._record(self.anchor, ['cf-booked'])
        ids = self.Reap._pending_for(self.anchor.with_context(
            sa_unlinked_cf_policy_ids=['cf-ctx', 'cf-booked']))
        self.assertEqual(ids, ['cf-booked'],
                         '帳上的照回（陽性對照）、context 塞的一律不回（#783）')


# ====================================================================== #
#  B 系列：SA route（#528 的原案）                                        #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestSaRouteReap(TransactionCase, _ReapMixin):

    def setUp(self):
        super().setUp()
        self.Reap = self.env[_REAP]
        self.Policy = self.env[_POLICY]
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 528B %s' % uuid.uuid4().hex[:6]})
        self.client = self._mock()
        with patch(MOCK_SA_CLIENT, return_value=self.client):
            self.sa = self.env['woow_paas_platform.security_access'].sudo(
            ).create({'name': 'SA 528B', 'workspace_id': self.workspace.id})
            _internal_sa(self.sa).write(
                {'state': 'active', 'tunnel_id': 'tun-528'})
            self.route = self.env[
                'woow_paas_platform.security_access_route'].sudo().create({
                    'security_access_id': self.sa.id,
                    'subdomain_prefix': self.sa.canonical_name,
                    'is_protected': True})
        self.client.sync_access_app.reset_mock()

    def _policy(self, cf_policy_id):
        pol = _skip_route_sync(self.Policy.sudo()).create({
            'route_id': self.route.id,
            'name': 'pol-%s' % uuid.uuid4().hex[:6], 'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})]})
        _internal_policy_cf(pol).cf_policy_id = cf_policy_id
        # 🔴 create() 回的 recordset 帶著呼叫時的 context；不關掉的話之後每一次
        # 動作都還套著 skip，「機制沒接好」與「fixture 把 sync 關掉了」會長得
        # 一模一樣。
        return pol.with_context(sa_skip_route_sync=False)

    def test_B1_suspended_unlink_books_the_doomed_id(self):
        """#528 主線：停權中刪 policy ⇒ operator 沒被打，但帳必須留下來。

        陽性對照與否定性斷言在同一支 mock 上：state 翻回 active 之後同樣的動作
        必須打得出去，否則「gate 擋住了」與「mock 根本沒接上」不可區分。
        """
        pol = self._policy('cf-doomed-b1')
        self.sa.write({'state': 'suspended'})

        with patch(MOCK_SA_CLIENT, return_value=self.client):
            pol.unlink()

        self.assertFalse(
            self.client.sync_access_app.called,
            '停權中的 state gate 本來就會擋下 sync（本 issue 不改這個行為）')
        self.assertIn(
            'cf-doomed-b1', self._pending(self.route),
            'sync 被 gate 擋掉時 doomed id 必須留在 durable 帳上——這正是 #528')

        # 陽性對照：同一支 mock、同一條 route，active 時必須打得出去。
        self.sa.write({'state': 'active'})
        pol2 = self._policy('cf-doomed-b1b')
        with patch(MOCK_SA_CLIENT, return_value=self.client):
            pol2.unlink()
        self.assertTrue(self.client.sync_access_app.called,
                        '陽性對照：active 狀態必須真的打到 operator')

    def test_B2_later_successful_sync_reaps_the_booked_id(self):
        """帳上的 id 要進得了 `existing_policy_ids`，成功之後帳要被消掉。"""
        pol = self._policy('cf-doomed-b2')
        self.sa.write({'state': 'suspended'})
        with patch(MOCK_SA_CLIENT, return_value=self.client):
            pol.unlink()
        self.assertIn('cf-doomed-b2', self._pending(self.route))

        self.sa.write({'state': 'active'})
        with patch(MOCK_SA_CLIENT, return_value=self.client):
            self.route.write({'session_duration': '8h'})

        payload = self.client.sync_access_app.call_args.args[0]
        self.assertIn(
            'cf-doomed-b2', payload['existing_policy_ids'],
            'operator 唯一的刪除通道就是這份清單，帳上的 id 必須進得去')
        self.assertEqual(self._pending(self.route), [],
                         '確認回收成功之後才消帳')

    def test_B3_forced_resume_sync_reaps_the_booked_id(self):
        """resume 的 forced sync（`sa_force_access_sync`）也要帶帳出去。

        SA 的 resume 收斂發生在 state 還沒翻回 active 之前，走的是 force 旁路
        ——那條路徑漏掉的話，停權期間累積的帳要等到下一次租戶自己動設定才收。
        """
        self.Reap._record(self.route, ['cf-doomed-b3'])
        self.sa.write({'state': 'suspended'})

        with patch(MOCK_SA_CLIENT, return_value=self.client):
            _force_access_sync(self.route)._sync_access_to_operator()

        payload = self.client.sync_access_app.call_args.args[0]
        self.assertIn('cf-doomed-b3', payload['existing_policy_ids'])
        self.assertEqual(self._pending(self.route), [])

    def test_B4_failed_sync_keeps_the_books(self):
        """operator 丟例外 ⇒ 帳一格都不能少（下一次成功的 sync 才是消帳時機）。"""
        self.Reap._record(self.route, ['cf-doomed-b4'])
        self.client.sync_access_app.side_effect = RuntimeError('cf down')

        with patch(MOCK_SA_CLIENT, return_value=self.client):
            with self.assertRaises(RuntimeError):
                self.route.write({'session_duration': '8h'})

        self.assertEqual(self._pending(self.route), ['cf-doomed-b4'])

    def test_B5_gate_skip_with_books_is_loud_and_otherwise_quiet(self):
        """方向 3：帶著待回收 id 被 gate 擋掉 ⇒ warning 且訊息要指名是哪顆 id。

        另一半同樣重要：帳上空的時候必須安靜。停權下 sync 被擋在正常流程裡到處
        都是，無條件出聲會變成 log 洪水，而洪水等於沒有訊號。
        """
        self.sa.write({'state': 'suspended'})

        # 安靜的那一半（帳上空的）——先驗，避免被下一段的寫入污染。
        with patch(MOCK_SA_CLIENT, return_value=self.client):
            with self.assertNoLogs(LOG_SA, level=logging.WARNING):
                self.route.write({'session_duration': '1h'})

        self.Reap._record(self.route, ['cf-doomed-b5'])
        with patch(MOCK_SA_CLIENT, return_value=self.client):
            with self.assertLogs(LOG_SA, level=logging.WARNING) as captured:
                self.route.write({'session_duration': '8h'})

        self.assertTrue(
            any('cf-doomed-b5' in line for line in captured.output),
            '訊息必須指名是哪一顆 id——只說「有幾顆」的話維運還是得自己翻 DB')

    def test_B6_orphaned_ids_stay_on_the_books(self):
        """operator 回報「送去刪但刪不掉」⇒ 留在帳上等下一輪，不是 log 完就忘。"""
        pol = self._policy('cf-stuck-b6')
        stuck = self._mock(_sync_result(app_id='app-live',
                                        orphaned=['cf-stuck-b6']))
        with patch(MOCK_SA_CLIENT, return_value=stuck):
            pol.unlink()
        self.assertTrue(stuck.sync_access_app.called,
                        '陽性對照：這一輪確實打到了 operator')
        self.assertEqual(self._pending(self.route), ['cf-stuck-b6'])

        # 迴路要閉合：下一次成功的 sync 必須把它**再送一次**；這次 operator 說
        # 刪掉了 ⇒ 帳才清空。少了前半句就只是「帳會自己消失」，那與漏掉沒兩樣。
        pol2 = self._policy('cf-clean-b6')
        with patch(MOCK_SA_CLIENT, return_value=self.client):
            pol2.unlink()
        payload = self.client.sync_access_app.call_args.args[0]
        self.assertIn('cf-stuck-b6', payload['existing_policy_ids'],
                      '上一輪刪不掉的那顆必須在下一次 sync 被重送')
        self.assertEqual(self._pending(self.route), [],
                         '這一輪 operator 沒回報孤兒 ⇒ 帳清空')

    def test_B7_active_unlink_behaviour_is_unchanged(self):
        """陽性對照組：active 狀態刪 policy 的原路徑行為一格都不能變。"""
        pol = self._policy('cf-live-b7')
        with patch(MOCK_SA_CLIENT, return_value=self.client):
            pol.unlink()
        payload = self.client.sync_access_app.call_args.args[0]
        self.assertIn('cf-live-b7', payload['existing_policy_ids'],
                      '被刪那一列的 cf id 必須照樣送去刪——#783 拿掉 context '
                      '通道之後這顆改由帳本供給，行為一格都不能變')
        self.assertEqual(self._pending(self.route), [],
                         '成功回收 ⇒ 不留殘帳')


    def test_B8_forged_context_never_reaches_the_payload_783(self):
        """#783：呼叫端塞進 context 的 cf id 不得進 `existing_policy_ids`。

        `/web/dataset/call_kw` 把呼叫端的 context **整包**換進 env，而
        `existing_policy_ids` 是 operator 唯一的刪除通道、`delete_reusable_policy`
        打的是**帳號級** `/accounts/{id}/access/policies/{pid}`（沒有 app 也沒有
        租戶邊界）⇒ 這條腿等於讓租戶指定平台去刪任何一顆它知道 id 的 CF policy。

        🔴 觸發器刻意用一次**普通的 write**，不是 unlink。偽造面不在 unlink 那一
        行——`_pending_for()` 讀的是「錨點當下的 env.context」，而**任何**會觸發
        `_sync_access_to_operator()` 的操作都沿用呼叫端的 context
        （`_access_sync_targets()` 用 `.mapped()`，不重置 env）。用 unlink 當觸發器
        會讓這條測試看起來只守住一條路徑，而那正是本題最容易寫錯的地方。

        四格判定，缺一不可（只留否定性那格的話，payload 整個壞掉也會通過）：
        """
        self._policy('cf-real-b8')
        self.Reap._record(self.route, ['cf-booked-b8'])

        with patch(MOCK_SA_CLIENT, return_value=self.client):
            self.route.with_context(
                sa_unlinked_cf_policy_ids=['cf-forged-b8']
            ).write({'session_duration': '8h'})

        self.assertTrue(self.client.sync_access_app.called,
                        '陽性對照：這一輪確實打到了 operator')
        payload = self.client.sync_access_app.call_args.args[0]
        self.assertIn('cf-real-b8', payload['existing_policy_ids'],
                      '陽性對照：列上真實存在的 cf id 照樣要送出去')
        self.assertIn('cf-booked-b8', payload['existing_policy_ids'],
                      '陽性對照：帳本上的舊帳照樣要送出去')
        self.assertNotIn(
            'cf-forged-b8', payload['existing_policy_ids'],
            '呼叫端 context 塞的 id 不得進 operator 的帳號級刪除清單（#783）')


# ====================================================================== #
#  C 系列：CloudService                                                   #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestCsReap(TransactionCase, _ReapMixin):

    def setUp(self):
        super().setUp()
        self.Reap = self.env[_REAP]
        self.Policy = self.env[_POLICY]
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 528C %s' % uuid.uuid4().hex[:6]})
        template = self.env['woow_paas_platform.cloud_app_template'].sudo(
        ).create({
            'name': 'CS 528 tmpl',
            'slug': 'cs528-%s' % uuid.uuid4().hex[:6],
            'helm_repo_url': '', 'helm_chart_name': 'oci://example.com/cs528',
            'helm_chart_version': '1.0.0', 'workload_type': 'container',
            'post_deploy_init_type': 'none', 'mcp_enabled': False})
        self.svc = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': 'svc-528', 'workspace_id': self.workspace.id,
                'template_id': template.id, 'state': 'running'})
        self.svc.subdomain = make_service_subdomain(
            self.workspace.ws_hash, self.svc.reference_id, self.svc.name)
        _cs_skip_access_sync(self.svc).write(
            {'is_protected': True})
        _internal_cs_access_identity(self.svc).access_app_id = 'app-live'
        self.client = self._mock()

    def _policy(self, cf_policy_id):
        pol = _skip_route_sync(self.Policy.sudo()).create({
            'cloud_service_id': self.svc.id,
            'name': 'pol-%s' % uuid.uuid4().hex[:6], 'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})]})
        _internal_policy_cf(pol).cf_policy_id = cf_policy_id
        return pol.with_context(sa_skip_route_sync=False)

    def test_C1_gated_unlink_books_and_later_sync_reaps(self):
        """CS 的三道 gate 拿掉 subdomain 那格當代表（現行碼撞不到，見檔頭表）。

        機制與 SA 腿同構：gate 擋掉 ⇒ 帳留著；之後任何一次成功的 sync 把它帶
        出去並消帳。
        """
        pol = self._policy('cf-doomed-c1')
        _cs_skip_access_sync(self.svc).write({'is_protected': False})
        self.svc.subdomain = False
        _cs_skip_access_sync(self.svc).write({'is_protected': True})

        with patch(MOCK_CS_CLIENT, return_value=self.client):
            pol.unlink()
        self.assertFalse(self.client.sync_access_app.called)
        self.assertIn('cf-doomed-c1', self._pending(self.svc))

        # 陽性對照 ＋ 回收：subdomain 補回來之後同一支 mock 必須打得出去。
        _cs_skip_access_sync(self.svc).write({'is_protected': False})
        self.svc.subdomain = make_service_subdomain(
            self.workspace.ws_hash, self.svc.reference_id, self.svc.name)
        _cs_skip_access_sync(self.svc).write({'is_protected': True})
        with patch(MOCK_CS_CLIENT, return_value=self.client):
            self.svc.write({'session_duration': '8h'})
        self.assertTrue(self.client.sync_access_app.called,
                        '陽性對照：有 subdomain 時必須真的打到 operator')
        payload = self.client.sync_access_app.call_args.args[0]
        self.assertIn('cf-doomed-c1', payload['existing_policy_ids'])
        self.assertEqual(self._pending(self.svc), [])

    def test_C2_gate_skip_with_books_is_loud(self):
        self.Reap._record(self.svc, ['cf-doomed-c2'])
        _cs_skip_access_sync(self.svc).write({'is_protected': False})
        self.svc.subdomain = False
        _cs_skip_access_sync(self.svc).write({'is_protected': True})
        with patch(MOCK_CS_CLIENT, return_value=self.client):
            with self.assertLogs(LOG_CS, level=logging.WARNING) as captured:
                self.svc.write({'session_duration': '8h'})
        self.assertTrue(
            any('cf-doomed-c2' in line for line in captured.output))


    def test_C3_forged_context_never_reaches_the_payload_783(self):
        """#783：呼叫端塞進 context 的 cf id 不得進 `existing_policy_ids`。

        `/web/dataset/call_kw` 把呼叫端的 context **整包**換進 env，而
        `existing_policy_ids` 是 operator 唯一的刪除通道、`delete_reusable_policy`
        打的是**帳號級** `/accounts/{id}/access/policies/{pid}`（沒有 app 也沒有
        租戶邊界）⇒ 這條腿等於讓租戶指定平台去刪任何一顆它知道 id 的 CF policy。

        🔴 觸發器刻意用一次**普通的 write**，不是 unlink。偽造面不在 unlink 那一
        行——`_pending_for()` 讀的是「錨點當下的 env.context」，而**任何**會觸發
        `_sync_access_to_operator()` 的操作都沿用呼叫端的 context
        （`_access_sync_targets()` 用 `.mapped()`，不重置 env）。用 unlink 當觸發器
        會讓這條測試看起來只守住一條路徑，而那正是本題最容易寫錯的地方。

        四格判定，缺一不可（只留否定性那格的話，payload 整個壞掉也會通過）：
        """
        self._policy('cf-real-c3')
        self.Reap._record(self.svc, ['cf-booked-c3'])

        with patch(MOCK_CS_CLIENT, return_value=self.client):
            self.svc.with_context(
                sa_unlinked_cf_policy_ids=['cf-forged-c3']
            ).write({'session_duration': '8h'})

        self.assertTrue(self.client.sync_access_app.called,
                        '陽性對照：這一輪確實打到了 operator')
        payload = self.client.sync_access_app.call_args.args[0]
        self.assertIn('cf-real-c3', payload['existing_policy_ids'],
                      '陽性對照：列上真實存在的 cf id 照樣要送出去')
        self.assertIn('cf-booked-c3', payload['existing_policy_ids'],
                      '陽性對照：帳本上的舊帳照樣要送出去')
        self.assertNotIn(
            'cf-forged-c3', payload['existing_policy_ids'],
            '呼叫端 context 塞的 id 不得進 operator 的帳號級刪除清單（#783）')


# ====================================================================== #
#  D 系列：SmartHomeRoute                                                 #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestShRouteReap(TransactionCase, _ReapMixin):

    def setUp(self):
        super().setUp()
        self.Reap = self.env[_REAP]
        self.Policy = self.env[_POLICY]
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 528D %s' % uuid.uuid4().hex[:6]})
        sh = sh_model(self.env).create({
            'name': 'sh-528-%s' % uuid.uuid4().hex[:6],
            'workspace_id': self.workspace.id, 'state': 'active',
            'tunnel_id': 'tun-%s' % uuid.uuid4().hex[:8]})
        sh.write({'subdomain': sh._generate_subdomain()})
        # 閘洗掉，免得 fixture 把受測守衛一起關掉。
        self.sh = sh.with_env(self.env)
        self.route = _sm_skip_access_sync(self.env[
            'woow_paas_platform.smart_home_route']).create(
                {'smart_home_id': self.sh.id, 'is_primary': True})
        self.route = self.route.with_context(sm_skip_access_sync=False)
        _sm_skip_access_sync(self.route).write(
            {'is_protected': True})
        _internal_sm_route_identity(self.route).access_app_id = 'app-live'
        self.client = self._mock()

    def _policy(self, cf_policy_id):
        pol = _skip_route_sync(self.Policy.sudo()).create({
            'smart_home_route_id': self.route.id,
            'name': 'pol-%s' % uuid.uuid4().hex[:6], 'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})]})
        _internal_policy_cf(pol).cf_policy_id = cf_policy_id
        return pol.with_context(sa_skip_route_sync=False)

    def test_D1_suspended_unlink_books_the_doomed_id(self):
        pol = self._policy('cf-doomed-d1')
        self.sh.write({'state': 'suspended'})

        with patch(MOCK_SH_CLIENT, return_value=self.client):
            pol.unlink()

        self.assertFalse(self.client.sync_access_app.called)
        self.assertIn('cf-doomed-d1', self._pending(self.route))

        # 陽性對照：同一支 mock，active 必須打得出去。
        self.sh.write({'state': 'active'})
        pol2 = self._policy('cf-doomed-d1b')
        with patch(MOCK_SH_CLIENT, return_value=self.client):
            pol2.unlink()
        self.assertTrue(self.client.sync_access_app.called)

    def test_D2_forced_resume_sync_reaps_the_booked_id(self):
        """SH 的 resume 走 `sm_force_access_sync`（與 SA 的 key 不同名）。"""
        self.Reap._record(self.route, ['cf-doomed-d2'])
        self.sh.write({'state': 'suspended'})

        with patch(MOCK_SH_CLIENT, return_value=self.client):
            _sm_forced_access_sync(self.route)._sync_access_to_operator()

        payload = self.client.sync_access_app.call_args.args[0]
        self.assertIn('cf-doomed-d2', payload['existing_policy_ids'])
        self.assertEqual(self._pending(self.route), [])

    def test_D3_gate_skip_with_books_is_loud(self):
        self.Reap._record(self.route, ['cf-doomed-d3'])
        self.sh.write({'state': 'suspended'})
        with patch(MOCK_SH_CLIENT, return_value=self.client):
            with self.assertLogs(LOG_SH, level=logging.WARNING) as captured:
                self.route.write({'session_duration': '8h'})
        self.assertTrue(
            any('cf-doomed-d3' in line for line in captured.output))

    def test_D4_forged_context_never_reaches_the_payload_783(self):
        """#783：呼叫端塞進 context 的 cf id 不得進 `existing_policy_ids`。

        `/web/dataset/call_kw` 把呼叫端的 context **整包**換進 env，而
        `existing_policy_ids` 是 operator 唯一的刪除通道、`delete_reusable_policy`
        打的是**帳號級** `/accounts/{id}/access/policies/{pid}`（沒有 app 也沒有
        租戶邊界）⇒ 這條腿等於讓租戶指定平台去刪任何一顆它知道 id 的 CF policy。

        🔴 觸發器刻意用一次**普通的 write**，不是 unlink。偽造面不在 unlink 那一
        行——`_pending_for()` 讀的是「錨點當下的 env.context」，而**任何**會觸發
        `_sync_access_to_operator()` 的操作都沿用呼叫端的 context
        （`_access_sync_targets()` 用 `.mapped()`，不重置 env）。用 unlink 當觸發器
        會讓這條測試看起來只守住一條路徑，而那正是本題最容易寫錯的地方。

        四格判定，缺一不可（只留否定性那格的話，payload 整個壞掉也會通過）：
        """
        self._policy('cf-real-d4')
        self.Reap._record(self.route, ['cf-booked-d4'])

        with patch(MOCK_SH_CLIENT, return_value=self.client):
            self.route.with_context(
                sa_unlinked_cf_policy_ids=['cf-forged-d4']
            ).write({'session_duration': '8h'})

        self.assertTrue(self.client.sync_access_app.called,
                        '陽性對照：這一輪確實打到了 operator')
        payload = self.client.sync_access_app.call_args.args[0]
        self.assertIn('cf-real-d4', payload['existing_policy_ids'],
                      '陽性對照：列上真實存在的 cf id 照樣要送出去')
        self.assertIn('cf-booked-d4', payload['existing_policy_ids'],
                      '陽性對照：帳本上的舊帳照樣要送出去')
        self.assertNotIn(
            'cf-forged-d4', payload['existing_policy_ids'],
            '呼叫端 context 塞的 id 不得進 operator 的帳號級刪除清單（#783）')
