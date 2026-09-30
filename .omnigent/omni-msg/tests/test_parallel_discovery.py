"""重現慢 metadata 串行累加；驗證 bounded GET 與 discovery 安全守線。"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from omni_msg.bridge import Store
from omni_msg.registration import Registrar
from omni_msg.discovery_reads import MAX_PARALLEL_READS, read_batch
from omni_msg.root_admission import RootAdmission, bundle_fingerprint
from omni_msg.transport import MessageError


class ParallelDiscoveryTests(unittest.TestCase):
    def test_slow_metadata_completes_inside_same_budget_without_more_than_four_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = Store(path / 'messages.sqlite3', {})
            bridge = Mock(store=store, transport=SimpleNamespace(url='http://127.0.0.1', token='fixture'))
            roots = {}
            graph = {}
            for i in range(4):
                root = f'root_{i}'
                roots[f'team-{i}'] = {'session_id': root, 'workspace': '/workspace'}
                for sid, parent in [(root, None)] + [(f'child_{i}_{j}', root) for j in range(3)]:
                    graph[sid] = {'id': sid, 'parent_session_id': parent, 'archived': False,
                                  'permission_level': 4, 'runner_id': 'runner',
                                  'workspace': None if parent else '/workspace'}
            registrar = Registrar(roots, 5, bridge, path, 6778, scan_budget=1)
            lock = threading.Lock()
            active = peak = 0

            def get(url, token, *, timeout, max_bytes):
                nonlocal active, peak
                route = urlsplit(url).path
                sid = route.removeprefix('/v1/sessions/').split('/')[0]
                if route.endswith('/child_sessions'):
                    return {'data': [{'id': key} for key, value in graph.items()
                                     if value['parent_session_id'] == sid], 'has_more': False}
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    threading.Event().wait(min(0.1, timeout))
                    if timeout < 0.1:
                        raise MessageError('transport_unknown', 502, unavailable=True)
                    return graph[sid]
                finally:
                    with lock:
                        active -= 1

            try:
                with patch('omni_msg.registration.request_json', side_effect=get), patch.object(registrar, '_notify'):
                    registrar.refresh()
                status = json.loads((path / 'registrations.json').read_text())
                self.assertTrue(status['ready'], status)
                self.assertEqual(len(status['members']), 16)
                self.assertGreater(peak, 1)
                self.assertLessEqual(peak, 4)
                self.assertEqual(active, 0)
            finally:
                store.close()


class ReadBatchTests(unittest.TestCase):
    def test_order_and_errors_are_preserved_without_background_reads(self):
        done = []

        def fetch(item, deadline):
            threading.Event().wait(0.01 * (3 - item))
            done.append(item)
            if item == 1:
                raise MessageError('http_503', 503, unavailable=True)
            return item * 2

        rows = read_batch([0, 1, 2], fetch, time.monotonic() + 1)
        self.assertEqual([row[0] for row in rows], [0, 1, 2])
        self.assertEqual(rows[0][1], 0)
        self.assertEqual(rows[1][2].code, 'http_503')
        self.assertEqual(rows[2][1], 4)
        self.assertCountEqual(done, [0, 1, 2])

    def test_expired_deadline_does_not_launch_reads(self):
        fetch = Mock()
        with self.assertRaises(MessageError) as error:
            read_batch([1], fetch, time.monotonic() - 1)
        self.assertEqual(error.exception.code, 'registration_scan_incomplete')
        fetch.assert_not_called()

    def test_batch_cannot_exceed_four(self):
        with self.assertRaises(ValueError):
            read_batch(list(range(MAX_PARALLEL_READS + 1)), Mock(), time.monotonic() + 1)

    def test_unexpected_exception_still_joins_workers(self):
        done = threading.Event()

        def fetch(item, deadline):
            if item == 0:
                raise RuntimeError('fixture')
            threading.Event().wait(0.02)
            done.set()

        with self.assertRaises(RuntimeError):
            read_batch([0, 1], fetch, time.monotonic() + 1)
        self.assertTrue(done.is_set())

    def test_deadline_expiry_after_batch_fails_closed(self):
        def fetch(item, deadline):
            threading.Event().wait(0.03)
            return item

        with self.assertRaises(MessageError) as error:
            read_batch([1, 2], fetch, time.monotonic() + 0.01)
        self.assertEqual(error.exception.code, 'registration_scan_incomplete')


class ParallelAdmissionTests(unittest.TestCase):
    def test_parallel_details_keep_bundle_cache_and_authenticated_owner_checks(self):
        from test_root_admission import bundle_bytes
        raw = bundle_bytes()
        rule = {'agent_name': 'woow_issue_leader', 'owner': 'alice', 'host_id': 'host-one',
                'workspace': '/workspace', 'bundle_fingerprint': bundle_fingerprint(raw)}
        nodes = {f'root_{i}': {'id': f'root_{i}', 'agent_name': 'woow_issue_leader',
                 'agent_id': 'trusted-agent', 'owner': 'alice', 'host_id': 'host-one',
                 'workspace': '/workspace', 'runner_id': 'runner', 'parent_session_id': None,
                 'permission_level': 4, 'archived': False} for i in range(4)}
        lock = threading.Lock()
        active = peak = 0
        bundle_reads = []

        def get(path, deadline):
            nonlocal active, peak
            if path.startswith('/v1/sessions?'):
                return {'data': list(nodes.values()), 'has_more': False}
            sid = path.split('/')[3].split('?')[0]
            if path.endswith('/agent'):
                return {'id': 'trusted-agent', 'name': 'woow_issue_leader', 'version': 1}
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                remaining = deadline - time.monotonic()
                threading.Event().wait(min(0.1, max(0, remaining)))
                if remaining < 0.1:
                    raise MessageError('registration_scan_incomplete')
                # 真實detail沒有owner；授權仍取自authenticated list。
                return {key: value for key, value in nodes[sid].items() if key != 'owner'}
            finally:
                with lock:
                    active -= 1

        def bundle(path, deadline):
            bundle_reads.append(path)
            return raw

        admission = RootAdmission([rule], get, bundle)
        proofs = admission.discover(time.monotonic() + 0.3)
        self.assertEqual(set(proofs), set(nodes))
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        self.assertEqual(active, 0)
        self.assertEqual(len(bundle_reads), 1)
        self.assertEqual(admission.status['rejected'], [])


if __name__ == '__main__':
    unittest.main()
