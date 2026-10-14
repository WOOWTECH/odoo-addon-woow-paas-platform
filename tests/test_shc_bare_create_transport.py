"""C1 bare-create transport regression against real bounded loopback HTTP.

Run: python3 tests/test_shc_bare_create_transport.py -v
CI: .gitea/workflows/shc-bare-create-transport.yml (requests required).
SHC_TRANSPORT_EVIDENCE=1 prints synthetic server receipt counts to stdout.
No Odoo runtime/committed-cursor durability or live provider claim is made.
"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
CLIENT_PATH = ROOT / 'src/services/paas_operator.py'
spec = importlib.util.spec_from_file_location('client_under_test', CLIENT_PATH)
client_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_module)
NAME = 'paas-qa-sm-reliability-op-00000000000000000000000000000001'
ACCOUNT = 'synthetic-account'
RESULT = {'tunnel_id': '8d484941-70ad-493d-a672-c976a7c8c101',
          'tunnel_name': NAME, 'provider_account_id': ACCOUNT}


@contextmanager
def boundary(status=201, mode='response', redirect_target=None):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(2)

        def log_message(self, *args):
            pass
        def respond(self, method):
            if method == 'POST':
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if raw:
                    payload = json.loads(raw)
                    if self.path == '/api/tunnels/bare':
                        assert payload == {'name': NAME, 'expected_account_id': ACCOUNT}
            # Count received mutations, not just the client's API invocation.
            calls.append((method, self.path))
            if mode == 'disconnect':
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            code = status if self.path == '/api/tunnels/bare' else 201
            self.send_response(code)
            if 300 <= code < 400:
                self.send_header('Location', redirect_target or '/replayed-create')
            # A valid-looking identity body must not override a redirect/error.
            body = b'not-json' if mode == 'malformed' else json.dumps(RESULT).encode()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            self.respond('POST')
        def do_GET(self):
            self.respond('GET')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    client = client_module.PaaSOperatorClient(
        'http://127.0.0.1:%s' % server.server_port, 'synthetic-only-key')
    client._session.trust_env = False
    try:
        yield client, calls
    finally:
        client._session.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive(), 'fixture server did not stop'
        if os.environ.get('SHC_TRANSPORT_EVIDENCE') == '1':
            print(json.dumps({
                'fixture': 'synthetic-loopback-http', 'status': status,
                'mode': mode, 'received_requests': calls,
                'listener_closed': True, 'thread_joined': True,
            }), flush=True)


class CreateTransportTest(unittest.TestCase):
    def test_307_308_cannot_replay_unknown_create(self):
        for status in (307, 308):
            with self.subTest(status=status), boundary(status) as (client, calls):
                error = None
                try:
                    client.create_bare_tunnel(NAME, ACCOUNT)
                except client_module.PaaSOperatorError as exc:
                    error = exc
                self.assertEqual(calls, [('POST', '/api/tunnels/bare')],
                                 'unknown first POST was replayed before journal regained control')
                self.assertIsInstance(error, client_module.PaaSOperatorAPIError)
                self.assertEqual(error.detail, 'bare_create_outcome_unknown')

    def test_other_redirects_cannot_become_apparent_authoritative_success(self):
        for status in (301, 302, 303, 304):
            with self.subTest(status=status), boundary(status) as (client, calls):
                with self.assertRaises(client_module.PaaSOperatorAPIError) as caught:
                    client.create_bare_tunnel(NAME, ACCOUNT)
                self.assertEqual(caught.exception.detail, 'bare_create_outcome_unknown')
                self.assertEqual(calls, [('POST', '/api/tunnels/bare')])

    def test_cross_origin_redirect_receives_no_request(self):
        with boundary() as (target, target_calls):
            with boundary(307, redirect_target=target.base_url + '/elsewhere') as (client, calls):
                with self.assertRaises(client_module.PaaSOperatorAPIError):
                    client.create_bare_tunnel(NAME, ACCOUNT)
                self.assertEqual(target_calls, [])
                self.assertEqual(len(calls), 1)

    def test_201_returns_exact_identity_once(self):
        with boundary() as (client, calls):
            self.assertEqual(client.create_bare_tunnel(NAME, ACCOUNT), RESULT)
            self.assertEqual(calls, [('POST', '/api/tunnels/bare')])

    def test_non201_never_becomes_known_success_or_automatic_retry(self):
        for status in (200, 202, 204, 400, 409, 429, 500, 502, 503):
            with self.subTest(status=status), boundary(status) as (client, calls):
                with self.assertRaises(client_module.PaaSOperatorAPIError) as caught:
                    client.create_bare_tunnel(NAME, ACCOUNT)
                self.assertEqual(caught.exception.detail, 'bare_create_outcome_unknown')
                self.assertEqual(calls, [('POST', '/api/tunnels/bare')])

    def test_lost_response_is_not_replayed(self):
        with boundary(mode='disconnect') as (client, calls):
            with self.assertRaises(client_module.PaaSOperatorConnectionError):
                client.create_bare_tunnel(NAME, ACCOUNT)
            self.assertEqual(calls, [('POST', '/api/tunnels/bare')])

    def test_malformed_201_is_error_and_not_replayed(self):
        with boundary(mode='malformed') as (client, calls):
            with self.assertRaises((client_module.PaaSOperatorError, ValueError)):
                client.create_bare_tunnel(NAME, ACCOUNT)
            self.assertEqual(calls, [('POST', '/api/tunnels/bare')])

    def test_init_calls_keep_one_request_and_original_timeout_forwarding(self):
        for kind, timeout in [('odoo', (120, 900)), ('n8n', (120, 600))]:
            with self.subTest(kind=kind), boundary() as (client, calls):
                # Spy wraps the real requests call; it does not fake HTTP.
                with patch.object(client._session, 'request', wraps=client._session.request) as sent:
                    if kind == 'odoo':
                        result = client.init_odoo('synthetic-ns', 'synthetic-release')
                    else:
                        result = client.init_n8n(
                            'synthetic-ns', 'synthetic-release',
                            'fixture@example.invalid', 'synthetic-only-password',
                        )
                self.assertEqual(result, RESULT)
                self.assertEqual(sent.call_count, 1)
                self.assertEqual(sent.call_args.kwargs['timeout'], timeout)
                self.assertNotIn('allow_redirects', sent.call_args.kwargs)
                self.assertEqual(calls, [(
                    'POST', '/api/releases/synthetic-ns/synthetic-release/init/' + kind,
                )])

    def test_other_calls_keep_existing_redirect_contract(self):
        with boundary(307) as (client, calls):
            self.assertEqual(client._request('POST', '/api/tunnels/bare'), RESULT)
            self.assertEqual(calls, [('POST', '/api/tunnels/bare'), ('POST', '/replayed-create')])


if __name__ == '__main__':
    unittest.main()
