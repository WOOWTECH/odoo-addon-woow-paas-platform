# -*- coding: utf-8 -*-
"""VPN 控制台（Headscale）上架 seed 的守線斷言（2026-10-04 公開上架）。

契約來源：woow-paas-charts charts/headscale 0.1.3（0.1.2＝#153 工作區＝區網、0.1.1＝#131、0.1.0＝#124）——
一個 Pod：headscale v0.29.4、Headplane 0.7.1（只綁 127.0.0.1）、nginx 入口（Service 第一個埠 3000，
平台密碼 Basic auth，固定送 Remote-User: admin）、選配 ngrok sidecar（租戶自己的 token；每 60 秒印
``WOOW_PUBLIC_URL=https://…``）。Service 第二個埠 8080＝Headscale（同工作區節點登入）。
平台側用到 #1412：``has_public_tunnel_address``／``public_tunnel_label``（P1）與 spec
``format: domain``（P2）。

核心守線：
1. **helm_secret＋帳密皆可自設**：htpasswd 是 Secret volume、nginx 每請求重讀 ⇒ 平台重設的
   helm upgrade 只改 Secret（pod template 不變）；帳號在 ``entrance.basicAuthUsername``。重設之後
   operator 要有 workload 可重啟：chart 0.1.1 的 StatefulSet 帶
   ``paas.woowtech.io/restart-on-credential-reset``（operator #1398）——0.1.0 沒有 Deployment 也沒有
   label，重設在 .207 正式站必定回退失敗。重設會重啟服務（數十秒），說明不能再寫「不必重啟」。
2. **headscale 與 bootstrap 同一版**：bootstrap 用它自己的 binary 跑 migration，Headscale 拒絕
   在較新 minor 遷移過的資料庫上跑、0.29 起也不能降版 ⇒ 兩顆 digest 必須對到同一個上游版本。
   digest 是唯一不會說謊的識別，所以用 digest→版本對照表守（換版時兩顆一起登記）。
3. **ngrok／entrance image 用 chart 預設**：這兩顆在 chart 裡以分開的 ``digest`` 欄位釘死，
   chart 的 image helper 只要 digest 非空就附加——範本若把 tag 寫成 ``tag@sha256`` 卻沒清空
   digest，會 render 成雙 digest、拉不到 image。
4. **對外位址**：控制協定過不了 Cloudflare 通道，只能經租戶 ngrok ⇒ 旗標開；標籤是 record 的資料值，
   seed 直接寫中文、不走 .po（docs/decisions/patterns/i18n-scope-data-values-vs-code-strings.md）。
5. **規格對帳**：1 vCPU／1 GB／1 GB——template min_*、plan cs_*、chart 唯一一顆 PVC 三處一致。
6. **租戶旋鈕只限應用層**；ngrok token 不在 config.sensitive（平台會剝掉，EMQX 0.1.1 教訓）；
   ngrok.domain 存檔即擋整條網址。
7. **上架即雙釘**：#1413 以隱藏卡片進 seed（隨 .206 上正式站），鏡像核可、chart 0.1.0 發佈、真 ngrok
   與整合實測通過後公開上架；``test_hidden_until_listed`` 從此是實際生效的守線（``is_hidden=False``
   ⟹ headscale／headscale-debug／headplane 三顆都要 ``tag@sha256``）。
"""
import json
import os
import re

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

JCR = 'jcr-prod.woowtech.io/woow-paas-docker-local/'
HEADSCALE_CHART = 'oci://%sheadscale' % JCR
PINNED_TAG_RE = r'^\d{8}\.\d{4}@sha256:[0-9a-f]{64}$'
# 上游版本對照（JCR 鏡像以 skopeo --preserve-digests 保留上游 index digest）。
# 換版時：headscale 與 headscale-debug 兩顆**一起**登記同一個版本。
HEADSCALE_DIGESTS = {
    # ghcr.io/juanfont/headscale:v0.29.4（JCR headscale:20261004.0159，paas-odoo-ci #233）
    'sha256:8833f828b414c0907b7e5c71da76473216fe17cce0818a166b536ec552c0903f': 'v0.29.4',
}
HEADSCALE_DEBUG_DIGESTS = {
    # ghcr.io/juanfont/headscale:v0.29.4-debug（JCR headscale-debug:20261004.0502）
    'sha256:2380cdb4951e5d63747d5880aeecaf7aea7702f06ea72a6b9d8010b9386195b8': 'v0.29.4',
}
TUNNEL_LABEL = 'VPN 登入網址（login server）'


@tagged('post_install', '-at_install')
class TestHeadscaleSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_headscale')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    def _optional(self):
        return {s['key']: s for s in self._tpl()._parse_helm_value_specs()['optional']}

    def test_marketplace_fields(self):
        tpl = self._tpl()
        self.assertEqual(tpl.slug, 'headscale')
        self.assertEqual(tpl.name, 'VPN 控制台（Headscale）')
        self.assertEqual(tpl.category, 'devops')
        self.assertEqual(tpl.default_port, 3000, 'nginx 入口（Headplane）是平台唯一對外入口')
        self.assertTrue(tpl.ingress_enabled)
        self.assertEqual(tpl.max_per_partner, 0)
        self.assertFalse(tpl.mcp_enabled)
        self.assertEqual(tpl.workload_type, 'container')

    def test_chart_oci_pin(self):
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(tpl.helm_chart_name, HEADSCALE_CHART)
        self.assertEqual(tpl.helm_chart_version, '0.1.3',
                         'helm_chart_version 留空會被供給閘門靜默擋在購買前（#552）；0.1.3＝'
                         '#1559 設定的 ngrok 網域等 agent 上線才當 server_url'
                         '（0.1.1＝restart-on-credential-reset label，.207 重設必失敗的修正）')

    def test_service_port_is_the_entrance(self):
        """Service 第一個埠＝入口；operator 依 Service 自己的埠順序挑第一個 HTTP 埠。"""
        v = self._values()
        self.assertEqual(v['service']['port'], self._tpl().default_port)
        self.assertNotIn('headscale', v, 'Headscale 的 8080（第二個埠）用 chart 預設，不在範本改')

    def test_credentials_helm_secret_and_settable(self):
        tpl = self._tpl()
        self.assertTrue(tpl.has_admin_gui)
        self.assertEqual(tpl.admin_password_carrier, 'helm_secret')
        self.assertTrue(tpl.admin_reset_supported)
        self.assertTrue(tpl.admin_password_settable)
        self.assertTrue(tpl.admin_credentials_settable)
        self.assertEqual(tpl.admin_username_helm_path, 'entrance.basicAuthUsername')
        self.assertEqual(
            self._values()['entrance']['basicAuthUsername'], 'admin',
            '帳號預設值要在範本裡（_admin_username 讀不到時不猜 chart 預設）')

    def test_images_from_jcr_and_double_pinned(self):
        v = self._values()
        images = {
            'image': v['image'],
            'bootstrap.image': v['bootstrap']['image'],
            'headplane.image': v['headplane']['image'],
        }
        self.assertEqual(images['image']['repository'], JCR + 'headscale')
        self.assertEqual(images['bootstrap.image']['repository'], JCR + 'headscale-debug')
        self.assertEqual(images['headplane.image']['repository'], JCR + 'headplane')
        for path, image in images.items():
            self.assertRegex(image['tag'], PINNED_TAG_RE,
                             '%s 一律 tag@sha256 雙釘（mutable tag 不可重現）' % path)

    def test_headscale_and_bootstrap_are_the_same_release(self):
        """bootstrap 用自己的 binary 跑 migration ⇒ 兩顆必須是同一個 headscale 版本。"""
        v = self._values()
        main = v['image']['tag'].split('@', 1)[1]
        debug = v['bootstrap']['image']['tag'].split('@', 1)[1]
        self.assertIn(main, HEADSCALE_DIGESTS,
                      'headscale digest 未登記版本；換版時兩顆一起登記')
        self.assertIn(debug, HEADSCALE_DEBUG_DIGESTS,
                      'headscale-debug digest 未登記版本；換版時兩顆一起登記')
        self.assertEqual(
            HEADSCALE_DIGESTS[main], HEADSCALE_DEBUG_DIGESTS[debug],
            'image 與 bootstrap.image 必須同一版：bootstrap 先跑 migration，版本不同會讓 '
            'Headscale 拒絕啟動或把資料庫遷移到無法回退的版本')

    def test_chart_pinned_images_are_not_half_overridden(self):
        """ngrok／entrance 在 chart 以分開的 digest 欄位釘死；只改 tag 會變成雙 digest。"""
        v = self._values()
        for sub in ('ngrok', 'entrance'):
            image = (v.get(sub) or {}).get('image')
            if image and '@' in str(image.get('tag', '')):
                self.assertEqual(
                    image.get('digest'), '',
                    '%s.image.tag 已帶 @sha256，必須同時把 digest 清成 ""（chart 的 '
                    'headscale.image helper 只要 digest 非空就再附加一次）' % sub)

    def test_hidden_until_listed(self):
        """上架 ⟹ 三顆 image 雙釘（2026-10-04 公開上架；之後若翻回隱藏，這條仍要綠）。"""
        tpl = self._tpl()
        self.assertTrue(tpl.is_active)
        self.assertFalse(tpl.conceal_instances, 'hidden-only（曝光層），實例層不設')
        if not tpl.is_hidden:
            v = self._values()
            for ref in (v['image']['tag'], v['bootstrap']['image']['tag'],
                        v['headplane']['image']['tag']):
                self.assertRegex(ref, PINNED_TAG_RE)

    def test_public_tunnel_address(self):
        """控制協定過不了 Cloudflare 通道 ⇒ 對外登入網址只能經租戶 ngrok，由平台回讀。"""
        tpl = self._tpl()
        self.assertTrue(tpl.has_public_tunnel_address)
        self.assertEqual(tpl.public_tunnel_label, TUNNEL_LABEL,
                         '服務頁那一格的標題直接寫中文（資料值）')

    def test_tunnel_label_is_a_data_value_not_in_po(self):
        """標籤是 record 的資料值 ⇒ seed 直接寫中文，⛔ 不進 .po（Eugene 定案通則）。"""
        po = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'i18n', 'zh_TW.po')
        entries = [e for e in PoFileReader(po)
                   if e['type'] == 'model'
                   and e['name'] == 'woow_paas_platform.cloud_app_template,public_tunnel_label'
                   and e['imd_name'] == 'cloud_app_headscale']
        self.assertEqual(entries, [], '資料值不走 .po：改 seed 的中文原文即可')

    def test_spec_matches_plan_and_pvc(self):
        tpl = self._tpl()
        self.assertEqual(tpl.min_vcpu, 1)
        self.assertEqual(tpl.min_ram_gb, 1.0)
        self.assertEqual(tpl.min_storage_gb, 1)
        pvc = int(self._values()['persistence']['size'].rstrip('Gi'))
        self.assertEqual(pvc, tpl.min_storage_gb, '唯一一顆 PVC＝min_storage_gb（quota 對帳）')
        plans = tpl.sudo().subscription_plan_ids
        self.assertTrue(plans)
        for plan in plans:
            self.assertEqual(plan.cs_vcpu, tpl.min_vcpu, plan.name)
            self.assertEqual(plan.cs_ram_gb, tpl.min_ram_gb, plan.name)
            self.assertEqual(plan.cs_storage_gb, pvc,
                             'cs_storage_gb 對應唯一那顆 PVC（#332 overlay 寫 persistence.size）')

    def test_plans_linked_and_priced(self):
        tpl = self._tpl()
        monthly = self.env.ref('woow_paas_platform.plan_headscale_monthly')
        yearly = self.env.ref('woow_paas_platform.plan_headscale_yearly')
        self.assertEqual(set(tpl.sudo().subscription_plan_ids.ids), {monthly.id, yearly.id})
        self.assertEqual(monthly.sh_plan_price, 750)
        self.assertEqual(yearly.sh_plan_price, 7500)
        self.assertEqual(yearly.sh_plan_price, monthly.sh_plan_price * 10, '年繳＝10 個月')
        for plan in (monthly, yearly):
            self.assertEqual(plan.service_type, 'cloud_service')
            self.assertTrue(plan.sh_trial)
            self.assertEqual(plan.sh_trial_duration, 7)
            self.assertEqual(plan.sh_trial_unit, 'day')
        for product, plan in (
            (self.env.ref('woow_paas_platform.product_headscale_monthly'), monthly),
            (self.env.ref('woow_paas_platform.product_headscale_yearly'), yearly),
        ):
            self.assertEqual(product.sh_subscription_plan_id, plan)
            self.assertEqual(product.list_price, plan.sh_plan_price)
            self.assertTrue(product.sh_product_subscribe)
            self.assertFalse(product.taxes_id, 'seed 明寫 taxes_id=[]，不吃 company 預設')

    def test_tenant_knobs_are_app_level_only(self):
        specs = self._tpl()._parse_helm_value_specs()
        self.assertEqual(specs['required'], [], '開通不需要租戶先填任何東西')
        by_key = self._optional()
        self.assertEqual(set(by_key), {
            'ngrok.enabled',
            'ngrok.authtoken',
            'ngrok.domain',
            'headscale.dns.magicDns',
            'headscale.dns.baseDomain',
            'headscale.dns.nameservers',
            'headscale.nodeExpiry',
            'headscale.logLevel',
        })
        self.assertEqual(by_key['ngrok.authtoken']['type'], 'password',
                         'password 型才會被平台在所有 API 回應中遮罩')
        self.assertEqual(by_key['ngrok.enabled']['type'], 'boolean')
        self.assertIs(by_key['ngrok.enabled']['default'], False)
        self.assertEqual(by_key['headscale.dns.magicDns']['type'], 'boolean')
        self.assertIs(by_key['headscale.dns.magicDns']['default'], True)
        self.assertEqual(by_key['headscale.dns.baseDomain']['default'], 'tailnet.internal')
        for key, values in (
                ('headscale.dns.nameservers', {'cloudflare', 'google', 'quad9'}),
                ('headscale.nodeExpiry', {'0', '90d', '180d', '365d'}),
                ('headscale.logLevel', {'info', 'warn', 'error', 'debug'}),
        ):
            spec = by_key[key]
            self.assertEqual(spec['type'], 'select', key)
            options = {o['value'] if isinstance(o, dict) else o for o in spec['options']}
            self.assertEqual(options, values, '%s 的選項要對齊 chart 執行時接受的值' % key)
            self.assertIn(spec['default'], options, key)
        for key in by_key:
            self.assertFalse(re.match(r'^(resources|persistence)\b', key),
                             'CPU/RAM/磁碟由訂閱方案 cs_* 決定，不得開放給租戶改')
            self.assertFalse(key.startswith('config.sensitive'),
                             'config.sensitive 會被平台整棵剝掉，租戶值到不了 chart')

    def test_ngrok_domain_is_validated_on_save(self):
        """#1412 P2：整條網址／埠／路徑在存檔就擋，不留給 chart 在 render 時 fail。"""
        tpl = self._tpl()
        spec = self._optional()['ngrok.domain']
        self.assertEqual(spec['type'], 'text')
        self.assertEqual(spec.get('format'), 'domain')
        self.assertEqual(tpl._formatted_value_specs(), {'ngrok.domain': 'domain'})
        self.assertIsNone(tpl._validate_spec_formats({'ngrok.domain': 'abc-123.ngrok-free.app'}))
        self.assertIsNone(tpl._validate_spec_formats({'ngrok.domain': ''}), '選填：空值放行')
        for bad in ('https://abc-123.ngrok-free.app/', 'abc-123.ngrok-free.app:443'):
            self.assertTrue(tpl._validate_spec_formats({'ngrok.domain': bad}), bad)

    def test_whitelist_filters_resource_overrides(self):
        """specs 的 key 同時是白名單：方案維度送進來要被擋掉。"""
        allowed, rejected = self._tpl()._filter_allowed_helm_values({
            'ngrok.enabled': True,
            'headscale.prefixes.v4': '10.0.0.0/8',
            'resources.limits.cpu': '16',
            'persistence.size': '500Gi',
        })
        self.assertEqual(allowed, {'ngrok.enabled': True})
        self.assertEqual(sorted(rejected), [
            'headscale.prefixes.v4', 'persistence.size', 'resources.limits.cpu'])

    def test_description_states_the_ngrok_caveats(self):
        """說明要讓租戶開通前就知道：換帳號／網域要重登、網域獨佔、免費額度、內部網址。"""
        desc = self._tpl().full_description
        for needle, why in (
                ('ngrok authtoken', '外部裝置登入一定要自己的 ngrok'),
                ('所有裝置都要重新登入', '換 ngrok 帳號或網域＝server_url 改變'),
                ('ERR_NGROK_334', '同一個免費網域同時只能給一台用'),
                ('1 GB', '免費額度要講清楚，正式使用建議升級'),
                ('-headscale:8080', '同工作區節點的內部網址'),
                ('Admin Credentials', '帳密可重設、可自設'),
                ('重新啟動，約數十秒', '重設帳密會重啟服務（operator #1398 重啟 opt-in StatefulSet）'),
                ('同工作區的 VPN 節點約 1 分鐘內自動重連',
                 'Headscale 重啟後內部網址節點靠 tailscale chart 0.1.4 的 liveness 回線（#1553，沙盒 42／46 秒）'),
        ):
            self.assertIn(needle, desc, why)
        self.assertNotIn('不必重啟', desc, '重設帳密會重啟服務，不能再寫「不必重啟」（與管理員帳密對話框一致）')
        self.assertNotIn('12 分鐘', desc, 'tailscale chart 0.1.4（#1553）起不再要等 8 分鐘看門狗')

    def test_no_public_url_injection(self):
        """chart 不需要平台注入自己的公開網址（Headplane 走相對路徑、server_url 由 ngrok 決定）。"""
        CloudService = self.env['woow_paas_platform.cloud_service']
        self.assertEqual(
            CloudService._inject_allowed_origins({}, 'headscale', 'https://paas-hs.example'), {})
