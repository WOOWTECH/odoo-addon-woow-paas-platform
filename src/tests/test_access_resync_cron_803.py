# -*- coding: utf-8 -*-
"""#803：`access_protection_lost` 打開之後，沒有任何週期性收斂會把它推回去。

## 守什麼

#794 給三腿加了 `access_protection_lost`：「CF Access app 已經被拆掉、而列還在」
——此時 `is_protected` 只是**意圖**，Cloudflare 上沒有任何東西在強制登入，而
hostname 照常由 tunnel 服務（對外可達且**未受保護**）。

那個旗標的消費路徑逐字寫在 `security_access.py::_access_mark_protection_lost`
的 docstring 裡：

> 平台**沒有週期性 Access re-sync**，只有 provision / resume / 直接寫
> `is_protected`／`login_*`／`session_duration` 三種事件會推。

三種都是**租戶或維運自己動手**才會發生的事件。沒人動 ⇒ 窗口不會自己關。本檔守的
就是那條缺席的收斂路徑：一條 cron 掃三張表上的旗標列、逐列重推
`_sync_access_to_operator()`。

## 三張表不是四張

issue #803 的四腿表格裡，SmartHome 那腿的錨點就是它底下的 route 列 ⇒ 掃
`smart_home_route` 即涵蓋。實際掃：`security_access_route`／`smart_home_route`／
`cloud_service`。

## 判別式紀律（本檔最容易寫成假綠的四格）

* **否定性斷言（`sync_access_app` 沒被呼叫）一律配同一支 mock 的陽性對照。**
  patch 點寫錯時「不該打 operator」會**無聲通過**（根本沒有東西被呼叫）。
  ⚠ 三腿 patch 點不同，判準是「**那個 model** 怎麼取得 client」不是「client
  定義在哪」：SA 走 `sa._get_operator_client()`（`models.security_access` 的
  module-level import）、SH 走 `sh._get_operator_client()`（`models.smart_home`）、
  CS 走 `self._get_operator_client()`，而 CS 那支是**函式內 late import**
  ⇒ patch 點在 `services.paas_operator`。
* **`assertLogs` 的 level 必須傳數值**（`logging.INFO` / `logging.WARNING`）。
  Odoo 的 `addLevelName(25, "INFO")` 覆寫了名稱→數值表 ⇒ 字串會解析錯，
  `assertLogs` 恆紅、`assertNoLogs` 恆綠（repo-lint `assertlogs-numeric-level`）。
* **「例外之後 DB 長怎樣」不得用 `assertRaises`**：它的 savepoint 會把區塊內的
  寫入整個回捲 ⇒ 那條測試**結構上永遠不可能綠**（#822 實踩）。本檔的失敗路徑
  斷言走 cron 自己的吞納點，不經 `assertRaises`。
* **佇列守衛的 blocker 必須是「永遠推不動」的列**（被 liveness gate 擋），不是
  「這一輪剛好失敗」的列——後者下一輪就消失了，證不到堵死。

## 為什麼是游標式掃描（#791 F1 的同一個坑）

`search(domain, limit=N)` 會**自己把自己堵死**：被 gate 永久擋住的旗標列
（SA/SH 停權中、CS 已進 `deleting`）永遠不消失，`_order` 讓它們佔住最小的 id
⇒ 累積到 N 筆之後每一輪撈到的都是同一批推不動的列，真正該推的一次都輪不到。
而那個狀態與「今天沒事做」在輸出上完全一樣。A5／B5／C5 各自釘住一腿，D1 用
#791 原尺度（520 筆）再釘一次。
"""
import logging
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged

from ..models.cloud_service import (
    _cs_skip_access_sync, _internal_cs_access_identity, _internal_provision,
)
from ..models.security_access import (
    _internal_identity as _internal_sa, _skip_route_sync,
)
from ..models.smart_home_route import (
    _internal_sm_route_identity, _sm_skip_access_sync,
)
from ..services.naming import make_service_subdomain
from .smart_home_fixture import sh_model

#: 🔴 三腿三個 patch 點，判準是「該 model 怎麼取得 client」不是「client 定義在哪」。
MOCK_SA_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
                  'get_paas_operator_client')
MOCK_CS_CLIENT = ('odoo.addons.woow_paas_platform.services.paas_operator.'
                  'get_paas_operator_client')
MOCK_SH_CLIENT = ('odoo.addons.woow_paas_platform.models.smart_home.'
                  'get_paas_operator_client')

#: 受測 cron 自己那支 logger（彙總那一行落在這裡）。
LOG_CRON = 'odoo.addons.woow_paas_platform.models.access_resync_cron'

_CRON_MODEL = 'woow_paas_platform.access_resync_cron'
_SA_ROUTE = 'woow_paas_platform.security_access_route'
_SH_ROUTE = 'woow_paas_platform.smart_home_route'
_CS = 'woow_paas_platform.cloud_service'


def _sync_result(app_id='app-803', policy_ids=None, orphaned=None):
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


class _ResyncMixin:
    """三腿共用件。"""

    def _mock(self, result=None):
        client = MagicMock()
        client.sync_access_app.return_value = (
            _sync_result() if result is None else result)
        client.sync_tunnel_routes.return_value = {'routes': []}
        return client

    def _run_cron(self, **kw):
        """在本腿的 patch 點底下跑一輪 cron。"""
        with patch(self.MOCK_CLIENT, return_value=self.client):
            return self.env[_CRON_MODEL]._cron_resync_access_protection(**kw)

    def _lost(self, record):
        """重讀旗標（不靠 ORM cache 的舊值）。"""
        record.invalidate_recordset(['access_protection_lost'])
        return record.access_protection_lost


# ====================================================================== #
#  A 系列：Security Access route 腿                                       #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestSaRouteAccessResync(TransactionCase, _ResyncMixin):
    """`woow_paas_platform.security_access_route`。

    ⚠ 旗標在 **route** 層不在 SA 層——domain 寫到 `security_access` 上會掃到一張
    根本沒有這個欄位的表。
    """

    MOCK_CLIENT = MOCK_SA_CLIENT

    def setUp(self):
        super().setUp()
        self.client = self._mock()
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 803A %s' % uuid.uuid4().hex[:6]})
        self.sa = self._sa('active')
        self.route = self._route(self.sa, lost=True)
        self.client.sync_access_app.reset_mock()

    def _sa(self, state):
        sa = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'SA 803A %s' % uuid.uuid4().hex[:6],
             'workspace_id': self.workspace.id})
        _internal_sa(sa).write(
            {'state': state, 'tunnel_id': 'tun-%s' % uuid.uuid4().hex[:8]})
        return sa

    def _route(self, sa, lost):
        route = _skip_route_sync(self.env[_SA_ROUTE].sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'r%s' % uuid.uuid4().hex[:10],
            'is_protected': True})
        route = route.with_context(sa_skip_route_sync=False)
        route.access_protection_lost = lost
        return route

    def test_A1_flagged_route_gets_resync_pushed(self):
        """旗標列存在 ⇒ cron 這一輪必須把它重推出去（證明這張表掃得到）。"""
        self._run_cron()
        self.assertTrue(
            self.client.sync_access_app.called,
            'security_access_route 上的旗標列必須被 cron 掃到並重推')
        payload = self.client.sync_access_app.call_args.args[0]
        self.assertEqual(
            payload['primary_hostname'], self.route.hostname,
            '推的必須是這一條 route 自己的 hostname，不是別條的')

    def test_A2_successful_resync_clears_the_flag(self):
        """重推成功 ⇒ 旗標被清掉。

        清除是 `_sync_access_to_operator()` 的既有行為（#794），本條驗的是
        「cron 真的走到那條路」——cron 自己不得另外寫清除碼。
        """
        self.assertTrue(self._lost(self.route), '前提：這一列是帶旗標的')
        self._run_cron()
        self.assertFalse(
            self._lost(self.route),
            '推成功 ⇒ 保護已生效 ⇒ 旗標必須由 sync 自己清掉')

    def test_A3_failed_resync_keeps_the_flag(self):
        """重推失敗 ⇒ 旗標留著（下一輪再來），而且失敗必須出聲。

        ⛔ 不得吞掉失敗：安靜地跳過與「推成功了」在輸出上不可區分，那正是本
        issue 要修的那一格的翻版。
        """
        self.client.sync_access_app.side_effect = Exception('operator down')
        with self.assertLogs(LOG_CRON, level=logging.WARNING) as captured:
            self._run_cron()
        self.assertTrue(
            self._lost(self.route),
            '推失敗 ⇒ 保護仍未生效 ⇒ 旗標必須留著讓 UI 說得出口')
        self.assertTrue(
            any('operator down' in m or str(self.route.id) in m
                for m in captured.output),
            '失敗要指名是哪一列／什麼錯，不能只留一行沒有指涉的 warning')

    def test_A4_unflagged_routes_are_never_touched(self):
        """陽性對照：沒有旗標的列一次都不能被推（domain 不得誤撈）。

        否定性斷言與陽性對照放在**同一支 mock、同一個方法**：patch 點寫錯時
        「不該打 operator」會無聲通過，而陽性對照那半會炸。
        """
        self.route.access_protection_lost = False
        self._run_cron()
        self.assertFalse(
            self.client.sync_access_app.called,
            '旗標是 False 的列不該進 cron 的 domain')

        # 陽性對照：同一支 mock、同一條 route，旗標打開就必須推得出去。
        self.route.access_protection_lost = True
        self._run_cron()
        self.assertTrue(
            self.client.sync_access_app.called,
            '陽性對照：旗標打開時必須真的打到 operator（證明 mock 有接上）')

    def test_A5_blocked_rows_cannot_starve_the_queue(self):
        """永遠推不動的列不得把該推的那筆擠出窗口（#791 F1 的同型）。

        blocker ＝ 停權 SA 底下的旗標列：gate 永久擋住 ⇒ 旗標永遠不會被清掉
        ⇒ 它們永遠佔住最小的 id。`search(domain, limit=3)` 這種形狀在這裡只會
        一直撈到同一批 blocker。
        """
        # 🔴 建立順序就是本測試的全部：blocker 必須佔住**較小**的 id，待推那筆
        # 排在它們後面。setUp 建的那筆 id 最小，留著旗標會讓它先被推掉、
        # 於是「有沒有被堵死」根本沒被問到 ⇒ 先把它移出 domain。
        self.route.access_protection_lost = False
        dead_sa = self._sa('suspended')
        blockers = [self._route(dead_sa, lost=True) for _ in range(5)]
        target = self._route(self.sa, lost=True)
        self.assertTrue(
            all(b.id < target.id for b in blockers),
            '前提：blocker 必須排在待推那筆的前面，否則證不到堵死')

        self._run_cron(scan_batch=3)

        self.assertTrue(
            self.client.sync_access_app.called,
            '前面卡著 5 筆推不動的列，可推的那筆仍必須在同一輪被送出')
        self.assertFalse(self._lost(target))
        for b in blockers:
            self.assertTrue(
                self._lost(b),
                'gate 擋掉的列旗標必須留著——保護確實沒生效，那是實話')

    def test_A6_unprotecting_after_loss_clears_flag_without_push(self):
        """#794 補洞：保護失效（app 已被拆、旗標打開）之後租戶直接把保護關掉
        ⇒ 列不再宣稱保護、CF 上也沒有 app，兩邊一致 ⇒ 不外呼、旗標就地清掉。

        兩條路都要清：租戶寫 `is_protected=False`（write hook 觸發 sync），以及
        已經處在「公開＋沒有 app＋旗標」的列被本 cron 掃到。對照：仍宣稱保護、
        只是被停權擋住的旗標列，旗標必須留著（與 A5 同一句話）。
        """
        # 路徑 1：租戶把保護關掉
        self.route._access_mark_protection_lost()
        self.assertTrue(self._lost(self.route), '前提：標記後旗標打開')
        self.assertFalse(self.route.access_app_id, '前提：標記清掉了 app id')
        self.client.sync_access_app.reset_mock()
        with patch(self.MOCK_CLIENT, return_value=self.client):
            self.route.write({'is_protected': False})
        self.client.sync_access_app.assert_not_called()
        self.assertFalse(
            self._lost(self.route),
            '改成公開 ⇒ 列不再宣稱保護 ⇒ 「保護未生效」必須清掉')

        # 路徑 2：cron 掃到已經是「公開＋沒有 app」的旗標列
        stale = self._route(self.sa, lost=True)
        _skip_route_sync(stale).write({'is_protected': False})
        gated = self._route(self._sa('suspended'), lost=True)
        self.client.sync_access_app.reset_mock()
        self._run_cron()
        self.client.sync_access_app.assert_not_called()
        self.assertFalse(
            self._lost(stale), 'cron 掃到公開且沒有 app 的旗標列也必須清掉')
        self.assertTrue(
            self._lost(gated),
            '仍宣稱保護、只是被停權擋住的列旗標必須留著——保護確實沒生效')


# ====================================================================== #
#  B 系列：Smart Home route 腿                                            #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestShRouteAccessResync(TransactionCase, _ResyncMixin):
    """`woow_paas_platform.smart_home_route`。"""

    MOCK_CLIENT = MOCK_SH_CLIENT

    def setUp(self):
        super().setUp()
        self.client = self._mock()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 803B %s' % uuid.uuid4().hex[:6]})
        self.sh, self.route = self._sh_with_route('active', lost=True)

    def _sh_with_route(self, state, lost):
        sh = sh_model(self.env).create({
            'name': 'sh-803-%s' % uuid.uuid4().hex[:6],
            'workspace_id': self.workspace.id, 'state': state,
            'tunnel_id': 'tun-%s' % uuid.uuid4().hex[:8]})
        sh.write({'subdomain': sh._generate_subdomain()})
        sh = sh.with_env(self.env)
        route = _sm_skip_access_sync(self.env[_SH_ROUTE]).create(
            {'smart_home_id': sh.id, 'is_primary': True})
        route = route.with_context(sm_skip_access_sync=False)
        _sm_skip_access_sync(route).write({'is_protected': True})
        _internal_sm_route_identity(route).access_app_id = 'app-live'
        route.access_protection_lost = lost
        return sh, route

    def test_B1_flagged_route_gets_resync_pushed(self):
        self._run_cron()
        self.assertTrue(
            self.client.sync_access_app.called,
            'smart_home_route 上的旗標列必須被 cron 掃到並重推')
        payload = self.client.sync_access_app.call_args.args[0]
        self.assertEqual(payload['primary_hostname'], self.route.hostname)

    def test_B2_successful_resync_clears_the_flag(self):
        self.assertTrue(self._lost(self.route), '前提：這一列是帶旗標的')
        self._run_cron()
        self.assertFalse(self._lost(self.route))

    def test_B3_failed_resync_keeps_the_flag(self):
        self.client.sync_access_app.side_effect = Exception('operator down')
        with self.assertLogs(LOG_CRON, level=logging.WARNING) as captured:
            self._run_cron()
        self.assertTrue(self._lost(self.route))
        self.assertTrue(
            any('operator down' in m or str(self.route.id) in m
                for m in captured.output))

    def test_B4_unflagged_routes_are_never_touched(self):
        self.route.access_protection_lost = False
        self._run_cron()
        self.assertFalse(self.client.sync_access_app.called)

        self.route.access_protection_lost = True
        self._run_cron()
        self.assertTrue(
            self.client.sync_access_app.called,
            '陽性對照：旗標打開時必須真的打到 operator')

    def test_B5_blocked_rows_cannot_starve_the_queue(self):
        # 建立順序見 A5 的註解：blocker 要佔住較小的 id。
        self.route.access_protection_lost = False
        blockers = [self._sh_with_route('suspended', lost=True)[1]
                    for _ in range(5)]
        target = self._sh_with_route('active', lost=True)[1]
        self.assertTrue(
            all(b.id < target.id for b in blockers),
            '前提：blocker 必須排在待推那筆的前面，否則證不到堵死')

        self._run_cron(scan_batch=3)

        self.assertTrue(self.client.sync_access_app.called)
        self.assertFalse(self._lost(target))
        for b in blockers:
            self.assertTrue(self._lost(b))

    def test_B6_unprotecting_after_loss_clears_flag_without_push(self):
        """同 A6（SH 腿）。對照列＝停權 SH 底下仍宣稱保護的旗標列。"""
        self.route._access_mark_protection_lost()
        self.assertTrue(self._lost(self.route), '前提：標記後旗標打開')
        self.assertFalse(self.route.access_app_id, '前提：標記清掉了 app id')
        self.client.sync_access_app.reset_mock()
        with patch(self.MOCK_CLIENT, return_value=self.client):
            self.route.write({'is_protected': False})
        self.client.sync_access_app.assert_not_called()
        self.assertFalse(self._lost(self.route))

        stale = self._sh_with_route('active', lost=True)[1]
        _internal_sm_route_identity(stale).access_app_id = False
        _sm_skip_access_sync(stale).write({'is_protected': False})
        gated = self._sh_with_route('suspended', lost=True)[1]
        self.client.sync_access_app.reset_mock()
        self._run_cron()
        self.client.sync_access_app.assert_not_called()
        self.assertFalse(self._lost(stale))
        self.assertTrue(self._lost(gated))


# ====================================================================== #
#  C 系列：Cloud Service 腿                                               #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestCsAccessResync(TransactionCase, _ResyncMixin):
    """`woow_paas_platform.cloud_service`。"""

    MOCK_CLIENT = MOCK_CS_CLIENT

    def setUp(self):
        super().setUp()
        self.client = self._mock()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 803C %s' % uuid.uuid4().hex[:6]})
        self.template = self.env[
            'woow_paas_platform.cloud_app_template'].sudo().create({
                'name': 'CS 803 tmpl',
                'slug': 'cs803-%s' % uuid.uuid4().hex[:6],
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/cs803',
                'helm_chart_version': '1.0.0', 'workload_type': 'container',
                'post_deploy_init_type': 'none', 'mcp_enabled': False})
        self.svc = self._service(with_subdomain=True, lost=True)

    def _service(self, with_subdomain, lost):
        svc = _internal_provision(self.env[_CS].sudo()).create({
            'name': 'svc-803-%s' % uuid.uuid4().hex[:6],
            'workspace_id': self.workspace.id,
            'template_id': self.template.id, 'state': 'running'})
        if with_subdomain:
            svc.subdomain = make_service_subdomain(
                self.workspace.ws_hash, svc.reference_id, svc.name)
        _cs_skip_access_sync(svc).write({'is_protected': True})
        _internal_cs_access_identity(svc).access_app_id = 'app-live'
        svc.access_protection_lost = lost
        return svc

    def test_C1_flagged_service_gets_resync_pushed(self):
        self._run_cron()
        self.assertTrue(
            self.client.sync_access_app.called,
            'cloud_service 上的旗標列必須被 cron 掃到並重推')
        payload = self.client.sync_access_app.call_args.args[0]
        self.assertIn(self.svc.subdomain, payload['primary_hostname'])

    def test_C2_successful_resync_clears_the_flag(self):
        self.assertTrue(self._lost(self.svc), '前提：這一列是帶旗標的')
        self._run_cron()
        self.assertFalse(self._lost(self.svc))

    def test_C3_failed_resync_keeps_the_flag(self):
        self.client.sync_access_app.side_effect = Exception('operator down')
        with self.assertLogs(LOG_CRON, level=logging.WARNING) as captured:
            self._run_cron()
        self.assertTrue(self._lost(self.svc))
        self.assertTrue(
            any('operator down' in m or str(self.svc.id) in m
                for m in captured.output))

    def test_C4_unflagged_services_are_never_touched(self):
        self.svc.access_protection_lost = False
        self._run_cron()
        self.assertFalse(self.client.sync_access_app.called)

        self.svc.access_protection_lost = True
        self._run_cron()
        self.assertTrue(
            self.client.sync_access_app.called,
            '陽性對照：旗標打開時必須真的打到 operator')

    def test_C5_blocked_rows_cannot_starve_the_queue(self):
        """CS 的 blocker ＝ 沒有 subdomain 的旗標列（gate 永久擋住）。"""
        # 建立順序見 A5 的註解：blocker 要佔住較小的 id。
        self.svc.access_protection_lost = False
        blockers = [self._service(with_subdomain=False, lost=True)
                    for _ in range(5)]
        target = self._service(with_subdomain=True, lost=True)
        self.assertTrue(
            all(b.id < target.id for b in blockers),
            '前提：blocker 必須排在待推那筆的前面，否則證不到堵死')

        self._run_cron(scan_batch=3)

        self.assertTrue(self.client.sync_access_app.called)
        self.assertFalse(self._lost(target))
        for b in blockers:
            self.assertTrue(self._lost(b))

    def test_C6_unprotecting_after_loss_clears_flag_without_push(self):
        """同 A6（CS 腿）。對照列＝沒有 subdomain、仍宣稱保護的旗標列。"""
        self.svc._access_mark_protection_lost()
        self.assertTrue(self._lost(self.svc), '前提：標記後旗標打開')
        self.assertFalse(self.svc.access_app_id, '前提：標記清掉了 app id')
        self.client.sync_access_app.reset_mock()
        with patch(self.MOCK_CLIENT, return_value=self.client):
            self.svc.write({'is_protected': False})
        self.client.sync_access_app.assert_not_called()
        self.assertFalse(self._lost(self.svc))

        stale = self._service(with_subdomain=True, lost=True)
        _internal_cs_access_identity(stale).access_app_id = False
        _cs_skip_access_sync(stale).write({'is_protected': False})
        gated = self._service(with_subdomain=False, lost=True)
        self.client.sync_access_app.reset_mock()
        self._run_cron()
        self.client.sync_access_app.assert_not_called()
        self.assertFalse(self._lost(stale))
        self.assertTrue(self._lost(gated))


# ====================================================================== #
#  D 系列：掃描形狀、每輪出聲、與 #854 身分守衛的相容                      #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestResyncCronScanShape(TransactionCase, _ResyncMixin):
    """跨腿的形狀守線。腿別用 SA route（建列最便宜，不需要每筆一個父物件）。"""

    MOCK_CLIENT = MOCK_SA_CLIENT

    def setUp(self):
        super().setUp()
        self.client = self._mock()
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 803D %s' % uuid.uuid4().hex[:6]})
        self.live_sa = self._sa('active')
        self.dead_sa = self._sa('suspended')

    def _sa(self, state):
        sa = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'SA 803D %s' % uuid.uuid4().hex[:6],
             'workspace_id': self.workspace.id})
        _internal_sa(sa).write(
            {'state': state, 'tunnel_id': 'tun-%s' % uuid.uuid4().hex[:8]})
        return sa

    def _routes(self, sa, count, lost=True):
        rows = _skip_route_sync(self.env[_SA_ROUTE].sudo()).create([{
            'security_access_id': sa.id,
            'subdomain_prefix': 'r%s' % uuid.uuid4().hex[:10],
            'is_protected': True} for _ in range(count)])
        rows = rows.with_context(sa_skip_route_sync=False)
        rows.access_protection_lost = lost
        return rows

    def _summary(self, captured):
        """彙總行：一輪只有一行，內容自帶計數。"""
        lines = [m for m in captured.output if 'scanned=' in m]
        self.assertEqual(
            len(lines), 1,
            '一輪一行彙總——多行等於逐列出聲（log 洪水），零行等於靜默')
        return lines[0]

    def _summary_record(self, captured):
        """彙總行的 `LogRecord`——判準與 `_summary()` 同一條，只是多帶 level。

        ⚠ 鎖的是**這一行**的 level，不是「整輪沒有 WARNING」：同一輪裡
        `truncated`（scan cap 打滿）與逐列失敗各自有獨立的 WARNING，拿整份
        `captured.output` 當判準會把那兩條也綁進來，改別的東西就會誤紅。
        """
        line = self._summary(captured)
        records = [r for r in captured.records if 'scanned=' in r.getMessage()]
        self.assertEqual(
            len(records), 1, '彙總行與 `_summary()` 的判準必須撈到同一筆')
        self.assertTrue(
            line.endswith(records[0].getMessage()),
            '撈到的 record 必須就是 `_summary()` 那一行')
        return records[0]

    def _assert_scan_cap_untouched(self, captured):
        """前提：fixture 遠低於 `max_scan`，scan cap 那條 WARNING 沒被誤觸。

        不驗這一格的話，「彙總行是 WARNING」有兩個都成立的解釋（升級條件命中／
        scan cap 打滿），而後者是別條 log ⇒ 兩格 level 測試會失去鑑別力。
        """
        self.assertEqual(
            [m for m in captured.output if 'scan cap' in m], [],
            'fixture 誤觸 scan cap ⇒ 這一輪的 WARNING 不是彙總行升級來的')

    def test_D1_full_scale_blockers_cannot_starve_the_queue(self):
        """#791 F1 原尺度：520 筆推不動的列擋在前面。

        舊窗口是 500。這裡照原尺度塞 520 筆永久 gate 擋住的旗標列，再放一筆
        真正推得動的在它們後面——`search(domain, limit=500)` 這種形狀撈到的
        永遠是同一批 blocker，而且回傳值與「今天沒事做」完全一樣（0、零 log）。
        """
        blockers = self._routes(self.dead_sa, 520)
        self.assertEqual(len(blockers), 520)
        target = self._routes(self.live_sa, 1)
        self.assertTrue(max(blockers.ids) < target.id)

        pushed = self._run_cron(scan_batch=7)

        self.assertTrue(
            self.client.sync_access_app.called,
            '前面卡著 520 筆收不掉的列，可推的那筆仍必須在同一輪被送出（F1）')
        self.assertFalse(self._lost(target))
        self.assertEqual(
            pushed, 1, '回傳值＝這一輪真的推動幾列，blocker 不算')
        self.assertEqual(
            len(blockers.exists()), 520,
            'blocker 只是推不動，不該被 cron 刪掉或改掉別的東西')

    def test_D2_round_summary_names_all_three_counts(self):
        """每輪彙總必須自帶三個計數：掃了幾列／推動幾列／被 gate 擋幾列。

        🔴 彙總而不是逐列，也不是完全不出聲：`cf_access_policy_reap.py` 的
        `_warn_if_deferred` docstring 逐字說「無條件 warning 會變成 log 洪水，
        而洪水等於沒有訊號」；而 #791 F1 說完全不出聲又與「今天沒事做」不可
        區分。彙總是同時解掉兩者的形狀。
        """
        self._routes(self.live_sa, 1)
        with self.assertLogs(LOG_CRON, level=logging.INFO) as captured:
            self._run_cron()
        line = self._summary(captured)
        for token in ('scanned=', 'pushed=', 'gated='):
            self.assertIn(
                token, line,
                '彙總行少了 %r ⇒ 這一輪到底做了什麼說不出口' % token)

    def test_D3_round_summary_counts_match_the_fixture(self):
        """計數的**值**要對，不是欄位在就算數。

        陰陽混合：2 筆推得動、3 筆被 gate 擋 ⇒ scanned=5 pushed=2 gated=3。
        只驗「欄位存在」的話，把三個計數都寫死成 0 也會通過。
        """
        self._routes(self.live_sa, 2)
        self._routes(self.dead_sa, 3)
        with self.assertLogs(LOG_CRON, level=logging.INFO) as captured:
            self._run_cron()
        line = self._summary(captured)
        self.assertIn('scanned=5', line)
        self.assertIn('pushed=2', line)
        self.assertIn('gated=3', line)

    def test_D4_quiet_round_still_emits_one_line(self):
        """零命中那一輪也要出聲一行，且那一行同樣自帶三個計數。

        「掃過了、沒事做」與「cron 根本沒跑」必須在 log 上分得開——這正是 F1
        要的鑑別力。而「掃了 0 列」與「掃了 500 列全被擋」是兩件事，同一句話
        要能分開，所以空手那輪不得寫成 'nothing to do'。
        """
        with self.assertLogs(LOG_CRON, level=logging.INFO) as captured:
            self._run_cron()
        line = self._summary(captured)
        self.assertIn('scanned=0', line)
        self.assertIn('pushed=0', line)
        self.assertIn('gated=0', line)

        # 對照：同一支 logger、同一行格式，全被擋的那輪讀起來必須不一樣。
        self._routes(self.dead_sa, 3)
        with self.assertLogs(LOG_CRON, level=logging.INFO) as captured2:
            self._run_cron()
        blocked_line = self._summary(captured2)
        self.assertIn('scanned=3', blocked_line)
        self.assertIn('gated=3', blocked_line)
        self.assertNotEqual(
            line.split('scanned=')[1], blocked_line.split('scanned=')[1],
            '空手那輪與全被擋那輪不得長成同一句話')

    def test_D5_resync_writeback_passes_the_identity_guard(self):
        """cron 觸發的成功 sync 會回存 `access_app_id`——那是身分錨點（#854）。

        `_ROUTE_IDENTITY_ANCHOR_FIELDS` 含 `access_app_id`，欄位級守衛對 su 也
        生效 ⇒ 回存必須走 module 自己的 internal-identity sentinel，而 cron 的
        執行身分要過得了那道守衛。這一格「以後有人動守衛就會靜默壞掉」，所以
        釘成常駐測試而不是一次性驗證。

        注入式紅燈（覆核用）：把 `models.security_access._internal_identity`
        patch 成 passthrough（`lambda r: r`）⇒ 本條必須紅，其餘條不受影響。
        """
        target = self._routes(self.live_sa, 1)
        pushed = self._run_cron()

        self.assertEqual(pushed, 1, '前提：這一輪真的推出去了')
        target.invalidate_recordset(['access_app_id'])
        self.assertEqual(
            target.access_app_id, 'app-803',
            'operator 回的 app id 必須真的落在列上——守衛擋掉回存時，'
            '旗標雖然被清掉，列上卻沒有重試錨點')
        self.assertFalse(self._lost(target))

    def test_D6_gated_only_round_stays_info(self):
        """`gated` 是預期狀態 ⇒ 只有 gated 的那一輪，彙總行必須留在 INFO。

        `gated` 記的是「被 liveness gate 擋掉、這輪推不動」的列（SA/SH 停權中、
        CS 已進 `deleting`）——那是**常態**，本方法的 docstring 逐字說過旗標留著
        是實話。長期停權的租戶會讓這一行每 15 分鐘 warn 一次＝96 行／日，而
        `cf_access_policy_reap._warn_if_deferred` 的 docstring 也逐字寫過那個下場：
        「無條件 warning 會變成 log 洪水，而洪水等於沒有訊號」。

        三個計數照印（D2／D3／D4 守著），這裡只釘 level。
        """
        self._routes(self.dead_sa, 3)
        with self.assertLogs(LOG_CRON, level=logging.INFO) as captured:
            self._run_cron()

        self._assert_scan_cap_untouched(captured)
        record = self._summary_record(captured)
        self.assertIn('gated=3', record.getMessage(), '前提：這一輪真的有 gated')
        self.assertIn('failed=0', record.getMessage(), '前提：這一輪沒有 failed')
        self.assertEqual(
            record.levelno, logging.INFO,
            '被 gate 擋掉是預期行為，升到 WARNING 等於每 15 分鐘拉一次假警報')

    def test_D7_failed_round_escalates_to_warning(self):
        """`failed`（推了但失敗）才是異常 ⇒ 彙總行必須升到 WARNING。

        與 D6 成對：只拿掉 `gated` 那半條件、把 `failed` 也一起拿掉的話，異常那
        一輪就跟著靜音了——這一格擋的正是那種過度修正。
        """
        self._routes(self.live_sa, 1)
        self.client.sync_access_app.side_effect = Exception('operator down')
        with self.assertLogs(LOG_CRON, level=logging.INFO) as captured:
            self._run_cron()

        self._assert_scan_cap_untouched(captured)
        record = self._summary_record(captured)
        self.assertIn('failed=1', record.getMessage(), '前提：這一輪真的有 failed')
        self.assertEqual(
            record.levelno, logging.WARNING,
            '推了而且失敗是異常，彙總行靜音等於沒有人會發現')
