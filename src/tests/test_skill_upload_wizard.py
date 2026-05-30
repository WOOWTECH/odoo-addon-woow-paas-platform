import base64
import hashlib
import io
import tarfile
import uuid

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


def _make_targz(files):
    """Build an in-memory .tar.gz from {path: text} and return raw bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        for path, content in files.items():
            data = content.encode('utf-8')
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


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
        self.raw = _make_targz({
            'SKILL.md': '---\nname: demo\nversion: 0.1.0\n---\n# Demo\n',
            'src/main.py': 'def run(x=""):\n    return x\n',
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

    def test_upload_does_not_extract_bundle(self):
        # Feed non-tar.gz random bytes: Odoo must NOT try to parse it.
        # The skill is still created; sha256 is computed over the raw bytes.
        junk = b'this is not a tar gz at all \x00\x01\x02'
        wiz = self._new_wizard(bundle_file=base64.b64encode(junk))
        wiz.action_upload()
        skill = self.Skill.search([('name', '=', f'up-{self.suffix}')], limit=1)
        self.assertEqual(skill.state, 'draft')
        self.assertEqual(
            skill.bundle_sha256, hashlib.sha256(junk).hexdigest(),
            "Odoo stores bytes verbatim and does not validate bundle structure")
