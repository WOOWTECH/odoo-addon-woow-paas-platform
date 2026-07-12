# -*- coding: utf-8 -*-
"""#336：每帳號（partner）限一 enforcement。"""
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError


@tagged('post_install', '-at_install')
class TestMaxPerPartnerField(TransactionCase):

    def test_field_exists_default_zero(self):
        tpl = self.env['woow_paas_platform.cloud_app_template'].search([], limit=1)
        self.assertTrue(tpl, '前置：seed 應有 template')
        self.assertIn('max_per_partner', tpl._fields)
        self.assertEqual(tpl.max_per_partner, 0)


@tagged('post_install', '-at_install')
class TestMaxPerPartnerEnforcement(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner_a = self.env['res.partner'].create({'name': '甲'})
        self.partner_b = self.env['res.partner'].create({'name': '乙'})
        self.user_a = self.env['res.users'].create({
            'name': '甲', 'login': 'limit_a@test', 'partner_id': self.partner_a.id,
        })
        self.user_b = self.env['res.users'].create({
            'name': '乙', 'login': 'limit_b@test', 'partner_id': self.partner_b.id,
        })
        # workspace：name + owner_id(→res.users) 為 create 必填（Task 0 校驗）
        self.ws_a = self.env['woow_paas_platform.workspace'].create({
            'name': 'ws-a', 'owner_id': self.user_a.id,
        })
        self.ws_b = self.env['woow_paas_platform.workspace'].create({
            'name': 'ws-b', 'owner_id': self.user_b.id,
        })
        # cloud_app_template：name/helm_repo_url/helm_chart_name/helm_chart_version
        # 為 required（Task 0 校驗）；helm 值鏡射 seed 慣例（repo/version 空字串）。
        self.tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'limit-tpl', 'slug': 'limit-tpl',
            'max_per_partner': 1,
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/limit-tpl',
            'helm_chart_version': '',
        })

    def _svc_vals(self, ws):
        # cloud_service：name/workspace_id/template_id 為 create 必填（Task 0 校驗）
        return {
            'name': 'svc', 'workspace_id': ws.id, 'template_id': self.tpl.id,
        }

    def test_second_create_blocked(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        Svc.create(self._svc_vals(self.ws_a))
        with self.assertRaises(ValidationError):
            Svc.create(self._svc_vals(self.ws_a))

    def test_different_partner_not_blocked(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        Svc.create(self._svc_vals(self.ws_a))
        svc_b = Svc.create(self._svc_vals(self.ws_b))
        self.assertTrue(svc_b)

    def test_zero_means_unlimited(self):
        self.tpl.max_per_partner = 0
        Svc = self.env['woow_paas_platform.cloud_service']
        Svc.create(self._svc_vals(self.ws_a))
        self.assertTrue(Svc.create(self._svc_vals(self.ws_a)))

    def test_find_active_for_partner(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        svc = Svc.create(self._svc_vals(self.ws_a))
        found = Svc._find_active_for_partner(self.tpl, self.partner_a)
        self.assertEqual(found, svc)
        self.assertFalse(Svc._find_active_for_partner(self.tpl, self.partner_b))
