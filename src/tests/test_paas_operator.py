"""Tests for PaaS Operator HTTP Client."""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase


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
