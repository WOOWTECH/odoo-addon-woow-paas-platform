"""就緒判定的 VM 分支 fail-closed 回歸測試（#423 W5a）。

**這是本次改動最重要的守線。**

背景（design §4.1 抓到的缺陷）：就緒判定原本在 model 與 controller 各有一份
完全同型的複製品，兩份都是

    all_ready = all(...) if active_pods else (not pods)

—— ``pods`` 為空時回 True。對 VM 型服務這是災難：virt-launcher pod 還沒建出來
（磁碟正在 import）時，服務就被標成 ``running``，租戶拿到一台開不了的機器。

而且**只修 model 層不夠**：租戶只要打開前台服務詳情頁，controller 那份就會把
狀態覆寫回 running。所以下面 model 與 controller 兩層都測。

W5a 的收斂做法是把判定抽成單一事實源 ``CloudService._is_release_ready``，
兩層都呼叫它。#1–#6 直接測該判定，#7–#10 測兩個 controller 呼叫點確實走到
同一條路（＝重構的驗收）。
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase


def _status(release_status='deployed', pods=None, **vm_fields):
    """組一份 operator ``get_status`` 形狀的回應。

    預設模擬「未帶 include_vm」——operator 此時回 ``vm_included: False`` 與
    ``vmis: None``（不是 ``[]``），這個區別正是 fail-closed 的依據。
    """
    body = {
        'release': {'status': release_status, 'revision': 1},
        'pods': pods if pods is not None else [],
        'vm_included': False,
        'vmis': None,
        'datavolumes': None,
        'vmi_retrieval_error': None,
        'datavolume_retrieval_error': None,
    }
    body.update(vm_fields)
    return body


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'W5a Test Workspace',
        })

    def _make_service(self, workload_type, state='deploying'):
        template = self.Template.create({
            'name': f'T-{workload_type}',
            'slug': f'slug-{workload_type}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'chart',
            'helm_chart_version': '1.0.0',
            'workload_type': workload_type,
            'post_deploy_init_type': 'none',
        })
        return self.Service.create({
            'name': f'svc-{workload_type}',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': state,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-test',
        })


class TestReadinessDecision(_Base):
    """#1–#6：單一事實源本身的判定（model 層）。"""

    # ---- VM：fail-closed ------------------------------------------------

    def test_1_vm_with_no_vmis_stays_not_ready(self):
        """🔴 核心回歸：VM 型 + 沒有 VMI → **絕不可**判定就緒。

        這正是 design 抓到的 fail-open：容器型邏輯在 pods 為空時回 True，
        套到 VM 上就是「磁碟還在 import 就說開好了」。
        """
        svc = self._make_service('vm')

        ready, detail = svc._is_release_ready(
            _status(vm_included=True, vmis=[])
        )

        self.assertFalse(ready, "VM 沒有 VMI 時必須 fail-closed")
        self.assertIn('no VMI', detail)

    def test_1b_vm_with_vmis_none_stays_not_ready(self):
        """漏帶 include_vm（vmis=None、vm_included=False）同樣不可就緒。

        這是程式錯誤，但寧可服務卡住也不能誤報就緒——且 detail 會明說原因，
        讓它可診斷而不是靜默。
        """
        svc = self._make_service('vm')

        ready, detail = svc._is_release_ready(_status())

        self.assertFalse(ready)
        self.assertIn('include_vm', detail)

    def test_2_vm_running_and_ready_is_ready(self):
        svc = self._make_service('vm')

        ready, _ = svc._is_release_ready(_status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': True}],
        ))

        self.assertTrue(ready)

    def test_3_vm_running_but_not_ready_condition(self):
        """phase=Running 但 Ready condition 為 False → 未就緒。

        兩者是獨立訊號：QEMU 起來（Running）遠早於 guest 可用（Ready）。
        """
        svc = self._make_service('vm')

        ready, detail = svc._is_release_ready(_status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': False}],
        ))

        self.assertFalse(ready)
        self.assertIn('not ready', detail)

    def test_4_vm_pending_phase_is_not_ready(self):
        svc = self._make_service('vm')

        ready, _ = svc._is_release_ready(_status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Pending', 'ready': False}],
        ))

        self.assertFalse(ready)

    def test_vmi_retrieval_error_is_fail_closed(self):
        """VMI 查詢失敗 → 狀態未知 → 不可就緒。"""
        svc = self._make_service('vm')

        ready, detail = svc._is_release_ready(_status(
            vm_included=True, vmis=None,
            vmi_retrieval_error='Forbidden',
        ))

        self.assertFalse(ready)
        self.assertIn('Forbidden', detail)

    def test_datavolume_error_does_not_block_a_ready_vm(self):
        """⚠ 語意分流：DataVolume 出錯**不**擋就緒。

        DataVolume 只供進度顯示。讓它擋住一台已經 Ready 的 VM，等於把
        「進度條壞了」升級成「服務不可用」。
        """
        svc = self._make_service('vm')

        ready, _ = svc._is_release_ready(_status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': True}],
            datavolume_retrieval_error='CRD missing',
        ))

        self.assertTrue(ready, "DataVolume 錯誤不該擋住已 Ready 的 VM")

    # ---- container：characterization（釘住現行行為，本輪不修）-----------

    def test_5_container_with_no_pods_keeps_current_behaviour(self):
        """容器型 + 無 pod → **維持現行的 True**（characterization）。

        這是既知的 fail-open，v1 刻意不修（design §4.1）：容器服務有一個合法
        短暫狀態是「release 已 deployed 但 pod 還沒被建出來」，改 fail-closed
        會讓**所有現存租戶服務**多卡一輪。此測試的用途是「改動沒有波及容器型」
        的證據，不是為這個行為背書。
        """
        svc = self._make_service('container')

        ready, _ = svc._is_release_ready(_status(pods=[]))

        self.assertTrue(ready)

    def test_6_container_with_ready_pods_is_ready(self):
        svc = self._make_service('container')

        ready, _ = svc._is_release_ready(_status(
            pods=[{'name': 'p', 'phase': 'Running', 'ready': '1/1'}],
        ))

        self.assertTrue(ready)

    def test_container_with_unready_pod_is_not_ready(self):
        svc = self._make_service('container')

        ready, _ = svc._is_release_ready(_status(
            pods=[{'name': 'p', 'phase': 'Running', 'ready': '0/1'}],
        ))

        self.assertFalse(ready)

    def test_container_ignores_vm_fields_entirely(self):
        """容器型不看 VM 欄位——即使 operator 意外回了 vmi 錯誤也一樣。"""
        svc = self._make_service('container')

        ready, _ = svc._is_release_ready(_status(
            pods=[{'name': 'p', 'phase': 'Running', 'ready': '1/1'}],
            vm_included=True, vmis=[], vmi_retrieval_error='boom',
        ))

        self.assertTrue(ready)


class TestIncludeVmFlagIsSent(_Base):
    """漏帶 include_vm 會讓 VM 服務永遠卡 deploying —— 用旗標本身寫斷言。"""

    def test_needs_vm_status_true_only_for_vm(self):
        self.assertTrue(self._make_service('vm')._needs_vm_status())
        self.assertFalse(self._make_service('container')._needs_vm_status())

    def test_model_poll_passes_include_vm_for_vm_service(self):
        svc = self._make_service('vm')
        client = MagicMock()
        client.get_status.return_value = _status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': True}],
        )

        svc._poll_and_update_status(client)

        self.assertTrue(
            client.get_status.call_args.kwargs['include_vm'],
            "VM 型服務 poll 時必須帶 include_vm=True",
        )

    def test_model_poll_omits_include_vm_for_container_service(self):
        """容器型不帶——省掉 operator 端兩次無謂的 CR 查詢。"""
        svc = self._make_service('container')
        client = MagicMock()
        client.get_status.return_value = _status(
            pods=[{'name': 'p', 'phase': 'Running', 'ready': '1/1'}],
        )

        svc._poll_and_update_status(client)

        self.assertFalse(client.get_status.call_args.kwargs['include_vm'])


class TestModelPollTransitions(_Base):
    """model 層 poll 的端到端狀態轉移。"""

    def test_vm_without_vmis_stays_deploying(self):
        """🔴 端到端版的 #1：整條 poll 跑完，state 必須還是 deploying。"""
        svc = self._make_service('vm')
        client = MagicMock()
        client.get_status.return_value = _status(vm_included=True, vmis=[])

        svc._poll_and_update_status(client)

        self.assertEqual(svc.state, 'deploying')

    def test_vm_with_ready_vmi_becomes_running(self):
        svc = self._make_service('vm')
        client = MagicMock()
        client.get_status.return_value = _status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': True}],
        )

        svc._poll_and_update_status(client)

        self.assertEqual(svc.state, 'running')

    def test_import_progress_is_stored_for_display(self):
        """進度落地供卡片顯示；未就緒時正是最有用的時候。"""
        svc = self._make_service('vm')
        client = MagicMock()
        client.get_status.return_value = _status(
            vm_included=True, vmis=[],
            datavolumes=[{'name': 'dv', 'phase': 'ImportInProgress',
                          'progress': '42.31%'}],
        )

        svc._poll_and_update_status(client)

        self.assertEqual(svc.import_progress, '42.31%')
        self.assertEqual(svc.state, 'deploying', "進度不得影響狀態機")

    def test_import_progress_cleared_when_absent(self):
        svc = self._make_service('vm')
        svc.import_progress = '50.0%'
        client = MagicMock()
        client.get_status.return_value = _status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': True}],
        )

        svc._poll_and_update_status(client)

        self.assertFalse(svc.import_progress)


class TestControllerLayerUsesSharedDecision(_Base):
    """#7–#10：controller 的兩個呼叫點必須走同一條判定。

    這四條同時是「controller 委派 model」重構的驗收：只要有人把判定邏輯抄回
    controller，VM 分支就會漏掉，而這些測試會紅。
    """

    def _controller(self):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController
        return PaasController()

    def _run_update(self, svc, status_body):
        """跑 controller 的 _update_service_status（mock 掉 operator client）。"""
        ctrl = self._controller()
        client = MagicMock()
        client.get_status.return_value = status_body

        module = 'odoo.addons.woow_paas_platform.controllers.paas'
        # ⚠ 不能用 patch(f'{module}.request')：odoo 的 request 是 werkzeug
        # LocalProxy，mock 在 __enter__ 期就會存取它而觸發
        # "RuntimeError: object is not bound"。改用 new= 直接塞替身，
        # 讓 mock 不去 introspect 原物件。
        fake_request = MagicMock()
        fake_request.env = self.env
        with patch(f'{module}.get_paas_operator_client', return_value=client), \
                patch(f'{module}.request', new=fake_request):
            ctrl._update_service_status(svc)
        return client

    def test_7_controller_vm_without_vmis_stays_deploying(self):
        """🔴 M4 守線：只修 model 層時，前台一 poll 就把 import 中的 VM
        標成 running。這條測試就是那個回歸的探針。"""
        svc = self._make_service('vm')

        self._run_update(svc, _status(vm_included=True, vmis=[]))

        self.assertEqual(svc.state, 'deploying')

    def test_8_controller_sends_include_vm(self):
        """第二個呼叫點（workspace 服務清單）與詳情頁走同一個方法——
        旗標沒帶到的話 #7 會假綠，故直接對旗標斷言。"""
        svc = self._make_service('vm')

        client = self._run_update(svc, _status(vm_included=True, vmis=[]))

        self.assertTrue(client.get_status.call_args.kwargs['include_vm'])

    def test_9_controller_container_behaviour_unchanged(self):
        """characterization：容器型 + 無 pod 仍翻 running（現行行為）。

        重構本身會改變 controller 的執行路徑，所以這條「不改」的承諾更需要
        測試守住。
        """
        svc = self._make_service('container')

        self._run_update(svc, _status(pods=[]))

        self.assertEqual(svc.state, 'running')

    def test_10_controller_vm_ready_becomes_running(self):
        svc = self._make_service('vm')

        self._run_update(svc, _status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': True}],
        ))

        self.assertEqual(svc.state, 'running')

    def test_controller_and_model_agree(self):
        """兩層對同一份 status 必須給出同一個答案——單一事實源的直接斷言。"""
        cases = [
            _status(vm_included=True, vmis=[]),
            _status(vm_included=True,
                    vmis=[{'name': 'v', 'phase': 'Running', 'ready': True}]),
            _status(vm_included=True,
                    vmis=[{'name': 'v', 'phase': 'Running', 'ready': False}]),
            _status(),
        ]
        for body in cases:
            svc_model = self._make_service('vm')
            svc_ctrl = self._make_service('vm')

            model_ready, _ = svc_model._is_release_ready(body)
            self._run_update(svc_ctrl, body)
            ctrl_ready = svc_ctrl.state == 'running'

            self.assertEqual(
                model_ready, ctrl_ready,
                f"兩層對同一份 status 判定不一致：{body}",
            )
