"""VM 型服務的 suspend/resume 與 runStrategy 同源（#423 W5a）。

兩件事：

1. **suspend/resume 走既有 scale_release 路徑**（W5a.6）——VM 沒有 replicas，
   分流由 operator 端負責（W4.3 把 replicas=0 映射成 runStrategy Halted）。
   平台這邊不需要特別處理，但那是個**假設**，用測試釘住。

2. 🔴 **helm values 的 runStrategy 必須與服務狀態同源**（W5a.4）。
   suspend 走的是 operator 的外科式 ``kubectl patch vm``（不動 helm release），
   所以 live 物件與 helm stored manifest 會漂移。下一次 helm upgrade 若 render
   出 chart 預設的 ``Always``，就會**把停權中（欠費）的 VM 喚醒**。

   這個失效是沉默的——沒有錯誤、沒有告警，只有一台本該停掉的機器繼續跑，
   然後變成計費爭議。所以必須有測試守住。
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase
from ..models.cloud_service import _internal_provision


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'RunStrategy Test Workspace',
        })
        self.template = self.Template.create({
            'name': 'HAOS',
            'slug': 'haos',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'oci://jcr-prod.woowtech.io/woow-paas-docker-local/haos',
            'helm_chart_version': '0.1.0',
            'workload_type': 'vm',
            'post_deploy_init_type': 'none',
            'default_port': 8123,
        })

    def _service(self, state='running'):
        return _internal_provision(self.Service).create({
            'name': 'svc-haos',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': state,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-haos',
        })


def _patch_operator_client():
    """patch model 層取得 operator client 的入口，回 (patcher, mock_client)。"""
    client = MagicMock()
    return patch(
        'odoo.addons.woow_paas_platform.services.paas_operator'
        '.get_paas_operator_client',
        return_value=client,
    ), client


class TestSuspendResumeUsesScaleRelease(_Base):
    """#1/#2：VM 型沿用既有 scale_release 路徑（分流在 operator 端）。"""

    def _patch_client(self, svc):
        return _patch_operator_client()

    def test_1_suspend_calls_scale_release_zero_and_records_pre_state(self):
        svc = self._service(state='running')
        patcher, client = self._patch_client(svc)

        with patcher:
            svc._do_suspend()

        client.scale_release.assert_called_once()
        self.assertEqual(client.scale_release.call_args.kwargs['replicas'], 0)
        self.assertEqual(svc.state, 'suspended')
        self.assertEqual(svc.pre_suspend_state, 'running')

    def test_2_resume_calls_scale_release_one_and_restores_state(self):
        svc = self._service(state='running')
        patcher, client = self._patch_client(svc)

        with patcher:
            svc._do_suspend()
            svc._do_resume()

        self.assertEqual(client.scale_release.call_args.kwargs['replicas'], 1)
        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.pre_suspend_state)


class TestRunStrategyIsSourcedFromState(_Base):
    """#3/#4：🔴 防 helm upgrade 喚醒停權中的 VM。"""

    def _built_values(self, svc):
        """取服務目前會送給 helm 的 values。"""
        return svc._assemble_deploy_values()['values']

    def test_3_suspended_service_renders_halted(self):
        """停權中的服務組出來的 values 必須是 Halted。

        少了這條，一次無關的 helm upgrade 就會把欠費租戶的 VM 重新開機。
        """
        svc = self._service(state='suspended')

        values = self._built_values(svc)

        self.assertEqual(
            values.get('vm', {}).get('runStrategy'), 'Halted',
            "停權中的 VM，helm values 必須是 Halted——否則下次 upgrade 會把它喚醒",
        )

    def test_4_running_service_renders_always(self):
        svc = self._service(state='running')

        values = self._built_values(svc)

        self.assertEqual(values.get('vm', {}).get('runStrategy'), 'Always')

    def test_deploying_service_renders_always(self):
        """非 suspended 的任何狀態都是 Always（只有停權才停機）。"""
        svc = self._service(state='deploying')

        values = self._built_values(svc)

        self.assertEqual(values.get('vm', {}).get('runStrategy'), 'Always')

    def test_container_service_gets_no_run_strategy(self):
        """容器型不該被塞 vm.runStrategy——chart 不認得，且會污染 values diff。"""
        container_tpl = self.Template.create({
            'name': 'Container App',
            'slug': 'container-app',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'chart',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
        })
        svc = _internal_provision(self.Service).create({
            'name': 'svc-container',
            'workspace_id': self.workspace.id,
            'template_id': container_tpl.id,
            'state': 'suspended',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-container',
        })

        values = self._built_values(svc)

        self.assertNotIn('vm', values)


class TestHaltedGuardSurvivesStateRewrite(_Base):
    """🔴 MAJOR-2：守衛不能只看 ``state == 'suspended'``。

    起因：當時的 reconfigure 路徑（``controllers/paas.py``）**先**把 state 寫成
    ``upgrading``、之後才組裝 values。守衛只讀 state 的話，屆時已讀不到
    ``suspended`` → 組出 ``Always`` → 以 ``reset_values=True`` 送出 →
    **停權中（欠費）的 VM 被喚醒**。

    那個時間窗現在不存在了：#482 起三條 upgrade 路徑對停權中的服務把 state 留在
    ``suspended``，#1647 起 reconfigure 也改成先組裝全量 values、後寫 state（同步預檢
    與背景 upgrade 共用同一組）。仍然成立、本 class 守的不變式是「**守衛不依賴呼叫順序，
    也不依賴 state 這一個欄位**」——state 不是 ``suspended``、服務卻還在停權的資料確實
    存在（#482 之前被 upgrade 改寫過 state 的存量、復權失敗寫成 ``error`` 而保留
    ``pre_suspend_state`` 的服務），將來也可能再出現改寫 state 的新路徑。
    """

    def test_halted_guard_holds_after_state_rewritten_to_upgrading(self):
        """state 已被改寫、但服務仍處於停權（MAJOR-2 當時 reconfigure 的時序）。"""
        svc = self._service(state='running')
        patcher, _client = _patch_operator_client()
        with patcher:
            svc._do_suspend()

        self.assertEqual(svc.state, 'suspended')
        self.assertEqual(svc.pre_suspend_state, 'running')

        # 某條路徑在組 values 之前改寫了 state（當年是 reconfigure 自己）。
        svc.write({'state': 'upgrading'})

        values = svc._assemble_deploy_values()['values']

        self.assertEqual(
            values.get('vm', {}).get('runStrategy'), 'Halted',
            "state 被改寫成 upgrading 後，守衛仍須認得這是停權中的 VM——"
            "否則下一次 helm upgrade 會把欠費租戶的機器喚醒",
        )

    def test_is_halted_reads_either_signal(self):
        """兩個訊號取聯集：任一為真都算停機。"""
        svc = self._service(state='suspended')
        self.assertTrue(svc._is_halted(), "state=suspended 應為 halted")

        svc.write({'state': 'upgrading', 'pre_suspend_state': 'running'})
        self.assertTrue(svc._is_halted(), "pre_suspend_state 有值應為 halted")

        svc.write({'state': 'running', 'pre_suspend_state': False})
        self.assertFalse(svc._is_halted())

    def _reconfigure_and_capture_values(self, svc):
        """以 controller 入口 ``_update_service`` 跑一次 reconfigure，回傳送給 operator 的 values。

        ⚠ 不能對 client.upgrade_release 斷言：_update_service 把實際的 upgrade 丟進
        daemon thread（paas.py:_bg_upgrade），該執行緒用自己的 cursor、也不吃主執行緒的
        patch。但 **values 是在主執行緒先組好才交給背景執行緒的**（背景執行緒無 ORM，不能
        自己組），所以 spy 攔到的就是真正送出去的內容。spy 記的是**最後一次**組裝：之後若
        又有人在寫完 state 才重組一次，攔到的會是那一次。
        """
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 2}
        captured = {}
        real_assemble = type(svc)._assemble_deploy_values

        # #1647：reconfigure 在寫入**之前**以 ``values=`` 組好全量 values（同步預檢與背景
        # upgrade 共用同一組），所以 spy 要原樣轉發參數。
        def _spy(self_svc, *args, **kwargs):
            result = real_assemble(self_svc, *args, **kwargs)
            captured['values'] = result['values']
            return result

        module = 'odoo.addons.woow_paas_platform.controllers.paas'
        fake_request = MagicMock()
        fake_request.env = self.env
        with patch(f'{module}.get_paas_operator_client', return_value=client), \
                patch(f'{module}.request', new=fake_request), \
                patch.object(type(svc), '_assemble_deploy_values', _spy):
            result = PaasController()._update_service(svc, values={}, version=None)

        self.assertTrue(result.get('success'), result)
        self.assertIn(
            'values', captured,
            "_update_service 應在主執行緒組裝 values（若這裡沒攔到，代表組裝"
            "被搬進背景執行緒，本測試需重寫）",
        )
        # 停權中的服務不做 compute 配額的同步預檢（upgrade 後會被 scale 回 0，#1647）。
        client.upgrade_preflight.assert_not_called()
        return captured['values']

    def test_update_service_full_path_sends_halted_for_suspended_vm(self):
        """🔴 走 ``_update_service`` **全路徑**，而非只對 helper 斷言。

        從 controller 入口跑進去，斷言**真正送給 operator 的 values** 是 Halted，而且
        reconfigure 之後服務仍然是 ``suspended``（#482）。

        這一條守的是「入口到送出」整段的接線（合併使用者設定、以 ``values=`` 組裝、交給
        背景 upgrade 的那一組）。它**不**鑑別 ``_is_halted`` 是否只看 state——#1647 起
        組裝發生在任何 state 寫入之前，此刻 state 本來就是 ``suspended``；那個退化由下一條
        ``…_when_state_was_rewritten`` 守。
        """
        svc = self._service(state='running')
        patcher, _c = _patch_operator_client()
        with patcher:
            svc._do_suspend()
        self.assertEqual(svc.state, 'suspended')

        values = self._reconfigure_and_capture_values(svc)

        self.assertEqual(
            values.get('vm', {}).get('runStrategy'), 'Halted',
            "停權中的 VM 改設定：送給 operator 的 values 必須是 Halted，"
            "否則 helm upgrade（reset_values）會把它喚醒",
        )
        self.assertEqual(svc.state, 'suspended', "reconfigure 不得把停權中的服務帶離 suspended")

    def test_update_service_full_path_sends_halted_when_state_was_rewritten(self):
        """🔴 全路徑版的 MAJOR-2：state 已經不是 ``suspended``，服務其實還在停權。

        這種資料確實存在——#482 之前，停權中的服務被 upgrade 改寫 state
        （suspended → upgrading → poll → running），``pre_suspend_state`` 還留著：帳面上
        執行中，VM 其實是停的、訂閱也沒在付費。租戶對它按「儲存設定」時，守衛只看
        ``state == 'suspended'`` 就會組出 ``Always``、把欠費的 VM 喚醒。

        ``_is_halted`` 退回只看 state 時，這一條會紅（上一條不會）。
        """
        for rewritten in ('running', 'upgrading'):
            with self.subTest(state=rewritten):
                svc = self._service(state='running')
                patcher, _c = _patch_operator_client()
                with patcher:
                    svc._do_suspend()
                svc.write({'state': rewritten})
                self.assertEqual(svc.pre_suspend_state, 'running', '前提：停權訊號還在')

                values = self._reconfigure_and_capture_values(svc)

                self.assertEqual(
                    values.get('vm', {}).get('runStrategy'), 'Halted',
                    "state 被改寫成 %s、但 pre_suspend_state 仍在：送給 operator 的 values "
                    "必須是 Halted，否則停權中的 VM 會被 helm upgrade 喚醒" % rewritten,
                )
                self.assertEqual(
                    svc.state, 'suspended',
                    "reconfigure 之後不得停在 upgrading（poll 會把它收斂成 running＝帳面復權）")

    def test_resumed_service_renders_always_again(self):
        """resume 後 pre_suspend_state 被清空 → 回到 Always。

        少了這條，守衛會變成「一旦停過權就永遠 Halted」。
        """
        svc = self._service(state='running')
        patcher, _client = _patch_operator_client()
        with patcher:
            svc._do_suspend()
            svc._do_resume()

        values = svc._assemble_deploy_values()['values']

        self.assertEqual(values.get('vm', {}).get('runStrategy'), 'Always')


class TestRollbackRefusesSuspended(_Base):
    """🔴 MAJOR-1：rollback 是繞過 runStrategy 守衛的第二條路。

    ``_rollback_service`` 直呼 ``client.rollback_release()``，**全程不經
    ``_assemble_deploy_values``**，所以 W5a.4 的守衛對它零覆蓋。而 helm
    rollback 重放的是 **stored manifest**（``runStrategy: Always``），停權卻
    是 operator 外科式 ``kubectl patch vm`` 改 **live 物件**——兩者本就不同步。
    結果是欠費停權的 VM 被喚醒並繼續計費。
    """

    def _controller(self):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController
        return PaasController()

    def test_suspended_service_cannot_rollback(self):
        svc = self._service(state='suspended')
        client = MagicMock()

        module = 'odoo.addons.woow_paas_platform.controllers.paas'
        fake_request = MagicMock()
        fake_request.env = self.env
        with patch(f'{module}.get_paas_operator_client', return_value=client), \
                patch(f'{module}.request', new=fake_request):
            result = self._controller()._rollback_service(svc, revision=1)

        self.assertFalse(result['success'])
        self.assertIn('suspended', result['error'])
        client.rollback_release.assert_not_called()

    def test_running_service_can_still_rollback(self):
        """只擋 suspended——正常服務的 rollback 不受影響。"""
        svc = self._service(state='running')
        client = MagicMock()
        client.rollback_release.return_value = {'message': 'ok'}

        module = 'odoo.addons.woow_paas_platform.controllers.paas'
        fake_request = MagicMock()
        fake_request.env = self.env
        with patch(f'{module}.get_paas_operator_client', return_value=client), \
                patch(f'{module}.request', new=fake_request):
            self._controller()._rollback_service(svc, revision=1)

        client.rollback_release.assert_called_once()

    def test_pending_and_deleting_still_blocked(self):
        """既有的兩個拒絕狀態不得因本次改動而放行。"""
        for state in ('pending', 'deleting'):
            svc = self._service(state=state)
            client = MagicMock()

            module = 'odoo.addons.woow_paas_platform.controllers.paas'
            fake_request = MagicMock()
            fake_request.env = self.env
            with patch(f'{module}.get_paas_operator_client', return_value=client), \
                    patch(f'{module}.request', new=fake_request):
                result = self._controller()._rollback_service(svc, revision=1)

            self.assertFalse(result['success'], f"{state} 應被擋")
            client.rollback_release.assert_not_called()


class TestHaosStorageClassInjection(_Base):
    """W5a.5：haos 的 storageClass 注入點。

    chart 端此鍵 **required**（woow-k3s 有兩個 default StorageClass，留空會讓
    DataVolume 落點不確定、可能掉到 node-local 的 local-path），所以平台這邊
    絕不可傳空字串。
    """

    def test_haos_maps_to_flat_persistence_storage_class(self):
        values = self.Service._inject_storage_class(
            {}, 'haos', 'longhorn-delete',
        )

        self.assertEqual(
            values['persistence']['storageClass'], 'longhorn-delete',
        )

    def test_injected_value_is_never_empty(self):
        """空值必須是不可能發生的——chart 會 render 失敗，症狀離根因很遠。"""
        values = self.Service._inject_storage_class(
            {}, 'haos', 'longhorn-delete',
        )

        self.assertTrue(values['persistence']['storageClass'])
