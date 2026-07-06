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


import json  # noqa: E402  (module 頂端已 import TransactionCase；此為新增依賴)
from odoo.tests.common import HttpCase, tagged  # noqa: E402


@tagged('post_install', '-at_install')
class TestWoowSessionInfoShowProjects(HttpCase):
    """/woow 把 woow_show_projects 注入 session_info（值隨 flag）。
    用 substring 斷言（json.dumps 產 `"woow_show_projects": true/false`），
    避免解析整包 session_info JSON 的脆弱正則。"""

    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].sudo().create({
            'name': 'sp_user', 'login': 'sp_user', 'password': 'sp_user',
            'email': 'sp_user@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })

    def _woow_html(self):
        res = self.url_open('/woow', timeout=30)
        self.assertEqual(res.status_code, 200, res.text[:200])
        return res.text

    def test_flag_on_injects_true(self):
        self.env['ir.config_parameter'].sudo().set_param(SHOW_PROJECTS_FLAG, 'True')
        self.authenticate('sp_user', 'sp_user')
        self.assertIn('"woow_show_projects": true', self._woow_html())

    def test_flag_off_injects_false(self):
        self.env['ir.config_parameter'].sudo().set_param(SHOW_PROJECTS_FLAG, 'False')
        self.authenticate('sp_user', 'sp_user')
        self.assertIn('"woow_show_projects": false', self._woow_html())


from odoo.addons.woow_paas_platform import hooks  # noqa: E402


class TestShowProjectsConfigLockstep(TransactionCase):
    """卸載清理 lockstep：show_projects 須在 hooks._CONFIG_PARAMETERS
    （CLAUDE.md 硬性要求；比照 test_deploy_env_config）。"""

    def test_show_projects_in_config_parameters_cleanup_list(self):
        self.assertIn(
            'woow_paas_platform.show_projects', hooks._CONFIG_PARAMETERS)
