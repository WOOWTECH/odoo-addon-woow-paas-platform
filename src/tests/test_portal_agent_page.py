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


@tagged('post_install', '-at_install')
class TestPortalAgentPage(PortalAgentFixture):

    def test_task_page_renders_chat_and_status_anchors(self):
        self.authenticate(self.alice.login, PASSWORD)
        resp = self.url_open(f'/my/tasks/{self.task.id}', timeout=20)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('woow_paas_platform.PortalAgentStatus', resp.text)
        self.assertIn('woow_paas_platform.PortalTaskChat', resp.text)

    def test_task_page_auto_joins_portal_partner(self):
        # Carol: shared on task but NOT yet a channel member.
        suffix = self.suffix
        partner_carol = self.env['res.partner'].create({
            'name': f'PG Carol {suffix}', 'email': f'pg-c-{suffix}@t.example'})
        carol = self.env['res.users'].create({
            'login': f'pg_carol_{suffix}', 'name': 'Carol',
            'password': PASSWORD, 'partner_id': partner_carol.id,
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        self.task.message_subscribe(partner_ids=[partner_carol.id])
        self.assertNotIn(partner_carol,
                         self.channel.sudo().channel_partner_ids)
        self.authenticate(carol.login, PASSWORD)
        resp = self.url_open(f'/my/tasks/{self.task.id}', timeout=20)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(partner_carol,
                      self.channel.sudo().channel_partner_ids)

    def test_task_page_no_agent_block_when_runtime_disabled(self):
        self.project.use_agent_runtime = False
        self.authenticate(self.alice.login, PASSWORD)
        resp = self.url_open(f'/my/tasks/{self.task.id}', timeout=20)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('woow_paas_platform.PortalTaskChat', resp.text)
        self.project.use_agent_runtime = True

    def test_project_page_renders_status_anchor(self):
        self.project.message_subscribe(partner_ids=[self.partner_alice.id])
        self.authenticate(self.alice.login, PASSWORD)
        resp = self.url_open(f'/my/projects/{self.project.id}', timeout=20)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('woow_paas_platform.PortalAgentStatus', resp.text)

    def test_unshared_portal_user_cannot_open_task_page(self):
        """Native portal access regression — Bob has no share at all."""
        self.authenticate(self.bob.login, PASSWORD)
        resp = self.url_open(f'/my/tasks/{self.task.id}', timeout=20)
        self.assertNotIn('woow_paas_platform.PortalTaskChat', resp.text)
