"""TDD — skill admin backend lifecycle transitions + is_latest invariant.

Spec: docs/plans/2026-Q2/2026-05-27-skill-admin-backend-ui-design.md §①
"""
import hashlib
import uuid
from datetime import timedelta

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSkillAdminLifecycle(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'woow_paas_platform.skill' not in cls.env.registry:
            raise RuntimeError(
                "prerequisite: 'woow_paas_platform.skill' not registered."
            )
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.project = cls.env['project.project'].sudo().create(
            {'name': 'Skill-LC %s' % uuid.uuid4().hex[:8]}
        )

    def _make_skill(self, name, version, state='draft'):
        """Create a skill with the required bundle_sha256 (default draft)."""
        digest = hashlib.sha256(
            ('%s-%s' % (name, version)).encode()
        ).hexdigest()
        return self.Skill.sudo().create({
            'name': name,
            'version': version,
            'project_id': self.project.id,
            'bundle_sha256': digest,
            'state': state,
        })

    def _latest_count(self, name):
        return self.Skill.sudo().search_count([
            ('project_id', '=', self.project.id),
            ('name', '=', name),
            ('is_latest', '=', True),
        ])

    # ---- action_publish ------------------------------------------------ #
    def test_publish_from_draft_sets_latest(self):
        skill = self._make_skill('alpha', '1.0.0')
        skill.action_publish()
        self.assertEqual(skill.state, 'published')
        self.assertTrue(skill.is_latest)
        self.assertTrue(skill.published_at, "published_at must be stamped")

    def test_publish_second_version_steals_latest(self):
        v1 = self._make_skill('beta', '1.0.0')
        v1.action_publish()
        v2 = self._make_skill('beta', '2.0.0')
        v2.action_publish()
        self.assertTrue(v2.is_latest)
        self.assertFalse(v1.is_latest, "older version must lose is_latest")
        self.assertEqual(self._latest_count('beta'), 1)

    # ---- action_deprecate ---------------------------------------------- #
    def test_deprecate_latest_reassigns_to_next_published(self):
        v1 = self._make_skill('gamma', '1.0.0')
        v1.action_publish()
        v2 = self._make_skill('gamma', '2.0.0')
        v2.action_publish()  # v2 is latest
        v2.action_deprecate()
        self.assertEqual(v2.state, 'deprecated')
        self.assertFalse(v2.is_latest)
        self.assertTrue(v1.is_latest, "is_latest must fall back to v1")
        self.assertEqual(self._latest_count('gamma'), 1)

    def test_deprecate_only_published_clears_latest(self):
        skill = self._make_skill('delta', '1.0.0')
        skill.action_publish()
        skill.action_deprecate()
        self.assertFalse(skill.is_latest)
        self.assertEqual(self._latest_count('delta'), 0)

    # ---- action_archive_skill ------------------------------------------ #
    def test_archive_clears_latest_and_recomputes(self):
        v1 = self._make_skill('eps', '1.0.0')
        v1.action_publish()
        v2 = self._make_skill('eps', '2.0.0')
        v2.action_publish()  # v2 latest
        v2.action_archive_skill()
        self.assertEqual(v2.state, 'archived')
        self.assertFalse(v2.is_latest)
        self.assertTrue(v1.is_latest)
        self.assertEqual(self._latest_count('eps'), 1)

    # ---- illegal transition guards ------------------------------------- #
    def test_publish_from_archived_raises(self):
        skill = self._make_skill('zeta', '1.0.0')
        skill.action_archive_skill()
        with self.assertRaises(UserError):
            skill.action_publish()

    def test_deprecate_from_draft_raises(self):
        skill = self._make_skill('eta', '1.0.0')
        with self.assertRaises(UserError):
            skill.action_deprecate()

    def test_archive_already_archived_raises(self):
        skill = self._make_skill('iota', '1.0.0')
        skill.action_archive_skill()
        with self.assertRaises(UserError):
            skill.action_archive_skill()

    # ---- re-publish ---------------------------------------------------- #
    def test_republish_from_deprecated_becomes_latest(self):
        v1 = self._make_skill('omicron', '1.0.0')
        v1.action_publish()
        v2 = self._make_skill('omicron', '2.0.0')
        v2.action_publish()          # v2 latest
        v1.action_deprecate()        # v1 deprecated（原本就不是 latest）
        # 整個 test 在同一 transaction 內毫秒級跑完，fields.Datetime.now()
        # 是秒粒度，v1/v2 兩次 publish 會撞同一秒。把 v2 的 published_at 退
        # 一秒，讓 v1 re-publish（當前秒）嚴格大於 v2，確定性驗證
        # 「most-recently-published 奪回 latest」語意，不依賴 wall-clock 推進。
        v2.write({
            'published_at': v2.published_at - timedelta(seconds=1),
        })
        v1.action_publish()          # re-publish v1：published_at 變最新 → v1 應奪回 latest
        self.assertEqual(v1.state, 'published')
        self.assertGreater(v1.published_at, v2.published_at)
        self.assertTrue(v1.is_latest)
        self.assertFalse(v2.is_latest)
        self.assertEqual(self._latest_count('omicron'), 1)

    # ---- invariant ----------------------------------------------------- #
    def test_invariant_at_most_one_latest_per_group(self):
        v1 = self._make_skill('theta', '1.0.0')
        v2 = self._make_skill('theta', '2.0.0')
        v3 = self._make_skill('theta', '3.0.0')
        v1.action_publish()
        v2.action_publish()
        v3.action_publish()
        v3.action_deprecate()   # published now {v1, v2} -> v2 latest
        v1.action_archive_skill()  # published now {v2} -> v2 latest
        self.assertLessEqual(self._latest_count('theta'), 1)
        self.assertTrue(v2.is_latest, "v2 must be the surviving latest")
