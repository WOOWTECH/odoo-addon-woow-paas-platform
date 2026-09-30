# -*- coding: utf-8 -*-
"""#552 層 4：沒釘 chart 版本就不准部署（fail-closed）＋ poll 補第五條回寫路徑。

## 缺陷

`helm_chart_version` 留空 ⇒ 整條管線不帶 `--version` ⇒ helm 解析成 registry
**當下的 latest**。`--atomic` 只在**失敗**時回滾，**成功跳版永久生效**；而
`reset_values=True` 又讓 values 以新版 chart 的預設重新渲染。租戶按一個按鈕，
服務底層 chart 換成未知版本，平台沒有記錄、沒有告警。

## 位置比實作重要

| 路徑 | 守在哪 | 為什麼 |
| --- | --- | --- |
| 首裝 | `_reject_reason_for_new_instance` | 可購買性裁決點 ⇒ **購買前擋**，且是兩個首裝入口的**共同咽喉** |
| 首裝（存量 pending） | `_execute_deploy` | 閘門上線前就躺在 `pending` 的列，cron 照樣會撿 |
| redeploy | `action_redeploy` | 同步方法，`UserError` 回得到呼叫端 |
| reset | operator `releases.py` 分流之前 | 兩條載體分支各有 helm upgrade 呼叫點（守在 operator 側，見該處測試） |

🔴 **刻意不守 reconfigure**：`_update_service` 走背景執行緒，失敗回不到前端 ⇒
在那裡 fail-closed 會讓全部商品的「儲存設定／改網域／改 addon」**靜默壞掉**。
那條路由 #552 層 2 的 fallback 負責（已合併），本檔的
`test_reconfigure_is_deliberately_not_gated` 把這個取捨釘成契約。

## 為什麼首裝閘門放在可購買性裁決點而不是各個 controller

首裝有**兩個入口**：marketplace 的 `_create_service`，與 HA PaaS 的
`controllers/ha_paas.py`（直接 `.sudo().create()`，不經 `_create_service`）。
守單一 controller 會被第二個繞過。兩者的共同咽喉是 `CloudService.create()` →
`_enforce_supply_gate` → `_reject_reason_for_new_instance`，而同一個判準又餵給
`_availability()` ⇒ 一處改動同時得到「購買前擋」與「兩個入口都涵蓋」。
"""
import json
import logging
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.models.cloud_app_template import SupplyGateError
from .test_poll_mirror_release_facts_627 import _stale_sync_at
from ..models.cloud_service import _internal_provision

_CLIENT_PATH = (
    'odoo.addons.woow_paas_platform.services.paas_operator'
    '.get_paas_operator_client'
)
_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '552C Workspace',
        })

    def _template(self, slug, chart_version, **extra):
        vals = {
            'name': '552C %s' % slug,
            'slug': slug,
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/%s' % slug,
            'helm_chart_version': chart_version,
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
        }
        vals.update(extra)
        return self.Template.create(vals)

    def _service(self, template, state='running', **extra):
        """建服務。

        ⚠ fixture 本身會走過受測守衛（`create()` → `_enforce_supply_gate`），
        所以**一律用有釘版的 template 建**，需要「未釘版」的情境時在建完之後才
        把 template 的版本清空。順序反過來的話測試會在抵達受測步驟之前就死，
        而那個 error 長得跟「守衛還在」一模一樣。
        """
        vals = {
            'name': 'svc-552c-%s' % template.slug,
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': state,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-552c-%s' % template.slug,
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        }
        vals.update(extra)
        return _internal_provision(self.Service).create(vals)


class TestPurchasabilityJudge(_Base):
    """單一裁決點：`_reject_reason_for_new_instance` / `_availability`。"""

    def test_unpinned_template_is_not_purchasable(self):
        template = self._template('unpinned-552c', '')
        self.assertEqual(
            template._availability(),
            {'purchasable': False, 'reason': 'unpinned_chart'},
            '未釘版的商品必須在 marketplace 就顯示為不可購買——擋在部署當下'
            '等於先收錢、建訂閱、寄確認信，再 error')

    def test_pinned_template_stays_purchasable(self):
        """陽性對照：判準不是恆假，否則上一條零鑑別力。"""
        template = self._template('pinned-552c', '1.0.0')
        self.assertTrue(template._availability()['purchasable'])

    def test_paused_still_wins_over_unpinned(self):
        """優先序不變：停售是營運明示的意志，訊息更貼近租戶處境。"""
        template = self._template('paused-552c', '')
        self.env['ir.config_parameter'].sudo().set_param(
            template._sales_paused_param_key(), '1')
        self.assertEqual(template._availability()['reason'], 'paused')

    def test_unpinned_wins_over_sold_out(self):
        """🔴 未釘版必須排在 sold_out **之前**。

        把平台端的設定缺陷報成「已售罄」是對租戶說謊，而且會讓營運去查一個
        根本不存在的名額問題。這條就是那個順序的守線——把判斷搬到 cap 之後
        就會紅。
        """
        # 先用釘版的建到真的售罄——fixture 本身要走過受測守衛，順序不能反。
        template = self._template('soldout-552c', '1.0.0')
        self.env['ir.config_parameter'].sudo().set_param(
            template._global_cap_param_key(), '1')
        self._service(template)

        # 🔴 前提斷言：這個 template **真的**已售罄。少了它，`_service` 的計數
        # 口徑一變（或 cap 沒生效）就會讓下面那條退化成恆真——順序改壞了也不紅。
        self.assertEqual(
            template._availability()['reason'], 'sold_out',
            '前提沒成立：本 template 並未真的售罄，下面那條斷言沒有鑑別力')

        template.write({'helm_chart_version': ''})
        self.assertEqual(
            template._availability()['reason'], 'unpinned_chart',
            '未釘版的商品即使同時達到名額上限，也不得被報成「已售罄」——'
            '把判斷搬到 cap 之後就會在這裡紅')


class TestFirstInstallGate(_Base):
    """兩個首裝入口的共同咽喉：`CloudService.create()`。"""

    def test_marketplace_entry_is_rejected(self):
        """入口 1：controller `_create_service`（走 `create()`）。"""
        template = self._template('mk-552c', '1.0.0')
        service = self._service(template)          # 先用釘版的建起來
        template.write({'helm_chart_version': ''})  # 再讓它變成未釘版

        with self.assertRaises(SupplyGateError) as err:
            self._service(template, **{'name': 'svc-second-552c'})
        self.assertIn(
            'has not pinned a deployment version', str(err.exception))
        self.assertTrue(service.exists(), '前提：既有服務不受影響，擋的是新開通')

    def test_ha_paas_entry_is_also_rejected(self):
        """入口 2：`controllers/ha_paas.py` 的 `.sudo().create()`。

        ⚠ 它**不經 `_create_service`**，所以守在那個 controller 會被繞過。這裡
        以 `sudo()` 重現該入口的形狀——同時也是「守衛對 su env 不豁免」的斷言
        （豁免會讓所有 TransactionCase 假綠，見 su-env-exempt-guard-false-green）。
        """
        template = self._template('ha-552c', '1.0.0')
        template.write({'helm_chart_version': ''})

        with self.assertRaises(SupplyGateError):
            _internal_provision(self.Service.sudo()).create({
                'name': 'svc-ha-552c',
                'workspace_id': self.workspace.id,
                'template_id': template.id,
                'state': 'pending',
                'helm_namespace': 'paas-ws-test',
                'helm_release_name': 'svc-ha-552c',
            })

    def test_pinned_template_can_still_be_created(self):
        """非回歸：釘了版就照常開通。"""
        service = self._service(self._template('ok-552c', '1.0.0'), state='pending')
        self.assertTrue(service.exists())


class TestExecuteDeployBackstop(_Base):
    """存量 `pending` 列的 backstop —— 閘門上線前就躺在那裡的服務。"""

    def _deploy_with_unpinned_template(self):
        template = self._template('backstop-552c', '1.0.0')
        service = self._service(template, state='pending')
        template.write({'helm_chart_version': ''})
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            service._execute_deploy()
        return service, client

    def test_deploy_is_aborted_before_helm_is_called(self):
        service, client = self._deploy_with_unpinned_template()
        self.assertFalse(
            client.install_release.called,
            '必須在打 operator **之前**中止；打出去就已經部署了未知版本')
        self.assertEqual(service.state, 'error')

    def test_error_message_names_the_real_cause(self):
        """🔴 例外型別必須是 `PaaSOperatorError`。

        換成任何其他型別就會落進 `_cron_process_pending_deploys` 的泛用
        `except Exception`，訊息被換成「Unexpected error during deployment.
        Check server logs.」——租戶與第一線都無從排錯（#520 症狀 4 同型）。
        這條斷言的是**訊息內容**，因為那正是型別選錯時會消失的東西。
        """
        service, _ = self._deploy_with_unpinned_template()
        self.assertIn(
            'has no deployment version pinned', service.error_message or '')
        self.assertNotIn('Unexpected error', service.error_message or '')

    def test_pinned_template_still_deploys(self):
        """陽性對照：backstop 不是恆擋。"""
        template = self._template('backstop-ok-552c', '1.0.0')
        service = self._service(template, state='pending')
        client = MagicMock()
        client.install_release.return_value = {'revision': 1, 'chart_version': '1.0.0'}
        with patch(_CLIENT_PATH, return_value=client):
            service._execute_deploy()
        self.assertTrue(client.install_release.called)
        self.assertNotEqual(service.state, 'error')


class TestRedeployGate(_Base):
    """後台 button ＋ `call_kw`，沒有 role gate ⇒ 一次誤按就是不可逆跳版。"""

    def test_redeploy_blocked_when_template_unpinned(self):
        template = self._template('redep-552c', '1.0.0')
        service = self._service(template)
        template.write({'helm_chart_version': ''})

        client = MagicMock()
        client.get_release.return_value = {'name': service.helm_release_name}
        with patch(_CLIENT_PATH, return_value=client):
            with self.assertRaises(UserError) as err:
                service.action_redeploy()

        self.assertIn('has no chart version pinned', str(err.exception))
        self.assertFalse(
            client.upgrade_release.called,
            'UserError 必須在打 operator 之前拋出')

    def test_redeploy_still_works_when_pinned(self):
        template = self._template('redep-ok-552c', '2.0.0')
        service = self._service(template)
        client = MagicMock()
        client.get_release.return_value = {'name': service.helm_release_name}
        client.upgrade_release.return_value = {'revision': 2, 'chart_version': '2.0.0'}
        with patch(_CLIENT_PATH, return_value=client):
            service.action_redeploy()
        self.assertTrue(client.upgrade_release.called)


class TestReconfigureIsDeliberatelyNotGated(_Base):
    """🔴 把「刻意不守 reconfigure」釘成契約。

    `_update_service` 走背景執行緒，失敗回不到前端 ⇒ 在那裡 fail-closed 會讓
    全部商品的「儲存設定／改網域／改 addon」**靜默壞掉**。誰之後想「順手補齊
    第五條閘門」，會先撞到這條測試與它的理由。
    """

    def test_reconfigure_is_deliberately_not_gated(self):
        template = self._template('reconf-552c', '1.0.0')
        service = self._service(template)
        template.write({'helm_chart_version': ''})
        service.write({'helm_chart_version': ''})

        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 2}
        fake_request = MagicMock()
        fake_request.env = self.env

        class _SyncThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                self._target()

        client_path = (
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.PaaSOperatorClient'
        )
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=fake_request), \
                patch(client_path, return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread):
            result = PaasController()._update_service(service, values={}, version=None)

        self.assertTrue(
            result['success'],
            'reconfigure 不得因為沒有版號而失敗——那條路失敗回不到前端，等於'
            '全部商品的「儲存設定」靜默壞掉')


class TestGuardDoesNotBlockShippedProducts(_Base):
    """🔴 只驗「空值被擋」＝把 fail-closed 寫成 fail-everything 而不自知。

    這組是另一半：**現行架上的商品必須全部照常通過**。#561 之後 seed 裡已經
    沒有任何空版號 template，所以這裡的樣本同時也是「未來新增 template 忘了
    釘版」的守線——那種疏漏在 prod 的症狀是**商品悄悄從貨架上消失**。
    """

    def _seeded_templates(self):
        templates = self.env['woow_paas_platform.cloud_app_template'].with_context(
            active_test=False).search([])
        # ⚠ 非空還不夠，要有 **floor**：全稱斷言在小集合上一樣會弱化，而
        # 「seed 只載入了一部分」與「全部載入」在 `assertTrue` 下輸出相同。
        # 15 是 #552 全程的實測數（四個空版號由 #561 補齊後仍是 15）；新增商品
        # 時這個下限只會更寬鬆，不會變成需要維護的計數斷言。
        self.assertGreaterEqual(
            len(templates), 15,
            '取樣點只有 %d 個 template（預期 ≥15）：seed 沒載入完 ⇒ 下面的'
            '全稱斷言會在一個殘缺集合上通過' % len(templates))
        return templates

    def test_no_shipped_template_is_blocked_by_this_guard(self):
        """全部 seed template 都不得被 `unpinned_chart` 擋下。

        ⚠ 斷言走 `_availability()`（真正的閘門）而不是直接讀欄位——讀欄位只是
        重述資料，測不到判準本身接錯欄位／取反的情形。
        """
        blocked = [
            t.slug for t in self._seeded_templates()
            if t._availability().get('reason') == 'unpinned_chart'
        ]
        self.assertFalse(
            blocked,
            '這些已上架商品會被 #552 層 4 擋在購買前（＝從貨架上消失）：%s。'
            '新增 template 時 helm_chart_version 不可留空。' % blocked)

    def test_helm_secret_carrier_products_are_pinned_and_purchasable(self):
        """走 `_reset_via_helm_secret`（operator 第二分支）的兩個商品。

        它們同時是「第二分支」與「有值不得被擋」兩個面向的樣本——只用自建的
        空值 fixture 測 reset，會把這兩面一起漏掉。
        """
        for xmlid in ('cloud_app_opendesign', 'cloud_app_hermes_agent'):
            template = self.env.ref('woow_paas_platform.%s' % xmlid)
            self.assertEqual(
                template.admin_password_carrier, 'helm_secret',
                '前提：%s 走的是 operator 的第二條 reset 分支' % xmlid)
            self.assertTrue(
                template._availability()['purchasable'],
                '%s 被層 4 擋下＝在售商品下架' % xmlid)

    def test_reset_on_a_shipped_product_sends_a_non_empty_version(self):
        """把 operator 側的新閘門與平台側接起來。

        operator 現在對「遠端 chart ref ＋ 空 version」回 400。這條證明平台對
        **實際在售**的商品送出去的一定不是空值 ⇒ 那道 400 不會打到任何現存租戶。
        少了它，operator 的閘門與平台的行為各自都「看起來對」。
        """
        template = self.env.ref('woow_paas_platform.cloud_app_opendesign')
        service = _internal_provision(self.Service.with_context(mcp_admin_password='old-pw')).create({
            'name': 'svc-552c-od',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-552c-od',
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        })
        client = MagicMock()
        client.reset_release_config.return_value = {'helm_revision': 2}
        with patch(_CLIENT_PATH, return_value=client):
            service._reset_admin_password()

        kwargs = client.reset_release_config.call_args.kwargs
        self.assertEqual(kwargs['admin_password_carrier'], 'helm_secret',
                         '前提：這顆真的走第二分支')
        self.assertTrue(
            kwargs['version'],
            'reset 送出空 version ⇒ operator 的 #552 閘門會回 400，'
            '在售商品的密碼重設整個壞掉')


class TestUpgradeTimeWarning(_Base):
    """`_warn_unpinned_active_templates`：把「靜默下架」變成升級 log 的一行 ERROR。

    這個 helper 自己也是「偵測到才發聲」的形狀 ⇒ 它壞掉的方向同樣是**安靜**。
    所以兩邊都測：該叫的時候叫（指名 slug），不該叫的時候不叫。
    """

    def test_active_unpinned_template_is_named_in_an_error_log(self):
        template = self._template('warn-552c', '1.0.0')
        template.write({'helm_chart_version': ''})

        # ⚠ 數值 logging.ERROR 而非字串 'ERROR'：Odoo 進程覆寫了名稱→數值表，
        # 字串形式會挑到被遮蔽的等級（#492，repo-lint assertlogs-numeric-level）。
        with self.assertLogs(
            'odoo.addons.woow_paas_platform.models.cloud_app_template',
            level=logging.ERROR,
        ) as captured:
            self.Template._warn_unpinned_active_templates()

        self.assertTrue(
            any('warn-552c' in line for line in captured.output),
            '訊息必須指名是哪個 slug——只說「有幾個」的話維運還是得自己去翻 DB')

    def test_silent_when_every_active_template_is_pinned(self):
        """陽性對照的反面：不得對正常狀態叫。

        前提：本測試的 fixture 是釘版的，且 seed 也全部釘版（由
        `TestGuardDoesNotBlockShippedProducts` 守），所以這裡的「沒有 ERROR」
        是有內容的斷言，不是因為方法根本沒跑。
        """
        self._template('warn-ok-552c', '1.0.0')
        with self.assertNoLogs(
            'odoo.addons.woow_paas_platform.models.cloud_app_template',
            level=logging.ERROR,
        ):
            self.Template._warn_unpinned_active_templates()

    def test_inactive_unpinned_template_is_only_a_warning(self):
        """已下架的商品沒有租戶會撞到 ⇒ 不該用 ERROR 稀釋真正的警報。

        🔴 **兩半都要斷言**。本測試第一版只寫了 `assertNoLogs(ERROR)` ⇒ 把整個
        `_warn_unpinned_active_templates` 改成 `pass` 也照樣綠，而測試名稱
        `is_only_a_warning` 宣稱的正是它沒驗的那一半。具體後果：有人把「已下架
        用 WARNING」改成「完全不 log」時這條不會響，而「重新上架前必須釘版」
        的訊號就此消失（#562 追蹤的正是那個場景）。

        斷言「沒有 X」時必須連「本來會有 Y」一起斷言——否則被測程式一被掏空，
        缺席那半就退化成恆真。
        """
        template = self._template('warn-inactive-552c', '1.0.0', is_active=False)
        template.write({'helm_chart_version': ''})

        # 前提：WARNING **真的**發生，而且指名 slug（維運不必自己翻 DB）。
        with self.assertLogs(
            'odoo.addons.woow_paas_platform.models.cloud_app_template',
            level=logging.WARNING,
        ) as captured:
            self.Template._warn_unpinned_active_templates()
        self.assertTrue(
            any('warn-inactive-552c' in line for line in captured.output),
            '已下架未釘版的商品必須被指名記下，否則重新上架前沒有任何提醒')

        # 守線：分級正確——不得升成 ERROR 稀釋在售商品那條真正的警報。
        self.assertFalse(
            [line for line in captured.output if line.startswith('ERROR')],
            '已下架商品沒有租戶撞得到，用 ERROR 會讓真正的在售警報被雜訊淹沒')


class TestPollRecordsOutOfBandChartVersion(_Base):
    """(a) 第五條回寫路徑：poll 是唯一抓得到「非平台發起的變動」的地方。

    #642 §4.1 ④：running／error／initializing×2 這四支各補上
    `last_release_sync_at` 的基準線＋前進斷言——與姊妹檔
    `test_poll_mirror_release_facts_627.py` 對 model 那份 poll 的覆蓋同形，這裡
    補的是 controller 那份。`_stale_sync_at()` 從那個檔案匯入，不在本檔手抄
    第二份：這正是 #627 的教訓（兩份實作、只驅動一份的測試全綠）在測試 helper
    層級的同構版本。
    """

    def _poll(self, service, status_payload):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        client = MagicMock()
        client.get_status.return_value = status_payload
        fake_request = MagicMock()
        fake_request.env = self.env
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=fake_request):
            PaasController()._update_service_status(service)
        service.invalidate_recordset()

    def _status(self, **release_overrides):
        """operator `get_status` 的回應。

        ⚠ `pods` **不可留空**：`_is_container_ready` 對空陣列是 fail-closed
        （#456／F-423-1，空陣列可能是 ResourceQuota 永久拒絕建 pod），不就緒就
        整段不會 write ⇒ 測試會在「什麼都沒發生」的狀態下對回寫下斷言。這正是
        前提斷言（`helm_revision` 有沒有動）擋下來的東西——第一版就是這樣紅的。
        """
        release = {'status': 'deployed', 'revision': 3, 'chart_version': '0.1.1'}
        release.update(release_overrides)
        return {
            'release': release,
            'pods': [{'name': 'p1', 'phase': 'Running', 'ready': '1/1'}],
        }

    def test_records_chart_version_changed_outside_odoo(self):
        """stg `svc-bd9d6ed3` 的形態：叢集被別人 upgrade 了，平台四條路徑都沒跑。"""
        template = self._template('poll-552c', '0.1.0')
        service = self._service(template, state='deploying')
        stale_sync_at = _stale_sync_at()
        service.write({
            'helm_chart_version': '0.1.0',
            'helm_revision': 2,
            'last_release_sync_at': stale_sync_at,
        })

        self._poll(service, self._status())

        self.assertEqual(service.helm_revision, 3, '前提：這條 poll 真的寫了')
        self.assertEqual(
            service.helm_chart_version, '0.1.1',
            'poll 是唯一抓得到非平台發起之 chart 變動的路徑；不回寫的話帳本'
            '永遠停在最後一次由 Odoo 發起的操作')
        self.assertGreater(
            service.last_release_sync_at, stale_sync_at,
            '#642 §4.1 ④：這條 write 分支必須把 last_release_sync_at 從基準線'
            '前進——語意是「最後一次問過 operator」，不是「值有沒有變」')

    def test_failed_release_branch_also_records_it(self):
        """`release_facts` 必須到得了**每個** write 分支，不只 happy path。

        只驗 deployed 那條的話，把 `**release_facts` 從其他分支拿掉照樣全綠。
        """
        template = self._template('pollfail-552c', '0.1.0')
        service = self._service(template, state='deploying')
        stale_sync_at = _stale_sync_at()
        service.write({
            'helm_chart_version': '0.1.0',
            'last_release_sync_at': stale_sync_at,
        })

        self._poll(service, self._status(status='failed', description='boom'))

        self.assertEqual(service.state, 'error', '前提：走的是 failed 分支')
        self.assertEqual(service.helm_chart_version, '0.1.1')
        self.assertGreater(
            service.last_release_sync_at, stale_sync_at,
            '就算走的是 error 分支，release_facts 仍然被展開進去，'
            'last_release_sync_at 也必須前進')

    def test_old_operator_without_the_field_keeps_the_value(self):
        template = self._template('polllegacy-552c', '0.1.0')
        service = self._service(template, state='deploying')
        service.write({'helm_chart_version': '0.1.0', 'helm_revision': 2})

        status = self._status()
        del status['release']['chart_version']
        self._poll(service, status)

        self.assertEqual(service.helm_revision, 3, '前提：這條 poll 真的寫了')
        self.assertEqual(service.helm_chart_version, '0.1.0')

    # ------------------------------------------------------------------ #
    #  #627：上面那支 `test_failed_release_branch_also_records_it` 的      #
    #  docstring 宣稱「必須到得了**每個** write 分支」，但實際只驅動了      #
    #  4 條裡的 2 條（→ running、→ error）。兩條 `initializing` 分支從來    #
    #  沒有任何測試對 `release_facts` 的內容斷言。                          #
    #                                                                     #
    #  ⇒ 「守錯層」只是表層問題（models 鏡像整份沒被驅動，見                #
    #     test_poll_mirror_release_facts_627.py）；底下還有一層是          #
    #     **在守對的那一層也沒守全**。以下兩支把 controller 補齊。          #
    # ------------------------------------------------------------------ #

    def _poll_with_init_stubbed(self, service, status_payload):
        """驅動 poll，但把 post-deploy init 擋掉。

        兩條 `initializing` 分支寫完 `release_facts` 之後會呼叫真正的 init（會去打
        operator 的 init 端點）。本檔要驗的是回寫，不是 init。
        """
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        client = MagicMock()
        client.get_status.return_value = status_payload
        fake_request = MagicMock()
        fake_request.env = self.env
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=fake_request):
            # #1257：controller 的 poll 只推進 state、不 dispatch init，不必再 patch。
            PaasController()._update_service_status(service)
        service.invalidate_recordset()

    def test_initializing_branch_from_deploying_records_it(self):
        """→ `initializing`（首裝：需要 init ＋ 原狀態 `deploying`）。"""
        template = self._template(
            'pollinit-552c', '0.1.0', post_deploy_init_type='n8n')
        service = self._service(template, state='deploying')
        stale_sync_at = _stale_sync_at()
        service.write({
            'helm_chart_version': '0.1.0',
            'helm_revision': 2,
            'last_release_sync_at': stale_sync_at,
        })

        self._poll_with_init_stubbed(service, self._status())

        self.assertEqual(service.state, 'initializing', '前提：走的是這條分支')
        self.assertEqual(service.helm_revision, 3, '前提：這條 poll 真的寫了')
        self.assertEqual(service.helm_chart_version, '0.1.1')
        self.assertGreater(
            service.last_release_sync_at, stale_sync_at,
            '首裝分支同樣必須把 last_release_sync_at 從基準線前進')

    def test_initializing_branch_from_upgrading_records_it(self):
        """→ `initializing`（升級後補 init：`upgrading` ＋ `addon_update_pending`）。

        這條分支正是「chart 剛被換過」最可能發生的時刻——漏掉它等於在最該記錄的那
        一刻不記錄。
        """
        template = self._template(
            'pollupg-552c', '0.1.0', post_deploy_init_type='n8n')
        service = self._service(template, state='upgrading')
        stale_sync_at = _stale_sync_at()
        service.write({
            'helm_chart_version': '0.1.0',
            'helm_revision': 2,
            'addon_update_pending': True,
            'last_release_sync_at': stale_sync_at,
        })

        self._poll_with_init_stubbed(service, self._status())

        self.assertEqual(service.state, 'initializing', '前提：走的是這條分支')
        self.assertEqual(service.helm_revision, 3, '前提：這條 poll 真的寫了')
        self.assertEqual(service.helm_chart_version, '0.1.1')
        self.assertGreater(
            service.last_release_sync_at, stale_sync_at,
            '「chart 剛被換過」最可能發生的這一刻，last_release_sync_at 也必須'
            '前進，不能只顧 chart_version')
