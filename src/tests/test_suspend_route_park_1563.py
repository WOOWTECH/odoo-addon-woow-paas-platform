"""#1563：欠費停權時把服務的對外網址指到共用的停權說明頁，復權時指回服務。

operator 端（``POST /api/releases/{ns}/{name}/route/park``／``/unpark``）負責改 tunnel
規則；本檔釘住平台端的呼叫時機與失敗語意：

* **停權**：scale 0 成功之後才 park；park 的任何失敗（含舊 operator 的 404）只記 log，
  停權照樣成功——提交點是 scale 0，說明頁只是給訪客看的。
* **復權**：scale 1 之後 unpark；失敗要 **fail-loud**（寫 ``error`` 與說明），並且保留
  ``pre_suspend_state`` ⇒ ``_is_halted()`` 仍為真，生命週期 cron 下一輪會再走一次復權。
  舊 operator 的 404 不算失敗（它不可能 park 過任何東西）。
* **範圍**：只有有對外網址的 Cloud Service（``_build_expose_config``）；沒 ingress／沒
  subdomain 的服務完全不呼叫。
* **#482 停權中的 redeploy／reconfigure** 不碰 route：upgrade 端點沒有 expose，route 只在
  首裝建立——停權中的服務被 upgrade 後網址仍是說明頁。
"""
import logging
from unittest.mock import MagicMock, call, patch

from odoo.tests.common import TransactionCase

from ..models.cloud_service import _internal_provision
from ..services.paas_operator import PaaSOperatorAPIError, PaaSOperatorConnectionError
from .publication_receipt_fixture import install_publication_receipts

_CLIENT_SEAM = (
    'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
)
NS = 'paas-ws-1563test'
REL = 'svc-1563'
SUB = 'paas-cs-1563test-abcd'


def _patch_client(client=None):
    client = install_publication_receipts(client or MagicMock())
    client.upgrade_release.return_value = {'revision': 2}
    client.park_release_route.return_value = {'status': 'parked'}
    client.unpark_release_route.return_value = {'status': 'unparked'}
    return patch(_CLIENT_SEAM, return_value=client), client


def _route_calls(client):
    """只取 scale／park／unpark 三種呼叫（依發生順序）。"""
    return [c for c in client.mock_calls
            if c[0] in ('scale_release', 'park_release_route', 'unpark_release_route')]


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        Template = self.env['woow_paas_platform.cloud_app_template']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '1563 Workspace',
        })
        self.template = Template.create({
            'name': 'Container 1563',
            'slug': 'container-1563',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'oci://example.com/container',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'ingress_enabled': True,
        })

    def _service(self, state='running', subdomain=SUB, template=None, **extra):
        vals = {
            'name': 'svc-1563',
            'workspace_id': self.workspace.id,
            'template_id': (template or self.template).id,
            'state': state,
            'helm_namespace': NS,
            'helm_release_name': REL,
            'subdomain': subdomain,
        }
        vals.update(extra)
        return _internal_provision(self.Service).create(vals)

    def _suspended(self, **kw):
        svc = self._service(**kw)
        patcher, _client = _patch_client()
        with patcher:
            svc._do_suspend()
        self.assertEqual(svc.state, 'suspended')
        return svc


class TestSuspendParksPublicRoute1563(_Base):

    def test_park_after_scale_down(self):
        svc = self._service()
        patcher, client = _patch_client()
        with patcher:
            svc._do_suspend()
        self.assertEqual(svc.state, 'suspended')
        self.assertEqual(svc.pre_suspend_state, 'running')
        self.assertEqual(_route_calls(client), [
            call.scale_release(REL, NS, replicas=0),
            call.park_release_route(NS, REL, SUB),
        ])

    def test_park_rejected_still_suspends(self):
        """說明頁沒部署（409）／Cloudflare 錯誤（502）：停權照樣成功，不寫錯誤。"""
        for status_code, detail in (
                (409, {'error_code': 'suspended_page_unavailable', 'message': 'no page'}),
                (502, {'error_code': 'cloudflare_error', 'message': 'cf down'})):
            svc = self._service(name=f'svc-1563-{status_code}',
                                subdomain=f'{SUB}-{status_code}')
            client = MagicMock()
            client.park_release_route.side_effect = PaaSOperatorAPIError(
                'API error', status_code=status_code, detail=detail)
            patcher, client = _patch_client(client)
            with patcher:
                svc._do_suspend()
            self.assertEqual(svc.state, 'suspended')
            self.assertFalse(svc.error_message)
            client.park_release_route.assert_called_once()

    def test_old_operator_404_still_suspends(self):
        svc = self._service()
        client = MagicMock()
        client.park_release_route.side_effect = PaaSOperatorAPIError(
            'API error: Not Found', status_code=404, detail='Not Found')
        patcher, client = _patch_client(client)
        with patcher, self.assertLogs(
                'odoo.addons.woow_paas_platform.models.cloud_service',
                level=logging.INFO) as logs:
            svc._do_suspend()
        self.assertEqual(svc.state, 'suspended')
        self.assertFalse(svc.error_message)
        self.assertTrue(any('older image' in line for line in logs.output))

    def test_unexpected_park_error_still_suspends(self):
        svc = self._service()
        client = MagicMock()
        client.park_release_route.side_effect = PaaSOperatorConnectionError('down')
        patcher, client = _patch_client(client)
        with patcher:
            svc._do_suspend()
        self.assertEqual(svc.state, 'suspended')
        self.assertFalse(svc.error_message)

    def test_scale_failure_does_not_park(self):
        svc = self._service()
        client = MagicMock()
        client.scale_release.side_effect = PaaSOperatorAPIError('boom', status_code=500)
        patcher, client = _patch_client(client)
        with patcher:
            svc._do_suspend()
        self.assertEqual(svc.state, 'error')
        client.park_release_route.assert_not_called()

    def test_no_public_url_no_park(self):
        """沒有對外網址（template 關 ingress、或沒有 subdomain）就不呼叫。"""
        no_ingress = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Container 1563 no ingress',
            'slug': 'container-1563-noingress',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'oci://example.com/container',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'ingress_enabled': False,
        })
        for svc in (self._service(template=no_ingress, name='svc-1563-a'),
                    self._service(subdomain=False, name='svc-1563-b')):
            patcher, client = _patch_client()
            with patcher:
                svc._do_suspend()
            self.assertEqual(svc.state, 'suspended')
            client.scale_release.assert_called_once()
            client.park_release_route.assert_not_called()


class TestResumeUnparksPublicRoute1563(_Base):

    def test_unpark_after_scale_up(self):
        svc = self._suspended()
        patcher, client = _patch_client()
        with patcher:
            svc._do_resume()
        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.pre_suspend_state)
        expose = {'enabled': True, 'subdomain': SUB}
        self.assertEqual(svc._build_expose_config(), expose)
        self.assertEqual(_route_calls(client), [
            call.scale_release(REL, NS, replicas=1),
            call.unpark_release_route(NS, REL, expose),
        ])

    def test_unpark_failure_is_loud_and_retried_by_the_cron(self):
        """🔴 付了錢的服務網址仍是說明頁＝必須看得到，而且 cron 要能重試。"""
        svc = self._suspended()
        client = MagicMock()
        client.unpark_release_route.side_effect = PaaSOperatorAPIError(
            'API error', status_code=409,
            detail={'error_code': 'route_target_unresolved',
                    'message': 'Cannot determine which service it should point back to'})
        patcher, client = _patch_client(client)
        with patcher:
            svc._do_resume()
        self.assertEqual(svc.state, 'error')
        self.assertIn('Cannot determine which service', svc.error_message)
        self.assertEqual(svc.pre_suspend_state, 'running')
        self.assertTrue(
            svc._is_halted(),
            "生命週期 cron 分支 2 只對 _is_halted() 為真的服務呼叫 _do_resume——"
            "清掉 pre_suspend_state 就再也不會重試",
        )

        # 下一輪 cron：scale 1（冪等）＋unpark 成功 ⇒ 復權完成。
        patcher, client = _patch_client()
        with patcher:
            svc._do_resume()
        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.pre_suspend_state)
        client.unpark_release_route.assert_called_once()

    def test_unexpected_unpark_error_is_loud(self):
        svc = self._suspended()
        client = MagicMock()
        client.unpark_release_route.side_effect = PaaSOperatorConnectionError(
            'Failed to connect to PaaS Operator service')
        patcher, client = _patch_client(client)
        with patcher:
            svc._do_resume()
        self.assertEqual(svc.state, 'error')
        self.assertTrue(svc._is_halted())

    def test_old_operator_404_is_not_a_failure(self):
        """平台先上、operator 還是舊 image：舊 operator 不可能 park 過，復權照常完成。"""
        svc = self._suspended()
        client = MagicMock()
        client.unpark_release_route.side_effect = PaaSOperatorAPIError(
            'API error: Not Found', status_code=404, detail='Not Found')
        patcher, client = _patch_client(client)
        with patcher:
            svc._do_resume()
        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.pre_suspend_state)

    def test_scale_failure_does_not_unpark(self):
        svc = self._suspended()
        client = MagicMock()
        client.scale_release.side_effect = PaaSOperatorAPIError('boom', status_code=500)
        patcher, client = _patch_client(client)
        with patcher:
            svc._do_resume()
        self.assertEqual(svc.state, 'error')
        client.unpark_release_route.assert_not_called()

    def test_no_public_url_no_unpark(self):
        svc = self._suspended(subdomain=False)
        patcher, client = _patch_client()
        with patcher:
            svc._do_resume()
        self.assertEqual(svc.state, 'running')
        client.unpark_release_route.assert_not_called()

    def test_disabled_or_not_parked_is_success(self):
        for status in ('disabled', 'not_parked', 'not_found'):
            svc = self._suspended(name=f'svc-1563-{status}',
                                  subdomain=f'{SUB}-{status.replace("_", "-")}')
            patcher, client = _patch_client()
            client.unpark_release_route.return_value = {'status': status}
            with patcher:
                svc._do_resume()
            self.assertEqual(svc.state, 'running', status)


class TestHaltedUpgradeKeepsRouteParked1563(_Base):
    """#482 的停權中 upgrade 路徑不得把 route 改回服務。

    route 只在首裝（``install_release`` 帶 ``expose``）建立；redeploy 的 upgrade 分支與
    reconfigure 都走 ``upgrade_release``（operator 的 PATCH 沒有任何 Cloudflare 動作）。
    """

    def _assert_route_untouched(self, client):
        client.unpark_release_route.assert_not_called()
        client.park_release_route.assert_not_called()
        client.install_release.assert_not_called()
        for c in client.upgrade_release.call_args_list:
            self.assertNotIn('expose', c.kwargs)

    def test_suspended_redeploy_does_not_unpark(self):
        svc = self._suspended()
        patcher, client = _patch_client()
        with patcher:
            svc.action_redeploy()
        self.assertEqual(svc.state, 'suspended')
        client.upgrade_release.assert_called_once()
        self._assert_route_untouched(client)

    def test_suspended_reconfigure_does_not_unpark(self):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        svc = self._suspended()
        patcher, client = _patch_client()
        module = 'odoo.addons.woow_paas_platform.controllers.paas'
        client_path = (
            'odoo.addons.woow_paas_platform.services.paas_operator.PaaSOperatorClient')
        fake_request = MagicMock()
        fake_request.env = self.env

        class _SyncThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                self._target()

        with patcher, \
                patch(f'{module}.get_paas_operator_client', return_value=client), \
                patch(f'{module}.request', new=fake_request), \
                patch(client_path, return_value=client), \
                patch(f'{module}.threading.Thread', _SyncThread):
            PaasController()._update_service(svc, values={}, version=None)
        self.assertEqual(svc.state, 'suspended')
        client.upgrade_release.assert_called_once()
        self._assert_route_untouched(client)


class TestOperatorClientRoutePark1563(TransactionCase):
    """client 送出的 wire 形狀（operator 端 RouteParkRequest／RouteUnparkRequest）。"""

    def _client(self):
        from ..services.paas_operator import PaaSOperatorClient, ROUTE_PARK_TIMEOUT
        client = PaaSOperatorClient('http://operator.test', 'k')
        client._request = MagicMock(return_value={'status': 'parked'})
        return client, ROUTE_PARK_TIMEOUT

    def test_park_body_carries_only_the_subdomain(self):
        client, timeout = self._client()
        client.park_release_route(NS, REL, SUB)
        client._request.assert_called_once_with(
            'POST', f'/api/releases/{NS}/{REL}/route/park',
            data={'subdomain': SUB}, timeout=timeout)

    def test_unpark_body_carries_the_install_time_expose(self):
        client, timeout = self._client()
        expose = {'enabled': True, 'subdomain': SUB, 'service_name': REL, 'service_port': 80}
        client.unpark_release_route(NS, REL, expose)
        client._request.assert_called_once_with(
            'POST', f'/api/releases/{NS}/{REL}/route/unpark',
            data={'expose': expose}, timeout=timeout)
