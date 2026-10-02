# -*- coding: utf-8 -*-
"""Nextcloud 上架 seed、公開網址注入、與「平台無法重設憑證」形態的守線斷言。

契約來源：woow-paas-charts charts/nextcloud 0.1.0（app :80 是唯一入口、ClusterIP-only、
release 前綴命名、自帶 postgresql(pgvector)＋redis、cron 走 sidecar、
NEXTCLOUD_TRUSTED_DOMAINS／OVERWRITEHOST 由 nextcloud.publicUrl 衍生）。

核心守線：
1. **公開網址必須被注入**：Nextcloud 以 trusted_domains 比對 Host，清單外的網域**整站**
   回「Access through untrusted domain」。chart 只知道 localhost（探針用），租戶 host 只有
   平台知道 ⇒ ``_inject_allowed_origins`` 必須有 ``nextcloud`` 這條。少了它的失效形態是
   「服務開得起來、租戶一打開就是錯誤頁」——不會有任何部署錯誤。
2. **app_managed**：``NEXTCLOUD_ADMIN_*`` 只在首次 ``occ maintenance:install`` 被吃掉，
   之後密碼活在 Nextcloud 自己的資料庫 ⇒ 平台只供給初始值、不得宣稱可重設。
3. **image 不得用 ``stable``**：該 tag 會跨大版本，而 Nextcloud 只支援逐版升級
   ——pod 一重啟落到下一個大版本就卡在維護模式。
4. **規格對帳**：2 vCPU / 4 Gi / 96 Gi ——四顆 PVC 加總 ＝ min_storage_gb，
   plan 的 cs_storage_gb 只對應使用者檔案那顆（#332 overlay 寫 ``persistence.size``）。
"""
import json
import re

from odoo.tests.common import TransactionCase, tagged

NC_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/nextcloud'
REDIS_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/redis'
NC_CHART = 'oci://jcr-prod.woowtech.io/woow-paas-docker-local/nextcloud'
PINNED_TAG_RE = r'^\d{8}\.\d{4}@sha256:[0-9a-f]{64}$'


@tagged('post_install', '-at_install')
class TestNextcloudSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_nextcloud')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    def test_marketplace_fields(self):
        tpl = self._tpl()
        self.assertEqual(tpl.slug, 'nextcloud')
        self.assertEqual(tpl.category, 'web')
        self.assertEqual(tpl.default_port, 80)
        self.assertTrue(tpl.ingress_enabled)
        self.assertEqual(tpl.max_per_partner, 0)
        self.assertFalse(tpl.mcp_enabled)
        self.assertEqual(tpl.workload_type, 'container')

    def test_chart_oci_pin(self):
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(tpl.helm_chart_name, NC_CHART)
        self.assertEqual(tpl.helm_chart_version, '0.1.0')

    def test_public_url_is_injected_into_the_chart(self):
        """沒有這條注入，租戶看到的是「不信任的網域」錯誤頁，而部署本身是成功的。"""
        CloudService = self.env['woow_paas_platform.cloud_service']
        origin = 'https://paas-nc-abc.woowtech.io'
        values = CloudService._inject_allowed_origins({}, 'nextcloud', origin)
        self.assertEqual(values, {'nextcloud': {'publicUrl': origin}})
        # 其他 slug 不受影響（同一張表服務三個既有商品）
        self.assertEqual(
            CloudService._inject_allowed_origins({}, 'code-server', origin),
            {'codeServer': {'publicUrl': origin}})

    def test_platform_cannot_reset_credentials(self):
        tpl = self._tpl()
        self.assertTrue(tpl.has_admin_gui)
        self.assertEqual(tpl.admin_password_carrier, 'app_managed')
        self.assertFalse(tpl.admin_reset_supported)
        self.assertFalse(tpl.admin_password_settable)
        self.assertFalse(tpl.admin_credentials_settable)
        self.assertFalse(tpl.admin_username_helm_path)

    def test_images_from_jcr_double_pinned_and_never_stable(self):
        v = self._values()
        self.assertEqual(v['nextcloud']['image']['repository'], NC_IMAGE)
        self.assertEqual(v['redis']['image']['repository'], REDIS_IMAGE)
        for ref in (v['nextcloud']['image']['tag'], v['redis']['image']['tag']):
            self.assertRegex(ref, PINNED_TAG_RE)
            self.assertNotIn('stable', ref,
                             'stable 會跨大版本，而 Nextcloud 只支援逐版升級')
        self.assertNotIn('docker.io', json.dumps(v))

    def test_hidden_until_mirror_lands(self):
        tpl = self._tpl()
        self.assertTrue(tpl.is_active)
        if not tpl.is_hidden:
            self.assertRegex(self._values()['nextcloud']['image']['tag'], PINNED_TAG_RE)
            self.assertRegex(self._values()['redis']['image']['tag'], PINNED_TAG_RE)

    def test_spec_matches_plan_and_pvcs(self):
        tpl = self._tpl()
        v = self._values()
        self.assertEqual(tpl.min_vcpu, 2)
        self.assertEqual(tpl.min_ram_gb, 4.0)
        self.assertEqual(tpl.min_storage_gb, 96)
        sizes = {
            'data': int(v['persistence']['data']['size'].rstrip('Gi')),
            'html': int(v['persistence']['html']['size'].rstrip('Gi')),
            'db': int(v['postgresql']['persistence']['size'].rstrip('Gi')),
            'redis': int(v['redis']['persistence']['size'].rstrip('Gi')),
        }
        self.assertEqual(sum(sizes.values()), tpl.min_storage_gb,
                         '四顆 PVC 加總必須等於 min_storage_gb（quota 對帳）')
        for plan in tpl.subscription_plan_ids:
            self.assertEqual(plan.cs_vcpu, tpl.min_vcpu)
            self.assertEqual(plan.cs_ram_gb, tpl.min_ram_gb)
            self.assertEqual(plan.cs_storage_gb, sizes['data'],
                             'cs_storage_gb 對應使用者檔案那顆（#332 overlay）')

    def test_plans_linked_and_priced(self):
        tpl = self._tpl()
        monthly = self.env.ref('woow_paas_platform.plan_nextcloud_monthly')
        yearly = self.env.ref('woow_paas_platform.plan_nextcloud_yearly')
        self.assertEqual(set(tpl.subscription_plan_ids.ids), {monthly.id, yearly.id})
        self.assertEqual(monthly.sh_plan_price, 4500)
        self.assertEqual(yearly.sh_plan_price, 45000)
        self.assertEqual(yearly.sh_plan_price, monthly.sh_plan_price * 10, '年繳＝10 個月')
        for product, plan in (
            (self.env.ref('woow_paas_platform.product_nextcloud_monthly'), monthly),
            (self.env.ref('woow_paas_platform.product_nextcloud_yearly'), yearly),
        ):
            self.assertEqual(product.sh_subscription_plan_id, plan)
            self.assertEqual(product.list_price, plan.sh_plan_price)
            self.assertFalse(product.taxes_id)

    def test_tenant_knobs_are_app_level_only(self):
        specs = self._tpl()._parse_helm_value_specs()
        self.assertEqual(specs['required'], [])
        keys = [s['key'] for s in specs['optional']]
        self.assertEqual(sorted(keys), [
            'nextcloud.phpMemoryLimit', 'nextcloud.phpUploadLimit'])
        for s in specs['optional']:
            self.assertEqual(s['type'], 'select')
            self.assertIn(s['default'], s['options'],
                          'select 的 default 必須在 options 裡，否則表單一開就是空值')
        for key in keys:
            self.assertFalse(re.match(r'^(resources|persistence)\b', key))

    def test_whitelist_filters_resource_overrides(self):
        allowed, rejected = self._tpl()._filter_allowed_helm_values({
            'nextcloud.phpUploadLimit': '2G',
            'resources.limits.memory': '64Gi',
        })
        self.assertEqual(allowed, {'nextcloud.phpUploadLimit': '2G'})
        self.assertEqual(rejected, ['resources.limits.memory'])
