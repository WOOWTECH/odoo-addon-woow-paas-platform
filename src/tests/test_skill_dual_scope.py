import uuid

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestSkillDualScope(TransactionCase):
    """Dual-scope schema: scope field, nullable project_id, D4 constraint."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.Project = cls.env['project.project']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.project = self.Project.sudo().create({'name': f'p-{self.suffix}'})

    def test_scope_field_exists_default_user(self):
        fields_ = self.Skill._fields
        self.assertIn('scope', fields_, "skill must declare a scope field")
        self.assertEqual(fields_['scope'].type, 'selection')
        self.assertEqual(fields_['scope'].default(self.Skill), 'user')

    def test_user_scope_requires_project(self):
        with self.assertRaises(ValidationError):
            self.Skill.sudo().create({
                'name': f'u-{self.suffix}', 'version': '0.1.0',
                'bundle_sha256': 'a' * 64, 'scope': 'user',
                # project_id intentionally omitted
            })

    def test_system_scope_forbids_project(self):
        with self.assertRaises(ValidationError):
            self.Skill.sudo().create({
                'name': f's-{self.suffix}', 'version': '0.1.0',
                'bundle_sha256': 'a' * 64, 'scope': 'system',
                'project_id': self.project.id,
            })

    def test_system_scope_creates_without_project(self):
        sk = self.Skill.sudo().create({
            'name': f'sys-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'a' * 64, 'scope': 'system',
        })
        self.assertFalse(sk.project_id)
        self.assertEqual(sk.scope, 'system')
