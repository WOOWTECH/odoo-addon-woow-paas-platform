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
