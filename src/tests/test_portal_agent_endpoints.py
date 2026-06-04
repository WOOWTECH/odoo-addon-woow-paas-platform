"""Endpoint-level portal access tests (HttpCase — \"ORM 綠 ≠ endpoint secure\").

Cross-user negatives for every portal-reachable AI endpoint:
chat/history, chat/post, agent-status, agent-pod-status.
(SSE /api/ai/stream is covered in test_portal_agent_page.py Task 3.)
"""
import json
import uuid

from odoo.tests.common import HttpCase, tagged

PASSWORD = 'Portal!234'


@tagged('post_install', '-at_install')
class TestPortalAgentEndpoints(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        cls.suffix = suffix
        Partner = cls.env['res.partner']
        User = cls.env['res.users']
        portal_group = cls.env.ref('base.group_portal')

        cls.partner_alice = Partner.create({
            'name': f'EP Alice {suffix}', 'email': f'ep-a-{suffix}@t.example'})
        cls.partner_bob = Partner.create({
            'name': f'EP Bob {suffix}', 'email': f'ep-b-{suffix}@t.example'})
        cls.alice = User.create({
            'login': f'ep_alice_{suffix}', 'name': 'Alice', 'password': PASSWORD,
            'partner_id': cls.partner_alice.id,
            'groups_id': [(6, 0, [portal_group.id])]})
        cls.bob = User.create({
            'login': f'ep_bob_{suffix}', 'name': 'Bob', 'password': PASSWORD,
            'partner_id': cls.partner_bob.id,
            'groups_id': [(6, 0, [portal_group.id])]})

        cls.project = cls.env['project.project'].create({
            'name': f'EP Proj {suffix}', 'privacy_visibility': 'portal'})
        cls.task = cls.env['project.task'].create({
            'name': f'EP Task {suffix}', 'project_id': cls.project.id,
            'ai_auto_reply': False})
        # Alice is shared on the task (read-mode share semantics).
        cls.task.message_subscribe(partner_ids=[cls.partner_alice.id])
        # Channel exists and BOTH portal partners are members — proves that
        # membership alone must NOT be sufficient for portal users.
        cls.channel = cls.task._create_chat_channel()
        cls.channel.sudo().write({'channel_member_ids': [
            (0, 0, {'partner_id': cls.partner_alice.id}),
            (0, 0, {'partner_id': cls.partner_bob.id}),
        ]})

    def _rpc(self, url, params):
        resp = self.url_open(
            url,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                             'params': params, 'id': 1}),
            headers={'Content-Type': 'application/json'},
            timeout=20,
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json().get('result') or {}

    # ---- chat history / post --------------------------------------------- #

    def test_history_shared_portal_ok(self):
        self.authenticate(self.alice.login, PASSWORD)
        res = self._rpc('/api/ai/chat/history',
                        {'channel_id': self.channel.id})
        self.assertTrue(res.get('success'), res)

    def test_history_member_but_unshared_denied(self):
        """Stale channel membership without task share must be denied."""
        self.authenticate(self.bob.login, PASSWORD)
        res = self._rpc('/api/ai/chat/history',
                        {'channel_id': self.channel.id})
        self.assertFalse(res.get('success'))
        self.assertEqual(res.get('error'), 'Access denied')

    def test_post_shared_portal_ok(self):
        self.authenticate(self.alice.login, PASSWORD)
        res = self._rpc('/api/ai/chat/post',
                        {'channel_id': self.channel.id, 'body': 'hello'})
        self.assertTrue(res.get('success'), res)

    def test_post_member_but_unshared_denied(self):
        self.authenticate(self.bob.login, PASSWORD)
        res = self._rpc('/api/ai/chat/post',
                        {'channel_id': self.channel.id, 'body': 'hi'})
        self.assertFalse(res.get('success'))
        self.assertEqual(res.get('error'), 'Access denied')

    def test_post_revoked_share_denied(self):
        """Un-share revokes access on the NEXT request despite membership."""
        self.authenticate(self.alice.login, PASSWORD)
        self.task.message_unsubscribe(partner_ids=[self.partner_alice.id])
        res = self._rpc('/api/ai/chat/post',
                        {'channel_id': self.channel.id, 'body': 'hi'})
        self.assertFalse(res.get('success'))
        # restore for other tests (class-level fixture)
        self.task.message_subscribe(partner_ids=[self.partner_alice.id])

    # ---- agent status ------------------------------------------------------ #

    def test_agent_status_shared_portal_ok(self):
        self.authenticate(self.alice.login, PASSWORD)
        res = self._rpc('/api/ai/agent-status',
                        {'project_id': self.project.id})
        self.assertTrue(res.get('success'), res)
        self.assertEqual(res['data']['scope'], 'project')

    def test_agent_status_unshared_portal_denied(self):
        self.authenticate(self.bob.login, PASSWORD)
        res = self._rpc('/api/ai/agent-status',
                        {'project_id': self.project.id})
        self.assertFalse(res.get('success'))
        self.assertEqual(res.get('error'), 'Access denied')

    def test_agent_pod_status_shared_portal_ok(self):
        self.authenticate(self.alice.login, PASSWORD)
        res = self._rpc('/api/ai/agent-pod-status',
                        {'project_id': self.project.id})
        self.assertTrue(res.get('success'), res)

    def test_agent_pod_status_unshared_portal_denied(self):
        self.authenticate(self.bob.login, PASSWORD)
        res = self._rpc('/api/ai/agent-pod-status',
                        {'project_id': self.project.id})
        self.assertFalse(res.get('success'))
        self.assertEqual(res.get('error'), 'Access denied')

    # ---- internal regression ----------------------------------------------- #

    def test_internal_non_task_channel_unchanged(self):
        """Internal users on non-task channels keep membership-only access."""
        admin = self.env.ref('base.user_admin')
        ch = self.env['discuss.channel'].sudo().create({
            'name': f'free-{self.suffix}', 'channel_type': 'channel',
            'channel_partner_ids': [(4, admin.partner_id.id)]})
        self.authenticate('admin', 'admin')
        res = self._rpc('/api/ai/chat/history', {'channel_id': ch.id})
        self.assertTrue(res.get('success'), res)

    def test_portal_non_task_channel_denied(self):
        """Portal users may only use task-bound channels."""
        ch = self.env['discuss.channel'].sudo().create({
            'name': f'free2-{self.suffix}', 'channel_type': 'channel',
            'channel_partner_ids': [(4, self.partner_alice.id)]})
        self.authenticate(self.alice.login, PASSWORD)
        res = self._rpc('/api/ai/chat/history', {'channel_id': ch.id})
        self.assertFalse(res.get('success'))
