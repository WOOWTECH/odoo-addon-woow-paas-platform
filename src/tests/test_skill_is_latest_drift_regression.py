"""TDD — is_latest drift regression locks.

Spec: docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md §①

Rationale: PR #81 維護 is_latest 不變式只在三個 transition method 結尾收斂。
任何繞過 transition 直接 ``create({'state':'published'})`` 或
``write({'state':'published'})`` 的程式化路徑（test fixture / 未來上傳 API /
demo data / admin 匯入）都會 drift — published skill 的 is_latest 恆 default
False、published_at 為 NULL。

本 test 鎖修法（model 層 create/write override 自動 recompute + 補
published_at），讓不變式對所有 ORM 寫入路徑成立。
"""
import hashlib
import uuid
from datetime import datetime

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSkillIsLatestDriftRegression(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'woow_paas_platform.skill' not in cls.env.registry:
            raise RuntimeError(
                "prerequisite: 'woow_paas_platform.skill' not registered."
            )
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.project = cls.env['project.project'].sudo().create(
            {'name': 'Skill-Drift %s' % uuid.uuid4().hex[:8]}
        )

    def _digest(self, name, version):
        return hashlib.sha256(
            ('%s-%s' % (name, version)).encode()
        ).hexdigest()

    def _latest_count(self, name):
        return self.Skill.sudo().search_count([
            ('project_id', '=', self.project.id),
            ('name', '=', name),
            ('is_latest', '=', True),
        ])

    # ---- create 直建 published 路徑（drift sample） ---------------------- #
    def test_create_published_directly_sets_is_latest_and_published_at(self):
        """直接 create state=published（繞過 action_publish）→ 修法後
        is_latest 必為 True、published_at 必有時戳。"""
        skill = self.Skill.sudo().create({
            'name': 'alpha-create',
            'version': '1.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('alpha-create', '1.0.0'),
            'state': 'published',
        })
        self.assertEqual(skill.state, 'published')
        self.assertTrue(
            skill.is_latest,
            "create({'state':'published'}) 必須自動 recompute is_latest "
            "（drift 防護：spec §①）",
        )
        self.assertTrue(
            skill.published_at,
            "create({'state':'published'}) 必須自動補 published_at",
        )

    # ---- write 改 state 路徑（drift sample） ---------------------------- #
    def test_write_state_to_published_sets_is_latest_and_published_at(self):
        """draft skill 用 write({'state':'published'}) 切換（繞過
        action_publish）→ 修法後 is_latest 必為 True、published_at 必有。"""
        skill = self.Skill.sudo().create({
            'name': 'beta-write',
            'version': '1.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('beta-write', '1.0.0'),
            'state': 'draft',
        })
        self.assertFalse(skill.is_latest)
        skill.write({'state': 'published'})
        self.assertTrue(
            skill.is_latest,
            "write({'state':'published'}) 必須自動 recompute is_latest",
        )
        self.assertTrue(
            skill.published_at,
            "write({'state':'published'}) 必須自動補 published_at",
        )

    # ---- 多版本程式化建 published：不變式收斂 -------------------------- #
    def test_multiple_programmatic_published_keeps_invariant(self):
        """連續直 create 三個 published 版本 → 至多一個 is_latest=True。"""
        self.Skill.sudo().create({
            'name': 'gamma-multi', 'version': '1.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('gamma-multi', '1.0.0'),
            'state': 'published',
        })
        self.Skill.sudo().create({
            'name': 'gamma-multi', 'version': '2.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('gamma-multi', '2.0.0'),
            'state': 'published',
        })
        v3 = self.Skill.sudo().create({
            'name': 'gamma-multi', 'version': '3.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('gamma-multi', '3.0.0'),
            'state': 'published',
        })
        self.assertEqual(
            self._latest_count('gamma-multi'), 1,
            "至多一個 is_latest=True per (project, name) group",
        )
        # 最新 create 的 v3 應為 winner（同秒粒度時 id desc tiebreak）
        v3.invalidate_recordset()
        self.assertTrue(
            v3.is_latest,
            "最新 published 應為 latest（id desc tiebreak）",
        )

    # ---- published_at 已給定時不被覆蓋 --------------------------------- #
    def test_create_published_with_explicit_published_at_keeps_it(self):
        """create 時若顯式給 published_at，hook 不應覆蓋（只在缺時補）。"""
        explicit_ts = datetime(2025, 1, 1, 12, 0, 0)
        skill = self.Skill.sudo().create({
            'name': 'delta-explicit',
            'version': '1.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('delta-explicit', '1.0.0'),
            'state': 'published',
            'published_at': explicit_ts,
        })
        self.assertEqual(
            skill.published_at, explicit_ts,
            "顯式 published_at 不應被 hook 覆蓋（只在缺時補）",
        )
        self.assertTrue(skill.is_latest)

    # ---- 非 published 不觸發 hook（無副作用） -------------------------- #
    def test_create_draft_does_not_set_is_latest_or_published_at(self):
        """create draft skill → is_latest 維持 False、published_at 維持 NULL。"""
        skill = self.Skill.sudo().create({
            'name': 'eps-draft',
            'version': '1.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('eps-draft', '1.0.0'),
            'state': 'draft',
        })
        self.assertFalse(skill.is_latest)
        self.assertFalse(skill.published_at)

    # ---- 既有 action_publish 行為仍正確（hook 不重複疊加破壞） ---------- #
    def test_action_publish_still_works_after_hook(self):
        """確認新 hook 不破壞既有 action_publish 行為（防迴歸）。"""
        skill = self.Skill.sudo().create({
            'name': 'zeta-action', 'version': '1.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('zeta-action', '1.0.0'),
            'state': 'draft',
        })
        skill.action_publish()
        self.assertEqual(skill.state, 'published')
        self.assertTrue(skill.is_latest)
        self.assertTrue(skill.published_at)
