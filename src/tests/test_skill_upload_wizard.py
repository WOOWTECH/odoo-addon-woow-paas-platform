import base64
import hashlib
import io
import tarfile
import uuid

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


def _make_targz(files, links=None):
    """Build an in-memory .tar.gz from {path: text} and return raw bytes.

    ``links`` is an optional {link_path: target} mapping; each is added as a
    symlink member (used by the security negative tests).
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        for path, content in files.items():
            data = content.encode('utf-8')
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for link_path, target in (links or {}).items():
            info = tarfile.TarInfo(name=link_path)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    return buf.getvalue()


# A compliant bundle: SKILL.md at tar root with name + description, plus the
# default entrypoint scripts/main.py present.
def _compliant_skill_md(name='demo', description='A demo skill'):
    return (
        '---\n'
        'name: %s\n'
        'description: %s\n'
        'version: 0.1.0\n'
        '---\n'
        '# Demo\n'
    ) % (name, description)


@tagged('post_install', '-at_install')
class TestSkillUploadWizard(TransactionCase):
    """Backend wizard uploads a .tar.gz → draft system skill (Odoo stores
    bytes only, never parses the bundle)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wizard = cls.env['woow_paas_platform.skill_upload_wizard']
        cls.Skill = cls.env['woow_paas_platform.skill']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        # Default fixture is a compliant bundle: root SKILL.md with name +
        # description and the default entrypoint scripts/main.py present, so
        # the happy-path tests pass the new structural validation. The wizard
        # name (up-<suffix>) intentionally differs from the frontmatter name
        # only where a test overrides it; the base SKILL.md uses the wizard
        # name to avoid a spurious name-mismatch warning.
        self.raw = _make_targz({
            'SKILL.md': _compliant_skill_md(
                name=f'up-{self.suffix}', description='via wizard'),
            'scripts/main.py': 'def run(x=""):\n    return x\n',
        })

    def _new_wizard(self, **overrides):
        vals = {
            'name': f'up-{self.suffix}',
            'version': '0.1.0',
            'title': 'Uploaded Skill',
            'description': 'via wizard',
            'bundle_file': base64.b64encode(self.raw),
            'bundle_filename': 'demo.tar.gz',
        }
        vals.update(overrides)
        return self.Wizard.create(vals)

    def test_upload_creates_draft_system_skill(self):
        wiz = self._new_wizard()
        wiz.action_upload()
        skill = self.Skill.search([
            ('name', '=', f'up-{self.suffix}'), ('version', '=', '0.1.0')])
        self.assertEqual(len(skill), 1)
        self.assertEqual(skill.scope, 'system')
        self.assertFalse(skill.project_id)
        self.assertEqual(skill.state, 'draft')
        self.assertEqual(skill.title, 'Uploaded Skill')
        self.assertEqual(skill.description, 'via wizard')
        # attachment anchored by skill.create() hook (Phase 1)
        self.assertTrue(skill.bundle_attachment_id)
        self.assertEqual(
            skill.bundle_attachment_id.res_model, 'woow_paas_platform.skill')
        self.assertEqual(skill.bundle_attachment_id.res_id, skill.id)
        self.assertFalse(skill.bundle_attachment_id.public)

    def test_sha256_matches_raw_bytes(self):
        wiz = self._new_wizard()
        wiz.action_upload()
        skill = self.Skill.search([('name', '=', f'up-{self.suffix}')], limit=1)
        self.assertEqual(
            skill.bundle_sha256, hashlib.sha256(self.raw).hexdigest(),
            "sha256 must be computed over the raw tar.gz bytes (pod contract)")

    def test_bundle_size_kb_computed(self):
        wiz = self._new_wizard()
        wiz.action_upload()
        skill = self.Skill.search([('name', '=', f'up-{self.suffix}')], limit=1)
        self.assertEqual(skill.bundle_size_kb, -(-len(self.raw) // 1024))

    def test_duplicate_name_version_hard_error(self):
        self._new_wizard().action_upload()
        with self.assertRaises(UserError):
            self._new_wizard().action_upload()

    def test_oversize_bundle_rejected(self):
        big = base64.b64encode(b'x' * (10 * 1024 * 1024 + 1))
        wiz = self._new_wizard(bundle_file=big)
        with self.assertRaises(UserError):
            wiz.action_upload()

    def test_returns_form_action_to_new_skill(self):
        wiz = self._new_wizard()
        action = wiz.action_upload()
        skill = self.Skill.search([('name', '=', f'up-{self.suffix}')], limit=1)
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'woow_paas_platform.skill')
        self.assertEqual(action['res_id'], skill.id)

    def test_non_gzip_junk_rejected(self):
        # (Was test_upload_does_not_extract_bundle.) issue #154 flips the
        # behaviour: non-tar.gz junk is now rejected at upload time with a
        # UserError, and NO skill / attachment row is created.
        junk = b'this is not a tar gz at all \x00\x01\x02'
        wiz = self._new_wizard(bundle_file=base64.b64encode(junk))
        with self.assertRaises(UserError):
            wiz.action_upload()
        self.assertFalse(
            self.Skill.search([('name', '=', f'up-{self.suffix}')]),
            "broken bundle must not create a skill row")

    # --- Hard rejections (one negative per rule) ---------------------------

    def test_skill_md_under_wrapper_dir_rejected(self):
        raw = _make_targz({
            'mypkg/SKILL.md': _compliant_skill_md(name=f'up-{self.suffix}'),
            'mypkg/scripts/main.py': 'x = 1\n',
        })
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        msg = str(cm.exception)
        self.assertIn('SKILL.md', msg)
        self.assertIn("wrapper dir 'mypkg/'", msg)
        self.assertFalse(self.Skill.search(
            [('name', '=', f'up-{self.suffix}')]))

    def test_missing_skill_md_rejected(self):
        raw = _make_targz({'scripts/main.py': 'x = 1\n'})
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        self.assertIn('SKILL.md', str(cm.exception))

    def test_missing_name_rejected(self):
        raw = _make_targz({
            'SKILL.md': '---\ndescription: no name here\n---\n# X\n',
            'scripts/main.py': 'x = 1\n',
        })
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        self.assertIn('name', str(cm.exception))

    def test_missing_description_rejected(self):
        raw = _make_targz({
            'SKILL.md': '---\nname: x\n---\n# X\n',
            'scripts/main.py': 'x = 1\n',
        })
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        self.assertIn('description', str(cm.exception))

    def test_empty_name_value_counts_as_missing(self):
        raw = _make_targz({
            'SKILL.md': '---\nname:\ndescription: d\n---\n# X\n',
            'scripts/main.py': 'x = 1\n',
        })
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        self.assertIn('name', str(cm.exception))

    def test_path_traversal_member_rejected(self):
        raw = _make_targz({
            'SKILL.md': _compliant_skill_md(name=f'up-{self.suffix}'),
            'scripts/main.py': 'x = 1\n',
            '../evil.py': 'pwn = 1\n',
        })
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        self.assertIn('..', str(cm.exception))

    def test_absolute_path_member_rejected(self):
        raw = _make_targz({
            'SKILL.md': _compliant_skill_md(name=f'up-{self.suffix}'),
            '/etc/evil.py': 'pwn = 1\n',
        })
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        self.assertIn('escapes bundle root', str(cm.exception))

    def test_symlink_member_rejected(self):
        raw = _make_targz(
            {
                'SKILL.md': _compliant_skill_md(name=f'up-{self.suffix}'),
                'scripts/main.py': 'x = 1\n',
            },
            links={'link.py': '/etc/passwd'},
        )
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        self.assertIn('symlink', str(cm.exception))

    def test_member_count_bomb_rejected(self):
        files = {'SKILL.md': _compliant_skill_md(name=f'up-{self.suffix}'),
                 'scripts/main.py': 'x = 1\n'}
        for i in range(2100):
            files['f%d.txt' % i] = 'x'
        raw = _make_targz(files)
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        self.assertIn('member', str(cm.exception))

    def test_multiple_violations_listed_at_once(self):
        # wrapper dir + missing name in the SAME bundle: the error must
        # mention both, not just the first one.
        raw = _make_targz({
            'mypkg/SKILL.md': '---\ndescription: d\n---\n# X\n',
            'mypkg/scripts/main.py': 'x = 1\n',
        })
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        msg = str(cm.exception)
        self.assertIn('SKILL.md', msg)
        self.assertIn('name', msg)

    # --- Soft warnings (skill IS created, notification returned) -----------

    def _assert_warning_action(self, action):
        self.assertEqual(action['type'], 'ir.actions.client')
        self.assertEqual(action['tag'], 'display_notification')
        self.assertEqual(action['params']['type'], 'warning')
        self.assertTrue(action['params']['sticky'])
        nxt = action['params']['next']
        self.assertEqual(nxt['type'], 'ir.actions.act_window')
        self.assertEqual(nxt['res_model'], 'woow_paas_platform.skill')
        return nxt

    def test_warn_missing_entrypoint_still_creates(self):
        # Compliant SKILL.md but no scripts/main.py present.
        raw = _make_targz({
            'SKILL.md': _compliant_skill_md(name=f'up-{self.suffix}'),
        })
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        action = wiz.action_upload()
        skill = self.Skill.search([('name', '=', f'up-{self.suffix}')], limit=1)
        self.assertEqual(len(skill), 1)
        nxt = self._assert_warning_action(action)
        self.assertEqual(nxt['res_id'], skill.id)
        self.assertIn('entrypoint', action['params']['message'])

    def test_warn_appledouble_still_creates(self):
        raw = _make_targz({
            'SKILL.md': _compliant_skill_md(name=f'up-{self.suffix}'),
            'scripts/main.py': 'x = 1\n',
            '._SKILL.md': 'junk',
        })
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        action = wiz.action_upload()
        skill = self.Skill.search([('name', '=', f'up-{self.suffix}')], limit=1)
        self.assertEqual(len(skill), 1)
        self._assert_warning_action(action)
        self.assertIn('COPYFILE_DISABLE=1', action['params']['message'])

    def test_warn_name_mismatch_still_creates(self):
        # frontmatter name differs from the wizard-entered name.
        raw = _make_targz({
            'SKILL.md': _compliant_skill_md(name='something-else'),
            'scripts/main.py': 'x = 1\n',
        })
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        action = wiz.action_upload()
        skill = self.Skill.search([('name', '=', f'up-{self.suffix}')], limit=1)
        self.assertEqual(len(skill), 1)
        self._assert_warning_action(action)
        self.assertIn('something-else', action['params']['message'])
