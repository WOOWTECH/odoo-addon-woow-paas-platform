from odoo.tests import TransactionCase, tagged


def _mk_skill(env, **vals):
    base = {
        'name': 'sk', 'version': '1.0.0',
        'bundle_sha256': 'a' * 64, 'scope': 'system',
    }
    base.update(vals)
    return env['woow_paas_platform.skill'].sudo().create(base)


@tagged('post_install', '-at_install')
class TestEffectiveSkillHelper(TransactionCase):
    """§2.1 — _effective_skill_ids 單一公式來源 + snapshot 重構不變式。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Session = cls.env['woow_paas_platform.agent_session']
        cls.project = cls.env['project.project'].sudo().create(
            {'name': 'FX Project'})
        # system published / system draft
        cls.sys_pub = _mk_skill(cls.env, name='sys-pub', state='published')
        cls.sys_draft = _mk_skill(cls.env, name='sys-draft')
        # user-scope：一顆會被 template 綁、一顆不綁
        cls.user_bound = _mk_skill(
            cls.env, name='user-bound', scope='user',
            project_id=cls.project.id, state='published')
        cls.user_unbound = _mk_skill(
            cls.env, name='user-unbound', scope='user',
            project_id=cls.project.id, state='published')
        # 同名遮蔽：bound 的 'sys-pub' user 版要遮蔽 system 版
        cls.shadow = _mk_skill(
            cls.env, name='sys-pub', version='2.0.0', scope='user',
            project_id=cls.project.id, state='published')
        cls.template = cls.env['woow_paas_platform.agent_template'].sudo() \
            .create({
                'name': 'FX Template',
                'project_ids': [(6, 0, [cls.project.id])],
                'skill_ids': [(6, 0, [cls.user_bound.id, cls.shadow.id])],
            })

    def test_snapshot_characterization(self):
        """重構前後 _build_skill_snapshot 輸出不變（spec §4.5 不變式）。"""
        snap = self.Session._build_skill_snapshot(self.project, self.template)
        names = [(e['name'], e['version']) for e in snap]
        # bound 在前（user-bound, sys-pub@2.0.0），system 補後且同名遮蔽
        self.assertIn(('user-bound', '1.0.0'), names)
        self.assertIn(('sys-pub', '2.0.0'), names)       # bound shadow 版
        self.assertNotIn(('sys-pub', '1.0.0'), names)    # system 版被遮蔽
        self.assertNotIn(('user-unbound', '1.0.0'), names)  # 歸屬≠生效
        self.assertNotIn(('sys-draft', '1.0.0'), names)  # draft 不進 pod
        for entry in snap:
            self.assertEqual(
                set(entry), {'name', 'version', 'sha256', 'description'})

    def test_effective_skill_ids_helper(self):
        """helper 回 recordset，與 snapshot 同一公式。"""
        eff = self.Session._effective_skill_ids(self.project)
        self.assertIn(self.user_bound, eff)
        self.assertIn(self.shadow, eff)
        self.assertNotIn(self.user_unbound, eff)
        self.assertNotIn(self.sys_draft, eff)
        # system 同名被 bound 遮蔽
        self.assertNotIn(self.sys_pub, eff)


from unittest.mock import patch


@tagged('post_install', '-at_install')
class TestSkillRepoint(TransactionCase):
    """§1 — admin 改 scope/project 時新舊兩組 is_latest 都重算 + push。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.proj_a = cls.env['project.project'].sudo().create({'name': 'RP A'})
        cls.proj_b = cls.env['project.project'].sudo().create({'name': 'RP B'})

    def _mk(self, **vals):
        return _mk_skill(self.env, **vals)

    def test_repoint_recomputes_old_and_new_group(self):
        # 舊組（proj_a, 'mv'）兩版：v2 是 is_latest
        v1 = self._mk(name='mv', version='1.0.0', scope='user',
                      project_id=self.proj_a.id, state='published')
        v2 = self._mk(name='mv', version='2.0.0', scope='user',
                      project_id=self.proj_a.id, state='published')
        v1._recompute_is_latest()
        self.assertTrue(v2.is_latest)
        # v2 搬去 proj_b → 舊組 winner 應回到 v1；新組 winner = v2
        v2.write({'project_id': self.proj_b.id})
        self.assertTrue(v1.is_latest, "old group must re-elect its winner")
        self.assertTrue(v2.is_latest, "new group winner is the mover")

    def test_repoint_pushes_snapshot_to_both_projects(self):
        sk = self._mk(name='mv2', scope='user',
                      project_id=self.proj_a.id, state='published')
        with patch.object(
            type(self.env['woow_paas_platform.agent_session']),
            '_push_snapshot_to_running_pods',
        ) as push:
            sk.write({'project_id': self.proj_b.id})
        pushed = {c.args[:3] for c in push.call_args_list}
        self.assertIn(('skill', 'user', self.proj_a.id), pushed)
        self.assertIn(('skill', 'user', self.proj_b.id), pushed)

    def test_scope_flip_by_admin_pushes_system_wide(self):
        sk = self._mk(name='mv3', scope='user',
                      project_id=self.proj_a.id, state='published')
        with patch.object(
            type(self.env['woow_paas_platform.agent_session']),
            '_push_snapshot_to_running_pods',
        ) as push:
            sk.write({'scope': 'system', 'project_id': False})
        self.assertIn(
            ('skill', 'system'), {c.args[:2] for c in push.call_args_list})


@tagged('post_install', '-at_install')
class TestSkillIsEffective(TransactionCase):
    """§2.1 — is_effective 與 _effective_skill_ids 同源（anti-pattern #3
    mitigation）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.project = cls.env['project.project'].sudo().create({'name': 'FE'})
        cls.bound = _mk_skill(cls.env, name='fe-bound', scope='user',
                              project_id=cls.project.id, state='published')
        cls.unbound = _mk_skill(cls.env, name='fe-unbound', scope='user',
                                project_id=cls.project.id, state='published')
        cls.sys_pub = _mk_skill(cls.env, name='fe-sys', state='published')
        cls.sys_draft = _mk_skill(cls.env, name='fe-sys-draft')
        cls.env['woow_paas_platform.agent_template'].sudo().create({
            'name': 'FE Tpl',
            'project_ids': [(6, 0, [cls.project.id])],
            'skill_ids': [(6, 0, [cls.bound.id])],
            'state': 'published',
        })

    def test_is_effective_matrix(self):
        self.assertTrue(self.bound.is_effective)      # bound published
        self.assertFalse(self.unbound.is_effective)   # 歸屬≠生效
        self.assertTrue(self.sys_pub.is_effective)    # system published
        self.assertFalse(self.sys_draft.is_effective)  # draft


from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestWizardUserScope(TransactionCase):
    """§2.2 — wizard per-scope 上傳與查重。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wizard = cls.env['woow_paas_platform.skill_upload_wizard']
        cls.proj = cls.env['project.project'].sudo().create({'name': 'WZ'})
        cls.proj2 = cls.env['project.project'].sudo().create({'name': 'WZ2'})
        cls.bundle = 'aGVsbG8='  # b64("hello")

    def _wiz(self, **vals):
        base = {'name': 'wz-skill', 'version': '1.0.0',
                'bundle_file': self.bundle}
        base.update(vals)
        return self.Wizard.create(base)

    def test_user_scope_upload_binds_project(self):
        action = self._wiz(scope='user', project_id=self.proj.id) \
            .action_upload()
        skill = self.env['woow_paas_platform.skill'].browse(action['res_id'])
        self.assertEqual(skill.scope, 'user')
        self.assertEqual(skill.project_id, self.proj)
        self.assertEqual(skill.state, 'draft')

    def test_user_scope_requires_project(self):
        with self.assertRaises(UserError):
            self._wiz(scope='user').action_upload()

    def test_system_scope_rejects_project(self):
        with self.assertRaises(UserError):
            self._wiz(scope='system', project_id=self.proj.id).action_upload()

    def test_dedupe_is_per_scope_and_project(self):
        self._wiz(scope='user', project_id=self.proj.id).action_upload()
        # 同 project 同名版 → 擋
        with self.assertRaises(UserError):
            self._wiz(scope='user', project_id=self.proj.id).action_upload()
        # 不同 project 同名版 → 放行
        self._wiz(scope='user', project_id=self.proj2.id).action_upload()
        # system 同名版（user 已存在）→ 放行（不同 scope 桶）
        self._wiz(scope='system').action_upload()

    def test_default_scope_stays_system_without_context(self):
        self.assertEqual(self._wiz().scope, 'system')


from odoo.exceptions import AccessError


@tagged('post_install', '-at_install')
class TestDedicatedTemplateFork(TransactionCase):
    """§2.3 — copy-on-write fork 與 resolved 狀態。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.proj = cls.env['project.project'].sudo().create({'name': 'FK'})
        cls.other = cls.env['project.project'].sudo().create({'name': 'FK2'})
        cls.sys_skill = _mk_skill(cls.env, name='fk-sys', state='published')
        cls.other_skill = _mk_skill(
            cls.env, name='fk-other', scope='user',
            project_id=cls.other.id, state='published')
        # 共用 template：綁兩個 project、綁 system + other 的 user skill
        cls.shared = cls.env['woow_paas_platform.agent_template'].sudo() \
            .create({
                'name': 'FK Shared',
                'project_ids': [(6, 0, [cls.proj.id, cls.other.id])],
                'skill_ids': [(6, 0, [cls.sys_skill.id, cls.other_skill.id])],
                'state': 'published',
            })

    def test_binding_status_shared_then_dedicated(self):
        self.assertEqual(self.proj.agent_template_binding_status, 'shared')
        self.proj.action_create_dedicated_agent_template()
        self.proj.invalidate_recordset()
        self.assertEqual(self.proj.agent_template_binding_status, 'dedicated')

    def test_fork_filters_foreign_user_scope(self):
        self.proj.action_create_dedicated_agent_template()
        fork = self.proj.resolved_agent_template_id
        self.assertNotEqual(fork, self.shared)
        self.assertIn(self.sys_skill, fork.skill_ids)       # system 帶入
        self.assertNotIn(self.other_skill, fork.skill_ids)  # 他租戶過濾
        self.assertEqual(fork.project_ids, self.proj)
        self.assertEqual(fork.state, 'published')
        self.assertFalse(fork.is_public)
        # 來源未被改動
        self.assertIn(self.other_skill, self.shared.skill_ids)

    def test_fork_requires_admin(self):
        user = self.env['res.users'].sudo().create({
            'name': 'FK User', 'login': 'fk_user',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(AccessError):
            self.proj.with_user(user).action_create_dedicated_agent_template()

    def test_fork_blocked_when_already_dedicated(self):
        self.proj.action_create_dedicated_agent_template()
        self.proj.invalidate_recordset()
        with self.assertRaises(UserError):
            self.proj.action_create_dedicated_agent_template()

    def test_inverse_skill_binding_writes_through_on_dedicated(self):
        self.proj.action_create_dedicated_agent_template()
        self.proj.invalidate_recordset()
        own = _mk_skill(self.env, name='fk-own', scope='user',
                        project_id=self.proj.id, state='published')
        fork = self.proj.resolved_agent_template_id
        self.proj.resolved_template_skill_ids = [(4, own.id)]
        self.assertIn(own, fork.skill_ids)

    def test_inverse_rejected_on_shared(self):
        with self.assertRaises(UserError):
            self.proj.resolved_template_skill_ids = [(4, self.sys_skill.id)]


@tagged('post_install', '-at_install')
class TestTemplateBindingPush(TransactionCase):
    """§2.3 連帶 — template 綁定變更 best-effort push（#111 語意）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.proj = cls.env['project.project'].sudo().create({'name': 'TP'})
        cls.skill = _mk_skill(cls.env, name='tp-sys', state='published')
        cls.tpl = cls.env['woow_paas_platform.agent_template'].sudo().create({
            'name': 'TP Tpl',
            'project_ids': [(6, 0, [cls.proj.id])],
            'state': 'published',
        })

    def test_skill_binding_change_pushes(self):
        with patch.object(
            type(self.env['woow_paas_platform.agent_session']),
            '_push_snapshot_to_running_pods',
        ) as push:
            self.tpl.write({'skill_ids': [(4, self.skill.id)]})
        self.assertIn(
            ('skill', 'user', self.proj.id),
            {c.args[:3] for c in push.call_args_list})

    def test_non_binding_write_does_not_push(self):
        with patch.object(
            type(self.env['woow_paas_platform.agent_session']),
            '_push_snapshot_to_running_pods',
        ) as push:
            self.tpl.write({'description': 'no push'})
        push.assert_not_called()
