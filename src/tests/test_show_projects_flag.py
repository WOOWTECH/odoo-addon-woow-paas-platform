"""#275 /woow Project 隱藏開關 — 後端。

分三 class：
- TestShowProjectsFlag：_show_projects fail-closed 矩陣（Task 1）
- TestShowProjectsConfigLockstep：_CONFIG_PARAMETERS 卸載清理 lockstep（Task 3）
- TestWoowSessionInfoShowProjects：/woow session_info 注入（Task 2）
"""
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import (
    _show_projects, SHOW_PROJECTS_FLAG,
)


class TestShowProjectsFlag(TransactionCase):
    """fail-closed：只有 'true'/'1'（strip+lower 後）才 True，其餘一律 False。"""

    def _set(self, raw):
        self.env['ir.config_parameter'].sudo().set_param(SHOW_PROJECTS_FLAG, raw)

    def test_unset_is_false(self):
        # falsy → Odoo 刪除 param → 等效未設；fail-closed 隱藏
        self._set(False)
        self.assertFalse(_show_projects(self.env))

    def test_falsey_raw_values_are_false(self):
        for raw in ('', 'false', 'False', '0', 'no', 'off'):
            with self.subTest(raw=raw):
                self._set(raw)
                self.assertFalse(_show_projects(self.env))

    def test_truthy_raw_values_are_true(self):
        for raw in ('true', 'True', '1', ' TRUE '):  # strip+lower 後命中白名單
            with self.subTest(raw=raw):
                self._set(raw)
                self.assertTrue(_show_projects(self.env))
