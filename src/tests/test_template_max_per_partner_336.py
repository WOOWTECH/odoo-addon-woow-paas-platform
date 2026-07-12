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
