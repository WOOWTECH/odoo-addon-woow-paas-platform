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


@tagged('post_install', '-at_install')
class TestPortalCrudWriteDenied(HttpCase):
    """Read-mode share must NOT widen into project/task CRUD write/delete.

    The portal leg in ``_check_project_access`` / ``_check_task_access`` was
    introduced to widen ONLY the read-only agent endpoints (chat, status).
    Those same two helpers are also the sole authorization gate for the sudo
    write/delete/create endpoints (``api_support_projects`` update/delete,
    ``api_support_tasks`` create, ``api_support_task_detail`` update/delete);
    the records are sudo-browsed so the native read-only portal ir.rule does
    NOT apply. A read-mode shared portal user must be denied on every mutation
    path (design spec §非目標: edit-mode out of scope; native read-mode share
    is read-only). These are the missing endpoint negatives.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        cls.suffix = suffix
        Partner = cls.env['res.partner']
        User = cls.env['res.users']
        portal_group = cls.env.ref('base.group_portal')

        cls.partner_alice = Partner.create({
            'name': f'CW Alice {suffix}', 'email': f'cw-a-{suffix}@t.example'})
        cls.alice = User.create({
            'login': f'cw_alice_{suffix}', 'name': 'Alice', 'password': PASSWORD,
            'partner_id': cls.partner_alice.id,
            'groups_id': [(6, 0, [portal_group.id])]})

        cls.project = cls.env['project.project'].create({
            'name': f'CW Proj {suffix}', 'privacy_visibility': 'portal'})
        cls.task = cls.env['project.task'].create({
            'name': f'CW Task {suffix}', 'project_id': cls.project.id,
            'ai_auto_reply': False})
        # Alice is shared on the task (read-mode share semantics).
        cls.task.message_subscribe(partner_ids=[cls.partner_alice.id])

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

    # ---- project update / delete ----------------------------------------- #

    def test_project_update_share_denied(self):
        self.authenticate(self.alice.login, PASSWORD)
        original = self.project.name
        res = self._rpc('/api/support/projects', {
            'action': 'update', 'project_id': self.project.id,
            'name': 'HIJACKED'})
        self.assertFalse(res.get('success'), res)
        self.assertEqual(res.get('error'), 'Access denied')
        self.project.invalidate_recordset(['name'])
        self.assertEqual(self.project.name, original,
                         'share user must not be able to rename the project')

    def test_project_delete_share_denied(self):
        self.authenticate(self.alice.login, PASSWORD)
        res = self._rpc('/api/support/projects', {
            'action': 'delete', 'project_id': self.project.id})
        self.assertFalse(res.get('success'), res)
        self.assertEqual(res.get('error'), 'Access denied')
        self.project.invalidate_recordset(['active'])
        self.assertTrue(self.project.active,
                        'share user must not be able to archive the project')

    # ---- task create ------------------------------------------------------ #

    def test_task_create_share_denied(self):
        self.authenticate(self.alice.login, PASSWORD)
        before = self.env['project.task'].sudo().search_count(
            [('project_id', '=', self.project.id)])
        res = self._rpc('/api/support/tasks', {
            'action': 'create', 'project_id': self.project.id,
            'name': 'injected task'})
        self.assertFalse(res.get('success'), res)
        self.assertEqual(res.get('error'), 'Access denied')
        after = self.env['project.task'].sudo().search_count(
            [('project_id', '=', self.project.id)])
        self.assertEqual(after, before,
                         'share user must not be able to inject tasks')

    # ---- task update / delete (api_support_task_detail) ------------------- #

    def test_task_update_share_denied(self):
        self.authenticate(self.alice.login, PASSWORD)
        original = self.task.name
        res = self._rpc(f'/api/support/tasks/detail/{self.task.id}', {
            'action': 'update', 'name': 'HIJACKED',
            'chat_enabled': True, 'ai_auto_reply': True})
        self.assertFalse(res.get('success'), res)
        self.assertEqual(res.get('error'), 'Access denied')
        self.task.invalidate_recordset(['name', 'chat_enabled', 'ai_auto_reply'])
        self.assertEqual(self.task.name, original,
                         'share user must not be able to rewrite the task')
        self.assertFalse(self.task.chat_enabled,
                         'share user must not flip chat_enabled')

    def test_task_delete_share_denied(self):
        self.authenticate(self.alice.login, PASSWORD)
        res = self._rpc(f'/api/support/tasks/detail/{self.task.id}', {
            'action': 'delete'})
        self.assertFalse(res.get('success'), res)
        self.assertEqual(res.get('error'), 'Access denied')
        self.task.invalidate_recordset(['active'])
        self.assertTrue(self.task.active,
                        'share user must not be able to archive the task')

    # ---- read-mode chat/status still works (no over-correction) ---------- #

    def test_task_get_share_still_allowed(self):
        """Guard is write-only — read-mode 'get' must remain accessible."""
        self.authenticate(self.alice.login, PASSWORD)
        res = self._rpc(f'/api/support/tasks/detail/{self.task.id}', {
            'action': 'get'})
        self.assertTrue(res.get('success'), res)
