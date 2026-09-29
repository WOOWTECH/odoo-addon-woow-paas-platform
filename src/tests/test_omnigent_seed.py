# -*- coding: utf-8 -*-
"""Omnigent 上架 seed 與 app_managed 密碼載體的守線斷言。

契約來源：woow-paas-charts charts/omnigent 0.1.0（server :8000 是唯一入口、無 auth-proxy；
admin 由 OMNIGENT_ACCOUNTS_INIT_ADMIN_* 在第一次開機建立；SQLite；runner 走上游
kubernetes managed sandbox），paas-odoo-ci mirror-image.yml #159/#160（上游 image 原樣鏡像）。

核心守線：
1. app_managed ⇒ admin_reset_supported=False：平台只供給初始密碼，之後密碼活在 omnigent
   自己的資料庫。照 helm_secret 走 reset 會 helm upgrade 成功、密碼卻沒變（假裝成功），
   所以 controller gate、model 防線、前端按鈕三處都必須擋下。
2. 開通時照樣供給（has_admin_gui=True），一次性顯示初始密碼。
3. 上架前 image／runner image 都必須雙釘 tag@sha256。
"""
import json

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

OMNI_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/omnigent-server-kubernetes'
OMNI_HOST_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/omnigent-host'
OMNI_CHART = 'oci://jcr-prod.woowtech.io/woow-paas-docker-local/omnigent'
PINNED_TAG_RE = r'^\d{8}\.\d{4}@sha256:[0-9a-f]{64}$'


@tagged('post_install', '-at_install')
class TestOmnigentSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_omnigent')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    def test_marketplace_fields(self):
        tpl = self._tpl()
        self.assertEqual(tpl.slug, 'omnigent')
        self.assertEqual(tpl.category, 'ai_llm')
        self.assertEqual(tpl.default_port, 8000, 'server 本身就是唯一入口（無 auth-proxy），:8000')
        self.assertTrue(tpl.ingress_enabled)
        self.assertEqual(tpl.max_per_partner, 0)
        self.assertFalse(tpl.mcp_enabled)

    def test_chart_oci_pin(self):
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(tpl.helm_chart_name, OMNI_CHART)
        self.assertEqual(tpl.helm_chart_version, '0.1.0')

    def test_initial_password_only(self):
        """has_admin_gui（開通時供給、一次性顯示）＋ app_managed（之後平台不能重設）。"""
        tpl = self._tpl()
        self.assertTrue(tpl.has_admin_gui)
        self.assertEqual(tpl.admin_password_carrier, 'app_managed')
        self.assertFalse(tpl.admin_reset_supported)
        self.assertFalse(tpl.admin_username_helm_path,
                         'omnigent 沒有改名 API，帳號不開放修改')
        self.assertEqual(self._values()['admin']['username'], 'admin')

    def test_images_mirrored_and_pinned(self):
        v = self._values()
        self.assertEqual(v['image']['repository'], OMNI_IMAGE)
        self.assertRegex(v['image']['tag'], PINNED_TAG_RE)
        self.assertTrue(v['runner']['image'].startswith(OMNI_HOST_IMAGE + ':'))
        host_tag = v['runner']['image'][len(OMNI_HOST_IMAGE) + 1:]
        self.assertRegex(host_tag, PINNED_TAG_RE,
                         'runner image 由 launcher 原樣寫進每個 Job，必須釘 digest')
        for ref in (v['image']['repository'], v['runner']['image']):
            self.assertNotIn('ghcr.io', ref, '平台只從 jcr-prod 拉（叢集連 ghcr 慢且會斷）')

    def test_resources_match_t1(self):
        """T1 = 4 vCPU / 8 Gi / 10 Gi：template min_*、plan cs_*、PVC 大小三處一致。"""
        tpl = self._tpl()
        self.assertEqual(tpl.min_vcpu, 4)
        self.assertEqual(tpl.min_ram_gb, 8.0)
        self.assertEqual(tpl.min_storage_gb, 10)
        self.assertEqual(self._values()['persistence']['size'], '10Gi')
        for plan in tpl.sudo().subscription_plan_ids:
            self.assertEqual(plan.cs_vcpu, 4, plan.name)
            self.assertEqual(plan.cs_ram_gb, 8.0, plan.name)
            self.assertEqual(plan.cs_storage_gb, 10, plan.name)

    def test_service_port_lockstep(self):
        v = self._values()
        self.assertEqual(v['service']['port'], 8000)
        self.assertEqual(v['service']['targetPort'], 8000)

    def test_plans_linked_and_priced(self):
        plans = self._tpl().sudo().subscription_plan_ids
        m = self.env.ref('woow_paas_platform.plan_omnigent_monthly')
        y = self.env.ref('woow_paas_platform.plan_omnigent_yearly')
        self.assertIn(m, plans)
        self.assertIn(y, plans)
        self.assertEqual(m.sh_plan_price, 4500)
        self.assertEqual(y.sh_plan_price, 45000)
        for xmlid in ('product_omnigent_monthly', 'product_omnigent_yearly'):
            p = self.env.ref('woow_paas_platform.%s' % xmlid)
            self.assertTrue(p.sh_product_subscribe, xmlid)
            self.assertFalse(p.taxes_id, '%s 免稅（同其他 cloud service 產品）' % xmlid)

    def test_hidden_until_verified(self):
        """未上架（is_hidden）但不 conceal：待正式環境實測後才公開。"""
        tpl = self._tpl()
        self.assertTrue(tpl.is_active)
        self.assertTrue(tpl.is_hidden)
        self.assertFalse(tpl.conceal_instances)

    def test_public_origin_injected_into_base_url(self):
        """accounts 模式要知道自己的 https 網址（__Host- cookie、WS Origin）；只有平台知道。"""
        CloudService = self.env['woow_paas_platform.cloud_service']
        values = CloudService._inject_allowed_origins(
            {}, 'omnigent', 'https://paas-cs-abc.woowtech.io')
        self.assertEqual(values, {'server': {'baseUrl': 'https://paas-cs-abc.woowtech.io'}})


@tagged('post_install', '-at_install')
class TestAppManagedCarrier(TransactionCase):
    """admin_reset_supported 是「平台能不能重設」的唯一事實源。"""

    def test_compute(self):
        Tpl = self.env['woow_paas_platform.cloud_app_template']
        cases = [
            (False, 'config_json', False),
            (True, 'config_json', True),
            (True, 'helm_secret', True),
            (True, 'app_managed', False),
            (False, 'app_managed', False),
        ]
        for has_gui, carrier, expected in cases:
            tpl = Tpl.new({'has_admin_gui': has_gui, 'admin_password_carrier': carrier})
            self.assertEqual(tpl.admin_reset_supported, expected, (has_gui, carrier))

    def test_existing_templates_unchanged(self):
        """既有 helm_secret 模板（pi-agent／open-design）仍可重設。"""
        for xmlid in ('cloud_app_pi_agent', 'cloud_app_open_design'):
            tpl = self.env.ref('woow_paas_platform.%s' % xmlid, raise_if_not_found=False)
            if tpl:
                self.assertTrue(tpl.admin_reset_supported, xmlid)

    def test_model_guard_refuses_before_operator(self):
        """model 端第二道防線：在碰 operator（與 row lock）之前就拒絕。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_omnigent')
        service = self.env['woow_paas_platform.cloud_service'].new({
            'name': 'omni-guard', 'template_id': tpl.id, 'state': 'running',
        })
        with self.assertRaisesRegex(UserError, 'manages its own admin password'):
            service._reset_admin_password()

    def test_username_path_forbidden(self):
        """app_managed 不能開放改帳號（沒有 chart Secret 可改）。"""
        from odoo.exceptions import ValidationError
        tpl = self.env.ref('woow_paas_platform.cloud_app_omnigent')
        with self.assertRaises(ValidationError):
            tpl.write({'admin_username_helm_path': 'admin.username'})
