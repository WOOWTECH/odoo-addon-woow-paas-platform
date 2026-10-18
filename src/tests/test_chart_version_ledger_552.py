# -*- coding: utf-8 -*-
"""#552：chart 版本帳本——回寫實際版本 ＋ reconfigure 的 version fallback。

## 兩個獨立缺陷

**(A) 帳本從不回寫。** operator 主動把版本切掉（``helm.py`` 的兩個 parser），
所以平台從頭到尾收不到實際 chart 版本；四條成功路徑（首裝 / redeploy / reset
admin password / reconfigure）都只寫 ``helm_revision``、``state`` 等欄位，
``helm_chart_version`` 停在首裝那一刻。stg 實證：``svc-a66927a6``（ha-paas）
Odoo 帳本 rev 1 / 0.1.0，叢集實際 rev 4 / **0.2.1**——差 3 個 revision、跨兩個
minor，而**租戶在服務詳情頁看到的就是那個錯的 0.1.0**（``paas.py`` 的
``_format_service`` 原樣回傳本欄）。

**(B) reconfigure 永遠不釘版。** ``_update_service`` 的 ``version`` 參數恆為
``None``——前端三個入口（ConfigurationTab 儲存設定 / EditDomainModal 改網域 /
AddonsTab 改 addon）都不帶它。於是 operator 不帶 ``--version``、helm 解析成
registry **當下的 latest**。⚠ 這條路**與 template 有沒有填版號無關**：
mcp-* 釘 0.1.5、home-assistant 釘 0.2.1 的 pin 在 reconfigure 上完全沒作用，
**全部 15 個 template 都中**。

(A) 與 (B) 是獨立的：把 seed 空版號填滿也不會讓 (A) 消失。

## 🔴 回寫對象只能是 per-service 欄位

``cloud_service.helm_chart_version``，**不得**是 ``template.helm_chart_version``。
seed（``data/cloud_app_templates.xml``）是 ``noupdate=0``，且檔尾 healer 每次
``-u`` 主動清 ``ir.model.data.noupdate`` ⇒ 寫進 template 的值**手改 DB 保不住**，
下一次發版就靜默還原（同紀律的明文警告見 ``models/cloud_app_template.py``
``_DEFAULT_GLOBAL_INSTANCE_CAPS`` 上方）。每條回寫測試因此都同時斷言
「service 變了（前提）」與「template 沒變（守線）」——少了前提那半，路徑根本沒
被走到時守線那半會退化成恆真。

## 刻意**不**做的事

不在 ``PaasOperatorClient.upgrade_release`` 加 fail-closed guard。那條路走背景
執行緒、失敗回不到前端，會讓全部 15 個商品的「儲存設定」**靜默壞掉**。
``test_reconfigure_with_no_pin_anywhere_still_sends_none`` 就是把這個取捨釘成契約。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase
from ..models.cloud_service import _internal_provision
from .ns_quota_fixture import answer_quota_reads

_CLIENT_PATH = (
    'odoo.addons.woow_paas_platform.services.paas_operator'
    '.get_paas_operator_client'
)
_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'


class _NonClosingCursor:
    """把背景執行緒的 ``odoo.registry(db).cursor()`` 導回測試 transaction。

    ``_bg_upgrade`` 自建 cursor，寫入落在**另一個** transaction；測試的 row 尚未
    commit，所以那邊的 ``svc.exists()`` 為 False ⇒ 整段回寫被跳過，在測試裡既
    觀察不到、也證明不了它存在（``test_suspended_redeploy_state_482`` 的 docstring
    已記下這個限制）。第四條回寫路徑就在那段 write 裡，所以這裡把 cursor 換成
    測試自己的，讓它真的被執行到。

    ``__exit__`` 刻意不關閉 cursor —— 關掉的話測試後續的斷言全部無法讀 DB。
    """

    def __init__(self, cr):
        self._cr = cr

    def __enter__(self):
        return self._cr

    def __exit__(self, *exc_info):
        return False


class _SyncThread:
    """把 ``threading.Thread`` 換成同步執行，讓 target 在測試堆疊裡跑完。"""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '552 Workspace',
        })

    def _template(self, slug, chart_version, **extra):
        vals = {
            'name': '552 %s' % slug,
            'slug': slug,
            # helm_repo_url 是 required（DB NOT NULL）；OCI chart 慣例是空字串。
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/%s' % slug,
            'helm_chart_version': chart_version,
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
        }
        vals.update(extra)
        return self.Template.create(vals)

    def _service(self, template, state='running', chart_version=None, **extra):
        vals = {
            'name': 'svc-552-%s' % template.slug,
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': state,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-552-%s' % template.slug,
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        }
        vals.update(extra)
        service = _internal_provision(self.Service).create(vals)
        # create 之後才寫，避免 create 的預設／onchange 蓋掉受測的起始值。
        service.write({'helm_chart_version': chart_version or False})
        return service

    def _assert_ledger_moved_but_template_untouched(
        self, service, template, expected, template_before,
    ):
        """回寫的兩半：service 真的變了（前提）＋ template 沒被碰（守線）。

        少了前提那半，「template 沒變」在路徑根本沒被走到時也會綠。
        """
        service.invalidate_recordset()
        template.invalidate_recordset()
        self.assertEqual(
            service.helm_chart_version, expected,
            'per-service 帳本必須記下 operator 回報的實際 chart 版本；'
            '停在舊值＝租戶在服務詳情頁看到的版本是錯的（#552 stg 實證）')
        self.assertEqual(
            template.helm_chart_version, template_before,
            '🔴 回寫對象只能是 cloud_service，不得動 template.helm_chart_version：'
            'seed 是 noupdate=0 且檔尾 healer 每次 -u 清 noupdate flag，'
            '寫進 template 的值下一次發版就靜默還原')


class TestExecuteDeployRecordsActualChartVersion(_Base):
    """路徑 1／4：首裝 ``_execute_deploy``。"""

    def _deploy(self, service, install_result):
        client = MagicMock()
        answer_quota_reads(client)
        client.install_release.return_value = install_result
        with patch(_CLIENT_PATH, return_value=client):
            service._execute_deploy()
        return client

    # ⚠ 這裡原本有一條 `test_records_version_resolved_by_helm_when_template_pin_
    # is_empty`（空 pin 首裝 ⇒ 記下 helm 挑的 latest）。#552 **層 4** 之後那個情境
    # 已經**結構上不可達**：空 pin 的 template 在 `create()` 就被供給閘門擋下，
    # 連 `_execute_deploy` 也有 backstop。留著它只會變成一條「測試造出產品碼不
    # 允許的狀態」的假情境。
    #
    # 它原本要守的東西沒有消失，只是換了地方：
    #   * 「回寫的是 operator 回報值而不是 pin」→ 下面那條（pin 0.1.5、回報 0.1.6）
    #   * 「空 pin 不得部署」→ `test_chart_pin_fail_closed_552.py` 的
    #     `TestFirstInstallGate` / `TestExecuteDeployBackstop`

    def test_records_version_even_when_it_differs_from_the_pin(self):
        """有 pin 也要記 operator 回報值——pin 是「要求」，回報值是「事實」。"""
        template = self._template('mcp-552', '0.1.5')
        service = self._service(template, state='pending', chart_version='0.1.5')

        self._deploy(service, {'revision': 1, 'chart_version': '0.1.6'})

        self._assert_ledger_moved_but_template_untouched(
            service, template, '0.1.6', '0.1.5')

    def test_old_operator_without_the_field_keeps_the_current_value(self):
        """向後相容：讀不到就保留原值，**不得**把帳本洗成空。

        前提斷言（``deployed_at`` / ``helm_revision``）不可省：少了它，這條在
        「整段 write 根本沒跑」時也會綠——那正是修法前的狀態。
        """
        template = self._template('legacy-552', '0.2.1')
        service = self._service(template, state='pending', chart_version='0.2.1')

        self._deploy(service, {'revision': 7})  # 舊 operator：沒有 chart_version

        service.invalidate_recordset()
        self.assertEqual(service.helm_revision, 7, '前提：這條 write 真的跑了')
        self.assertTrue(service.deployed_at, '前提：這條 write 真的跑了')
        self.assertEqual(
            service.helm_chart_version, '0.2.1',
            '舊 operator 不回報版本時必須保留原值；洗成空會讓漂移偵測'
            '（兩邊都要有值才算漂移）從此永遠看不到這台服務')


    def test_zero_revision_records_the_first_revision(self):
        """#1414：首裝回報 0（舊 operator）時記 1，不記 0。"""
        template = self._template('zero-1414', '0.2.1')
        service = self._service(template, state='pending', chart_version='0.2.1')

        self._deploy(service, {'revision': 0, 'chart_version': '0.2.1'})

        service.invalidate_recordset()
        self.assertTrue(service.deployed_at, '前提：這條 write 真的跑了')
        self.assertEqual(service.helm_revision, 1)


class TestRedeployRecordsActualChartVersion(_Base):
    """路徑 2／4：``action_redeploy``（既有 release ⇒ helm upgrade）。"""

    def _redeploy(self, service, upgrade_result):
        client = MagicMock()
        client.get_release.return_value = {'name': service.helm_release_name}
        client.upgrade_release.return_value = upgrade_result
        with patch(_CLIENT_PATH, return_value=client):
            service.action_redeploy()
        return client

    def test_records_version_resolved_by_helm(self):
        template = self._template('redeploy-552', '0.2.1')
        service = self._service(template, chart_version='0.1.0')

        self._redeploy(service, {'revision': 4, 'chart_version': '0.2.1'})

        # 這正是 stg svc-a66927a6 的形狀：帳本停在 0.1.0、叢集已是 0.2.1。
        self._assert_ledger_moved_but_template_untouched(
            service, template, '0.2.1', '0.2.1')

    def test_old_operator_without_the_field_keeps_the_current_value(self):
        template = self._template('redeploy-legacy-552', '0.2.1')
        service = self._service(template, chart_version='0.1.0')

        self._redeploy(service, {'revision': 4})

        service.invalidate_recordset()
        self.assertEqual(service.helm_revision, 4, '前提：這條 write 真的跑了')
        self.assertEqual(service.helm_chart_version, '0.1.0')


    def test_zero_revision_keeps_the_ledger(self):
        """#1414：舊 operator 的 upgrade 回報 revision 0——帳本不得被寫成 0。"""
        template = self._template('redeploy-zero-1414', '0.2.1')
        service = self._service(template, chart_version='0.1.0')
        service.sudo().write({'helm_revision': 3})

        self._redeploy(service, {'revision': 0, 'chart_version': '0.2.1'})

        service.invalidate_recordset()
        self.assertEqual(service.helm_chart_version, '0.2.1', '前提：這條 write 真的跑了')
        self.assertEqual(service.helm_revision, 3)


class TestResetAdminPasswordRecordsActualChartVersion(_Base):
    """路徑 3／4：``_reset_admin_password``。

    ⚠ Odoo 側是**一個**入口，但 operator 進去後依 ``admin_password_carrier``
    分流成兩條實作（config_json ／ ``_reset_via_helm_secret``）。兩個載體都測，
    是為了釘住「平台不因載體而走不同的回寫邏輯」——operator 側兩條分支各自
    回報 ``chart_version`` 的守線在 ``extra/paas-operator/tests/``。
    """

    def _reset(self, service, reset_result):
        client = MagicMock()
        client.reset_release_config.return_value = reset_result
        with patch(_CLIENT_PATH, return_value=client):
            service._reset_admin_password()
        return client

    def _mcp_service(self, carrier, chart_version='0.1.5'):
        template = self._template(
            'reset-552-%s' % carrier.replace('_', '-'), chart_version,
            category='mcp', has_admin_gui=True, admin_password_carrier=carrier,
        )
        service = _internal_provision(self.Service.with_context(mcp_admin_password='old-pw')).create({
            'name': 'svc-552-reset-%s' % carrier,
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-552-reset',
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        })
        service.write({'helm_chart_version': '0.1.0'})
        return template, service

    def test_config_json_carrier_records_actual_version(self):
        template, service = self._mcp_service('config_json')

        self._reset(service, {'helm_revision': 3, 'chart_version': '0.1.5'})

        self._assert_ledger_moved_but_template_untouched(
            service, template, '0.1.5', '0.1.5')

    def test_helm_secret_carrier_records_actual_version(self):
        """走 ``_reset_via_helm_secret`` 的 open-design / hermes-agent 形狀。"""
        template, service = self._mcp_service('helm_secret', chart_version='0.1.1')

        self._reset(service, {'helm_revision': 3, 'chart_version': '0.1.1'})

        self._assert_ledger_moved_but_template_untouched(
            service, template, '0.1.1', '0.1.1')

    def test_old_operator_without_the_field_keeps_the_current_value(self):
        template, service = self._mcp_service('config_json')

        self._reset(service, {'helm_revision': 3})

        service.invalidate_recordset()
        self.assertEqual(
            service.state, 'upgrading', '前提：成功路徑的 write 真的跑了')
        self.assertEqual(service.helm_chart_version, '0.1.0')


class TestReconfigureVersionFallback(_Base):
    """缺陷 (B)：``_update_service`` 送給 operator 的版本。

    斷言落在 ``client.upgrade_release`` 的 ``version`` kwarg——那是「有沒有帶
    ``--version``」的唯一可觀察點，也是 registry latest 與釘版的分界。
    """

    def _run_update(self, service, client, version=None):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        fake_request = MagicMock()
        fake_request.env = self.env
        fake_registry = MagicMock()
        fake_registry.cursor.return_value = _NonClosingCursor(self.env.cr)

        client_path = (
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.PaaSOperatorClient'
        )
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=fake_request), \
                patch(client_path, return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread), \
                patch('odoo.registry', return_value=fake_registry):
            return PaasController()._update_service(
                service, values={}, version=version)

    def _client(self, upgrade_result=None):
        client = MagicMock()
        client.upgrade_release.return_value = upgrade_result or {'revision': 2}
        return client

    def _sent_version(self, client):
        self.assertTrue(
            client.upgrade_release.call_args,
            '前提：背景執行緒真的呼叫了 upgrade_release')
        return client.upgrade_release.call_args.kwargs['version']

    def test_falls_back_to_the_services_own_chart_version(self):
        """前端從不帶 version ⇒ 沒有 fallback 就等於「升到 registry latest」。"""
        template = self._template('reconf-552', '0.2.1')
        service = self._service(template, chart_version='0.1.5')

        client = self._client()
        self._run_update(service, client)

        self.assertEqual(
            self._sent_version(client), '0.1.5',
            'reconfigure 的語意是改設定、不是升版 ⇒ 先原地釘住服務現在跑的版本')

    def test_falls_back_to_the_template_pin_when_the_service_has_none(self):
        """legacy 服務（帳本空）才退到商品的 pin。"""
        template = self._template('reconf-legacy-552', '0.2.1')
        service = self._service(template, chart_version=False)

        client = self._client()
        self._run_update(service, client)

        self.assertEqual(self._sent_version(client), '0.2.1')

    def test_explicit_other_version_is_rejected(self):
        """#1525 推翻了原本「呼叫端指定的版本優先」的契約：那等於讓租戶自選 chart
        （可降回有洞的舊版）。現在不一致的版本直接回錯誤、不呼叫 operator。
        完整矩陣見 ``test_tenant_chart_version_1525``。"""
        template = self._template('reconf-explicit-552', '0.2.1')
        service = self._service(template, chart_version='0.1.5')

        client = self._client()
        result = self._run_update(service, client, version='9.9.9')

        self.assertFalse(result['success'], result)
        client.upgrade_release.assert_not_called()

    def test_reconfigure_with_no_pin_anywhere_still_sends_none(self):
        """🔴 三處全空時**不得**拒絕——這是刻意的取捨，釘成契約。

        在這條路上 fail-closed 會讓「儲存設定 / 改網域 / 改 addon」失敗在背景
        執行緒裡、回不到前端 ⇒ 全部 15 個商品靜默壞掉。判空要往前移到購買前的
        閘門，不是這裡（#552 修法順序：fallback 先於 fail-closed）。
        """
        # ⚠ 順序：先用**釘版的** template 把服務建起來，再把兩邊清空。#552 層 4
        # 之後空版號的 template 在 `create()` 就會被供給閘門擋下——順序反過來，
        # 測試會在抵達受測步驟之前就死，而那個 error 長得跟「reconfigure 被擋」
        # 一模一樣。可達性沒有變（管理員把已上架商品的版本清掉即可重現）。
        template = self._template('reconf-blank-552', '1.0.0')
        service = self._service(template, chart_version='1.0.0')
        template.write({'helm_chart_version': ''})
        service.write({'helm_chart_version': False})

        client = self._client()
        result = self._run_update(service, client)

        self.assertTrue(result['success'], 'reconfigure 不得因為沒有版號而失敗')
        # ⚠ 斷言 falsy 而非 ``is None``：template 的空版號實測是**空字串**不是
        # NULL（#552 對 stg／prod 都量過），所以 ``or`` 鏈的結果是 ``''``。兩者
        # 對下游是同一件事——``PaasOperatorClient.upgrade_release`` 的閘門是
        # ``if version:``，falsy 就不帶 ``--version``。把 ``None`` 寫死成契約
        # 只會在 seed 用空字串時假紅，測不到任何真實差異。
        self.assertFalse(
            self._sent_version(client),
            '三處全空時不得憑空生出版本；同時也不得拒絕請求')

    def test_main_thread_write_uses_the_same_resolved_version(self):
        """兩個消費端（樂觀 write ／ upgrade_kwargs）必須同源，不得語意分岔。"""
        template = self._template('reconf-write-552', '0.2.1')
        service = self._service(template, chart_version=False)

        client = self._client()
        self._run_update(service, client)

        service.invalidate_recordset()
        self.assertEqual(service.helm_chart_version, self._sent_version(client))


class TestUpgradeRequestOmitsFalsyVersion(TransactionCase):
    """把上一條依賴的前提釘成契約：falsy version ⇒ request 不帶 ``version``。

    ``test_reconfigure_with_no_pin_anywhere_still_sends_none`` 只斷言 resolved
    version 是 falsy，而「falsy ⇒ helm 不會收到 ``--version``」這一步發生在
    ``PaasOperatorClient`` 裡。少了這條，那個斷言就得靠散文說服人。
    """

    def _sent_data(self, version):
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorClient,
        )
        client = PaaSOperatorClient('http://operator.invalid', 'k')
        with patch.object(client, '_request', return_value={}) as request:
            client.upgrade_release(
                namespace='paas-ws-test', release_name='svc',
                chart='oci://example.com/c', version=version,
            )
        return request.call_args.kwargs['data']

    def test_empty_string_version_is_not_forwarded(self):
        self.assertNotIn('version', self._sent_data(''))

    def test_none_version_is_not_forwarded(self):
        self.assertNotIn('version', self._sent_data(None))

    def test_real_version_is_forwarded(self):
        """陽性對照：閘門不是恆假——不然上面兩條零鑑別力。"""
        self.assertEqual(self._sent_data('0.1.5')['version'], '0.1.5')


class TestReconfigureBackgroundWriteBack(TestReconfigureVersionFallback):
    """路徑 4／4：reconfigure 背景執行緒的回寫（缺陷 (A) 的最後一條）。"""

    def test_background_thread_records_the_version_operator_reports(self):
        template = self._template('reconf-bg-552', '2.0.0')
        service = self._service(template, chart_version='2.0.0')

        client = self._client({'revision': 5, 'chart_version': '2.0.2'})
        self._run_update(service, client)

        service.invalidate_recordset()
        self.assertEqual(
            service.helm_revision, 5,
            '前提：背景執行緒的那段 write 真的跑到了（cursor 代理生效）')
        self.assertEqual(
            service.helm_chart_version, '2.0.2',
            '主執行緒寫的是升級**前**的樂觀值；成功後必須被 operator 回報的'
            '實際版本覆蓋，否則 reconfigure 造成的跳版永遠不會進帳本')
        template.invalidate_recordset()
        self.assertEqual(template.helm_chart_version, '2.0.0')

    def test_old_operator_without_the_field_keeps_the_optimistic_value(self):
        template = self._template('reconf-bg-legacy-552', '2.0.0')
        service = self._service(template, chart_version='2.0.0')

        client = self._client({'revision': 5})
        self._run_update(service, client)

        service.invalidate_recordset()
        self.assertEqual(service.helm_revision, 5, '前提：那段 write 真的跑了')
        self.assertEqual(service.helm_chart_version, '2.0.0')
