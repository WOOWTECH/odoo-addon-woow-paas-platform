"""Tests for PaaS Operator HTTP Client."""
import json
from unittest.mock import MagicMock, patch

import requests

from odoo.tests.common import TransactionCase


class SimulatedHTTPTransport:
    """只模擬 HTTP boundary 的 connect/read inactivity，不耗真實牆鐘時間。"""

    def __init__(self, connect_seconds=0, read_seconds=0, payload=None):
        self.connect_seconds = connect_seconds
        self.read_seconds = read_seconds
        self.payload = payload if payload is not None else {'success': True}
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        timeout = kwargs['timeout']
        connect, read = timeout if isinstance(timeout, tuple) else (timeout, timeout)
        if self.connect_seconds > connect:
            raise requests.exceptions.ConnectTimeout('simulated connect inactivity')
        if self.read_seconds > read:
            raise requests.exceptions.ReadTimeout('simulated read inactivity')
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(self.payload).encode()
        return response


class TestPaaSOperatorClient(TransactionCase):
    """Test cases for PaaSOperatorClient HTTP client."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        # Import here to avoid import issues during module loading
        from ..services.paas_operator import (
            PaaSOperatorClient,
            PaaSOperatorError,
            PaaSOperatorConnectionError,
            PaaSOperatorTimeoutError,
            PaaSOperatorAPIError,
        )
        self.PaaSOperatorClient = PaaSOperatorClient
        self.PaaSOperatorError = PaaSOperatorError
        self.PaaSOperatorConnectionError = PaaSOperatorConnectionError
        self.PaaSOperatorTimeoutError = PaaSOperatorTimeoutError
        self.PaaSOperatorAPIError = PaaSOperatorAPIError

        self.base_url = 'http://paas-operator:8000'
        self.api_key = 'test-api-key'
        self.client = PaaSOperatorClient(
            base_url=self.base_url,
            api_key=self.api_key,
        )

    def test_init_odoo_accepts_slow_response_with_dedicated_read_budget(self):
        """#1152：800s contract simulation，舊 HELM 120s wiring 必須逾時。"""
        transport = SimulatedHTTPTransport(read_seconds=800)
        with patch('requests.Session.request', side_effect=transport):
            result = self.client.init_odoo('paas-ws-a1b2c3d4', 'test-odoo')
        self.assertTrue(result['success'])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0]['timeout'], (120, 900))
        self.assertEqual(transport.calls[0]['method'], 'POST')
        self.assertEqual(
            transport.calls[0]['url'],
            self.base_url + '/api/releases/paas-ws-a1b2c3d4/test-odoo/init/odoo',
        )

    def test_init_odoo_connect_timeout_is_not_extended(self):
        """read 可等 900s，但連線失敗仍沿用 120s，且不自動 retry。"""
        transport = SimulatedHTTPTransport(connect_seconds=121)
        # ConnectTimeout 同時繼承 ConnectionError；保留既有 except 順序與分類。
        with patch('requests.Session.request', side_effect=transport):
            with self.assertRaises(self.PaaSOperatorConnectionError) as caught:
                self.client.init_odoo('paas-ws-a1b2c3d4', 'test-odoo')
        self.assertEqual(caught.exception.detail, 'simulated connect inactivity')
        self.assertEqual(len(transport.calls), 1)

    def test_init_odoo_read_timeout_still_propagates_without_retry(self):
        """超出 read inactivity 預算仍傳遞既有 timeout 例外，不代表遠端取消。"""
        transport = SimulatedHTTPTransport(read_seconds=901)
        with patch('requests.Session.request', side_effect=transport):
            with self.assertRaises(self.PaaSOperatorTimeoutError) as caught:
                self.client.init_odoo('paas-ws-a1b2c3d4', 'test-odoo')
        self.assertEqual(caught.exception.detail, 'simulated read inactivity')
        self.assertEqual(len(transport.calls), 1)

    def test_init_odoo_not_ready_response_is_not_reclassified(self):
        """operator 的 wait_ready 失敗 payload 原樣交回，不新增 retry 或分類。"""
        payload = {'success': False, 'error': '[wait_ready] Odoo not ready'}
        transport = SimulatedHTTPTransport(read_seconds=680, payload=payload)
        with patch('requests.Session.request', side_effect=transport):
            result = self.client.init_odoo('paas-ws-a1b2c3d4', 'test-odoo')
        self.assertEqual(result, payload)
        self.assertEqual(len(transport.calls), 1)

    def test_other_operations_keep_scalar_timeouts(self):
        """init 的 tuple 不得連帶改變 HELM／reset／一般讀取的 scalar 預算。"""
        transport = SimulatedHTTPTransport()
        with patch('requests.Session.request', side_effect=transport):
            self.client.uninstall_release('paas-ws-a1b2c3d4', 'test-odoo')
            self.client.get_release('paas-ws-a1b2c3d4', 'test-odoo')
            self.client.health_check()
            self.client.reset_release_config('paas-ws-a1b2c3d4', 'test-odoo')
        self.assertEqual([call['timeout'] for call in transport.calls], [120, 30, 10, 75])

    def test_init_n8n_uses_dedicated_connect_read_budget(self):
        """#1257 Q31：n8n init 不再共用 HELM 120，改用專屬 (connect 120, read 600)。"""
        transport = SimulatedHTTPTransport()
        with patch('requests.Session.request', side_effect=transport):
            self.client.init_n8n('paas-ws-a1b2c3d4', 'test-n8n', 'test@example.invalid', 'test-only')
        self.assertEqual([call['timeout'] for call in transport.calls], [(120, 600)])

    def test_client_initialization(self):
        """Test client initializes with correct headers."""
        self.assertEqual(self.client.base_url, self.base_url)
        self.assertEqual(self.client.api_key, self.api_key)
        self.assertEqual(
            self.client._session.headers['X-API-Key'],
            self.api_key,
        )
        self.assertEqual(
            self.client._session.headers['Content-Type'],
            'application/json',
        )

    def test_client_strips_trailing_slash(self):
        """Test client strips trailing slash from base URL."""
        client = self.PaaSOperatorClient(
            base_url='http://operator:8000/',
            api_key='key',
        )
        self.assertEqual(client.base_url, 'http://operator:8000')

    @patch('requests.Session.request')
    def test_health_check_success(self, mock_request):
        """Test successful health check."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"status": "healthy", "helm_version": "v3.13.3"}'
        mock_response.json.return_value = {
            'status': 'healthy',
            'helm_version': 'v3.13.3',
        }
        mock_request.return_value = mock_response

        result = self.client.health_check()

        self.assertEqual(result['status'], 'healthy')
        self.assertEqual(result['helm_version'], 'v3.13.3')
        mock_request.assert_called_once()

    @patch('requests.Session.request')
    def test_create_namespace_success(self, mock_request):
        """Test successful namespace creation."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.content = b'{"message": "Namespace created"}'
        mock_response.json.return_value = {'message': 'Namespace created'}
        mock_request.return_value = mock_response

        result = self.client.create_namespace(
            namespace='paas-ws-a1b2c3d4',
            cpu_limit='2',
            memory_limit='4Gi',
            storage_limit='20Gi',
        )

        self.assertIn('message', result)
        call_kwargs = mock_request.call_args
        self.assertEqual(call_kwargs.kwargs['method'], 'POST')
        self.assertIn('/api/namespaces', call_kwargs.kwargs['url'])

    @patch('requests.Session.request')
    def test_install_release_success(self, mock_request):
        """Test successful release installation."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.content = json.dumps({
            'name': 'test-release',
            'namespace': 'paas-ws-a1b2c3d4',
            'revision': 1,
            'status': 'deployed',
        }).encode()
        mock_response.json.return_value = {
            'name': 'test-release',
            'namespace': 'paas-ws-a1b2c3d4',
            'revision': 1,
            'status': 'deployed',
        }
        mock_request.return_value = mock_response

        result = self.client.install_release(
            namespace='paas-ws-a1b2c3d4',
            release_name='test-release',
            chart='nginx',
            version='15.0.0',
            values={'replicaCount': 2},
        )

        self.assertEqual(result['name'], 'test-release')
        self.assertEqual(result['status'], 'deployed')
        call_kwargs = mock_request.call_args
        self.assertEqual(call_kwargs.kwargs['method'], 'POST')
        self.assertIn('/api/releases', call_kwargs.kwargs['url'])

    @patch('requests.Session.request')
    def test_get_release_success(self, mock_request):
        """Test successful get release."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = json.dumps({
            'name': 'test-release',
            'namespace': 'paas-ws-a1b2c3d4',
            'revision': 2,
            'status': 'deployed',
        }).encode()
        mock_response.json.return_value = {
            'name': 'test-release',
            'namespace': 'paas-ws-a1b2c3d4',
            'revision': 2,
            'status': 'deployed',
        }
        mock_request.return_value = mock_response

        result = self.client.get_release('paas-ws-a1b2c3d4', 'test-release')

        self.assertEqual(result['name'], 'test-release')
        self.assertEqual(result['revision'], 2)

    @patch('requests.Session.request')
    def test_upgrade_release_success(self, mock_request):
        """Test successful release upgrade."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = json.dumps({
            'name': 'test-release',
            'revision': 3,
            'status': 'deployed',
        }).encode()
        mock_response.json.return_value = {
            'name': 'test-release',
            'revision': 3,
            'status': 'deployed',
        }
        mock_request.return_value = mock_response

        result = self.client.upgrade_release(
            namespace='paas-ws-a1b2c3d4',
            release_name='test-release',
            values={'replicaCount': 5},
            version='16.0.0',
        )

        self.assertEqual(result['revision'], 3)
        call_kwargs = mock_request.call_args
        self.assertEqual(call_kwargs.kwargs['method'], 'PATCH')

    @patch('requests.Session.request')
    def test_uninstall_release_success(self, mock_request):
        """Test successful release uninstallation."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"message": "Release uninstalled"}'
        mock_response.json.return_value = {'message': 'Release uninstalled'}
        mock_request.return_value = mock_response

        result = self.client.uninstall_release('paas-ws-a1b2c3d4', 'test-release')

        self.assertIn('message', result)
        call_kwargs = mock_request.call_args
        self.assertEqual(call_kwargs.kwargs['method'], 'DELETE')

    @patch('requests.Session.request')
    def test_rollback_release_success(self, mock_request):
        """Test successful release rollback."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"message": "Rollback successful"}'
        mock_response.json.return_value = {'message': 'Rollback successful'}
        mock_request.return_value = mock_response

        result = self.client.rollback_release(
            namespace='paas-ws-a1b2c3d4',
            release_name='test-release',
            revision=1,
        )

        self.assertIn('message', result)
        call_kwargs = mock_request.call_args
        self.assertEqual(call_kwargs.kwargs['method'], 'POST')
        self.assertIn('/rollback', call_kwargs.kwargs['url'])

    @patch('requests.Session.request')
    def test_get_revisions_success(self, mock_request):
        """Test successful get revision history."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = json.dumps({
            'revisions': [
                {'revision': 1, 'status': 'superseded'},
                {'revision': 2, 'status': 'deployed'},
            ]
        }).encode()
        mock_response.json.return_value = {
            'revisions': [
                {'revision': 1, 'status': 'superseded'},
                {'revision': 2, 'status': 'deployed'},
            ]
        }
        mock_request.return_value = mock_response

        result = self.client.get_revisions('paas-ws-a1b2c3d4', 'test-release')

        self.assertEqual(len(result['revisions']), 2)

    @patch('requests.Session.request')
    def test_get_status_success(self, mock_request):
        """Test successful get release status."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = json.dumps({
            'release': {'name': 'test-release', 'status': 'deployed'},
            'pods': [{'name': 'pod-1', 'phase': 'Running'}],
        }).encode()
        mock_response.json.return_value = {
            'release': {'name': 'test-release', 'status': 'deployed'},
            'pods': [{'name': 'pod-1', 'phase': 'Running'}],
        }
        mock_request.return_value = mock_response

        result = self.client.get_status('paas-ws-a1b2c3d4', 'test-release')

        self.assertEqual(result['release']['status'], 'deployed')
        self.assertEqual(len(result['pods']), 1)

    @staticmethod
    def _status_ok_response():
        mock_response = MagicMock()
        mock_response.status_code = 200
        body = {'release': {'name': 'test-release', 'status': 'deployed'}, 'pods': []}
        mock_response.content = json.dumps(body).encode()
        mock_response.json.return_value = body
        return mock_response

    @patch('requests.Session.request')
    def test_get_status_sends_include_workloads(self, mock_request):
        """#1647：include_workloads 以 query string 送出（operator 端是 FastAPI Query）。"""
        mock_request.return_value = self._status_ok_response()

        self.client.get_status('paas-ws-a1b2c3d4', 'test-release', include_workloads=True)

        sent = mock_request.call_args.kwargs
        self.assertEqual(sent['method'], 'GET')
        self.assertEqual(
            sent['url'],
            self.base_url + '/api/releases/paas-ws-a1b2c3d4/test-release/status')
        self.assertEqual(sent['params'], {'include_workloads': 'true'},
                         '只帶 include_workloads 時不得順帶送出 include_vm')
        self.assertIsNone(sent['json'], '旗標不得放進 body')

        # 兩個旗標互不影響。
        self.client.get_status('paas-ws-a1b2c3d4', 'test-release',
                               include_vm=True, include_workloads=True)
        self.assertEqual(mock_request.call_args.kwargs['params'],
                         {'include_vm': 'true', 'include_workloads': 'true'})

    @patch('requests.Session.request')
    def test_get_status_omits_params_by_default(self, mock_request):
        """#1647：不帶旗標時 wire 與以前完全相同（沒有 query string）。"""
        mock_request.return_value = self._status_ok_response()

        self.client.get_status('paas-ws-a1b2c3d4', 'test-release')
        self.assertIsNone(mock_request.call_args.kwargs['params'])

        self.client.get_status('paas-ws-a1b2c3d4', 'test-release', include_vm=True)
        self.assertEqual(mock_request.call_args.kwargs['params'], {'include_vm': 'true'},
                         '只帶 include_vm 時不得多出 include_workloads')

    @patch('requests.Session.request')
    def test_api_error_handling(self, mock_request):
        """Test API error handling."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {'detail': 'Invalid request'}
        mock_request.return_value = mock_response

        with self.assertRaises(self.PaaSOperatorAPIError) as context:
            self.client.health_check()

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn('Invalid request', context.exception.detail)

    @patch('requests.Session.request')
    def test_not_found_error(self, mock_request):
        """Test 404 error handling."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.json.return_value = {'detail': 'Release not found'}
        mock_request.return_value = mock_response

        with self.assertRaises(self.PaaSOperatorAPIError) as context:
            self.client.get_release('paas-ws-a1b2c3d4', 'missing')

        self.assertEqual(context.exception.status_code, 404)

    @patch('requests.Session.request')
    def test_status_not_found_raises_without_duplicate_error_log(self, mock_request):
        """#2044: caller classifies the expected delete/status race."""
        from ..services import paas_operator as client_module

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.json.return_value = {'detail': 'Release not found'}
        mock_request.return_value = mock_response

        with patch.object(client_module, '_logger') as mock_logger:
            with self.assertRaises(self.PaaSOperatorAPIError) as context:
                self.client.get_status('paas-ws-a1b2c3d4', 'missing')

        self.assertEqual(context.exception.status_code, 404)
        mock_logger.error.assert_not_called()
        mock_logger.debug.assert_called()

    @patch('requests.Session.request')
    def test_status_server_error_still_logs_error(self, mock_request):
        """#2044: only the explicit missing-release outcome is quiet."""
        from ..services import paas_operator as client_module

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.return_value = {'detail': 'Helm failed'}
        mock_request.return_value = mock_response

        with patch.object(client_module, '_logger') as mock_logger:
            with self.assertRaises(self.PaaSOperatorAPIError) as context:
                self.client.get_status('paas-ws-a1b2c3d4', 'broken')

        self.assertEqual(context.exception.status_code, 500)
        mock_logger.error.assert_called_once()

    @patch('requests.Session.request')
    def test_connection_error_handling(self, mock_request):
        """Test connection error handling."""
        from requests.exceptions import ConnectionError

        mock_request.side_effect = ConnectionError('Connection refused')

        with self.assertRaises(self.PaaSOperatorConnectionError):
            self.client.health_check()

    @patch('requests.Session.request')
    def test_timeout_error_handling(self, mock_request):
        """Test timeout error handling."""
        from requests.exceptions import Timeout

        mock_request.side_effect = Timeout('Request timed out')

        with self.assertRaises(self.PaaSOperatorTimeoutError):
            self.client.health_check()

    @patch('requests.Session.request')
    def test_empty_response_handling(self, mock_request):
        """Test handling of empty response body."""
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.content = b''
        mock_request.return_value = mock_response

        result = self.client._request('DELETE', '/api/test')

        self.assertEqual(result, {})

    @patch('requests.Session.request')
    def test_error_parsing_with_text_response(self, mock_request):
        """Test error parsing when response is not JSON."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = 'Internal Server Error'
        mock_response.json.side_effect = ValueError('Not JSON')
        mock_request.return_value = mock_response

        with self.assertRaises(self.PaaSOperatorAPIError) as context:
            self.client.health_check()

        self.assertIn('Internal Server Error', context.exception.detail)


class TestSecurityAccessClientMethods(TransactionCase):
    """sync_tunnel_routes / check_route_availability HTTP contract."""

    def _make_client(self):
        from ..services.paas_operator import PaaSOperatorClient
        return PaaSOperatorClient(base_url='http://op:8000', api_key='k')

    def test_sync_tunnel_routes_contract(self):
        """sync_tunnel_routes builds the declarative PUT route-sync request."""
        client = self._make_client()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {
                'tunnel_id': 'tun-1',
                'routes': [{
                    'subdomain': 'a',
                    'hostname': 'a.woowtech.io',
                    'dns_record_id': 'rec-a',
                }],
            }
            result = client.sync_tunnel_routes(
                'tun-1',
                routes=[{
                    'subdomain': 'a',
                    'service_url': 'http://localhost:80',
                    'http_host_header': True,
                }],
                comment='paas-sa-x-y',
            )
        mock_req.assert_called_once_with(
            'PUT', '/api/tunnels/tun-1/routes',
            data={
                'routes': [{
                    'subdomain': 'a',
                    'service_url': 'http://localhost:80',
                    'http_host_header': True,
                }],
                'comment': 'paas-sa-x-y',
            },
            timeout=120,
        )
        self.assertEqual(result['routes'][0]['dns_record_id'], 'rec-a')

    def test_sync_tunnel_routes_omits_comment_when_absent(self):
        """sync_tunnel_routes leaves comment out of the body when not provided."""
        client = self._make_client()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'tunnel_id': 'tun-1', 'routes': []}
            client.sync_tunnel_routes(
                'tun-1',
                routes=[{'subdomain': 'a', 'service_url': 'http://localhost:80',
                         'http_host_header': True}],
            )
        mock_req.assert_called_once_with(
            'PUT', '/api/tunnels/tun-1/routes',
            data={
                'routes': [{'subdomain': 'a', 'service_url': 'http://localhost:80',
                            'http_host_header': True}],
            },
            timeout=120,
        )

    def test_check_route_availability_contract(self):
        """check_route_availability builds the GET availability query."""
        client = self._make_client()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {
                'subdomain': 'a',
                'hostname': 'a.woowtech.io',
                'available': True,
            }
            result = client.check_route_availability('a')
        mock_req.assert_called_once_with(
            'GET', '/api/routes/availability', params={'subdomain': 'a'},
        )
        self.assertTrue(result['available'])


class TestTunnelOwnershipClaimWire(TransactionCase):
    """#531：三個 tunnel 寫入端點的 client 都必須把 ownership claim 送出去。

    為什麼三支一起釘（不是只釘 issue 標題那一支）：這是 missed-forward class ——
    「上游手上有值、組裝點沒轉下去」。三個 method 共用同一把平台級 API key、打同
    一個 sink，只轉一個等於沒轉。而漏轉的症狀是 operator 端**靜默**當成
    claim-missing（stage 1 放行），輸出與「有送且驗過」**完全一樣** ⇒ 沒有任何
    執行期訊號，只有這種 contract test 擋得住。

    ⚠ 載體本身也是契約的一部分，不只是「有沒有送」：``/routes`` 與 ``/config``
    走 ``data=``（JSON body），``DELETE`` 走 ``params=``（query string）。下面逐支
    斷言的是**綁定位置**，不是「參數有出現」—— #769 的事故形狀正是兩端對同一個
    欄位的綁定位置不一致，而兩邊的測試都不會紅。
    """

    CLAIM = 'paas-sa-ws1-abc123'

    def _make_client(self):
        from ..services.paas_operator import PaaSOperatorClient
        return PaaSOperatorClient(base_url='http://op:8000', api_key='k')

    def test_sync_tunnel_routes_sends_claim_in_body(self):
        client = self._make_client()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'tunnel_id': 'tun-1', 'routes': []}
            client.sync_tunnel_routes(
                'tun-1',
                routes=[{'subdomain': 'a', 'service_url': 'http://localhost:80',
                         'http_host_header': True}],
                comment='paas-sa-ws1-comment',
                tunnel_name=self.CLAIM,
            )
        kwargs = mock_req.call_args.kwargs
        # 先斷言「鍵在」再斷言「值對」：直接 kwargs['data']['tunnel_name'] 在漏轉時
        # 是 KeyError（error 不是 failure），紅得比較難讀。實測過這個差別。
        self.assertIn(
            'tunnel_name', kwargs['data'],
            f"/routes 沒有把 claim 轉下去，實得 body 鍵：{sorted(kwargs['data'])}")
        self.assertEqual(kwargs['data']['tunnel_name'], self.CLAIM)
        self.assertIsNone(
            kwargs.get('params'),
            '/routes 的 claim 必須在 body，不得漂到 query string')
        # claim 與 comment 是**兩個獨立欄位**，不是同一個值換名字：comment 是 DNS
        # record 的 traceability stamp（operator 只寫不讀）、claim 是授權判準。
        # 合併會讓「改 stamp 格式」等於「改安全姿態」。
        self.assertEqual(kwargs['data']['comment'], 'paas-sa-ws1-comment')
        self.assertNotEqual(kwargs['data']['tunnel_name'],
                            kwargs['data']['comment'])

    def test_configure_tunnel_sends_claim_in_body(self):
        client = self._make_client()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {}
            client.configure_tunnel(
                'tun-1',
                hostname='a.woowtech.io',
                service_url='http_status:503',
                tunnel_name=self.CLAIM,
            )
        mock_req.assert_called_once_with(
            'PUT', '/api/tunnels/tun-1/config',
            data={
                'hostname': 'a.woowtech.io',
                'service_url': 'http_status:503',
                'tunnel_name': self.CLAIM,
            },
            timeout=120,
        )

    def test_delete_tunnel_sends_claim_as_query_param(self):
        """DELETE 走 query param，不是 body —— 刻意與姊妹端點不一致。

        理由見 client 的 docstring：帶 body 的 DELETE 會被中間層丟掉，而丟掉的
        後果是**靜默退化成沒帶 claim**（翻到 stage 2 就變成 teardown 全被拒、
        tunnel 拆不掉還在計費）。這條斷言把載體本身釘住 —— 若有人「為了一致」
        把它改成 body，這裡就紅。
        """
        client = self._make_client()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {}
            client.delete_tunnel('tun-1', tunnel_name=self.CLAIM)
        mock_req.assert_called_once_with(
            'DELETE', '/api/tunnels/tun-1',
            params={'tunnel_name': self.CLAIM},
            timeout=120,
        )

    def test_all_three_omit_the_claim_when_the_caller_has_none(self):
        """負分支：claim 為空時三支都**不得**送出空值。

        送 ``tunnel_name=''`` 與不送，在 operator 端都算 claim-missing —— 但空字串
        會讓「這個呼叫端升版了沒」在 body／access log 上分不出來，而那正是判斷
        「可以翻預設值了沒」唯一的訊號。

        （這條是 mutation-resistant 的負分支鎖：把 client 裡的 ``if tunnel_name:``
        guard 拿掉，這支就紅。只鎖正分支的話，guard 被改壞不會有任何測試變色。）
        """
        client = self._make_client()
        cases = (
            ('sync_tunnel_routes', dict(routes=[])),
            ('configure_tunnel', dict(hostname='a.woowtech.io',
                                      service_url='http://localhost:80')),
            ('delete_tunnel', {}),
        )
        for method, kwargs in cases:
            with self.subTest(method=method):
                with patch.object(client, '_request') as mock_req:
                    mock_req.return_value = {}
                    getattr(client, method)('tun-1', tunnel_name=False, **kwargs)
                sent = mock_req.call_args.kwargs
                self.assertNotIn(
                    'tunnel_name', sent.get('data') or {},
                    '空 claim 不得進 body')
                self.assertIsNone(
                    sent.get('params'), '空 claim 不得進 query param')


class TestStructuredErrorDetailCrossesTheLayer(TransactionCase):
    """#531：operator 的結構化 detail（dict）必須完整抵達 Odoo 這一側。

    為什麼需要這條：operator 端把 ``ErrorResponse.detail`` 放寬成 ``str | dict``
    之後，``_parse_error`` 的**型別標注仍寫 ``-> str``**，但實作是
    ``error_data.get('detail', str(error_data))`` —— detail 是 dict 時它回的就是
    dict，一路帶進 ``PaaSOperatorAPIError.detail``。標注與事實不符的地方沒有任何
    一側的測試會經過：operator 的 pytest 看不到這個 client，平台的測試又都把
    client mock 掉。這正是 #769 的形狀（契約跨在兩個部署單元之間）。
    """

    def _client(self):
        from ..services.paas_operator import PaaSOperatorClient
        return PaaSOperatorClient(base_url='http://op:8000', api_key='k')

    @staticmethod
    def _response(status_code, payload):
        resp = MagicMock()
        resp.status_code = status_code
        resp.content = json.dumps(payload).encode()
        resp.json.return_value = payload
        return resp

    def test_dict_detail_arrives_as_a_dict_not_its_repr(self):
        """斷言**型別**與**鍵值**，不是「字串裡看得到那些字」。

        ``str(dict)`` 會滿足一大票天真的斷言（``assertIn('tunnel_id', detail)``
        對 ``"{'tunnel_id': ...}"`` 也成立），那種測試在 ``_parse_error`` 真的
        退化成回字串時**照樣綠**。所以先鎖 ``isinstance(..., dict)``，再取鍵值。
        """
        from ..services.paas_operator import PaaSOperatorAPIError

        detail = {
            'error': 'tunnel_ownership_mismatch',
            'tunnel_id': 'tun-victim-0001',
            'operation': 'sync_routes',
            'claimed_name': 'paas-sa-attacker-def456',
        }
        client = self._client()
        with patch.object(
            client._session, 'request',
            return_value=self._response(403, {'detail': detail,
                                              'error_type': 'HTTPException'}),
        ):
            with self.assertRaises(PaaSOperatorAPIError) as ctx:
                client.sync_tunnel_routes(
                    'tun-victim-0001', routes=[], tunnel_name='whatever')

        exc = ctx.exception
        self.assertEqual(exc.status_code, 403)
        self.assertIsInstance(
            exc.detail, dict,
            '結構化 detail 必須以 dict 抵達；退化成 str 會讓下游只能做字串比對')
        self.assertEqual(exc.detail['error'], 'tunnel_ownership_mismatch')
        self.assertEqual(exc.detail['tunnel_id'], 'tun-victim-0001')
        self.assertEqual(exc.detail['operation'], 'sync_routes')
        # 渲染進 message 之後人還要看得懂是哪一種拒絕（維運只看得到這一行）。
        self.assertIn('tunnel_ownership_mismatch', exc.message)

    def test_structured_detail_is_never_an_empty_dict(self):
        """空 dict 是 falsy —— 消費端的 ``e.detail or e.message`` 會靜默改用 message。

        這條把那個形狀直接演一遍：**空 dict 會讓結構化資訊無聲消失**，所以
        operator 端組 detail 的地方有 assert 保證三個鍵必有值（見
        ``_ownership_error``）。這裡守的是「一旦哪天真的漏了，這個退化長什麼樣」，
        讓不變式在兩側都留下痕跡。
        """
        from ..services.paas_operator import PaaSOperatorAPIError

        client = self._client()
        with patch.object(
            client._session, 'request',
            return_value=self._response(403, {'detail': {}}),
        ):
            with self.assertRaises(PaaSOperatorAPIError) as ctx:
                client.delete_tunnel('tun-1', tunnel_name='x')

        exc = ctx.exception
        self.assertEqual(
            exc.detail or exc.message, exc.message,
            '空 dict 是 falsy：`e.detail or e.message` 會退回 message —— '
            '這就是結構化 detail 不得為空 dict 的理由')

    def test_string_detail_still_works(self):
        """陽性對照：既有的字串 detail 不得因為型別放寬而改變行為。"""
        from ..services.paas_operator import PaaSOperatorAPIError

        client = self._client()
        with patch.object(
            client._session, 'request',
            return_value=self._response(
                500, {'detail': 'Failed to sync tunnel routes: boom'}),
        ):
            with self.assertRaises(PaaSOperatorAPIError) as ctx:
                client.delete_tunnel('tun-1', tunnel_name='x')

        self.assertIsInstance(ctx.exception.detail, str)
        self.assertIn('boom', ctx.exception.detail)


class TestGetPaaSOperatorClient(TransactionCase):
    """Test get_paas_operator_client helper function."""

    def test_returns_none_when_not_configured(self):
        """Test returns None when operator URL not configured."""
        from ..services.paas_operator import get_paas_operator_client

        # Ensure config params are empty
        IrConfigParam = self.env['ir.config_parameter'].sudo()
        IrConfigParam.set_param('woow_paas_platform.operator_url', '')
        IrConfigParam.set_param('woow_paas_platform.operator_api_key', '')

        client = get_paas_operator_client(self.env)

        self.assertIsNone(client)

    def test_returns_none_when_api_key_missing(self):
        """Test returns None when API key not configured."""
        from ..services.paas_operator import get_paas_operator_client

        IrConfigParam = self.env['ir.config_parameter'].sudo()
        IrConfigParam.set_param('woow_paas_platform.operator_url', 'http://localhost:8000')
        IrConfigParam.set_param('woow_paas_platform.operator_api_key', '')

        client = get_paas_operator_client(self.env)

        self.assertIsNone(client)

    def test_returns_client_when_configured(self):
        """Test returns client when properly configured."""
        from ..services.paas_operator import get_paas_operator_client, PaaSOperatorClient

        IrConfigParam = self.env['ir.config_parameter'].sudo()
        IrConfigParam.set_param('woow_paas_platform.operator_url', 'http://localhost:8000')
        IrConfigParam.set_param('woow_paas_platform.operator_api_key', 'test-key')

        client = get_paas_operator_client(self.env)

        self.assertIsInstance(client, PaaSOperatorClient)
        self.assertEqual(client.base_url, 'http://localhost:8000')
        self.assertEqual(client.api_key, 'test-key')


class TestSecurityAccessAccessClientMethods(TransactionCase):
    """sync_access_app / list_identity_providers HTTP contract."""

    def _make_client(self):
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorClient,
        )
        return PaaSOperatorClient(base_url='http://op:8000', api_key='k')

    def test_sync_access_contract(self):
        client = self._make_client()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {
                'app_id': 'app-1', 'policy_ids': ['pol-1'],
                'allowed_idps': ['idp-g']}
            result = client.sync_access_app({
                'enabled': True, 'canonical_name': 'paas-sa-x-y',
                'app_name': 'gw', 'primary_hostname': 'paas-sa-x-y.woowtech.io',
                'protected_hostnames': ['paas-sa-x-y.woowtech.io'],
                'session_duration': '8h', 'login_idp_types': ['google'],
                'policies': [], 'existing_app_id': None,
                'existing_policy_ids': []})
        mock_req.assert_called_once_with(
            'PUT', '/api/access/sync', data={
                'enabled': True, 'canonical_name': 'paas-sa-x-y',
                'app_name': 'gw', 'primary_hostname': 'paas-sa-x-y.woowtech.io',
                'protected_hostnames': ['paas-sa-x-y.woowtech.io'],
                'session_duration': '8h', 'login_idp_types': ['google'],
                'policies': [], 'existing_app_id': None,
                'existing_policy_ids': []},
            timeout=120)
        self.assertEqual(result['app_id'], 'app-1')

    def test_list_identity_providers_contract(self):
        client = self._make_client()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'identity_providers': [
                {'id': 'idp-g', 'name': 'G', 'type': 'google'}]}
            result = client.list_identity_providers()
        mock_req.assert_called_once_with(
            'GET', '/api/access/identity-providers')
        self.assertEqual(result['identity_providers'][0]['type'], 'google')

    def test_review_access_app_contract(self):
        client = self._make_client()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {
                'status': 'unique', 'app_id': 'app-1', 'match_count': 1}
            result = client.review_access_app('owned app', 'owned.example.test')
        mock_req.assert_called_once_with(
            'POST', '/api/access/review-application',
            data={'app_name': 'owned app',
                  'primary_hostname': 'owned.example.test'},
            timeout=120)
        self.assertEqual(result['app_id'], 'app-1')
