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

    def test_user_dup_same_project_name_version_rejected(self):
        self.Skill.sudo().create({
            'name': 'dup', 'version': '0.1.0', 'bundle_sha256': 'a' * 64,
            'scope': 'user', 'project_id': self.project.id,
        })
        with self.assertRaises(Exception):  # noqa: BLE001  (IntegrityError)
            self.Skill.sudo().create({
                'name': 'dup', 'version': '0.1.0', 'bundle_sha256': 'b' * 64,
                'scope': 'user', 'project_id': self.project.id,
            })
            self.env.flush_all()

    def test_system_dup_same_name_version_rejected(self):
        self.Skill.sudo().create({
            'name': 'sysdup', 'version': '0.1.0', 'bundle_sha256': 'a' * 64,
            'scope': 'system',
        })
        with self.assertRaises(Exception):  # noqa: BLE001
            self.Skill.sudo().create({
                'name': 'sysdup', 'version': '0.1.0', 'bundle_sha256': 'b' * 64,
                'scope': 'system',
            })
            self.env.flush_all()

    def test_cross_scope_same_name_version_allowed(self):
        sys_sk = self.Skill.sudo().create({
            'name': 'shared', 'version': '0.1.0', 'bundle_sha256': 'a' * 64,
            'scope': 'system',
        })
        usr_sk = self.Skill.sudo().create({
            'name': 'shared', 'version': '0.1.0', 'bundle_sha256': 'b' * 64,
            'scope': 'user', 'project_id': self.project.id,
        })
        self.env.flush_all()
        self.assertTrue(sys_sk.id and usr_sk.id)

    def test_system_is_latest_grouped_platform_wide(self):
        v1 = self.Skill.sudo().create({
            'name': 'syslatest', 'version': '0.1.0', 'bundle_sha256': 'a' * 64,
            'scope': 'system', 'state': 'published',
        })
        v2 = self.Skill.sudo().create({
            'name': 'syslatest', 'version': '0.2.0', 'bundle_sha256': 'b' * 64,
            'scope': 'system', 'state': 'published',
        })
        v1.invalidate_recordset()
        v2.invalidate_recordset()
        self.assertFalse(v1.is_latest, "older system version loses is_latest")
        self.assertTrue(v2.is_latest, "newest published system version is latest")

    def test_system_and_user_is_latest_independent(self):
        sys_sk = self.Skill.sudo().create({
            'name': 'indep', 'version': '0.1.0', 'bundle_sha256': 'a' * 64,
            'scope': 'system', 'state': 'published',
        })
        usr_sk = self.Skill.sudo().create({
            'name': 'indep', 'version': '0.1.0', 'bundle_sha256': 'b' * 64,
            'scope': 'user', 'project_id': self.project.id, 'state': 'published',
        })
        sys_sk.invalidate_recordset()
        usr_sk.invalidate_recordset()
        self.assertTrue(sys_sk.is_latest, "system bucket has its own latest")
        self.assertTrue(usr_sk.is_latest, "user bucket has its own latest")
