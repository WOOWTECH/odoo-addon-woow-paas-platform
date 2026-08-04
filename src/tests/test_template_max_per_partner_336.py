# -*- coding: utf-8 -*-
"""#336：每帳號（partner）限一 enforcement。"""
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError


@tagged('post_install', '-at_install')
class TestMaxPerPartnerField(TransactionCase):

    def test_field_exists_default_zero(self):
        """欄位存在 ＋ default 為 0，order-independent（與 seed 排序/內容解耦）。

        欄位存在直接查 ``_fields``（無需任何 record）；default 語意用「不設值的
        fixture」驗證。舊版 ``search([], limit=1)`` 假設首筆 template
        ``max_per_partner==0``，但 ``_order='name'`` 下 'HA PaaS'（#345 工作項2
        補 1 後）字母序在前會被取到而踩紅——改為直驗欄位不變式（#345 工作項1）。
        """
        Template = self.env['woow_paas_platform.cloud_app_template']
        self.assertIn('max_per_partner', Template._fields)
        # 不設 max_per_partner 的 fixture → 驗 default==0（驗「欄位 default 語意」，
        # 而非舊法「存在某 0 值 seed」；helm 值鏡射 setUp / seed 慣例；#552 層 4 後 seed 慣例＝釘版）。
        tpl = Template.create({
            'name': 'default-probe', 'slug': 'default-probe',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/default-probe',
            'helm_chart_version': '1.0.0',
        })
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
        # 為 required（Task 0 校驗）；helm 值鏡射 seed 慣例（repo 空字串）。
        # ⚠ version 不可留空：#552 層 4 起空版號＝不可購買，create 會被擋。
        self.tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'limit-tpl', 'slug': 'limit-tpl',
            'max_per_partner': 1,
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/limit-tpl',
            'helm_chart_version': '1.0.0',
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

    def _advisory_lock_held(self, tpl_id, partner_id):
        """同 tx 查 pg_locks：(template, partner) 雙 int4 advisory xact lock 是否持有。
        objsubid=2 ＝雙 int4 key 模式（單 bigint 為 1）；classid/objid 轉 bigint 比對。"""
        self.env.cr.execute("""
            SELECT 1 FROM pg_locks
            WHERE locktype = 'advisory' AND objsubid = 2
              AND classid::bigint = %s AND objid::bigint = %s
        """, (tpl_id, partner_id))
        return bool(self.env.cr.fetchone())

    def test_advisory_lock_acquired_on_enforced_create(self):
        """#345 並發防護（wired 驗證）：enforced create（max_per_partner>0）在計數前
        取 (template, partner) 為鍵的 advisory xact lock。單連線 TransactionCase 無法
        真並發，此測只證「鎖被取且用對鍵」——競態正確性靠與 provision advisory lock
        先例一致 + code review，非本測所能覆蓋。"""
        Svc = self.env['woow_paas_platform.cloud_service']
        Svc.create(self._svc_vals(self.ws_a))
        self.assertTrue(
            self._advisory_lock_held(self.tpl.id, self.partner_a.id),
            'enforced create 應取 (template, partner) advisory lock')

    def test_no_advisory_lock_when_unlimited(self):
        """max_per_partner=0（不限制）不進 enforcement 迴圈 → 不取鎖（零開銷驗證）。"""
        self.tpl.max_per_partner = 0
        Svc = self.env['woow_paas_platform.cloud_service']
        Svc.create(self._svc_vals(self.ws_a))
        self.assertFalse(
            self._advisory_lock_held(self.tpl.id, self.partner_a.id),
            'max_per_partner=0 不應取鎖')
