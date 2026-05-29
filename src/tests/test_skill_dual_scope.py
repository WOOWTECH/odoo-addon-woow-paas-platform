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


@tagged('post_install', '-at_install')
class TestSkillDualScopeAcl(TransactionCase):
    """system_readonly + user-scope leg + scope-flip lock."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.Project = cls.env['project.project']
        cls.PAccess = cls.env['woow_paas_platform.project_access']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.alice = self.env['res.users'].sudo().create({
            'name': 'Alice', 'login': f'alice-{self.suffix}',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.project = self.Project.sudo().create({'name': f'p-{self.suffix}'})
        self.PAccess.sudo().create({
            'project_id': self.project.id,
            'partner_id': self.alice.partner_id.id, 'role': 'owner',
        })
        self.system_skill = self.Skill.sudo().create({
            'name': f'sys-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'a' * 64, 'scope': 'system', 'state': 'published',
        })
        self.user_skill = self.Skill.sudo().create({
            'name': f'usr-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'b' * 64, 'scope': 'user',
            'project_id': self.project.id,
        })

    def test_group_user_can_read_system_skill(self):
        rec = self.Skill.with_user(self.alice).search([
            ('id', '=', self.system_skill.id)])
        self.assertEqual(rec.ids, [self.system_skill.id],
                         "any internal user can read a system skill")

    def test_group_user_cannot_create_system_skill(self):
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            self.Skill.with_user(self.alice).create({
                'name': f'evil-{self.suffix}', 'version': '0.1.0',
                'bundle_sha256': 'c' * 64, 'scope': 'system',
            })

    def test_group_user_cannot_flip_user_skill_to_system(self):
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            self.user_skill.with_user(self.alice).write({
                'scope': 'system', 'project_id': False,
            })


@tagged('post_install', '-at_install')
class TestSkillSubsetConstraint(TransactionCase):
    """Template bound skills ⊆ (project user skills ∪ system skills)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.Project = cls.env['project.project']
        cls.Template = cls.env['woow_paas_platform.agent_template']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.p1 = self.Project.sudo().create({'name': f'p1-{self.suffix}'})
        self.p2 = self.Project.sudo().create({'name': f'p2-{self.suffix}'})
        self.p1_skill = self.Skill.sudo().create({
            'name': f'p1s-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'a' * 64, 'scope': 'user', 'project_id': self.p1.id,
        })
        self.p2_skill = self.Skill.sudo().create({
            'name': f'p2s-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'b' * 64, 'scope': 'user', 'project_id': self.p2.id,
        })
        self.sys_skill = self.Skill.sudo().create({
            'name': f'sys-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'c' * 64, 'scope': 'system',
        })

    def test_project_skill_ids_reverse(self):
        self.assertIn(self.p1_skill, self.p1.skill_ids)
        self.assertNotIn(self.p2_skill, self.p1.skill_ids)

    def test_bind_cross_project_user_skill_rejected(self):
        with self.assertRaises(ValidationError):
            self.Template.sudo().create({
                'name': f't-{self.suffix}',
                'project_ids': [(6, 0, [self.p1.id])],
                'skill_ids': [(6, 0, [self.p2_skill.id])],  # belongs to p2, not bound
            })

    def test_bind_system_skill_allowed(self):
        tmpl = self.Template.sudo().create({
            'name': f't-{self.suffix}',
            'project_ids': [(6, 0, [self.p1.id])],
            'skill_ids': [(6, 0, [self.p1_skill.id, self.sys_skill.id])],
        })
        self.assertTrue(tmpl.id)


@tagged('post_install', '-at_install')
class TestSkillPodLoading(TransactionCase):
    """system skills union into snapshot; download dual-leg."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.Project = cls.env['project.project']
        cls.Template = cls.env['woow_paas_platform.agent_template']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.project = self.Project.sudo().create({'name': f'p-{self.suffix}'})
        self.proj_skill = self.Skill.sudo().create({
            'name': f'proj-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'a' * 64, 'scope': 'user',
            'project_id': self.project.id, 'state': 'published',
        })
        self.sys_skill = self.Skill.sudo().create({
            'name': f'sys-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'b' * 64, 'scope': 'system', 'state': 'published',
        })
        self.template = self.Template.sudo().create({
            'name': f't-{self.suffix}',
            'project_ids': [(6, 0, [self.project.id])],
            'skill_ids': [(6, 0, [self.proj_skill.id])],
        })

    def test_snapshot_unions_system_skill(self):
        from odoo.addons.woow_paas_platform.controllers.ai_assistant import (
            AiAssistantController,
        )
        snap = AiAssistantController()._build_skill_snapshot(
            self.project, self.template)
        names = {e['name'] for e in snap}
        self.assertIn(self.proj_skill.name, names, "bound project skill present")
        self.assertIn(self.sys_skill.name, names, "system skill auto-unioned")

    def test_snapshot_project_name_shadows_system(self):
        from odoo.addons.woow_paas_platform.controllers.ai_assistant import (
            AiAssistantController,
        )
        # a system skill sharing the bound project skill's NAME must be shadowed
        self.Skill.sudo().create({
            'name': self.proj_skill.name, 'version': '9.9.9',
            'bundle_sha256': 'c' * 64, 'scope': 'system', 'state': 'published',
        })
        snap = AiAssistantController()._build_skill_snapshot(
            self.project, self.template)
        same_name = [e for e in snap if e['name'] == self.proj_skill.name]
        self.assertEqual(len(same_name), 1, "project skill shadows system on name")
        self.assertEqual(same_name[0]['sha256'], 'a' * 64, "project version wins")
