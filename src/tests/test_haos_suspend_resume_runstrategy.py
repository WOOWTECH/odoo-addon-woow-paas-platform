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
        return self.Service.create({
            'name': 'svc-haos',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': state,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-haos',
        })


class TestSuspendResumeUsesScaleRelease(_Base):
    """#1/#2：VM 型沿用既有 scale_release 路徑（分流在 operator 端）。"""

    def _patch_client(self, svc):
        client = MagicMock()
        return patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client',
            return_value=client,
        ), client

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
        svc = self.Service.create({
            'name': 'svc-container',
            'workspace_id': self.workspace.id,
            'template_id': container_tpl.id,
            'state': 'suspended',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-container',
        })

        values = self._built_values(svc)

        self.assertNotIn('vm', values)


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
