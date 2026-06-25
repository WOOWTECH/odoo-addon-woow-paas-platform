"""Refactor regression: snapshot/resolve 下沉到 agent_session model 後，
model 版輸出與 controller delegate 一致（spec Refactor①）。"""
import uuid

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSnapshotRefactor(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()
        self.project = self.env['project.project'].sudo().create(
            {'name': f'snap-{uuid.uuid4().hex[:6]}'})

    def _make_template(self, **kw):
        vals = {'name': f'tmpl-{uuid.uuid4().hex[:6]}'}
        vals.update(kw)
        return self.env['woow_paas_platform.agent_template'].sudo().create(vals)

    def test_skill_snapshot_shape(self):
        """system published skill 出現在 snapshot；draft 不出現。"""
        Skill = self.env['woow_paas_platform.skill'].sudo()
        Skill.create({'name': f'sk-{uuid.uuid4().hex[:6]}', 'version': '1.0.0',
                      'scope': 'system', 'state': 'published',
                      'bundle_sha256': 'a' * 64})
        template = self._make_template()
        snap = self.Session._build_skill_snapshot(self.project, template)
        self.assertIsInstance(snap, list)
        self.assertTrue(all('name' in s and 'sha256' in s for s in snap))

    def test_mcp_snapshot_shape(self):
        template = self._make_template()
        snap = self.Session._build_mcp_snapshot(self.project, template)
        self.assertIn('servers', snap)
        self.assertIsInstance(snap['servers'], dict)

    def test_resolve_returns_recordset(self):
        template = self._make_template(is_public=True, state='published',
                                       project_ids=[(6, 0, [self.project.id])])
        resolved = self.Session._resolve_agent_template(self.project)
        self.assertEqual(resolved.id, template.id)
