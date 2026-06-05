"""Portal SSE gate + portal page rendering (HttpCase)."""
import json
import re
import uuid

from odoo.tests.common import HttpCase, tagged

PASSWORD = 'Portal!234'


class PortalAgentFixture(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        cls.suffix = suffix
        Partner = cls.env['res.partner']
        User = cls.env['res.users']
        portal_group = cls.env.ref('base.group_portal')

        cls.partner_alice = Partner.create({
            'name': f'PG Alice {suffix}', 'email': f'pg-a-{suffix}@t.example'})
        cls.partner_bob = Partner.create({
            'name': f'PG Bob {suffix}', 'email': f'pg-b-{suffix}@t.example'})
        cls.alice = User.create({
            'login': f'pg_alice_{suffix}', 'name': 'Alice', 'password': PASSWORD,
            'partner_id': cls.partner_alice.id,
            'groups_id': [(6, 0, [portal_group.id])]})
        cls.bob = User.create({
            'login': f'pg_bob_{suffix}', 'name': 'Bob', 'password': PASSWORD,
            'partner_id': cls.partner_bob.id,
            'groups_id': [(6, 0, [portal_group.id])]})

        cls.project = cls.env['project.project'].create({
            'name': f'PG Proj {suffix}', 'privacy_visibility': 'portal',
            'use_agent_runtime': True})
        cls.task = cls.env['project.task'].create({
            'name': f'PG Task {suffix}', 'project_id': cls.project.id,
            'ai_auto_reply': False})
        cls.task.message_subscribe(partner_ids=[cls.partner_alice.id])
        cls.channel = cls.task._create_chat_channel()
        cls.channel.sudo().write({'channel_member_ids': [
            (0, 0, {'partner_id': cls.partner_alice.id}),
            (0, 0, {'partner_id': cls.partner_bob.id}),
        ]})

    def _csrf_token(self):
        for page in ('/my', '/web'):
            resp = self.url_open(page, timeout=15)
            if resp.status_code != 200:
                continue
            m = re.search(
                r'csrf_token[\"\']?\s*[:=]\s*[\"\']([^\"\']+)[\"\']', resp.text)
            if m:
                return m.group(1)
        self.fail('csrf token could not be extracted — fixture issue')

    def _first_sse_frame(self, resp):
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith('data:'):
                return json.loads(line[len('data:'):].strip())
        return {}


@tagged('post_install', '-at_install')
class TestPortalSseGate(PortalAgentFixture):

    def test_shared_portal_reaches_runtime_branch(self):
        """Alice passes both gates; error (if any) is runtime-stage, not ACL."""
        self.authenticate(self.alice.login, PASSWORD)
        token = self._csrf_token()
        resp = self.url_open(
            f'/api/ai/stream/{self.channel.id}?csrf_token={token}', timeout=30)
        frame = self._first_sse_frame(resp)
        self.assertNotIn(frame.get('error_code'),
                         ('access_denied', 'project_access_denied',
                          'csrf_error', 'channel_not_found'),
                         frame)

    def test_unshared_portal_denied_at_channel_gate(self):
        self.authenticate(self.bob.login, PASSWORD)
        token = self._csrf_token()
        resp = self.url_open(
            f'/api/ai/stream/{self.channel.id}?csrf_token={token}', timeout=30)
        frame = self._first_sse_frame(resp)
        self.assertEqual(frame.get('error_code'), 'access_denied', frame)
