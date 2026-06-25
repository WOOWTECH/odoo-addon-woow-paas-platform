"""#123 cold-start retry: client timeout 分級 + spawn retry + WS connect retry."""
import json as _json
import os
import re
import sys
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests.common import HttpCase, TransactionCase, tagged

_CLIENT_MOD = "odoo.addons.woow_paas_platform.services.agent_runtime_client"
_ENV_NAMESPACE_SECRET = 'AGENT_RUNTIME_NAMESPACE_SECRET'


@tagged('post_install', '-at_install')
class TestColdStartClientTimeout(TransactionCase):
    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        import importlib
        self.mod = importlib.import_module(_CLIENT_MOD)
        self.client = self.mod.get_agent_runtime_client(self.env)

    def _ok(self, payload=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.json.return_value = payload or {}
        return resp

    def test_spawn_uses_90s_timeout(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._ok(
                {'pod_ws_url': 'ws://pod', 'auth_token': 't',
                 'pod_state': 'active', 'spawn_age_ms': 10})
            project = self.env['project.project'].sudo().create(
                {'name': f'to-{uuid.uuid4().hex[:6]}'})
            self.client.spawn_or_attach(
                project=project, channel_id=1, invoker_user_id=2,
                invoker_role='user',
                agent_template=MagicMock(id=1, model_config_id=None),
                mcp_snapshot={}, skill_snapshot=[])
            self.assertEqual(mock_requests.request.call_args.kwargs.get('timeout'), 90)

    def test_invalidate_uses_10s_timeout(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._ok({})
            self.client.invalidate_mcp('pa1234567890abcd', {})
            self.assertEqual(mock_requests.request.call_args.kwargs.get('timeout'), 10)

    def test_reap_keeps_default_30s_timeout(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._ok({})
            self.client.reap('pa1234567890abcd')
            self.assertEqual(mock_requests.request.call_args.kwargs.get('timeout'), 30)


@tagged('post_install', '-at_install')
class TestSpawnRetry(TransactionCase):
    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        import importlib
        self.mod = importlib.import_module(_CLIENT_MOD)
        self.client = self.mod.get_agent_runtime_client(self.env)

    def _ok(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.json.return_value = {'pod_ws_url': 'ws://pod', 'auth_token': 't',
                                  'pod_state': 'active', 'spawn_age_ms': 10}
        return resp

    def test_spawn_retries_on_timeout_then_succeeds(self):
        import requests as real_requests
        with patch.object(self.mod, 'requests', create=True) as mock_requests, \
                patch.object(self.mod.time, 'sleep', return_value=None):
            mock_requests.request.side_effect = [
                real_requests.exceptions.ReadTimeout('t1'),
                real_requests.exceptions.ReadTimeout('t2'),
                self._ok(),
            ]
            project = self.env['project.project'].sudo().create(
                {'name': f'rt-{uuid.uuid4().hex[:6]}'})
            result = self.client.spawn_or_attach(
                project=project, channel_id=1, invoker_user_id=2,
                invoker_role='user',
                agent_template=MagicMock(id=1, model_config_id=None),
                mcp_snapshot={}, skill_snapshot=[])
            self.assertEqual(result['pod_ws_url'], 'ws://pod')
            self.assertEqual(mock_requests.request.call_count, 3)

    def test_spawn_does_not_retry_on_4xx(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests, \
                patch.object(self.mod.time, 'sleep', return_value=None):
            bad = MagicMock()
            bad.status_code = 400
            bad.ok = False
            bad.json.return_value = {'detail': 'bad request'}
            bad.text = 'bad request'
            mock_requests.request.return_value = bad
            project = self.env['project.project'].sudo().create(
                {'name': f'b4-{uuid.uuid4().hex[:6]}'})
            with self.assertRaises(self.mod.AgentRuntimeOperatorError):
                self.client.spawn_or_attach(
                    project=project, channel_id=1, invoker_user_id=2,
                    invoker_role='user',
                    agent_template=MagicMock(id=1, model_config_id=None),
                    mcp_snapshot={}, skill_snapshot=[])
            self.assertEqual(mock_requests.request.call_count, 1)  # no retry on 4xx


class _RetryFakeWSConn:
    def __init__(self, frames):
        self._frames = list(frames)
        self.closed = False
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        import asyncio
        await asyncio.sleep(0)
        self.closed = True
        return False

    async def send(self, message):
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        raise StopAsyncIteration


def _make_retry_ws(fail_times, conn):
    import types
    mod = types.ModuleType("websockets")
    mod._attempts = 0

    def connect(uri, *args, **kwargs):
        mod._attempts += 1
        if mod._attempts <= fail_times:
            raise ConnectionRefusedError("pod not ready (simulated)")
        return conn

    mod.connect = connect
    return mod


@tagged('post_install', '-at_install')
class TestWsConnectRetry(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = cls.env.ref('base.user_admin')

    def setUp(self):
        super().setUp()
        s = uuid.uuid4().hex[:8]
        Project = self.env['project.project'].sudo()
        self.project = Project.create({'name': f'wsr-{s}'})
        self.project.message_subscribe(partner_ids=[self.admin.partner_id.id])
        self.project.write({'use_agent_runtime': True})
        self.env['woow_paas_platform.agent_template'].sudo().create({
            'name': f'tmpl-{s}', 'is_public': True, 'state': 'published',
            'project_ids': [(6, 0, [self.project.id])]})
        self.channel = self.env['discuss.channel'].sudo().create({
            'name': f'wsr-ch-{s}', 'channel_type': 'channel',
            'channel_partner_ids': [(4, self.admin.partner_id.id)]})
        self.env['project.task'].sudo().create({
            'name': f'wsr-task-{s}', 'project_id': self.project.id,
            'channel_id': self.channel.id})

    def _csrf_token(self):
        for page in ('/woow', '/web'):
            resp = self.url_open(page, timeout=15)
            if resp.status_code != 200:
                continue
            m = re.search(r'csrf_token[\"\']?\s*[:=]\s*[\"\']([^\"\']+)[\"\']',
                          resp.text)
            if m:
                return m.group(1)
        self.fail("csrf token not found (fixture issue)")

    def _fake_client(self):
        client = MagicMock()
        client.spawn_or_attach.return_value = {
            'pod_ws_url': 'ws://pod.local:8000/v1/chat',
            'auth_token': 'tok', 'pod_state': 'spawning', 'spawn_age_ms': 5}
        return client

    def test_connect_retries_then_streams_done(self):
        conn = _RetryFakeWSConn(frames=[
            _json.dumps({'type': 'text_chunk', 'payload': {'content': 'hi'}}),
            _json.dumps({'type': 'done', 'payload': {}}),
        ])
        fake = _make_retry_ws(fail_times=2, conn=conn)
        prev = sys.modules.get('websockets')
        sys.modules['websockets'] = fake
        self.addCleanup(lambda: sys.modules.__setitem__('websockets', prev)
                        if prev else sys.modules.pop('websockets', None))
        self.authenticate('admin', 'admin')
        token = self._csrf_token()
        url = f'/api/ai/stream/{self.channel.id}?csrf_token={token}'
        with patch(_CLIENT_MOD + '.get_agent_runtime_client',
                   return_value=self._fake_client()):
            resp = self.url_open(url, timeout=25)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('"stage": "spawning"', resp.text)   # 進度 event 有出
        self.assertIn('"done": true', resp.text)          # 最終仍成功
        self.assertEqual(fake._attempts, 3)               # 2 fail + 1 success
        self.assertTrue(conn.sent)                         # user_message 有送
        self.assertEqual(len(conn.sent), 1)               # 只送一次（不重送）
