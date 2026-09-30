"""使用 fake clock 重現串行 GET 超過 20 秒的 discovery，零外部副作用。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from omni_msg.bridge import Store
from omni_msg import registration
from omni_msg.registration import Registrar
from omni_msg.transport import MessageError


class ScanBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory / 'messages.sqlite3', {})
        self.clock = 0.0
        self.graph = {
            sid: {'id': sid, 'parent_session_id': parent, 'archived': False,
                  'permission_level': 4, 'runner_id': 'runner-1',
                  'workspace': None if parent else '/workspace'}
            for sid, parent in [('root_a', None), ('child_a', 'root_a'),
                                ('root_b', None), ('child_b', 'root_b')]
        }
        self.bridge = Mock(store=self.store, transport=SimpleNamespace(
            url='http://127.0.0.1', token='fixture-only'))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def registrar(self, **kwargs):
        return Registrar({
            'team-a': {'session_id': 'root_a', 'workspace': '/workspace'},
            'team-b': {'session_id': 'root_b', 'workspace': '/workspace'},
        }, 5, self.bridge, self.directory, 6778, **kwargs)

    def get_json(self, url, token, *, timeout, max_bytes):
        # 每個 GET 固定花 4 秒；實際走 _get／_remaining／_discover，8 次 GET 共 32 秒。
        self.clock += min(4, timeout)
        if timeout < 4:
            raise MessageError('transport_unknown', 502, unavailable=True)
        path = urlsplit(url).path
        sid = path.removeprefix('/v1/sessions/').split('/')[0]
        if path.endswith('/child_sessions'):
            return {'data': [{'id': key} for key, row in self.graph.items()
                             if row['parent_session_id'] == sid], 'has_more': False}
        return self.graph[sid]

    def refresh(self, registrar):
        with patch('omni_msg.registration.time.monotonic', side_effect=lambda: self.clock), \
                patch('omni_msg.registration.request_json', side_effect=self.get_json), \
                patch.object(registrar, '_notify'):
            registrar.refresh()
        return json.loads((self.directory / 'registrations.json').read_text())

    def test_default_budget_allows_discovery_longer_than_20_seconds(self):
        registrar = self.registrar()
        status = self.refresh(registrar)
        self.assertTrue(status['ready'], status)
        self.assertEqual(len(status['members']), 4)
        self.assertEqual(self.clock, 32)
        actors, expires = self.bridge.replace_actors.call_args.args
        self.assertEqual(len(actors), 4)
        self.assertEqual(expires - self.clock, 93)

    def test_explicit_20_second_budget_still_fails_closed(self):
        registrar = self.registrar(scan_budget=20)
        status = self.refresh(registrar)
        self.assertFalse(status['ready'])
        self.assertEqual(status['error'], 'registration_scan_incomplete')
        self.bridge.replace_actors.assert_called_with({}, 0)
        self.assertFalse(list((self.directory / 'clients').glob('*.json')))

    def test_configured_budget_controls_deadline_and_finite_lease(self):
        registrar = self.registrar(scan_budget=40)
        status = self.refresh(registrar)
        self.assertTrue(status['ready'])
        self.assertEqual(self.bridge.replace_actors.call_args.args[1] - self.clock, 53)

    def test_failed_scan_revokes_previously_valid_snapshot(self):
        registrar = self.registrar()
        self.assertTrue(self.refresh(registrar)['ready'])
        with patch.object(registrar, '_discover', side_effect=MessageError('registration_scan_incomplete')):
            registrar.refresh()
        self.bridge.replace_actors.assert_called_with({}, 0)
        status = json.loads((self.directory / 'registrations.json').read_text())
        self.assertFalse(status['ready'])
        self.assertIsNone(status['expires_at'])

    def test_expired_budget_cannot_be_used_for_another_get(self):
        registrar = self.registrar(scan_budget=60)
        with patch('omni_msg.registration.time.monotonic', return_value=60):
            with self.assertRaises(MessageError) as error:
                registrar._remaining(60)
        self.assertEqual(error.exception.code, 'registration_scan_incomplete')

    def test_scan_budget_bounds(self):
        for value in [1, '60', 120]:
            with self.subTest(value=value):
                self.assertEqual(registration.scan_seconds(value), float(value))
        for value in [0, -1, 121, True, None, 'nan', 'inf', 'invalid']:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                registration.scan_seconds(value)


from test_registration import RegistrationHarness


class ScanBudgetCLITests(RegistrationHarness):
    def provision(self, *extra):
        self.cli('provision', '--directory', str(self.config), '--roots', str(self.roots),
                 '--server-url', f'http://127.0.0.1:{self.api.server_port}',
                 '--token-file', str(self.token), '--port', str(self.port),
                 '--poll-seconds', '0.1', *extra)

    def test_provision_configured_budget_reaches_serve(self):
        self.provision('--scan-seconds', '40')
        path = self.config / 'server.json'
        config = json.loads(path.read_text())
        self.assertEqual(config['registration']['scan_seconds'], 40)
        self.start(provision=False)
        status = self.ready()
        lease = status['expires_at'] - status['checked_at']
        self.assertGreater(lease, 30)
        self.assertLessEqual(lease, 80.1)

    def test_legacy_config_without_budget_uses_default(self):
        self.provision()
        path = self.config / 'server.json'
        config = json.loads(path.read_text())
        self.assertEqual(config['registration'].pop('scan_seconds'), 60)
        path.write_text(json.dumps(config))
        self.start(provision=False)
        status = self.ready()
        self.assertGreater(status['expires_at'] - status['checked_at'], 60)

    def test_invalid_budget_is_rejected_before_store_creation(self):
        self.provision()
        path = self.config / 'server.json'
        config = json.loads(path.read_text())
        for value in [0, 121, True, 'nan', None]:
            with self.subTest(value=value):
                config['registration']['scan_seconds'] = value
                path.write_text(json.dumps(config))
                self.cli('serve', '--server-config', str(path), '--state-dir', str(self.state),
                         '--port', str(self.port), expected=2)
                self.assertFalse(self.state.exists())

    def test_unknown_registration_key_is_still_rejected(self):
        self.provision()
        path = self.config / 'server.json'
        config = json.loads(path.read_text())
        config['registration']['scan_unlimited'] = True
        path.write_text(json.dumps(config))
        result = self.cli('serve', '--server-config', str(path), '--state-dir', str(self.state),
                          '--port', str(self.port), expected=2)
        self.assertEqual(result['error'], 'invalid_registration_config')
        self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
