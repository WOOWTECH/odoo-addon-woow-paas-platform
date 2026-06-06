import base64
import hashlib
import io
import tarfile
import uuid

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


def _make_targz(files, links=None, special=None):
    """Build an in-memory .tar.gz from {path: text} and return raw bytes.

    ``links`` is an optional {link_path: target} mapping; each is added as a
    symlink member (used by the security negative tests).
    ``special`` is an optional {path: typeflag} mapping for device / FIFO /
    other special-file members (used by the special-file negative tests).
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
        for path, typeflag in (special or {}).items():
            info = tarfile.TarInfo(name=path)
            info.type = typeflag
            info.size = 0
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
        # issue #154 follow-up M1: the cap must be enforced by STREAMING
        # iteration (early-abort), not by materialising the whole member list
        # first (tar.getmembers() on a million-member bomb is itself the DoS).
        # Monkeypatch a tiny cap and build only cap+5 members so the fixture
        # stays cheap; assert the scan stops at cap+1 instead of reading all.
        from odoo.addons.woow_paas_platform.wizard import (
            skill_bundle_validation as sbv)
        original_cap = sbv.MAX_MEMBER_COUNT
        sbv.MAX_MEMBER_COUNT = 5
        # Count how many members the streaming scan actually reads via the
        # path-traversal check, which the loop body runs once per member.
        seen = []
        original_is_traversal = sbv._is_path_traversal

        def _counting(name):
            seen.append(name)
            return original_is_traversal(name)

        sbv._is_path_traversal = _counting
        try:
            files = {'SKILL.md': _compliant_skill_md(name=f'up-{self.suffix}'),
                     'scripts/main.py': 'x = 1\n'}
            for i in range(10):  # 12 members total, far above the cap of 5
                files['f%d.txt' % i] = 'x'
            raw = _make_targz(files)
            wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
            with self.assertRaises(UserError) as cm:
                wiz.action_upload()
            self.assertIn('member cap', str(cm.exception))
            # Early-abort: the loop body runs for at most cap+1 members (the
            # +1 is the member that trips the cap), never the full 12.
            self.assertLessEqual(
                len(seen), sbv.MAX_MEMBER_COUNT + 1,
                "member-count guard must early-abort, not materialise all "
                "members (read %d, cap+1=%d)" % (
                    len(seen), sbv.MAX_MEMBER_COUNT + 1))
            self.assertLess(
                len(seen), 12, "must not have scanned the whole bomb")
        finally:
            sbv.MAX_MEMBER_COUNT = original_cap
            sbv._is_path_traversal = original_is_traversal

    def test_member_name_too_long_rejected(self):
        # M1: a member with a pathologically long name is a getmembers()
        # amplification vector; reject it before reading the body.
        from odoo.addons.woow_paas_platform.wizard import (
            skill_bundle_validation as sbv)
        original = sbv.MAX_MEMBER_NAME_LENGTH
        sbv.MAX_MEMBER_NAME_LENGTH = 64
        try:
            raw = _make_targz({
                'SKILL.md': _compliant_skill_md(name=f'up-{self.suffix}'),
                'scripts/main.py': 'x = 1\n',
                ('a' * 200 + '.txt'): 'x',
            })
            wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
            with self.assertRaises(UserError) as cm:
                wiz.action_upload()
            self.assertIn('characters', str(cm.exception))
        finally:
            sbv.MAX_MEMBER_NAME_LENGTH = original

    def test_fifo_member_rejected(self):
        # M2: a FIFO member makes the runtime's data extract filter raise
        # SpecialFileError and abort the whole bundle on the pod; reject it
        # at upload time.
        raw = _make_targz(
            {'SKILL.md': _compliant_skill_md(name=f'up-{self.suffix}'),
             'scripts/main.py': 'x = 1\n'},
            special={'a_fifo': tarfile.FIFOTYPE},
        )
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        self.assertIn('special files', str(cm.exception))
        self.assertFalse(self.Skill.search(
            [('name', '=', f'up-{self.suffix}')]))

    def test_device_member_rejected(self):
        # M2: char-device member, same SpecialFileError abort risk as FIFO.
        raw = _make_targz(
            {'SKILL.md': _compliant_skill_md(name=f'up-{self.suffix}'),
             'scripts/main.py': 'x = 1\n'},
            special={'a_dev': tarfile.CHRTYPE},
        )
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        self.assertIn('special files', str(cm.exception))

    def test_malformed_yaml_tab_indent_rejected(self):
        # M3: a tab-indented frontmatter line makes yaml.safe_load raise a
        # ScannerError on the pod (loader.parse_skill_frontmatter); the wizard
        # mirrors that parser and rejects it up front. Skip when PyYAML is
        # absent (stdlib fallback cannot detect YAML grammar errors — a
        # documented contract gap, but production always has PyYAML).
        from odoo.addons.woow_paas_platform.wizard import (
            skill_bundle_validation as sbv)
        if sbv.yaml is None:
            self.skipTest("PyYAML unavailable; stdlib fallback cannot detect "
                          "YAML grammar errors")
        raw = _make_targz({
            'SKILL.md': '---\nname: x\n\tbad: y\ndescription: d\n---\n# X\n',
            'scripts/main.py': 'x = 1\n',
        })
        wiz = self._new_wizard(name='x', bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        self.assertIn('not valid YAML', str(cm.exception))
        self.assertFalse(self.Skill.search([('name', '=', 'x')]))

    def test_malformed_yaml_unterminated_quote_rejected(self):
        # M3: an unterminated quote is a ScannerError on the pod too.
        from odoo.addons.woow_paas_platform.wizard import (
            skill_bundle_validation as sbv)
        if sbv.yaml is None:
            self.skipTest("PyYAML unavailable; stdlib fallback cannot detect "
                          "YAML grammar errors")
        raw = _make_targz({
            'SKILL.md': '---\nname: "unterminated\ndescription: d\n---\n# X\n',
            'scripts/main.py': 'x = 1\n',
        })
        wiz = self._new_wizard(name='x', bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        self.assertIn('not valid YAML', str(cm.exception))

    def test_frontmatter_inline_comment_no_false_mismatch(self):
        # M3 (also fixes should-fix S2): `name: foo # comment` must parse to
        # `foo`, not `foo # comment`, so it does NOT raise a spurious
        # name-mismatch warning against the wizard name `foo`. Requires the
        # yaml path (stdlib fallback keeps the comment in the value).
        from odoo.addons.woow_paas_platform.wizard import (
            skill_bundle_validation as sbv)
        if sbv.yaml is None:
            self.skipTest("PyYAML unavailable; stdlib fallback keeps the "
                          "inline comment in the value")
        raw = _make_targz({
            'SKILL.md': '---\nname: foo # the slug\ndescription: bar\n---\n# X\n',
            'scripts/main.py': 'x = 1\n',
        })
        wiz = self._new_wizard(name='foo', bundle_file=base64.b64encode(raw))
        action = wiz.action_upload()
        # Compliant + name matches -> plain act_window, no warning notification.
        self.assertEqual(action['type'], 'ir.actions.act_window')
        skill = self.Skill.search([('name', '=', 'foo')], limit=1)
        self.assertEqual(len(skill), 1)

    def test_in_bounds_relative_symlink_still_rejected(self):
        # should-fix S1: the runtime's data filter PERMITS in-bounds relative
        # links, but the wizard deliberately hard-blocks ALL links
        # (defense-in-depth). Pin that intentional over-block, and assert the
        # message is honest about being stricter than the runtime.
        raw = _make_targz(
            {'SKILL.md': _compliant_skill_md(name=f'up-{self.suffix}'),
             'scripts/main.py': 'x = 1\n'},
            links={'alias.py': 'scripts/main.py'},  # in-bounds, relative
        )
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        msg = str(cm.exception)
        self.assertIn('link member', msg)
        self.assertIn('stricter than the runtime', msg)
        self.assertFalse(self.Skill.search(
            [('name', '=', f'up-{self.suffix}')]))

    def test_skill_md_as_directory_rejected(self):
        # N2: a DIRTYPE member named SKILL.md must report "exists as a
        # directory, not a file", not the generic "not present at any level".
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tf:
            di = tarfile.TarInfo(name='SKILL.md')
            di.type = tarfile.DIRTYPE
            tf.addfile(di)
            data = b'x = 1\n'
            fi = tarfile.TarInfo(name='scripts/main.py')
            fi.size = len(data)
            tf.addfile(fi, io.BytesIO(data))
        wiz = self._new_wizard(bundle_file=base64.b64encode(buf.getvalue()))
        with self.assertRaises(UserError) as cm:
            wiz.action_upload()
        self.assertIn('exists as a directory', str(cm.exception))

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
