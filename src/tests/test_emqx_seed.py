# -*- coding: utf-8 -*-
"""EMQX 上架 seed 與「平台無法重設憑證」形態的守線斷言。

契約來源：woow-paas-charts charts/emqx 0.1.1（dashboard :18083 是唯一入口、ClusterIP-only、
release 前綴命名、built_in_database authenticator ＋ bootstrap_file 種第一組 MQTT 帳密、
ngrok sidecar 對外轉 1883），以及 paas-odoo-ci mirror-image.yml 鏡像進 JCR 的 image。

核心守線：
1. **app_managed**：EMQX 的 ``dashboard.default_password`` 依官方文件「只在第一次開機生效、
   之後變更一律忽略」，首次登入還會強制改密碼 ⇒ 平台只供給初始值、**不得**宣稱可重設。
   carrier 選錯的代價不對稱（helm_secret 誤標＝helm upgrade 成功但密碼沒變＝假裝成功），
   所以這條用三個欄位一起釘：carrier / settable / reset_supported。
2. **上架前提**：image 雙釘 ``tag@sha256``（emqx 與 ngrok 兩顆都要），且鏡像落地前保持隱藏。
3. **規格對帳**：1 vCPU / 2 Gi / 12 Gi ——template min_*、plan cs_*、chart 兩顆 PVC 加總三處一致。
   ⚠ plan 的 ``cs_storage_gb`` 只對應 data（#332 overlay 寫 ``persistence.size``），
   log 是固定 overhead，故 min_storage_gb = cs_storage_gb + log。
4. **可調參數白名單**：只有應用層旋鈕（ngrok 三項 + ws listener），CPU/RAM/磁碟一律不開
   ——那三個維度由訂閱方案決定，開放等於讓租戶改自己買的規格。
5. **授權（⛔ 最重要）**：EMQX 自 5.9.0 起為 BSL 1.1，明文禁止當託管 MQTT 服務提供給第三方
   ——平台正是這個用途。只能釘 5.8.x（Apache-2.0）。digest 是唯一不會說謊的識別，所以用
   **白名單**守：seed 的 emqx digest 必須是下面登記過的 Apache-2.0 版；已知的 BSL 版另列黑名單，
   讓「有人把 #1321 的 5.10.5 換回來」紅得一眼看懂。換 5.8.x 小版時：確認 image 標籤
   ``org.opencontainers.image.licenses`` 是 Apache-2.0 後，把 amd64 子 manifest digest 加進白名單。
"""
import json
import re

from odoo.tests.common import TransactionCase, tagged

EMQX_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/emqx'
NGROK_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/ngrok'
EMQX_CHART = 'oci://jcr-prod.woowtech.io/woow-paas-docker-local/emqx'
PINNED_TAG_RE = r'^\d{8}\.\d{4}@sha256:[0-9a-f]{64}$'
# Apache-2.0 的 EMQX（amd64 子 manifest；repo 慣例釘 amd64 child）。新增前先核對 image 標籤。
APACHE_EMQX_DIGESTS = {
    # emqx/emqx:5.8.9 — labels: licenses=Apache-2.0, edition=Opensource
    'sha256:8b92a101d1a1642a191507b9133c46aad095bb64300dba1770108a2c7261b11a',
}
# 已知的 BSL 1.1 版（不得用於平台）。
BSL_EMQX_DIGESTS = {
    # emqx/emqx:5.10.5 — labels: licenses=BSL-1.1, edition=Enterprise（#1321 曾誤釘）
    'sha256:03970774f34c75c95a2ed4218c29826aa488b39a03a3280c0e1aa1e3568fbc23',
}


@tagged('post_install', '-at_install')
class TestEmqxSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_emqx')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    def test_marketplace_fields(self):
        tpl = self._tpl()
        self.assertEqual(tpl.slug, 'emqx')
        self.assertEqual(tpl.category, 'automation')
        self.assertEqual(tpl.default_port, 18083, 'dashboard 是平台唯一對外入口')
        self.assertTrue(tpl.ingress_enabled)
        self.assertEqual(tpl.max_per_partner, 0)
        self.assertFalse(tpl.mcp_enabled)
        self.assertEqual(tpl.workload_type, 'container')

    def test_chart_oci_pin(self):
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(tpl.helm_chart_name, EMQX_CHART)
        self.assertEqual(tpl.helm_chart_version, '0.1.1',
                         'helm_chart_version 留空會被供給閘門靜默擋在購買前（#552）')

    def test_platform_cannot_reset_credentials(self):
        """EMQX 首開之後密碼活在它自己的資料庫，平台重設只會「假裝成功」。"""
        tpl = self._tpl()
        self.assertTrue(tpl.has_admin_gui, '平台要供給初始 dashboard 密碼')
        self.assertEqual(tpl.admin_password_carrier, 'app_managed')
        self.assertFalse(tpl.admin_reset_supported)
        self.assertFalse(tpl.admin_password_settable)
        self.assertFalse(tpl.admin_credentials_settable)
        self.assertFalse(tpl.admin_username_helm_path)

    def test_images_from_jcr_and_double_pinned(self):
        v = self._values()
        self.assertEqual(v['image']['repository'], EMQX_IMAGE)
        self.assertEqual(v['ngrok']['image']['repository'], NGROK_IMAGE)
        for ref in (v['image']['tag'], v['ngrok']['image']['tag']):
            self.assertRegex(ref, PINNED_TAG_RE,
                             'image 一律 tag@sha256 雙釘（mutable tag 不可重現）')
        for repo in (v['image']['repository'], v['ngrok']['image']['repository']):
            self.assertNotIn('docker.io', repo)
        # ⛔ 授權：5.9+ 是 BSL 1.1（禁止託管）；只能是登記過的 Apache-2.0 digest。
        digest = v['image']['tag'].split('@', 1)[1]
        self.assertNotIn(
            digest, BSL_EMQX_DIGESTS,
            '這是 BSL 1.1 授權的 EMQX（5.9+），不得當託管服務提供給租戶')
        self.assertIn(
            digest, APACHE_EMQX_DIGESTS,
            'EMQX digest 未登記為 Apache-2.0 版；核對 image 標籤 '
            'org.opencontainers.image.licenses 後再加進 APACHE_EMQX_DIGESTS')

    def test_hidden_until_mirror_lands(self):
        """鏡像與 stg e2e 完成前不上貨架；要翻 is_hidden 時 pin 必須已經在位。"""
        tpl = self._tpl()
        self.assertTrue(tpl.is_active)
        if not tpl.is_hidden:
            self.assertRegex(self._values()['image']['tag'], PINNED_TAG_RE)
            self.assertRegex(self._values()['ngrok']['image']['tag'], PINNED_TAG_RE)

    def test_spec_matches_plan_and_pvcs(self):
        tpl = self._tpl()
        v = self._values()
        self.assertEqual(tpl.min_vcpu, 1)
        self.assertEqual(tpl.min_ram_gb, 2.0)
        self.assertEqual(tpl.min_storage_gb, 12)
        data = int(v['persistence']['data']['size'].rstrip('Gi'))
        log = int(v['persistence']['log']['size'].rstrip('Gi'))
        self.assertEqual(data + log, tpl.min_storage_gb,
                         'data＋log 必須等於 min_storage_gb（quota 對帳）')
        limits = v['emqx']['resources']['limits']
        self.assertEqual(limits['cpu'], str(tpl.min_vcpu))
        self.assertEqual(limits['memory'], '%dGi' % int(tpl.min_ram_gb))
        for plan in tpl.subscription_plan_ids:
            self.assertEqual(plan.cs_vcpu, tpl.min_vcpu)
            self.assertEqual(plan.cs_ram_gb, tpl.min_ram_gb)
            self.assertEqual(plan.cs_storage_gb, data,
                             'cs_storage_gb 對應 data volume（#332 overlay 寫 persistence.size）')

    def test_plans_linked_and_priced(self):
        tpl = self._tpl()
        monthly = self.env.ref('woow_paas_platform.plan_emqx_monthly')
        yearly = self.env.ref('woow_paas_platform.plan_emqx_yearly')
        self.assertEqual(set(tpl.subscription_plan_ids.ids), {monthly.id, yearly.id})
        self.assertEqual(monthly.sh_plan_price, 2500)
        self.assertEqual(yearly.sh_plan_price, 25000)
        self.assertEqual(yearly.sh_plan_price, monthly.sh_plan_price * 10, '年繳＝10 個月')
        for plan in (monthly, yearly):
            self.assertEqual(plan.service_type, 'cloud_service')
            self.assertTrue(plan.sh_trial)
            self.assertEqual(plan.sh_trial_duration, 7)
        for product, plan in (
            (self.env.ref('woow_paas_platform.product_emqx_monthly'), monthly),
            (self.env.ref('woow_paas_platform.product_emqx_yearly'), yearly),
        ):
            self.assertEqual(product.sh_subscription_plan_id, plan)
            self.assertEqual(product.list_price, plan.sh_plan_price)
            self.assertFalse(product.taxes_id, 'seed 明寫 taxes_id=[]，不吃 company 預設')

    def test_tenant_knobs_are_app_level_only(self):
        specs = self._tpl()._parse_helm_value_specs()
        self.assertEqual(specs['required'], [], '開通不需要租戶先填任何東西')
        keys = [s['key'] for s in specs['optional']]
        self.assertEqual(sorted(keys), sorted([
            'ngrok.enabled',
            'config.sensitive.ngrok_authtoken',
            'ngrok.remoteAddr',
            'emqx.listeners.ws.enabled',
        ]))
        by_key = {s['key']: s for s in specs['optional']}
        self.assertEqual(by_key['config.sensitive.ngrok_authtoken']['type'], 'password')
        self.assertEqual(by_key['ngrok.enabled']['type'], 'boolean')
        self.assertEqual(by_key['emqx.listeners.ws.enabled']['type'], 'boolean')
        for key in keys:
            self.assertFalse(re.match(r'^(resources|persistence)\b', key),
                             'CPU/RAM/磁碟由訂閱方案 cs_* 決定，不得開放給租戶改')

    def test_whitelist_filters_resource_overrides(self):
        """specs 的 key 同時是白名單：方案維度送進來要被擋掉。"""
        tpl = self._tpl()
        allowed, rejected = tpl._filter_allowed_helm_values({
            'ngrok.enabled': True,
            'resources.limits.cpu': '16',
            'persistence.size': '500Gi',
        })
        self.assertEqual(allowed, {'ngrok.enabled': True})
        self.assertEqual(sorted(rejected), ['persistence.size', 'resources.limits.cpu'])
