import base64
import importlib.util
import io
import os
import tarfile
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged


def _compliant_bundle_b64(name='wz-skill'):
    """Build a base64 .tar.gz that passes the wizard's structural validation
    (issue #154): root SKILL.md with name + description + default entrypoint
    scripts/main.py. frontmatter name matches the wizard name so no soft
    warning fires and action_upload returns the plain act_window.
    """
    files = {
        'SKILL.md': (
            '---\nname: %s\ndescription: bound skill\n---\n# X\n' % name),
        'scripts/main.py': 'def run(x=""):\n    return x\n',
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        for path, content in files.items():
            data = content.encode('utf-8')
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue())


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


@tagged('post_install', '-at_install')
class TestWizardUserScope(TransactionCase):
    """§2.2 — wizard per-scope 上傳與查重。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wizard = cls.env['woow_paas_platform.skill_upload_wizard']
        cls.proj = cls.env['project.project'].sudo().create({'name': 'WZ'})
        cls.proj2 = cls.env['project.project'].sudo().create({'name': 'WZ2'})
        # issue #154: the wizard now validates tar structure on upload, so the
        # fixture must be a compliant bundle (was b64("hello"), which is not a
        # gzip tar). frontmatter name matches the wizard name (wz-skill).
        cls.bundle = _compliant_bundle_b64(name='wz-skill')

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

    def test_fork_does_not_clobber_platform_default(self):
        """fork 只動本 project 的 explicit pick，不得改寫平台預設 template
        指向或把它綁進 project（default-agent-template-project-bind-silent-
        autoenable pitfall 守線：平台預設必須維持 unbound + 指向不變）。"""
        # 合格平台預設：unbound + published + public（tenant-neutral）。
        platform_default = self.env['woow_paas_platform.agent_template'] \
            .sudo().create({
                'name': 'FK Platform Default',
                'state': 'published',
                'is_public': True,
            })
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.default_agent_template_id',
            str(platform_default.id))

        self.proj.action_create_dedicated_agent_template()

        self.assertEqual(
            self.env['ir.config_parameter'].sudo().get_param(
                'woow_paas_platform.default_agent_template_id'),
            str(platform_default.id),
            "fork 不得改寫 default_agent_template_id 指向。",
        )
        platform_default.invalidate_recordset()
        self.assertFalse(
            platform_default.project_ids,
            "fork 不得把平台預設 template 綁進 project（必須維持 unbound）。",
        )


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


def _inject_dual_bind(env, template, *skills):
    """Insert agent_template_skill_rel rows by raw SQL, bypassing the ORM
    (and therefore the _check_no_duplicate_skill_name constraint that the
    rest of issue #156 lands). Simulates pre-constraint legacy stock data:
    a template that already binds two same-named skill rows from a time
    before the duplicate-name guard existed. Tests that exercise the
    snapshot dedup / fork-propagation paths need this state but cannot reach
    it through normal create/write once the constraint is live.
    """
    for skill in skills:
        env.cr.execute(
            "INSERT INTO agent_template_skill_rel (template_id, skill_id) "
            "VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (template.id, skill.id),
        )
    # Raw SQL bypasses the ORM cache — force a reload so subsequent reads of
    # template.skill_ids see the injected rows.
    env.invalidate_all()


@tagged('post_install', '-at_install')
class TestSkillDisplayName(TransactionCase):
    """§D1/D2 — display_name disambiguates same-name skills by version +
    scope label; _rec_names_search lets the M2M tag picker match on
    name/version. display_name never enters the pod wire contract."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.project = cls.env['project.project'].sudo().create(
            {'name': 'DN Project'})

    def test_display_name_system_scope_format(self):
        sk = _mk_skill(self.env, name='test-echo', version='0.1.0',
                       scope='system')
        dn = sk.display_name
        self.assertIn('v0.1.0', dn)
        self.assertIn('System', dn)
        self.assertTrue(dn.startswith('test-echo'))

    def test_display_name_user_scope_format(self):
        sk = _mk_skill(self.env, name='test-echo', version='0.1.0',
                       scope='user', project_id=self.project.id)
        dn = sk.display_name
        self.assertRegex(dn, r'v\d+\.\d+\.\d+')
        self.assertIn('User', dn)

    def test_display_name_distinguishes_same_name_cross_scope(self):
        sys_sk = _mk_skill(self.env, name='test-echo', version='0.1.0',
                           scope='system')
        usr_sk = _mk_skill(self.env, name='test-echo', version='0.1.0',
                           scope='user', project_id=self.project.id)
        self.assertNotEqual(sys_sk.display_name, usr_sk.display_name)

    def test_display_name_distinguishes_same_name_versions(self):
        v1 = _mk_skill(self.env, name='test-echo', version='0.1.0',
                       scope='system')
        v2 = _mk_skill(self.env, name='test-echo', version='0.2.0',
                       scope='system')
        self.assertNotEqual(v1.display_name, v2.display_name)

    def test_display_name_recomputes_on_version_write(self):
        sk = _mk_skill(self.env, name='test-echo', version='0.1.0',
                       scope='system')
        before = sk.display_name
        sk.write({'version': '0.9.9'})
        after = sk.display_name
        self.assertNotEqual(before, after)
        self.assertIn('v0.9.9', after)

    def test_rec_names_search_by_name(self):
        _mk_skill(self.env, name='test-echo', version='0.1.0', scope='system')
        found = self.Skill.name_search('test-echo')
        self.assertTrue(found, "name_search by name must match")
        self.assertIn('test-echo',
                      ' '.join(label for _id, label in found))

    def test_rec_names_search_by_version(self):
        _mk_skill(self.env, name='rec-ver', version='7.7.7', scope='system')
        found = self.Skill.name_search('7.7.7')
        ids = [_id for _id, _label in found]
        self.assertTrue(ids, "name_search by version string must match")


@tagged('post_install', '-at_install')
class TestTemplateNoDuplicateSkillName(TransactionCase):
    """§D3 — agent_template rejects binding two skills with the same name.
    A pod loads skills by name, so two same-name bindings would collide.
    The guard fires on direct form write, inverse project write, and fork
    propagation; it is NOT bypassed for superuser (no legitimate same-name
    dual-bind path exists)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Template = cls.env['woow_paas_platform.agent_template']
        cls.project = cls.env['project.project'].sudo().create(
            {'name': 'DUP Project'})
        # Two published same-name skills: one system, one user bound to the
        # project (so _check_skill_subset is satisfied — the only failing
        # constraint should be the new duplicate-name guard).
        cls.sys_skill = _mk_skill(
            cls.env, name='test-echo', version='0.1.0', scope='system',
            state='published')
        cls.usr_skill = _mk_skill(
            cls.env, name='test-echo', version='0.2.0', scope='user',
            project_id=cls.project.id, state='published')
        # A differently-named user skill on the same project for the
        # negative (different-names-ok) case.
        cls.other_skill = _mk_skill(
            cls.env, name='other-skill', version='1.0.0', scope='user',
            project_id=cls.project.id, state='published')

    def test_template_form_direct_dual_bind_rejected(self):
        with self.assertRaises(ValidationError):
            self.Template.sudo().create({
                'name': 'DUP Direct',
                'project_ids': [(6, 0, [self.project.id])],
                'skill_ids': [(6, 0, [self.sys_skill.id, self.usr_skill.id])],
            })

    def test_template_write_add_duplicate_rejected(self):
        tpl = self.Template.sudo().create({
            'name': 'DUP Write',
            'project_ids': [(6, 0, [self.project.id])],
            'skill_ids': [(6, 0, [self.sys_skill.id])],
        })
        with self.assertRaises(ValidationError):
            tpl.write({'skill_ids': [(4, self.usr_skill.id)]})

    def test_project_inverse_dual_bind_rejected(self):
        # Build a dedicated template for the project (fork), then push a
        # same-name dual-bind through the resolved_template_skill_ids inverse.
        self.project.action_create_dedicated_agent_template()
        self.project.invalidate_recordset()
        self.assertEqual(
            self.project.agent_template_binding_status, 'dedicated')
        with self.assertRaises(ValidationError):
            self.project.resolved_template_skill_ids = [
                (6, 0, [self.sys_skill.id, self.usr_skill.id])]

    def test_fork_propagates_caught_existing_source_dup(self):
        # A shared source template carries a legacy same-name dual-bind
        # (system + this-project user). Forking copies the final values, so
        # the dedicated fork inherits both same-name rows and must trip the
        # constraint at copy(default=) create time.
        # The source must bind MORE than this project: project_ids == project
        # computes binding_status 'dedicated' and the fork action gates on it
        # before the constraint could ever fire.
        other_project = self.env['project.project'].sudo().create(
            {'name': 'DUP Other Project'})
        source = self.Template.sudo().create({
            'name': 'DUP Source',
            'project_ids': [(6, 0, [self.project.id, other_project.id])],
            'state': 'published',
            'is_public': True,
        })
        _inject_dual_bind(self.env, source, self.sys_skill, self.usr_skill)
        # Point the project at the tainted shared source so the fork action
        # resolves and copies it.
        self.project.agent_template_id = source
        self.project.invalidate_recordset()
        with self.assertRaises(ValidationError):
            self.project.action_create_dedicated_agent_template()

    def test_constraint_not_bypassed_for_superuser(self):
        with self.assertRaises(ValidationError):
            self.Template.with_user(self.env.ref('base.user_root')).create({
                'name': 'DUP Superuser',
                'project_ids': [(6, 0, [self.project.id])],
                'skill_ids': [(6, 0, [self.sys_skill.id, self.usr_skill.id])],
            })

    def test_duplicate_error_message_lists_name_and_guides(self):
        with self.assertRaisesRegex(ValidationError, r'test-echo') as cm:
            self.Template.sudo().create({
                'name': 'DUP Msg',
                'project_ids': [(6, 0, [self.project.id])],
                'skill_ids': [(6, 0, [self.sys_skill.id, self.usr_skill.id])],
            })
        msg = str(cm.exception)
        self.assertIn('Unbind', msg)
        # Must not leak the winning skill's identity or the dedup mechanics.
        self.assertNotIn('sha256', msg.lower())
        self.assertNotIn(str(self.sys_skill.id), msg)
        self.assertNotIn(str(self.usr_skill.id), msg)

    def test_different_names_bind_ok(self):
        tpl = self.Template.sudo().create({
            'name': 'DUP None',
            'project_ids': [(6, 0, [self.project.id])],
            'skill_ids': [(6, 0, [self.sys_skill.id, self.other_skill.id])],
        })
        self.assertIn(self.sys_skill, tpl.skill_ids)
        self.assertIn(self.other_skill, tpl.skill_ids)

    def test_batch_create_multi_template_rejected(self):
        # The constraint loops over self — a batch create where only the
        # SECOND record carries the dup must still raise (no first-record-
        # only blind spot).
        with self.assertRaises(ValidationError):
            self.Template.sudo().create([
                {
                    'name': 'DUP Batch OK',
                    'project_ids': [(6, 0, [self.project.id])],
                    'skill_ids': [(6, 0, [self.other_skill.id])],
                },
                {
                    'name': 'DUP Batch BAD',
                    'project_ids': [(6, 0, [self.project.id])],
                    'skill_ids': [(6, 0, [self.sys_skill.id,
                                          self.usr_skill.id])],
                },
            ])


@tagged('post_install', '-at_install')
class TestSnapshotDedup(TransactionCase):
    """§D4 — _build_skill_snapshot tolerates legacy same-name dual-bind:
    dict-by-name dedup (user-scope wins over system, mirroring the
    project-leg-over-system endpoint precedence), logs a warning, never
    raises, and emits the unchanged {name,version,sha256,description} wire
    keys (no _scope leak)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Session = cls.env['woow_paas_platform.agent_session']
        cls.project = cls.env['project.project'].sudo().create(
            {'name': 'DEDUP Project'})
        cls.sys_skill = _mk_skill(
            cls.env, name='test-echo', version='0.1.0', scope='system',
            bundle_sha256='b' * 64, state='published')
        cls.usr_skill = _mk_skill(
            cls.env, name='test-echo', version='0.2.0', scope='user',
            project_id=cls.project.id, bundle_sha256='c' * 64,
            state='published')
        # Legacy template with the same-name dual-bind injected past the
        # constraint (simulates pre-#156 stock data).
        cls.template = cls.env['woow_paas_platform.agent_template'].sudo() \
            .create({
                'name': 'DEDUP Tpl',
                'project_ids': [(6, 0, [cls.project.id])],
                'state': 'published',
            })
        _inject_dual_bind(cls.env, cls.template, cls.sys_skill, cls.usr_skill)

    def test_snapshot_dedupes_existing_dual_bind_keeping_user_scope(self):
        snap = self.Session._build_skill_snapshot(self.project, self.template)
        echo = [e for e in snap if e['name'] == 'test-echo']
        self.assertEqual(len(echo), 1, "same-name dual-bind must collapse")
        self.assertEqual(echo[0]['version'], '0.2.0')  # user-scope wins
        self.assertEqual(echo[0]['sha256'], 'c' * 64)

    def test_snapshot_never_raises_on_dual_bind(self):
        try:
            self.Session._build_skill_snapshot(self.project, self.template)
        except Exception as exc:  # noqa: BLE001 — intent: must not raise
            self.fail("snapshot must not raise on legacy dual-bind: %r" % exc)

    def test_snapshot_dedup_emits_warning(self):
        with self.assertLogs(
                'odoo.addons.woow_paas_platform.models.agent_session',
                level='WARNING'):
            self.Session._build_skill_snapshot(self.project, self.template)

    def test_snapshot_entry_keys_unchanged_after_dedup(self):
        snap = self.Session._build_skill_snapshot(self.project, self.template)
        self.assertTrue(snap)
        for entry in snap:
            self.assertEqual(
                set(entry), {'name', 'version', 'sha256', 'description'},
                "wire contract keys must stay {name,version,sha256,"
                "description} — no _scope leak")

    def test_snapshot_triple_same_name_collapses_to_one(self):
        # Three same-name rows (system + two user versions — both partial
        # unique indexes allow this) must still collapse to ONE entry and
        # never raise, with a user-scope row winning over system.
        third = _mk_skill(
            self.env, name='test-echo', version='0.1.0', scope='user',
            project_id=self.project.id, bundle_sha256='d' * 64,
            state='published')
        _inject_dual_bind(self.env, self.template, third)
        snap = self.Session._build_skill_snapshot(self.project, self.template)
        echo = [e for e in snap if e['name'] == 'test-echo']
        self.assertEqual(len(echo), 1, "triple same-name must collapse too")
        self.assertIn(echo[0]['sha256'], {'c' * 64, 'd' * 64},
                      "a user-scope row must win over the system one")


def _load_migrate_34():
    """Load migrate(cr, version) from 18.0.1.0.34/post-migrate.py via
    importlib (hyphenated filename — same convention as the 18.0.1.0.26
    migration tests). None = file missing, the canonical red signal."""
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), '..', 'migrations', '18.0.1.0.34',
        'post-migrate.py'))
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(
        'skill_dual_bind_post_migrate_18_0_1_0_34', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, 'migrate', None)


@tagged('post_install', '-at_install')
class TestDualBindMigration(TransactionCase):
    """§D5 — conservative legacy clean-up in 18.0.1.0.34/post-migrate.py:
    a clear (user, system) same-name pair drops the system rel row (same
    direction as the runtime dedup / bundle endpoint), a same-scope pair is
    AMBIGUOUS and must be left untouched (warning only), and the whole
    migration is idempotent."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.project = cls.env['project.project'].sudo().create(
            {'name': 'MIG Project'})
        # Case 1: clear (user, system) same-name pair.
        cls.mig_sys = _mk_skill(
            cls.env, name='mig-echo', version='0.1.0', scope='system',
            state='published')
        cls.mig_usr = _mk_skill(
            cls.env, name='mig-echo', version='0.2.0', scope='user',
            project_id=cls.project.id, state='published')
        # Case 2: ambiguous same-scope pair (two user versions).
        cls.twin_a = _mk_skill(
            cls.env, name='mig-twin', version='0.1.0', scope='user',
            project_id=cls.project.id, state='published')
        cls.twin_b = _mk_skill(
            cls.env, name='mig-twin', version='0.2.0', scope='user',
            project_id=cls.project.id, state='published')
        cls.template = cls.env['woow_paas_platform.agent_template'].sudo() \
            .create({
                'name': 'MIG Tpl',
                'project_ids': [(6, 0, [cls.project.id])],
                'skill_ids': [(6, 0, [cls.mig_usr.id, cls.twin_a.id])],
            })
        _inject_dual_bind(cls.env, cls.template, cls.mig_sys, cls.twin_b)

    def _bound(self):
        self.env.invalidate_all()
        return sorted(
            (s.name, s.scope, s.version) for s in self.template.skill_ids)

    def test_migration_file_exposes_migrate(self):
        self.assertIsNotNone(
            _load_migrate_34(),
            "src/migrations/18.0.1.0.34/post-migrate.py missing or doesn't "
            "expose migrate(cr, version)")

    def test_migration_drops_system_leg_keeps_user_and_ambiguous(self):
        migrate = _load_migrate_34()
        migrate(self.env.cr, '18.0.1.0.33')
        bound = self._bound()
        self.assertNotIn(('mig-echo', 'system', '0.1.0'), bound,
                         "clear (user,system) pair: system leg must go")
        self.assertIn(('mig-echo', 'user', '0.2.0'), bound,
                      "clear (user,system) pair: user leg must survive")
        self.assertIn(('mig-twin', 'user', '0.1.0'), bound,
                      "ambiguous same-scope pair must be left untouched")
        self.assertIn(('mig-twin', 'user', '0.2.0'), bound,
                      "ambiguous same-scope pair must be left untouched")

    def test_migration_is_idempotent(self):
        migrate = _load_migrate_34()
        migrate(self.env.cr, '18.0.1.0.33')
        after_first = self._bound()
        migrate(self.env.cr, '18.0.1.0.33')
        self.assertEqual(self._bound(), after_first,
                         "second run must be a no-op")
